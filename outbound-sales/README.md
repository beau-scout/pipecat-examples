# RunScout School Safety Outreach Bot

Meet Hailey, RunScout's school-safety outreach agent built with Pipecat. She calls a list of schools and districts in batches of 5 over Daily PSTN, greets whoever answers ("Hi there, this is Hailey calling from RunScout dot A I."), and asks who is in charge of safety and security. If asked why she's calling, she explains that RunScout connects to a school's existing camera systems to detect everyday incidents such as student elopement or propped-open doors. She collects the security decision maker's name, role, email, and phone (with extension) — reading the email back to confirm it — or gets transferred, then reports the contact to the server, says thanks, and hangs up.

When a contact is captured, the server **validates it against the school's website domain and verifies it through Apollo enrichment**, then **enrolls the contact into an Apollo sequence** so a RunScout teammate follows up by email and phone.

The example also shows the **Pipecat evals** feature: the same bot runs in a text-only eval mode so you can test its behavior in seconds, with no phone calls and no audio.

This project was scaffolded with the Pipecat CLI:

```bash
pipecat init outbound-sales --bot-type telephony -t daily_pstn_dialout \
  --daily-pstn-mode dial-out -m cascade \
  --stt deepgram_stt --llm openai_llm --tts cartesia_tts --eval
```

## How It Works

```
leads.csv → dialer.py → server.py /dialout → Daily room + dial-out
                                              ↓
server.py /call_result ← bot.py (Hailey) ← call answered
        ↓
  Apollo: verify + enroll in sequence
```

1. `dialer.py` reads `leads.csv` (schools and districts) and starts calls in batches of 5
2. For each school, `server.py` creates a Daily room with dial-out enabled and starts a bot
3. The bot dials the school's number; when they answer, Hailey asks who handles safety and security. If she's talking to a gatekeeper she collects the security person's contact info; if she's transferred to (or reaches) the security decision maker directly, she explains what RunScout does and asks for a good time for a senior rep to follow up
4. Hailey saves the security contact with the `save_contact_info` tool (name, role, email, phone, extension, and the best follow-up time) and hangs up with the `end_call` tool
5. Every finished call reports one outcome row to `server.py`, which logs it and keeps it in memory; the dialer polls `GET /results` to know when a batch is done, then starts the next batch
6. On a captured contact, `server.py` verifies the email/phone through Apollo (and validates the email domain against the school's website), then enrolls the contact in an Apollo sequence for the team to follow up. (This is a demo: a real production app would also save outcomes to a database.)

## Configuration

- **Bot Type**: Telephony
- **Transport(s)**: Daily PSTN (Dial-out), plus an eval transport for testing
- **Pipeline**: Cascade
  - **STT**: Deepgram
  - **LLM**: OpenAI
  - **TTS**: Cartesia

> **Note**: This example currently installs `pipecat-ai` from the `main` branch on GitHub, because the CLI and evals features are newer than the latest PyPI release. Once 1.4.0 ships, switch the dependency in `server/pyproject.toml` to the PyPI version.

## Setup

All commands run from the `server/` directory.

1. Create a virtual environment and install dependencies

   ```bash
   cd server
   uv sync
   ```

2. Set up environment variables

   ```bash
   cp .env.example .env
   # Edit .env with your API keys
   ```

3. Buy a phone number

   Instructions on how to do that can be found at this [docs link](https://docs.daily.co/reference/rest-api/phone-numbers/buy-phone-number)

4. Request dial-out enablement

   For compliance reasons, to enable dial-out for your Daily account, you must request enablement via the form. You can find out more about dial-out, and the form, at the [link here](https://docs.daily.co/guides/products/dial-in-dial-out#main)

## Test Hailey with Evals (no phone needed)

Evals drive the bot over a local WebSocket in text mode: no telephony, no audio, no waiting. An OpenAI model (gpt-4o-mini) judges the responses, so only `OPENAI_API_KEY` is needed.

Run the whole suite (spawns a fresh bot per scenario):

```bash
uv run pipecat eval suite evals.yaml
```

Expected output:

```
  ✓ bot.py gatekeeper_refusal (8089ms)
  ✓ bot.py happy_path (14954ms)
  ✓ bot.py transfer_path (18785ms)

  3/3 passed  ·  19.3s
```

The scenarios live in `scenarios/`:

- `happy_path.yaml`: the front office hands over the safety director's email and phone (with extension); Hailey reads the email back to confirm
- `transfer_path.yaml`: the front office transfers Hailey to the district's safety director
- `gatekeeper_refusal.yaml`: "take us off your list"; Hailey must not argue

To iterate on a single scenario:

```bash
# Terminal 1 (Hailey exits when she hangs up, so restart between runs)
uv run bot.py -t eval

# Terminal 2
uv run pipecat eval run scenarios/happy_path.yaml -v
```

This is the fast dev loop: tweak the system prompt in `bot.py`, re-run the suite, repeat.

## Run a Real Call

You'll need two terminal windows open:

1. **Terminal 1**: Start the webhook server:

   ```bash
   uv run server.py
   ```

   This runs on port 7867 and handles dial-out requests.

2. **Terminal 2**: Start the bot server:

   ```bash
   uv run bot.py -t daily
   ```

   This runs on port 7860 and handles the bot logic.

3. **Test a single call**

   ```bash
   curl -X POST "http://localhost:7867/dialout" \
     -H "Content-Type: application/json" \
     -d '{
       "dialout_settings": { "phone_number": "+15551234567" },
       "lead": { "phone": "+15551234567", "name": "Beau", "company": "Acme Robotics" }
     }'
   ```

   Answer the call and have a chat with Hailey. When the call ends, the outcome is logged in the webhook server's terminal (Terminal 1).

## Run a Batch Campaign

Edit `leads.csv` with real numbers (`phone,school`), then with both servers running:

```bash
uv run dialer.py
```

The dialer calls in batches of 5, waits for every call in a batch to finish (or time out after 6 minutes), then starts the next batch. Leads that already have a result are skipped, so you can stop and re-run the dialer while the server stays up.

Each result row is logged to the server terminal and has: timestamp, call_id, lead phone/school, outcome, contact name/role/phone/extension/email, the best follow-up time (when Hailey reached the security person directly), an Apollo verification note, and notes. Outcomes are `contact_captured`, `refused`, `wrong_number`, `transferred_no_info`, `voicemail`, `other`, `hung_up`, `no_answer`, `dialout_error`, `timeout`, or `error`.

> **Voicemail note**: when Hailey reaches a voicemail or answering machine she leaves a short message asking them to call RunScout's main number back, then ends the call with outcome `voicemail`. Detection is prompt-based (the model recognizes the recorded greeting from the transcript); this is best effort, not carrier answering-machine detection. A true "no answer" where the line is never picked up ends as `no_answer` with no message left, since there is no audio channel to leave one on.

> **Production note**: results live in the server's memory and are gone when it restarts. That's on purpose: this is a demo, and the in-memory store plus terminal logging stand in for a database. In a real production app, have `POST /call_result` write to a database, and remember that on Pipecat Cloud each bot runs in its own container, so `SERVER_URL` must point at a server the bots can reach (not localhost).

## Apollo Integration

When a call captures a security contact, `server.py` hands the outcome row to `apollo_utils.py`, which:

1. **Verifies the contact through Apollo enrichment** (People Match), which returns the school's official website/domain plus Apollo's verification of the email and any phone it has on file.
2. **Validates the captured email** against the school/district website domain, recording a `email_domain_match` of `yes`/`no`/`unknown` in the result row's verification note.
3. **Creates (or updates) the contact** in Apollo with the verified email, phone (with extension), title, and school, then **adds them to an Apollo sequence**.

This runs on the machine where `server.py` lives, so your `APOLLO_API_KEY` stays there and never ships to the cloud bot. Configure it with these environment variables (see `.env.example`):

- `APOLLO_API_KEY` — your Apollo REST API key. **Leave blank to disable the whole integration** (calls still work; contacts just aren't enrolled).
- `APOLLO_SEQUENCE_ID` — the sequence to enroll into. Defaults to "Runscout School Safety — K-12 Outreach".
- `APOLLO_EMAIL_ACCOUNT_ID` — the mailbox to send from. Defaults to `xiomara@runscout.ai`.
- `APOLLO_ENROLL_STATUS` — `paused` (default; held for a teammate to review before any email sends) or `active` (sequence starts immediately).

Each enrichment costs 1 Apollo credit per matched person (0 if not found). Enrollment is best effort: an Apollo failure is logged but never blocks call-result recording.

## Environment Configuration

The bot supports two deployment modes controlled by the `ENV` variable:

### Local Development (`ENV=local`)

- Uses your local server for handling dial-out requests and starting the bot
- Default configuration for development and testing

### Production (`ENV=production`)

- Bot is deployed to Pipecat Cloud; requires `PIPECAT_API_KEY` and `PIPECAT_AGENT_NAME`
- Set these when deploying to production environments
- Your FastAPI server runs either locally or deployed to your infrastructure

## Project Structure

```
outbound-sales/
├── server/                  # Python bot server
│   ├── bot.py               # Hailey: pipeline, tools, dial-out + eval modes
│   ├── server.py            # FastAPI webhook server for Daily PSTN dial-out
│   ├── server_utils.py      # Data models, room creation, bot starting
│   ├── dialer.py            # Batch dialer (5 calls at a time)
│   ├── leads.csv            # Who to call (phone,name,company)
│   ├── evals.yaml           # Eval suite manifest
│   ├── scenarios/           # Text-mode eval scenarios
│   ├── pyproject.toml       # Python dependencies
│   ├── .env.example         # Environment variables template
│   ├── Dockerfile           # Container image for Pipecat Cloud
│   └── pcc-deploy.toml      # Pipecat Cloud deployment config
├── .gitignore
└── README.md
```

Key pieces in `bot.py`:

- **Dual-mode entry point**: `bot()` detects eval runs (`-t eval`) and serves the eval WebSocket transport instead of dialing out. Real calls use `DailyTransport` with full audio.
- **`save_contact_info` tool**: stores the decision maker's name, role, phone, and email on the call result.
- **`end_call` tool**: flushes the pipeline (so the goodbye and tool events are delivered), then shuts down gracefully. Hanging up is just the bot leaving the Daily room.
- **`DialoutManager`**: retries the dial-out up to 5 times before giving up.

## Deploying to Pipecat Cloud

This project is configured for deployment to Pipecat Cloud. You can learn how to deploy in the [Pipecat Quickstart Guide](https://docs.pipecat.ai/getting-started/quickstart#step-2-deploy-to-production).

> **Before you deploy**: update the fields in `server/pcc-deploy.toml` for your own account. `image` points at the example author's Docker Hub repo, so change it to your own registry repo and tag. Also set `agent_name` to the agent name in your Pipecat Cloud account and `secret_set` to the secret set you created with `pipecat cloud secrets set`.

Refer to the [Pipecat Cloud Documentation](https://docs.pipecat.ai/deployment/pipecat-cloud/introduction) to learn more about configuring, deploying, and managing your agents. Remember the production note above: the in-memory results store needs replacing with a database (and a reachable `SERVER_URL`) when bots run in separate containers.

## Learn More

- [Pipecat Documentation](https://docs.pipecat.ai/)
- [Pipecat GitHub](https://github.com/pipecat-ai/pipecat)
- [Pipecat Examples](https://github.com/pipecat-ai/pipecat-examples)
- [Discord Community](https://discord.gg/pipecat)
