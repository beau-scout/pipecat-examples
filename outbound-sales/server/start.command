#!/bin/bash
# Double-click this file in Finder to launch the RunScout dialer control panel.
# It sets up a local Python environment (first run only), starts the server, and
# opens the control page in your browser. Close the Terminal window to stop.
set -e
cd "$(dirname "$0")"

# Create the virtual environment on first run.
if [ ! -d .venv ]; then
  echo "First run: creating virtual environment…"
  uv venv --python 3.11
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# The control server only needs these (no pipecat / onnxruntime — the bot runs
# on Pipecat Cloud, not here).
uv pip install -q fastapi uvicorn aiohttp loguru python-dotenv pydantic

# Open the control panel once the server has had a moment to start.
( sleep 2; open "http://localhost:${PORT:-7867}/" ) &

echo "Starting control panel at http://localhost:${PORT:-7867}/"
python server.py
