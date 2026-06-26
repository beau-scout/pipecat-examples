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
import html
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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
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

# Phrases that signal an EXTENDED seasonal closure (summer/holiday break), not just
# normal after-hours. The bot's explicit ``closed_for_summer`` reason is the primary
# trigger; this text fallback is deliberately narrow to multi-word seasonal phrases.
# Broad daily/weekly wording ("closed until", "reopen on", "will reopen", "school is
# out") was REMOVED — it matched after-hours messages like "closed until 8am" /
# "reopen on Monday" and wrongly paused open schools for 30 days.
_CLOSED_HINTS = (
    "closed for summer",
    "closed for the summer",
    "summer break",
    "out for the summer",
    "school is out for the summer",
    "closed for the season",
    "winter break",
    "spring break",
    "closed for the holidays",
    "reopen in august",
    "reopen in september",
    "reopens in august",
    "reopens in september",
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
# page's Start/Stop buttons. Only one campaign runs at a time. "spend" is the
# estimated $ spent on LLM tokens since this campaign started (reset on start).
CAMPAIGN: dict[str, object] = {
    "proc": None, "started_at": None, "region": None, "limit": None, "spend": 0.0,
}

LEADS_CSV = SERVER_DIR / "leads.csv"

# Hard $ ceiling per campaign run. When the estimated LLM spend since the campaign
# started crosses this, the campaign is auto-stopped — so a batch of stuck/looping
# calls can't silently run up hundreds of dollars (the per-call cost cap bounds one
# call; this bounds the whole run). Deliberately conservative; raise it via env for
# a large full-list run. 0 disables the ceiling.
CAMPAIGN_SPEND_LIMIT = float(os.getenv("CAMPAIGN_SPEND_LIMIT", "25"))

# Approx Anthropic price per 1M tokens (USD): (input, output, cache_read, cache_write).
# Used only for the running cost ESTIMATE/ceiling, not billing. Defaults to Sonnet;
# set COST_MODEL to match the bot's ANTHROPIC_MODEL for a closer estimate.
_MODEL_PRICES = {
    "claude-sonnet-4-6": (3.0, 15.0, 0.30, 3.75),
    "claude-opus-4-8": (15.0, 75.0, 1.50, 18.75),
    "claude-haiku-4-5": (1.0, 5.0, 0.10, 1.25),
}
_COST_MODEL = os.getenv("COST_MODEL", "claude-sonnet-4-6")


def _estimate_call_cost(row: dict) -> float:
    """Estimate one call's LLM cost in dollars from its recorded token usage."""
    try:
        u = json.loads(row.get("usage") or "{}")
    except (json.JSONDecodeError, TypeError):
        return 0.0
    if not u:
        return 0.0
    pin, pout, pcr, pcw = _MODEL_PRICES.get(_COST_MODEL, _MODEL_PRICES["claude-sonnet-4-6"])
    return (
        u.get("prompt_tokens", 0) * pin
        + u.get("completion_tokens", 0) * pout
        + u.get("cache_read_tokens", 0) * pcr
        + u.get("cache_creation_tokens", 0) * pcw
    ) / 1_000_000


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

    # Record the provisional "in_progress" row BEFORE creating the room / starting
    # the bot, so this number counts as attempted from the first moment — not only
    # when the bot reports an outcome at call end, and not only after the multi-
    # second room-creation + bot-start. Without this, stopping and restarting a
    # campaign mid-dial re-dials every in-flight number (the resume logic still
    # sees it as "never called"), double-dialing the same school. The bot's
    # terminal outcome overwrites this row when the call ends (see
    # handle_call_result); if the bot never starts, the dialer posts an error row
    # (also an overwrite), and a row that never resolves becomes retry-eligible
    # again after the dialer's cooldown.
    if call_id not in CALL_RESULTS:
        CALL_RESULTS[call_id] = {
            "call_id": call_id,
            "lead_phone": lead.phone,
            "lead_name": lead.name or "",
            "lead_company": lead.company or "",
            "outcome": "in_progress",
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        _save_results()

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
    existing = CALL_RESULTS.get(call_id)
    existing_out = existing.get("outcome") if existing else None
    # First TERMINAL outcome per call_id normally wins (so a dialer timeout stands
    # even if a slow bot reports later). Two exceptions: a provisional "in_progress"
    # row (written at dial time so restarts don't re-dial in-flight calls) is always
    # replaced; and a bot's captured contact OVERRIDES a dialer "timeout" — the
    # dialer's per-batch timeout can fire moments before a slow bot reports a real
    # contact, and that captured contact is the authoritative result we can't lose.
    replaceable = existing_out in (None, "in_progress") or (
        existing_out == "timeout" and row.get("outcome") == "contact_captured"
    )
    if existing is not None and not replaceable:
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
        # Per-call token/cost summary (so cost is watchable live in this log).
        cost = ""
        try:
            u = json.loads(row.get("usage") or "{}")
        except (json.JSONDecodeError, TypeError):
            u = {}
        if u:
            cached = u.get("cache_read_tokens", 0)
            total_in = u.get("prompt_tokens", 0) + cached + u.get("cache_creation_tokens", 0)
            pct = round(100 * cached / total_in) if total_in else 0
            cost = (
                f"  ·  {u.get('llm_calls', 0)} calls, "
                f"in {total_in} tok ({pct}% cached), out {u.get('completion_tokens', 0)} tok"
            )
        # One concise line per call. The full transcript is NOT dumped here — it
        # made the terminal unreadable across a batch. Read transcripts on the
        # /transcripts page (all calls, one scrollable view) or the control panel.
        try:
            turns = json.loads(row.get("transcript") or "[]")
        except (json.JSONDecodeError, TypeError):
            turns = []
        turn_note = f"  ·  {len(turns)} turns" if turns else "  ·  (bot never spoke)"
        # Campaign spend ceiling: accumulate this call's estimated $ and, while a
        # campaign is running, show the running total — and auto-stop the campaign
        # if it crosses the ceiling so a batch of stuck calls can't run up hundreds.
        spend_note = ""
        if _campaign_running():
            CAMPAIGN["spend"] = float(CAMPAIGN["spend"]) + _estimate_call_cost(row)
            spend_note = f"  ·  campaign ~${CAMPAIGN['spend']:.2f}"
        logger.info(f"✓ {who}: {row.get('outcome')}{detail}{cost}{turn_note}{spend_note}")
        # On a captured contact, push it to Apollo so the team can follow up.
        # Best effort: enroll_security_contact never raises.
        await enroll_security_contact(row)
        if (
            CAMPAIGN_SPEND_LIMIT > 0
            and _campaign_running()
            and float(CAMPAIGN["spend"]) >= CAMPAIGN_SPEND_LIMIT
        ):
            logger.error(
                f"⛔ Campaign spend ceiling hit (~${CAMPAIGN['spend']:.2f} ≥ "
                f"${CAMPAIGN_SPEND_LIMIT:.2f}) — auto-stopping. Raise CAMPAIGN_SPEND_LIMIT "
                f"to run further."
            )
            await _terminate_campaign()
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


# Outcome → colored badge, so the transcripts page is scannable at a glance.
_OUTCOME_COLORS = {
    "contact_captured": "#16a34a",
    "voicemail": "#0891b2",
    "closed_for_summer": "#7c3aed",
    "refused": "#dc2626",
    "wrong_number": "#dc2626",
    "no_answer": "#6b7280",
    "hung_up": "#d97706",
    "timeout": "#d97706",
    "max_turns": "#d97706",
    "error": "#dc2626",
    "in_progress": "#2563eb",
}


@app.get("/transcripts", response_class=HTMLResponse)
async def transcripts_page():
    """One scrollable page with every call's transcript, newest first — the
    readable alternative to scrolling the server's terminal log. Each call is a
    card: school, outcome badge, token/cost summary, then the Hailey/Caller turns.
    """
    rows = sorted(
        CALL_RESULTS.values(),
        key=lambda r: r.get("timestamp", ""),
        reverse=True,
    )

    def esc(s) -> str:
        return html.escape(str(s or ""))

    cards = []
    for row in rows:
        outcome = row.get("outcome", "unknown")
        color = _OUTCOME_COLORS.get(outcome, "#6b7280")
        school = row.get("lead_company") or row.get("lead_phone") or "unknown"
        when = row.get("timestamp", "")
        notes = row.get("notes", "")

        # Token/cost summary, if present.
        cost = ""
        try:
            u = json.loads(row.get("usage") or "{}")
        except (json.JSONDecodeError, TypeError):
            u = {}
        if u:
            cached = u.get("cache_read_tokens", 0)
            total_in = u.get("prompt_tokens", 0) + cached + u.get("cache_creation_tokens", 0)
            pct = round(100 * cached / total_in) if total_in else 0
            cost = (
                f"{u.get('llm_calls', 0)} LLM calls · in {total_in} tok "
                f"({pct}% cached) · out {u.get('completion_tokens', 0)} tok"
            )

        try:
            turns = json.loads(row.get("transcript") or "[]")
        except (json.JSONDecodeError, TypeError):
            turns = []
        if turns:
            lines = "".join(
                f'<div class="turn {"bot" if t.get("role") == "assistant" else "caller"}">'
                f'<span class="who">{"Hailey" if t.get("role") == "assistant" else "Caller"}</span>'
                f'<span class="text">{esc(t.get("text", ""))}</span></div>'
                for t in turns
            )
            body = f'<div class="transcript">{lines}</div>'
        elif outcome == "in_progress":
            body = '<div class="empty">Call in progress…</div>'
        else:
            body = '<div class="empty">No transcript — the bot never spoke (no answer / instant hangup).</div>'

        cards.append(
            f'<div class="card">'
            f'<div class="head">'
            f'<span class="school">{esc(school)}</span>'
            f'<span class="badge" style="background:{color}">{esc(outcome)}</span>'
            f'<span class="when">{esc(when)}</span>'
            f"</div>"
            f'{f"<div class=notes>{esc(notes)}</div>" if notes else ""}'
            f'{f"<div class=cost>{esc(cost)}</div>" if cost else ""}'
            f"{body}"
            f"</div>"
        )

    page = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Call transcripts ({len(rows)})</title>
<style>
  :root {{ --bg:#0f172a; --card:#1e293b; --muted:#94a3b8; --text:#e2e8f0; --line:#334155; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; }}
  header {{ position:sticky; top:0; background:var(--bg); border-bottom:1px solid var(--line); padding:16px 24px; display:flex; gap:16px; align-items:baseline; z-index:1; }}
  header h1 {{ font-size:18px; margin:0; }}
  header a {{ color:var(--muted); text-decoration:none; font-size:14px; }}
  header .count {{ color:var(--muted); font-size:14px; }}
  main {{ max-width:900px; margin:0 auto; padding:24px; display:flex; flex-direction:column; gap:16px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; }}
  .head {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; }}
  .school {{ font-weight:600; font-size:16px; }}
  .badge {{ color:#fff; font-size:12px; font-weight:600; padding:2px 8px; border-radius:999px; }}
  .when {{ color:var(--muted); font-size:13px; margin-left:auto; }}
  .notes {{ color:#fbbf24; font-size:13px; margin-top:8px; }}
  .cost {{ color:var(--muted); font-size:12px; margin-top:6px; font-variant-numeric:tabular-nums; }}
  .transcript {{ margin-top:12px; border-top:1px solid var(--line); padding-top:12px; display:flex; flex-direction:column; gap:6px; }}
  .turn {{ display:flex; gap:10px; }}
  .turn .who {{ flex:0 0 56px; font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.03em; padding-top:1px; }}
  .turn.bot .who {{ color:#38bdf8; }}
  .turn.caller .who {{ color:#a3a3a3; }}
  .turn .text {{ flex:1; white-space:pre-wrap; }}
  .empty {{ color:var(--muted); font-style:italic; margin-top:10px; font-size:14px; }}
</style></head>
<body>
  <header>
    <h1>Call transcripts</h1>
    <span class="count">{len(rows)} call(s)</span>
    <a href="/">← control panel</a>
  </header>
  <main>
    {"".join(cards) if cards else '<div class="empty">No calls recorded yet.</div>'}
  </main>
</body></html>"""
    return HTMLResponse(page)


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
    CAMPAIGN["spend"] = 0.0  # reset the per-campaign spend ceiling counter
    logger.info(f"Campaign started (pid {proc.pid}, region={region or 'all'}, limit={limit or 'none'})")
    return {"status": "started", "pid": proc.pid, "region": region, "limit": limit}


async def _terminate_campaign() -> bool:
    """Terminate the dialer subprocess if running. Returns True if it was running.
    Already-placed calls on Pipecat Cloud finish; only new dialing stops."""
    proc = CAMPAIGN["proc"]
    if not _campaign_running():
        return False
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except TimeoutError:
        proc.kill()
    return True


@app.post("/campaign/stop")
async def campaign_stop():
    """Stop the running campaign. New calls stop; calls already placed finish."""
    if not await _terminate_campaign():
        return {"status": "not_running"}
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
