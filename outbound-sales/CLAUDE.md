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

- **Call results persist across restarts.** Outcomes are held in server.py's memory (`CALL_RESULTS`) AND saved to `call_results.json` (override with `RESULTS_FILE`), loaded on startup. This is what lets a campaign resume where it left off: the dialer skips any number already in results, so the saved file = progress. The file holds contact PII and is gitignored. `Clear stats` wipes both memory and the file (so it also resets resume progress). A real production app would use a database instead of a JSON file.
- The control panel runs the campaign by spawning `dialer.py` as a subprocess (`/campaign/start`); Stop terminates it. Stats come straight from `CALL_RESULTS` via `_compute_stats()`. `apollo_utils.enroll_security_contact` mutates the result row with verified email/phone and a verification note before stats read it.
- `leads.csv` is `phone,school,region`. The control panel's Region dropdown (from `/regions`) and "Max calls" box pass `--region`/`--limit` to the dialer so a run can be scoped to one state/segment or capped. Always validate on a small region+limit before dialing the whole file (it's thousands of numbers across NV/AZ/CA/TX/UT, charter and private).
- Apollo is opt-in: with no `APOLLO_API_KEY` the integration is a logged no-op and calls still work. Enrollment defaults to the K-12 sequence, xiomara@runscout.ai, status `paused`. People Match costs 1 Apollo credit per matched contact.
- Voicemail is prompt-based: Hailey recognizes a recorded greeting from the transcript and leaves a callback message (`MAIN_CALLBACK_NUMBER`, default 210-594-2600), then ends with reason `voicemail`. It is not carrier answering-machine detection. This also covers voicemail reached *after* IVR navigation: on `IVRStatus.COMPLETED` (the navigator believes it's being transferred) the bot stages Hailey's conversation prompt (without speaking yet) so she handles whatever plays next — a live person or that department's voicemail. Without this the navigator stays in IVR mode and just loops `<ivr>wait/completed</ivr>` at a voicemail greeting and never leaves a message.
- IVR classification uses `SchoolIVRNavigator`, a subclass of pipecat's `IVRNavigator` that overrides `CLASSIFIER_PROMPT` (the library exposes no constructor arg for it; `__init__` reads `self.CLASSIFIER_PROMPT`, so a class-attribute override is the clean injection point). The stock classifier mislabeled automated school menus as live people (Amplus Durango), stranding the whole call — the classifier only runs until its first decision, so a menu wrongly handed to Hailey never self-corrects. The school-tuned prompt biases toward `ivr` for any recorded/automated content (greetings, menus, re-prompts/timeouts, voicemail) while still catching a front-office staffer's short natural greeting, and breaks ties toward `ivr` (a person misread as a menu self-corrects faster than the reverse).
- Cost guards are layered and mode-independent. `TurnLimiter` (`MAX_LLM_TURNS`, default 40) counts `LLMContextFrame`s into the bare LLM, but the IVRNavigator makes its own completions that bypass it. `UsageTracker` (`MAX_LLM_CALLS`, default 60) counts every completion via `MetricsFrame`s — including the navigator's — and force-ends past the cap, so a misclassified looping menu can't run up unbounded spend. `call_watchdog` (`MAX_CALL_SECONDS`, default 240) is the absolute time backstop. Hailey's prompt also tells her she can't press keys and to end the call if she's clearly stuck reacting to a recording/menu.
- The outcome report from the bot doubles as the dialer's "call finished" signal. If you touch the shutdown path in `run_bot`, keep the `report_result` call in the `finally` block.
- In `end_call`, `worker.flush_pipeline()` must run before pushing `EndWorkerFrame`. The eval websocket server closes as soon as the EndFrame passes the input transport, so anything still queued would be lost.
- Hailey does NOT speak first. The person who answers speaks first ("Hello" / "Hello, Lincoln Elementary"), and Hailey's first reply is generated by the LLM so she can branch: she always introduces herself, then either goes straight to the security question (if they named the school) or confirms the school first (if they didn't). This was deliberately switched away from a canned bot-speaks-first greeting — on Daily PSTN dial-out the greeting fired before the callee's media was bridged and got clipped (`on_dialout_answered` is SIP signaling only, before `on_first_participant_joined`). Letting the human speak first sidesteps that race entirely.
- Smart Turn's silence fallback is capped at 1s (`stop_secs=1.0`) on purpose; don't raise it back to the 3s default.
- Calls are recorded with Daily cloud recording, started from the bot's meeting token. There is no local recording code. Recording is suppressed per-region for two-party-consent states via `_should_record` in `server_utils.py` (default: California; configurable with `NO_RECORD_REGIONS`). The lead's `region` is passed from the dialer through `/dialout` to drive this.
- Hailey's voice (TTS) is provider-switchable via `TTS_PROVIDER` in `bot.py`: `cartesia` (default, lowest latency/cost; voice "Sierra") or `elevenlabs` (requires `ELEVENLABS_API_KEY`). Each reads its own key + voice ID (`CARTESIA_VOICE_ID` / `ELEVENLABS_VOICE_ID`); ElevenLabs defaults to the `eleven_turbo_v2_5` model. Set a voice ID to use a library voice or a clone of the operator's real voice.
- **Before deploying to Pipecat Cloud**, change the fields in `pcc-deploy.toml`: `agent_name`, `image` (it points at the example author's Docker Hub repo), and `secret_set` are all account-specific.
- Python deps come from `pyproject.toml` via `uv sync`; `pipecat-ai` installs from the GitHub `main` branch until 1.4.0 ships on PyPI.
