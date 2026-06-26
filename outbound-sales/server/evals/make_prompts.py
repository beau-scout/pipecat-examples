#!/usr/bin/env python3
"""Generate prompt snapshot files for the simulation eval harness.

Run from outbound-sales/server/ whenever bot.py's prompts change:

    PYTHONPATH=. uv run python evals/make_prompts.py

Writes four files to evals/prompts/ (committed alongside this script):
  hailey_named.txt     — system_prompt() when the school name IS known
  hailey_unnamed.txt   — system_prompt() when the school name is NOT known
  ivr_classifier.txt   — SchoolIVRNavigator.CLASSIFIER_PROMPT
  ivr_navigation.txt   — ivr_goal_for() output for a named school

These files are read by the simulation agents in simulate.js to verify
Hailey's real prompts, so they must stay in sync with bot.py.
"""

import pathlib
import sys

# Run from outbound-sales/server/ so bot.py imports cleanly.
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from bot import SchoolIVRNavigator, ivr_goal_for, system_prompt  # noqa: E402
from pipecat.extensions.ivr.ivr_navigator import IVRNavigator  # noqa: E402
from server_utils import Lead  # noqa: E402

OUT = pathlib.Path(__file__).parent / "prompts"
OUT.mkdir(exist_ok=True)

named_lead = Lead(phone="+15550100001", company="Lincoln Elementary School")
unnamed_lead = Lead(phone="+15550100002", company=None)

# Full IVR navigation prompt: library base template + school-specific goal.
ivr_nav_full = IVRNavigator.IVR_NAVIGATION_BASE.format(goal=ivr_goal_for(named_lead))

files = {
    "hailey_named.txt": system_prompt(named_lead),
    "hailey_unnamed.txt": system_prompt(unnamed_lead),
    "ivr_classifier.txt": SchoolIVRNavigator.CLASSIFIER_PROMPT,
    "ivr_navigation.txt": ivr_nav_full,
}

for name, content in files.items():
    path = OUT / name
    path.write_text(content)
    lines = content.count("\n") + 1
    print(f"  wrote {path.relative_to(pathlib.Path(__file__).parent.parent.parent)} ({lines} lines)")

print(f"\nDone. {len(files)} prompt file(s) in {OUT.relative_to(pathlib.Path(__file__).parent.parent.parent)}")
