#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Claude-backed judge for the Pipecat eval suite.

Pipecat's eval harness has no built-in Anthropic judge (only ollama / openai),
so the scenarios point their ``judge.eval.factory`` at ``claude_judge`` here.
The factory returns an ``AnthropicLLMService``, which exposes the
``run_inference(context, max_tokens, system_instruction)`` method the
``EvalJudge`` calls to grade each turn — keeping the whole project on Claude
(no OpenAI).

Run the suite from the ``server/`` directory so this module is importable, e.g.:

    PYTHONPATH=. uv run pipecat eval suite evals.yaml
"""

import os
from typing import Any


def claude_judge(config: dict) -> Any:
    """Build the eval judge's LLM service (Claude).

    Args:
        config: The scenario's ``judge.eval`` block. Honors ``model`` (defaults
            to claude-haiku-4-5 — fast and cheap, which is what a yes/no/continue
            grader wants) and an optional ``api_key`` (else ANTHROPIC_API_KEY).
    """
    # Imported lazily so this module is cheap to import and only pulls in pipecat
    # when the eval harness actually constructs the judge.
    from pipecat.services.anthropic.llm import AnthropicLLMService

    model = config.get("model") or "claude-haiku-4-5"
    api_key = config.get("api_key") or os.environ.get("ANTHROPIC_API_KEY")
    return AnthropicLLMService(
        api_key=api_key,
        settings=AnthropicLLMService.Settings(model=model),
    )
