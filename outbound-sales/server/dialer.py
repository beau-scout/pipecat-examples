#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Batch dialer: calls every school in leads.csv, five at a time.

leads.csv has columns ``phone,school`` (the school or district name; an
optional ``name`` column for a known contact is also honored). For each batch
it POSTs /dialout to server.py once per lead, then polls the server's /results
endpoint until every call in the batch has an outcome row (the bot reports one
when a call ends) or the timeout passes.

Call ordering: every number is dialed once first, then the list loops back over
the "non-contacts" — numbers where no human was reached (voicemail, no answer,
timeout, hung up, etc.). Numbers that reached a human with a definitive result
(``contact_captured``, ``refused``, ``wrong_number``) are never re-dialed. Since
server.py persists results to disk, this resume/retry behavior carries across
restarts: stop and restart and it picks up where it left off, finishing the
first pass before retrying any non-contact.

Usage::

    uv run dialer.py [--leads leads.csv] [--server http://localhost:7867]

Note: results live in server.py's memory and are logged to its terminal. This
is demo plumbing; in a real production app the bot would report outcomes to a
webhook backed by a database, and the dialer would query that.
"""

import argparse
import asyncio
import csv
import datetime
import time
import uuid
from pathlib import Path

import aiohttp
from loguru import logger

BATCH_SIZE = 5
POLL_INTERVAL_SECS = 5
# Lowered from 360 to bound how long the dialer waits on a stuck call. The bot
# itself force-ends a call at MAX_CALL_SECONDS (default 240), so this is a hair
# longer to let the bot's own outcome land first, then it's a hard backstop.
CALL_TIMEOUT_SECS = 270


def read_leads(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return [row for row in csv.DictReader(f) if row.get("phone")]


async def report_result(session: aiohttp.ClientSession, server_url: str, row: dict):
    """POST one outcome row (timeout/error) to the server's results log."""
    async with session.post(f"{server_url}/call_result", json=row) as response:
        response.raise_for_status()


async def fetch_results(session: aiohttp.ClientSession, server_url: str) -> dict[str, dict]:
    """Fetch all recorded results, keyed by call_id."""
    async with session.get(f"{server_url}/results") as response:
        response.raise_for_status()
        return await response.json()


async def dial_lead(session: aiohttp.ClientSession, server_url: str, lead: dict, call_id: str):
    """POST one /dialout request. Raises on failure."""
    payload = {
        "dialout_settings": {"phone_number": lead["phone"]},
        "lead": {
            "phone": lead["phone"],
            "name": lead.get("name") or None,
            # The school/district name lands on the lead's "company" field.
            "company": lead.get("school") or lead.get("company") or None,
            # Region drives recording policy (no recording in two-party states).
            "region": lead.get("region") or None,
        },
        "call_id": call_id,
    }
    async with session.post(f"{server_url}/dialout", json=payload) as response:
        if response.status != 200:
            raise RuntimeError(f"/dialout returned {response.status}: {await response.text()}")


async def run_batch(session: aiohttp.ClientSession, server_url: str, batch: list[dict]):
    """Dial one batch and wait until every call has an outcome row."""
    pending: dict[str, dict] = {}

    for lead in batch:
        call_id = uuid.uuid4().hex
        try:
            await dial_lead(session, server_url, lead, call_id)
            school = lead.get("school") or lead.get("company") or "unknown"
            logger.info(f"☎  dialing {school} ({lead['phone']}) [{call_id[:8]}]")
            pending[call_id] = lead
        except Exception as e:
            logger.error(f"Failed to start call to {lead['phone']}: {e}")
            await report_result(
                session,
                server_url,
                {
                    "call_id": call_id,
                    "lead_phone": lead["phone"],
                    "lead_name": lead.get("name", ""),
                    "lead_company": lead.get("school") or lead.get("company", ""),
                    "outcome": "error",
                    "notes": str(e),
                },
            )

    deadline = time.monotonic() + CALL_TIMEOUT_SECS
    while pending and time.monotonic() < deadline:
        await asyncio.sleep(POLL_INTERVAL_SECS)
        rows = await fetch_results(session, server_url)
        for call_id in list(pending):
            if call_id in rows:
                lead = pending.pop(call_id)
                school = lead.get("school") or lead.get("company") or lead["phone"]
                logger.info(f"   ↳ {school}: {rows[call_id]['outcome']}")

    # Anything still pending gets a timeout row. The bot may still report its
    # own row later; the server keeps the first row per call_id.
    for call_id, lead in pending.items():
        logger.warning(f"Call to {lead['phone']} timed out after {CALL_TIMEOUT_SECS}s")
        await report_result(
            session,
            server_url,
            {
                "call_id": call_id,
                "lead_phone": lead["phone"],
                "lead_name": lead.get("name", ""),
                "lead_company": lead.get("school") or lead.get("company", ""),
                "outcome": "timeout",
            },
        )


async def main():
    parser = argparse.ArgumentParser(description="Batch dialer for the RunScout school-safety bot")
    parser.add_argument("--leads", default="leads.csv", help="Path to the leads CSV")
    parser.add_argument("--server", default="http://localhost:7867", help="server.py base URL")
    parser.add_argument("--region", default=None, help="Only call leads whose region matches")
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of new calls this run")
    args = parser.parse_args()

    leads = read_leads(Path(args.leads))
    if args.region:
        leads = [lead for lead in leads if (lead.get("region") or "") == args.region]
        logger.info(f"Filtered to region '{args.region}': {len(leads)} lead(s)")

    async with aiohttp.ClientSession() as session:
        try:
            results = await fetch_results(session, args.server)
        except aiohttp.ClientError as e:
            logger.error(f"Could not reach server at {args.server}: {e}. Is server.py running?")
            return

        # Outcomes that mean we reached a human and got a definitive answer —
        # never re-dial these. Everything else (voicemail, no answer, timeout,
        # hung up, transfer with no info, error) is a "non-contact" we loop back
        # and retry on a later pass.
        REACHED_OUTCOMES = {"contact_captured", "refused", "wrong_number"}
        done_phones = {
            row["lead_phone"]
            for row in results.values()
            if row.get("outcome") in REACHED_OUTCOMES
        }
        attempted_phones = {row["lead_phone"] for row in results.values()}

        # Schools that announced a seasonal closure are paused until retry_after
        # (set by the server, default 30 days out). Skip them until then.
        now_iso = datetime.datetime.now().isoformat()
        paused_phones = {
            row["lead_phone"]
            for row in results.values()
            if row.get("retry_after", "") > now_iso
        }

        # Call everyone once FIRST, then restart the list over the non-contacts.
        # Ordering never-called leads ahead of retries guarantees the whole list
        # is covered before any number is dialed a second time. Paused (seasonal
        # closure) numbers are held out until their retry_after passes.
        never_called = [lead for lead in leads if lead["phone"] not in attempted_phones]
        retry = [
            lead
            for lead in leads
            if lead["phone"] in attempted_phones
            and lead["phone"] not in done_phones
            and lead["phone"] not in paused_phones
        ]
        todo = never_called + retry
        if args.limit:
            todo = todo[: args.limit]

        done_count = sum(1 for lead in leads if lead["phone"] in done_phones)
        paused_count = sum(1 for lead in leads if lead["phone"] in paused_phones)
        logger.info(
            f"{len(leads)} lead(s): {len(never_called)} not yet called, "
            f"{len(retry)} non-contact(s) to retry, {done_count} already reached a human, "
            f"{paused_count} paused (seasonal closure)"
        )
        if not todo:
            logger.info("Nothing to do — every number has already reached a human.")
            return

        for i in range(0, len(todo), BATCH_SIZE):
            batch = todo[i : i + BATCH_SIZE]
            logger.info(f"--- Batch {i // BATCH_SIZE + 1}: {len(batch)} call(s) ---")
            await run_batch(session, args.server, batch)

        rows = await fetch_results(session, args.server)
        captured = [row for row in rows.values() if row["outcome"] == "contact_captured"]
        logger.info(f"Done. {len(rows)} call(s) recorded, {len(captured)} contact(s) captured.")
        for row in captured:
            phone = row.get("contact_phone") or ""
            if phone and row.get("contact_extension"):
                phone += f" x{row['contact_extension']}"
            best_time = row.get("contact_best_time")
            when = f" — follow up: {best_time}" if best_time else ""
            logger.info(
                f"  {row.get('contact_name')} ({row.get('contact_role')}) "
                f"at {row.get('lead_company') or row.get('lead_phone')}: "
                f"{row.get('contact_email') or '(no email)'} / {phone or '(no phone)'}{when}"
            )


if __name__ == "__main__":
    asyncio.run(main())
