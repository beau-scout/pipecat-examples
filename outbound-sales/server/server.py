#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Webhook server to handle Daily PSTN dial-out requests and start the voice bot.

This server provides endpoints for handling Daily PSTN dial-out requests and starting the bot.
The server automatically detects the environment (local vs production) and routes
bot starting requests accordingly:
- Local: Uses internal /start endpoint
- Production: Calls Pipecat Cloud API

All call data (room_url, token, dialout_settings) flows through the body parameter
to ensure consistency between local and cloud deployments.
"""

import asyncio
import csv
import datetime
import json
import os
import sys
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger

from apollo_utils import enroll_security_contact
from server_utils import (
    AgentRequest,
    Lead,
    create_daily_room,
    dialout_request_from_request,
    start_bot_local,
    start_bot_production,
)

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle and shared resources.

    Creates a shared aiohttp session for making HTTP requests to bot endpoints.
    The session is reused across requests for better performance through connection pooling.
    """
    # Load any persisted call results so a campaign resumes where it left off.
    _load_results()
    # Create shared HTTP session for bot API calls
    app.state.http_session = aiohttp.ClientSession()
    logger.info("Created shared HTTP session")
    yield
    # Clean up: close the session on shutdown
    await app.state.http_session.close()
    logger.info("Closed shared HTTP session")


app = FastAPI(lifespan=lifespan)

# Call results keyed by call_id. Kept in memory while the server runs AND
# persisted to disk (RESULTS_FILE) so a campaign resumes across restarts: the
# dialer skips any number that already has a result, so saved history = where
# the last session left off. A real production app would use a database; a JSON
# file is enough for a single-operator dialer.
CALL_RESULTS: dict[str, dict[str, str]] = {}

SERVER_DIR = Path(__file__).parent
PORT = int(os.getenv("PORT", "7867"))

# Where results are persisted between sessions. Override with RESULTS_FILE.
RESULTS_FILE = Path(os.getenv("RESULTS_FILE", str(SERVER_DIR / "call_results.json")))


def _load_results() -> None:
    """Load persisted call results into memory on startup (best effort)."""
    try:
        with open(RESULTS_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            CALL_RESULTS.update(data)
            logger.info(f"Loaded {len(CALL_RESULTS)} saved call result(s) from {RESULTS_FILE.name}")
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Could not load saved results from {RESULTS_FILE}: {e}")


def _save_results() -> None:
    """Persist all call results to disk (atomic write, best effort)."""
    try:
        tmp = RESULTS_FILE.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(CALL_RESULTS, f)
        tmp.replace(RESULTS_FILE)
    except OSError as e:
        logger.warning(f"Could not save results to {RESULTS_FILE}: {e}")


# How long to skip a school that says it's closed for an extended/seasonal break
# before trying it again. Default 30 days; override with CLOSED_PAUSE_DAYS.
CLOSED_PAUSE_DAYS = int(os.getenv("CLOSED_PAUSE_DAYS", "30"))

# Phrases in a call's notes/transcript that signal an extended seasonal closure
# (summer/holiday break) rather than just normal after-hours. Kept specific to
# avoid pausing a school that's only briefly unavailable.
_CLOSED_HINTS = (
    "closed for summer",
    "closed for the summer",
    "summer break",
    "out for summer",
    "out for the summer",
    "school is out",
    "closed until",
    "reopen on",
    "reopens on",
    "will reopen",
    "closed for the season",
)


def _apply_seasonal_pause(row: dict) -> None:
    """Stamp ``retry_after`` (now + CLOSED_PAUSE_DAYS) when a call reached an
    extended-closure recording, so the dialer skips that school until then.

    Triggered by the bot's reason (``closed_for_summer``) or, as a fallback that
    works without redeploying the bot, summer/holiday-closure phrases in the
    call's notes or transcript.
    """
    text = f"{row.get('notes', '')} {row.get('transcript', '')}".lower()
    is_closed = row.get("outcome") == "closed_for_summer" or any(h in text for h in _CLOSED_HINTS)
    if is_closed:
        until = datetime.datetime.now() + datetime.timedelta(days=CLOSED_PAUSE_DAYS)
        row["retry_after"] = until.isoformat(timespec="seconds")
        row["seasonal_closure"] = "yes"


# The running batch campaign (dialer.py subprocess), driven from the control
# page's Start/Stop buttons. Only one campaign runs at a time.
CAMPAIGN: dict[str, object] = {"proc": None, "started_at": None, "region": None, "limit": None}

LEADS_CSV = SERVER_DIR / "leads.csv"


def _campaign_running() -> bool:
    proc = CAMPAIGN["proc"]
    return proc is not None and proc.returncode is None


def _regions() -> list[dict]:
    """Region names and their lead counts, read from leads.csv (for the picker)."""
    try:
        with open(LEADS_CSV, newline="") as f:
            counts = Counter(
                (row.get("region") or "Unspecified").strip()
                for row in csv.DictReader(f)
                if row.get("phone")
            )
    except FileNotFoundError:
        return []
    return [{"region": r, "count": c} for r, c in sorted(counts.items())]


def _compute_stats() -> dict:
    """Summarize CALL_RESULTS for the control page: totals, outcomes, contacts."""
    outcomes: dict[str, int] = {}
    contacts: list[dict] = []
    for row in CALL_RESULTS.values():
        outcome = row.get("outcome", "unknown")
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if outcome == "contact_captured":
            contacts.append(
                {
                    "school": row.get("lead_company", ""),
                    "lead_phone": row.get("lead_phone", ""),
                    "name": row.get("contact_name", ""),
                    "role": row.get("contact_role", ""),
                    "email": row.get("contact_email", ""),
                    "phone": row.get("contact_phone", ""),
                    "extension": row.get("contact_extension", ""),
                    "best_time": row.get("contact_best_time", ""),
                    "verification": row.get("verification", ""),
                    "timestamp": row.get("timestamp", ""),
                }
            )
    contacts.sort(key=lambda c: c.get("timestamp", ""), reverse=True)

    all_calls = [
        {
            "call_id": row.get("call_id", ""),
            "school": row.get("lead_company", "") or row.get("lead_phone", ""),
            "outcome": row.get("outcome", "unknown"),
            "notes": row.get("notes", ""),
            "timestamp": row.get("timestamp", ""),
            "has_transcript": bool(
                row.get("transcript") and row.get("transcript") not in ("[]", "", None)
            ),
        }
        for row in CALL_RESULTS.values()
    ]
    all_calls.sort(key=lambda c: c.get("timestamp", ""), reverse=True)

    return {
        "total_calls": len(CALL_RESULTS),
        "contacts_captured": len(contacts),
        "outcomes": outcomes,
        "contacts": contacts,
        "all_calls": all_calls,
    }


@app.post("/dialout")
async def handle_dial_out_request(request: Request) -> JSONResponse:
    """Handle dial-out request.

    This endpoint:
    1. Receives dial-out request with phone number and optional caller ID
    2. Creates a Daily room with dial-out capabilities
    3. Starts the bot (locally or via Pipecat Cloud based on ENV)
    4. Returns room details for monitoring

    Args:
        request: FastAPI request containing dialout_settings

    Returns:
        JSONResponse: Success status with room_url and token

    Raises:
        HTTPException: If request data is invalid or bot fails to start
    """
    dialout_request = await dialout_request_from_request(request)

    # Lead and call_id are optional in the request (e.g. a quick curl test);
    # fall back to the bare phone number and a fresh id.
    lead = dialout_request.lead or Lead(phone=dialout_request.dialout_settings.phone_number)
    call_id = dialout_request.call_id or uuid.uuid4().hex
    who = f"{lead.company or 'unknown'} ({lead.phone}) [{call_id[:8]}]"

    logger.info(f"📞 {who}: creating room…")
    daily_room_config = await create_daily_room(dialout_request, request.app.state.http_session)

    # Default the caller ID to the purchased number's id from the environment.
    if not dialout_request.dialout_settings.caller_id and os.getenv("CALLER_ID"):
        dialout_request.dialout_settings.caller_id = os.getenv("CALLER_ID")

    agent_request = AgentRequest(
        room_url=daily_room_config.room_url,
        token=daily_room_config.token,
        dialout_settings=dialout_request.dialout_settings,
        lead=lead,
        call_id=call_id,
    )

    logger.info(f"📞 {who}: starting bot and dialing…")
    try:
        if os.getenv("ENV") == "production":
            await start_bot_production(agent_request, request.app.state.http_session)
        else:
            await start_bot_local(agent_request, request.app.state.http_session)
    except Exception as e:
        logger.error(f"📞 {who}: failed to start bot: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to start bot: {str(e)}")
    logger.info(f"📞 {who}: bot started, call ringing")

    return JSONResponse(
        {
            "status": "success",
            "room_url": daily_room_config.room_url,
            "token": daily_room_config.token,
            "phone_number": dialout_request.dialout_settings.phone_number,
            "call_id": call_id,
        }
    )


@app.post("/call_result")
async def handle_call_result(request: Request) -> JSONResponse:
    """Record one call's outcome.

    Bots report their outcome here when a call ends; the dialer reports
    timeout and error rows. The first report for a call_id wins, so a timeout
    verdict from the dialer stands even if a slow bot reports later.

    This is where a real production app would write to a database. The demo
    just logs the result to the terminal and keeps it in memory.
    """
    row = await request.json()
    call_id = row.get("call_id")
    if not call_id:
        raise HTTPException(status_code=400, detail="Missing 'call_id'")
    row = {"timestamp": datetime.datetime.now().isoformat(timespec="seconds"), **row}
    if call_id in CALL_RESULTS:
        logger.debug(f"Ignoring duplicate result for call {call_id}: {row}")
    else:
        # Pause schools that announced an extended seasonal closure for 30 days.
        _apply_seasonal_pause(row)
        if row.get("retry_after"):
            logger.info(
                f"Call {call_id}: seasonal closure — pausing {row.get('lead_company') or row.get('lead_phone')} "
                f"until {row['retry_after']}"
            )
        CALL_RESULTS[call_id] = row
        _save_results()  # persist so progress survives a restart
        # Concise per-call line (not the whole row — the transcript is huge).
        who = f"{row.get('lead_company') or row.get('lead_phone')} [{call_id[:8]}]"
        detail = ""
        if row.get("contact_name"):
            detail = f" — {row['contact_name']} ({row.get('contact_role', '')})"
        elif row.get("notes"):
            detail = f" — {row['notes']}"
        logger.info(f"✓ {who}: {row.get('outcome')}{detail}")
        # On a captured contact, push it to Apollo so the team can follow up.
        # Best effort: enroll_security_contact never raises.
        await enroll_security_contact(row)
    return JSONResponse({"status": "ok"})


@app.get("/call/{call_id}")
async def get_call(call_id: str):
    """Return the full result row for one call, including its transcript."""
    row = CALL_RESULTS.get(call_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return row


@app.get("/results")
async def get_results() -> dict[str, dict[str, str]]:
    """Return all recorded call results keyed by call_id. Polled by dialer.py."""
    return CALL_RESULTS


@app.get("/health")
async def health_check():
    """Health check endpoint.

    Returns:
        dict: Status indicating server health
    """
    return {"status": "healthy"}


# ----------------- Control page ----------------- #


@app.get("/")
async def control_page():
    """Serve the single-page control UI (Start/Stop the campaign, live stats)."""
    return FileResponse(SERVER_DIR / "static" / "control.html")


@app.get("/regions")
async def regions():
    """Available lead regions and their counts, for the control page picker."""
    return {"regions": _regions(), "total": sum(r["count"] for r in _regions())}


@app.get("/campaign/status")
async def campaign_status():
    """Whether a campaign is running, plus live stats for the control page."""
    started_at = CAMPAIGN["started_at"]
    return {
        "running": _campaign_running(),
        "started_at": started_at.isoformat() if started_at else None,
        "region": CAMPAIGN["region"],
        "limit": CAMPAIGN["limit"],
        "stats": _compute_stats(),
    }


@app.post("/campaign/start")
async def campaign_start(region: str | None = None, limit: int | None = None):
    """Start the batch dialer (dialer.py) as a background subprocess.

    Optional query params scope the run: ``region`` calls only that region's
    leads; ``limit`` caps the number of new calls placed this run.
    """
    if _campaign_running():
        raise HTTPException(status_code=409, detail="A campaign is already running.")

    cmd = [sys.executable, "dialer.py", "--server", f"http://localhost:{PORT}"]
    if region:
        cmd += ["--region", region]
    if limit:
        cmd += ["--limit", str(limit)]

    proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(SERVER_DIR))
    CAMPAIGN["proc"] = proc
    CAMPAIGN["started_at"] = datetime.datetime.now()
    CAMPAIGN["region"] = region
    CAMPAIGN["limit"] = limit
    logger.info(f"Campaign started (pid {proc.pid}, region={region or 'all'}, limit={limit or 'none'})")
    return {"status": "started", "pid": proc.pid, "region": region, "limit": limit}


@app.post("/campaign/stop")
async def campaign_stop():
    """Stop the running campaign. New calls stop; calls already placed finish."""
    proc = CAMPAIGN["proc"]
    if not _campaign_running():
        return {"status": "not_running"}

    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except TimeoutError:
        proc.kill()
    logger.info("Campaign stopped")
    return {"status": "stopped"}


@app.post("/results/clear")
async def clear_results():
    """Clear the in-memory results so the next campaign starts fresh stats."""
    if _campaign_running():
        raise HTTPException(status_code=409, detail="Stop the campaign before clearing results.")
    count = len(CALL_RESULTS)
    CALL_RESULTS.clear()
    _save_results()  # persist the cleared state too
    logger.info(f"Cleared {count} result(s)")
    return {"status": "cleared", "cleared": count}


# ----------------- Main ----------------- #


if __name__ == "__main__":
    logger.info(f"Starting server on port {PORT}")
    logger.info(f"Control panel: http://localhost:{PORT}/")
    # access_log=False silences uvicorn's per-request lines — the control page
    # and dialer poll /campaign/status and /results every couple seconds, which
    # otherwise floods the console. The meaningful events (each call's steps and
    # outcome, campaign start/stop) are logged explicitly via loguru instead.
    uvicorn.run("server:app", host="0.0.0.0", port=PORT, reload=True, access_log=False)
