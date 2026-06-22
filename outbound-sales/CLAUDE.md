# CLAUDE.md - outbound-sales

Hailey, RunScout's school-safety outreach voice agent built with Pipecat. The dialer reads `leads.csv` (schools/districts), server.py creates a Daily room with PSTN dial-out and starts a bot, and Hailey asks who is in charge of safety/security and collects their contact info. On a captured contact, server.py verifies it through Apollo (and validates the email against the school website) and enrolls it in an Apollo sequence.

## Layout

All code lives in `server/`. Run every command from there with `uv run`.

- `bot.py`: Hailey's pipeline, tools (`save_contact_info`, `end_call`), dial-out retry logic, voicemail handling, and the eval entry point
- `server.py`: FastAPI server (port 7867); `/dialout` starts calls, `/call_result` records outcomes + triggers Apollo enrollment, `/results` is polled by the dialer, plus the control panel (`/`, `/campaign/start|stop`, `/campaign/status`, `/results/clear`)
- `server_utils.py`: data models, Daily room creation (direct REST API, owner token), bot starting, `report_result`
- `apollo_utils.py`: Apollo verification (People Match) + email-domain validation + sequence enrollment; no-op unless `APOLLO_API_KEY` is set
- `dialer.py`: batch dialer, 5 calls at a time
- `static/control.html`: single-page control UI (Start/Stop, live stats, captured contacts)
- `start.command`: macOS double-click launcher (sets up venv, starts server, opens the control page)
- `scenarios/` + `evals.yaml`: text-mode behavioral evals

## Dev loop

Prefer evals over real calls. They run the same bot in text mode with no telephony:

```bash
PYTHONPATH=. uv run pipecat eval suite evals.yaml          # whole suite
uv run bot.py -t eval                                      # then: PYTHONPATH=. uv run pipecat eval run scenarios/happy_path.yaml -v
```

The bot exits when Hailey hangs up, so restart `bot.py -t eval` between single-scenario runs.

Real calls (local bot): two terminals — `uv run server.py` (port 7867) and `uv run bot.py -t daily` (port 7860), plus a purchased Daily phone number and dial-out enablement.

Control panel: with `server.py` running, open `http://localhost:7867/` to start/stop a batch campaign and watch live stats. `start.command` (double-click on macOS) does the venv setup, starts the server, and opens the page. In production the bot runs on Pipecat Cloud, so `bot.py` is not run locally; only `server.py` (control panel + webhook) runs on the operator's machine, and `SERVER_URL` must be reachable by the cloud bots.

## Rules and gotchas

- **This is a demo: results are NOT saved to files.** Call outcomes are logged to the terminal and held in server.py's memory (`CALL_RESULTS`). A real production app would write them to a database in `/call_result`. Do not add file or CSV persistence.
- The control panel runs the campaign by spawning `dialer.py` as a subprocess (`/campaign/start`); Stop terminates it. Stats come straight from `CALL_RESULTS` via `_compute_stats()`. `apollo_utils.enroll_security_contact` mutates the result row with verified email/phone and a verification note before stats read it.
- `leads.csv` is `phone,school,region`. The control panel's Region dropdown (from `/regions`) and "Max calls" box pass `--region`/`--limit` to the dialer so a run can be scoped to one state/segment or capped. Always validate on a small region+limit before dialing the whole file (it's thousands of numbers across NV/AZ/CA/TX/UT, charter and private).
- Apollo is opt-in: with no `APOLLO_API_KEY` the integration is a logged no-op and calls still work. Enrollment defaults to the K-12 sequence, xiomara@runscout.ai, status `paused`. People Match costs 1 Apollo credit per matched contact.
- Voicemail is prompt-based: Hailey recognizes a recorded greeting from the transcript and leaves a callback message (`MAIN_CALLBACK_NUMBER`, default 210-594-2600), then ends with reason `voicemail`. It is not carrier answering-machine detection.
- The outcome report from the bot doubles as the dialer's "call finished" signal. If you touch the shutdown path in `run_bot`, keep the `report_result` call in the `finally` block.
- In `end_call`, `worker.flush_pipeline()` must run before pushing `EndWorkerFrame`. The eval websocket server closes as soon as the EndFrame passes the input transport, so anything still queued would be lost.
- The first reply is canned (`CannedGreetingGate`), skipping the LLM round-trip. Eval runs push it as LLM response frames because text-mode evals never see TTS output.
- Smart Turn's silence fallback is capped at 1s (`stop_secs=1.0`) on purpose; don't raise it back to the 3s default.
- Calls are recorded with Daily cloud recording, started from the bot's meeting token. There is no local recording code. Recording is suppressed per-region for two-party-consent states via `_should_record` in `server_utils.py` (default: California; configurable with `NO_RECORD_REGIONS`). The lead's `region` is passed from the dialer through `/dialout` to drive this.
- **Before deploying to Pipecat Cloud**, change the fields in `pcc-deploy.toml`: `agent_name`, `image` (it points at the example author's Docker Hub repo), and `secret_set` are all account-specific.
- Python deps come from `pyproject.toml` via `uv sync`; `pipecat-ai` installs from the GitHub `main` branch until 1.4.0 ships on PyPI.
