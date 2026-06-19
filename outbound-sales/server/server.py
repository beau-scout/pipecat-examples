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
import datetime
import os
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger

from server_utils import (
    AgentRequest,
    Lead,
    create_daily_room,
    dialout_request_from_request,
    start_bot_local,
    start_bot_production,
)
from apollo_utils import enroll_security_contact

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle and shared resources.

    Creates a shared aiohttp session for making HTTP requests to bot endpoints.
    The session is reused across requests for better performance through connection pooling.
    """
    # Create shared HTTP session for bot API calls
    app.state.http_session = aiohttp.ClientSession()
    logger.info("Created shared HTTP session")
    yield
    # Clean up: close the session on shutdown
    await app.state.http_session.close()
    logger.info("Closed shared HTTP session")


app = FastAPI(lifespan=lifespan)

# Call results keyed by call_id. This is the demo stand-in for persistence:
# each result is logged to the terminal and kept in memory while the server
# runs. A real production app would save these to a database instead.
CALL_RESULTS: dict[str, dict[str, str]] = {}

SERVER_DIR = Path(__file__).parent
PORT = int(os.getenv("PORT", "7867"))

# The running batch campaign (dialer.py subprocess), driven from the control
# page's Start/Stop buttons. Only one campaign runs at a time.
CAMPAIGN: dict[str, object] = {"proc": None, "started_at": None}


def _campaign_running() -> bool:
    proc = CAMPAIGN["proc"]
    return proc is not None and proc.returncode is None


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
                    "verification": row.get("verification", ""),
                    "timestamp": row.get("timestamp", ""),
                }
            )
    contacts.sort(key=lambda c: c.get("timestamp", ""), reverse=True)
    return {
        "total_calls": len(CALL_RESULTS),
        "contacts_captured": len(contacts),
        "outcomes": outcomes,
        "contacts": contacts,
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
    logger.debug("Received dial-out request")

    dialout_request = await dialout_request_from_request(request)

    daily_room_config = await create_daily_room(dialout_request, request.app.state.http_session)

    # Lead and call_id are optional in the request (e.g. a quick curl test);
    # fall back to the bare phone number and a fresh id.
    lead = dialout_request.lead or Lead(phone=dialout_request.dialout_settings.phone_number)
    call_id = dialout_request.call_id or uuid.uuid4().hex

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

    try:
        if os.getenv("ENV") == "production":
            await start_bot_production(agent_request, request.app.state.http_session)
        else:
            await start_bot_local(agent_request, request.app.state.http_session)
    except Exception as e:
        logger.error(f"Error starting bot: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to start bot: {str(e)}")

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
        CALL_RESULTS[call_id] = row
        logger.info(f"Call {call_id} finished ({row.get('outcome')}): {row}")
        # On a captured contact, push it to Apollo so the team can follow up.
        # Best effort: enroll_security_contact never raises.
        await enroll_security_contact(row)
    return JSONResponse({"status": "ok"})


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


@app.get("/campaign/status")
async def campaign_status():
    """Whether a campaign is running, plus live stats for the control page."""
    started_at = CAMPAIGN["started_at"]
    return {
        "running": _campaign_running(),
        "started_at": started_at.isoformat() if started_at else None,
        "stats": _compute_stats(),
    }


@app.post("/campaign/start")
async def campaign_start():
    """Start the batch dialer (dialer.py) as a background subprocess."""
    if _campaign_running():
        raise HTTPException(status_code=409, detail="A campaign is already running.")

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "dialer.py",
        "--server",
        f"http://localhost:{PORT}",
        cwd=str(SERVER_DIR),
    )
    CAMPAIGN["proc"] = proc
    CAMPAIGN["started_at"] = datetime.datetime.now()
    logger.info(f"Campaign started (pid {proc.pid})")
    return {"status": "started", "pid": proc.pid}


@app.post("/campaign/stop")
async def campaign_stop():
    """Stop the running campaign. New calls stop; calls already placed finish."""
    proc = CAMPAIGN["proc"]
    if not _campaign_running():
        return {"status": "not_running"}

    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
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
    logger.info(f"Cleared {count} result(s)")
    return {"status": "cleared", "cleared": count}


# ----------------- Main ----------------- #


if __name__ == "__main__":
    logger.info(f"Starting server on port {PORT}")
    logger.info(f"Control panel: http://localhost:{PORT}/")
    uvicorn.run("server:app", host="0.0.0.0", port=PORT, reload=True)
