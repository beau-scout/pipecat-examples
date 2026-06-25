#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""outbound-sales - Hailey, RunScout's school-safety outreach voice agent.

Hailey calls a school or district main line, greets whoever answers, says she's
calling from RunScout (runscout.ai), and asks who is in charge of safety and
security. If asked why, she explains that RunScout connects to a school's
existing camera systems to detect everyday incidents like student elopement or
propped-open doors. She collects the security decision maker's contact info (or
gets transferred), reports it to server.py, says thanks, and hangs up. On a
captured contact, server.py enrolls them in an Apollo sequence for follow-up.

Required AI services:
- Deepgram (Speech-to-Text)
- Anthropic / Claude (LLM)
- Cartesia (Text-to-Speech; default) or ElevenLabs, via TTS_PROVIDER

Run a real call (see README for the full flow)::

    uv run bot.py -t daily

Run in eval mode for fast, text-only testing::

    uv run bot.py -t eval
    PYTHONPATH=. uv run pipecat eval run scenarios/happy_path.yaml
"""

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.extensions.ivr.ivr_navigator import IVRNavigator, IVRStatus
from pipecat.frames.frames import (
    EndWorkerFrame,
    Frame,
    FunctionCallResultProperties,
    LLMContextFrame,
    LLMMessagesUpdateFrame,
    MetricsFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.metrics.metrics import LLMUsageMetricsData
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.runner.types import EvalRunnerArguments, RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.cartesia.tts import CartesiaTTSService, GenerationConfig
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.base_transport import BaseTransport
from pipecat.transports.daily.transport import DailyParams, DailyTransport
from pipecat.transports.websocket.server import WebsocketServerParams
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from server_utils import AgentRequest, DialoutSettings, Lead, report_result

load_dotenv(override=True)

# Lead used when running evals (`-t eval`), where there's no real call request.
# For school outreach the answerer's name is unknown; "company" carries the
# school or district name. Override with `--runner-body lead.json` if needed.
EVAL_LEAD = Lead(phone="+15550100001", company="Lincoln Elementary School")

# RunScout's main number, left in a voicemail when Hailey reaches a machine.
MAIN_CALLBACK_NUMBER = os.getenv("MAIN_CALLBACK_NUMBER", "210-594-2600")

# Hard cost guards. A call stuck on a phone tree, hold music, or dead air
# triggers an LLM completion per audio fragment and would otherwise run
# unbounded — exactly the kind of runaway that can rack up API spend. These cap
# every call: it's force-ended once it exceeds either limit. Tune via env.
MAX_CALL_SECONDS = int(os.getenv("MAX_CALL_SECONDS", "240"))  # absolute per-call cap
MAX_LLM_TURNS = int(os.getenv("MAX_LLM_TURNS", "40"))  # max LLM completions per call
# Hard backstop on TOTAL LLM completions in any mode. The IVRNavigator makes its
# own completions (classifier + navigation) that bypass TurnLimiter, so a
# misclassified looping menu can churn calls TurnLimiter never sees. UsageTracker
# counts every completion via MetricsFrames and force-ends past this. Set above
# MAX_LLM_TURNS so the conversational guard fires first on normal calls.
MAX_LLM_CALLS = int(os.getenv("MAX_LLM_CALLS", "60"))


class DialoutManager:
    """Manages dialout attempts with retry logic.

    Handles the complexity of initiating outbound calls with automatic retry
    on failure, up to a configurable maximum number of attempts.

    Args:
        transport: The Daily transport instance for making the dialout
        dialout_settings: Settings containing phone number and optional caller ID
        max_retries: Maximum number of dialout attempts (default: 5)
    """

    def __init__(
        self,
        transport: BaseTransport,
        dialout_settings: DialoutSettings,
        max_retries: int | None = 5,
    ):
        self._transport = transport
        self._phone_number = dialout_settings.phone_number
        self._caller_id = dialout_settings.caller_id
        self._max_retries = max_retries
        self._attempt_count = 0
        self._is_successful = False

    async def attempt_dialout(self) -> bool:
        """Attempt to start a dialout call.

        Returns:
            True if dialout attempt was initiated, False if max retries reached
            or call already successful
        """
        if self._attempt_count >= self._max_retries:
            logger.error(
                f"Maximum retry attempts ({self._max_retries}) reached. Giving up on dialout."
            )
            return False

        if self._is_successful:
            logger.debug("Dialout already successful, skipping attempt")
            return False

        self._attempt_count += 1
        logger.info(
            f"Attempting dialout (attempt {self._attempt_count}/{self._max_retries}) to: {self._phone_number}"
        )

        dialout_params = {"phoneNumber": self._phone_number}
        if self._caller_id:
            # The id (UUID) of a phone number purchased through Daily.
            dialout_params["callerId"] = self._caller_id
        await self._transport.start_dialout(dialout_params)
        return True

    def mark_successful(self):
        """Mark the dialout as successful to prevent further retry attempts."""
        self._is_successful = True

    @property
    def is_successful(self) -> bool:
        """Whether the dial-out has been answered."""
        return self._is_successful

    def should_retry(self) -> bool:
        """Check if another dialout attempt should be made."""
        return self._attempt_count < self._max_retries and not self._is_successful


@dataclass
class CallResult:
    """What we learned on one call. Reported to server.py when the call ends."""

    call_id: str
    lead: Lead
    contact: dict[str, str] | None = None
    end_reason: str | None = None
    notes: str = ""
    # True once end_call has started the graceful pipeline shutdown.
    ending: bool = False
    # Full conversation transcript, populated at call end from the LLM context.
    transcript: list[dict] = field(default_factory=list)
    # Per-call LLM token usage, accumulated by UsageTracker (for cost logging).
    usage: dict = field(default_factory=dict)
    # True once a live person was reached (IVRNavigator conversation hand-off).
    reached_human: bool = False

    @property
    def outcome(self) -> str:
        if self.contact:
            return "contact_captured"
        if self.end_reason:
            # A cost-guard cutoff (ran too long / too many turns) on a call where
            # a real person was on the line is a hangup, not a system label —
            # report it as such so the dashboard reflects we did reach someone.
            if self.reached_human and self.end_reason in ("max_turns", "timeout"):
                return "hung_up"
            return self.end_reason
        # No explicit end reason. If we never reached a live person, the call
        # died in the phone system (menu/IVR dead-end) — that's NOT a hangup and
        # should be re-tried, so report "no_answer". Only call it "hung_up" if a
        # real person was on the line and the call dropped.
        return "hung_up" if self.reached_human else "no_answer"

    def to_row(self) -> dict[str, str]:
        contact = self.contact or {}
        return {
            "call_id": self.call_id,
            "lead_phone": self.lead.phone,
            "lead_name": self.lead.name or "",
            "lead_company": self.lead.company or "",
            "outcome": self.outcome,
            "contact_name": contact.get("name", ""),
            "contact_role": contact.get("role", ""),
            "contact_phone": contact.get("phone", ""),
            "contact_extension": contact.get("extension", ""),
            "contact_email": contact.get("email", ""),
            "contact_best_time": contact.get("best_time", ""),
            "notes": self.notes,
            "transcript": json.dumps(self.transcript),
            "usage": json.dumps(self.usage),
        }


# Deepgram (like Whisper and most STT) hallucinates a stock phrase out of the
# silence/noise right after a call connects — "Thank you for calling", "Thank
# you", "Bye", etc. Left unfiltered, the bot treats that phantom phrase as the
# callee's greeting and replies before any human has spoken (the "she didn't
# wait for me to say hello" bug — confirmed in a real call transcript where the
# only user turn was "Thank you for calling."). These are exact, lowercased
# phrases; a real greeting like "Thank you for calling Lincoln Elementary, how
# can I help you?" is longer and won't match.
_STT_HALLUCINATIONS = frozenset(
    {
        "thank you",
        "thank you.",
        "thanks",
        "thanks.",
        "thank you for calling",
        "thank you for calling.",
        "thanks for calling",
        "thanks for calling.",
        "thank you for watching",
        "thank you for watching.",
        "thanks for watching",
        "thank you very much",
        "thank you very much.",
        "bye",
        "bye.",
        "bye bye",
        "goodbye",
        "you",
        "you.",
        "okay",
        "okay.",
        "please subscribe",
    }
)


class StartupHallucinationFilter(FrameProcessor):
    """Drops stock STT hallucinations until the first real user turn.

    Hailey is the caller, so she must wait for the callee to actually speak.
    But STT can emit a phantom phrase from the connect-silence, which would
    otherwise count as the callee's greeting and make Hailey reply too early.
    This swallows TranscriptionFrames whose text is just a known hallucination
    (or a single stray character) until a genuine utterance arrives, after
    which it passes everything through untouched.
    """

    def __init__(self):
        super().__init__()
        self._real_turn_seen = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if (
            not self._real_turn_seen
            and direction == FrameDirection.DOWNSTREAM
            and isinstance(frame, TranscriptionFrame)
        ):
            text = (frame.text or "").strip()
            if len(text) <= 1 or text.lower().rstrip(".!? ") in {
                h.rstrip(".") for h in _STT_HALLUCINATIONS
            }:
                logger.debug(f"Dropping likely STT hallucination on connect: {frame.text!r}")
                return
            self._real_turn_seen = True
        await self.push_frame(frame, direction)


class TurnLimiter(FrameProcessor):
    """Cost guard: force-ends the call after MAX_LLM_TURNS LLM completions.

    Each LLMContextFrame reaching the LLM is one (paid) completion. A call stuck
    on a phone tree / hold music / dead air produces a steady stream of these
    with near-zero useful output, so a hard cap stops a single call from running
    up unbounded API spend. Sits just before the LLM and swallows the frame that
    would trip the limit so no further completion fires.
    """

    def __init__(self, max_turns: int):
        super().__init__()
        self._max = max_turns
        self._count = 0
        self._on_limit = None  # async callback, set once the worker exists

    def set_on_limit(self, callback):
        self._on_limit = callback

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, LLMContextFrame):
            self._count += 1
            if self._count > self._max and self._on_limit is not None:
                logger.warning(
                    f"Cost guard: {self._max} LLM turns reached — force-ending the call."
                )
                await self._on_limit()
                return  # swallow so the LLM doesn't run on this turn
        await self.push_frame(frame, direction)


class UsageTracker(FrameProcessor):
    """Accumulates per-call LLM token usage from MetricsFrames so each call's
    cost can be logged and shown in the dashboard. Counts LLM completions and
    input/output/cache tokens; the cache split shows prompt caching working.

    Also enforces a mode-independent hard cap on total LLM completions. Unlike
    TurnLimiter (which only sees LLMContextFrames flowing into the bare LLM and
    is bypassed by the IVRNavigator's internal run_llm calls), this counts every
    completion the model actually made via its MetricsFrame, so it catches an
    IVR-mode runaway — a misclassified menu that loops the navigator/Hailey
    forever — that the TurnLimiter cannot see."""

    def __init__(self, totals: dict, max_calls: int):
        super().__init__()
        self._t = totals
        self._max = max_calls
        self._on_limit = None  # async callback, set once the worker exists
        self._fired = False

    def set_on_limit(self, callback):
        self._on_limit = callback

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, MetricsFrame):
            for d in frame.data:
                if isinstance(d, LLMUsageMetricsData):
                    u = d.value
                    self._t["llm_calls"] += 1
                    self._t["prompt_tokens"] += u.prompt_tokens or 0
                    self._t["completion_tokens"] += u.completion_tokens or 0
                    self._t["cache_read_tokens"] += u.cache_read_input_tokens or 0
                    self._t["cache_creation_tokens"] += u.cache_creation_input_tokens or 0
                    if (
                        self._t["llm_calls"] > self._max
                        and self._on_limit is not None
                        and not self._fired
                    ):
                        self._fired = True
                        logger.warning(
                            f"Cost guard: {self._max} LLM completions reached "
                            "(any mode) — force-ending the call."
                        )
                        await self._on_limit()
        await self.push_frame(frame, direction)


def system_prompt(lead: Lead) -> str:
    if lead.company:
        place_line = f"You are calling {lead.company}."
        # We know the school's name, so we can confirm it if they don't say it.
        first_turn = f"""1. WAIT for them to speak first. You are the caller, so the person who answers greets first ("Hello", "Hello, {lead.company}", "Front office", etc.). Stay silent until you have actually heard them say something — never speak the instant the call connects, never speak into silence, and never react to a click, a beep, or background noise.
   Once they have greeted you, ALWAYS introduce yourself first — "Hi, this is Hailey from RunScout" — every time, even if they already named the school. What you say next depends on how they answered:
   - If they ALREADY named the school (e.g. "Hello, {lead.company}"): after introducing yourself, ask who is in charge of safety and security. For example: "Hi, this is Hailey from RunScout! Who's in charge of safety and security there?"
   - If they did NOT name the school (just "Hello", "Front office", etc.): after introducing yourself, confirm you've reached the right place. For example: "Hi, this is Hailey from RunScout — have I reached {lead.company}?" Once they confirm, ask who is in charge of safety and security.
   Never skip the introduction, and never open with the security question before saying who you are."""
    else:
        place_line = "You are calling a school or school district main line."
        # We do NOT know the school's name — don't ask which school it is.
        first_turn = """1. WAIT for them to speak first. You are the caller, so the person who answers greets first ("Hello", "Hello, [school name]", "Front office", etc.). Stay silent until you have actually heard them say something — never speak the instant the call connects, never speak into silence, and never react to a click, a beep, or background noise.
   Once they have greeted you, ALWAYS introduce yourself first — "Hi, this is Hailey from RunScout" — then ask who is in charge of safety and security. For example: "Hi, this is Hailey from RunScout! Who's in charge of safety and security there?" You already know you dialed a school, so do not ask which school it is. Never skip the introduction, and never open with the security question before saying who you are."""

    return f"""You are Hailey, a friendly representative calling on behalf of RunScout (runscout.ai). You are on an outbound phone call to a school or school district. {place_line} Whoever answers is most likely a front-office staffer, not the person you ultimately need.

This is a real phone conversation: your replies are spoken aloud. Keep them short (one or two sentences), warm, and natural. Never use lists, emojis, or any formatting that can't be spoken.

Sound like a relaxed, friendly human on the phone — warm, unhurried, with natural contractions — never like you're reading an ad. Always introduce yourself, but say it in your own easy, conversational words and adapt to how they answered; don't recite a fixed script. Same for your fuller answer to "what does that mean / what do you do?" — explain it naturally, not as a memorized pitch. That is what keeps you sounding human.

Your goal is simple: find out who is in charge of safety and security at this school or district, collect their contact information, and get a good time for one of our founders to call them. You are NOT trying to speak with that person right now — you are gathering their details and a callback time for a teammate to follow up.

What RunScout is — when they ask what it is or why you're calling, slow down and explain it like you'd casually describe your job to a friend, in your OWN words (not a memorized pitch). Warm lead-in, then just two short, plain sentences with a clear pause between them — simple everyday words, no jargon, vary the wording naturally each call. The two things to get across: (1) RunScout helps schools stay safer using the security cameras they already have, and (2) when something comes up — like a propped-open door, or a kid wandering off — it instantly pings their security team with a text and an email, including a short video clip, so they can jump on it. Don't rattle off features or cram it into one breath. Say those two beats, then stop and let them react; share a little more only if they seem interested.

Follow this flow — one question at a time, nothing extra:
{first_turn}
2. If they ask why you're calling before answering, give the one-line RunScout explanation, then ask who handles security. Once you know who handles security, get their name and role.
3. Ask for their direct phone number (and extension if it's a switchboard line). One ask — no follow-up.
4. Ask for their email address. One ask — no follow-up.
5. Read the email back to confirm — spoken naturally, never the raw address. Replace "@" with "at" and "." with "dot", pause between chunks. Example: "dana.smith@lincoln.k12.ca.us" → "dana dot smith, at lincoln dot k twelve, dot c a, dot u s — did I get that right?" Pass the real email address to save_contact_info, not the spoken version.
6. Ask for a good time for one of our founders to call. One ask — no follow-up.
7. CALL save_contact_info NOW, before you say goodbye, with everything you collected: name, role, phone, extension, email, and best time. This is REQUIRED — the contact is lost unless you call it. Do not skip it, and do not say the closing line until you have called it. (Pass the real phone number and email, not the spoken-out versions.) Don't narrate the saving — no "let me jot that down" / "let me save that"; just call the tool quietly. A brief warm acknowledgment of the time ("Perfect, got it") is fine.
8. Then end cleanly with exactly: "Thank you for your help, I really appreciate it. Have a wonderful day!" and immediately call end_call. No recap, no "is there anything else."

Critical rule on follow-ups: after each question, wait for the answer. Do NOT add a second question or a clarifying phrase in the same turn. One question. Stop. Wait.

Rules:
- If someone asks whether you are a real person, a robot, AI, or automated: be honest. Acknowledge that you are an AI voice assistant — say something like "I am an AI assistant calling on behalf of RunScout" — and then continue naturally. Never claim to be a human. Never double down on being a person if pressed. Being straightforward builds more trust than pretending.
- Do NOT ask to be transferred or to speak with the security person now. If they offer to transfer you or put them on the line, politely decline — say there's no need, you just want to leave their details so a senior rep can follow up — and continue collecting the contact info and callback time.
- Whatever callback time they give you, just accept it. Confirm it back warmly ("Great, after 3 it is — got it") and move on. NEVER push back, negotiate, or suggest a different time, even if they pick an evening or an odd hour. Don't ask "A.M. or P.M." — if it's genuinely ambiguous, assume the most natural reading and confirm it. Their preferred time is always fine.
- EVERYTHING you say is spoken aloud on the call. NEVER narrate your actions, thoughts, or what you're hearing, and never use stage directions or bracketed/asterisk text. Only say words you intend the other person to hear. If you have nothing to say, say nothing at all.
- By the time you're talking, you've already been connected to a person (phone menus are handled for you before you join). So just talk to them naturally — don't try to press keys or navigate menus. You CANNOT press keys, so never say you will ("let me press 1", "I'll select option 2") — those words just get spoken aloud and accomplish nothing.
- If, despite that, it becomes clear you are actually hearing an automated phone menu or recording rather than a live person — e.g. the same options keep repeating ("press 1 for…", "to reach X press Y", "main menu", "your call could not be completed", a timeout prompt) and nobody is actually responding to what you say — do NOT keep talking to it and do NOT narrate pressing keys. You have no way to navigate it, so call end_call with reason "no_answer" right away. Never get stuck in a loop reacting to a recording.
- If you reach a voicemail or answering machine (a recorded greeting, an instruction to leave a message, a beep, a long recorded hours/closure message, and no live person responds): wait for the beep if there is one, then leave a short, friendly message — "Hi, this is Hailey calling from RunScout about school safety. When you have a moment, please give us a call back at {MAIN_CALLBACK_NUMBER}. Thank you!" Then call end_call with reason "voicemail". Do NOT ask a recording questions or try to have a conversation with it.
- If the recording says the school is closed for summer or an extended break (e.g. "closed for summer break", "we'll reopen on July sixteenth", "out for the summer"): leave the same callback message, then call end_call with reason "closed_for_summer" (not "voicemail") so we hold off re-calling for a while.
- If they decline, aren't interested, or ask to be removed from your list: apologize once, thank them, say goodbye, and call end_call with reason "refused". Never argue or push back.
- If this is clearly a wrong number, apologize, say goodbye, and call end_call with reason "wrong_number".
- Never ask the person which school you've reached or "which school am I calling?" — you dialed a school's main line, so you already know it's a school. If you don't have its name, just proceed to ask who handles safety and security. Asking which school sounds confused and robotic.
- Don't ask again for information you already have.
- Capture the phone extension whenever there is one; people often give a main number plus an extension.
- Never invent contact information. Only save what the person actually told you."""


async def run_bot(
    transport: BaseTransport,
    runner_args: RunnerArguments,
    *,
    lead: Lead,
    dialout_settings: DialoutSettings | None,
    call_id: str,
    report_results: bool,
) -> None:
    """Run Hailey for one session.

    Args:
        transport: The transport for this session (Daily for real calls, the
            eval websocket transport for `-t eval` runs).
        runner_args: Runner session arguments.
        lead: Who we're calling (name personalizes the greeting).
        dialout_settings: Dial-out settings for real calls; None on eval runs.
        call_id: Identifier for this call, minted by dialer.py ("eval" on eval runs).
        report_results: Whether to report an outcome row to server.py at call end.
    """
    logger.info(f"Starting bot for call {call_id} to {lead.phone}")

    result = CallResult(call_id=call_id, lead=lead)

    # Speech-to-Text service
    stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))

    # Text-to-Speech service. TTS_PROVIDER picks the vendor so you can A/B a
    # voice clone on a real call: "cartesia" (default — lowest latency and cost)
    # or "elevenlabs" (higher cloning fidelity). Each provider reads its own
    # voice ID, so flipping the flag swaps both the engine and the voice.
    tts_provider = os.getenv("TTS_PROVIDER", "cartesia").lower()
    if tts_provider == "elevenlabs":
        tts = ElevenLabsTTSService(
            api_key=os.getenv("ELEVENLABS_API_KEY"),
            settings=ElevenLabsTTSService.Settings(
                # Default: ElevenLabs "Rachel" (warm, natural female). Override
                # with ELEVENLABS_VOICE_ID to use a different library voice or a
                # clone — auditioning a few in the ElevenLabs dashboard is the
                # quickest way to find the one you like best.
                voice=os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),
                # Turbo v2.5: ElevenLabs' best quality-per-latency model, the
                # right balance for a real-time phone call. (Flash v2.5 is a hair
                # faster but flatter; multilingual_v2 is richer but too slow.)
                model=os.getenv("ELEVENLABS_MODEL", "eleven_turbo_v2_5"),
            ),
        )
    else:
        tts = CartesiaTTSService(
            api_key=os.getenv("CARTESIA_API_KEY"),
            settings=CartesiaTTSService.Settings(
                # Default: Cartesia "Sierra - California Girl"
                voice=os.getenv("CARTESIA_VOICE_ID", "b7d50908-b17c-442d-ad8d-810c63997ed9"),
                # Speak a touch slower than default (1.0) for a calmer, less
                # "automated" delivery — most noticeable on the scripted opening
                # line. Tune CARTESIA_SPEED (0.6–1.5; lower = slower) to taste.
                generation_config=GenerationConfig(
                    speed=float(os.getenv("CARTESIA_SPEED", "0.9")),
                ),
            ),
        )

    # LLM service (Claude). Default to Sonnet 4.6; override with ANTHROPIC_MODEL
    # (e.g. claude-haiku-4-5 for the lowest phone-call latency/cost). Thinking is
    # left off by default — extended thinking would add seconds of dead air.
    #
    # Cost: the large static system prompt + tool schemas are the bulk of every
    # turn's input. enable_prompt_caching marks them cacheable so repeated turns
    # within a call re-read them at ~10% cost instead of full price — the single
    # biggest lever on per-call spend. max_tokens caps the (short, spoken) reply
    # so a turn can't run away generating output.
    # NOTE: system_instruction is intentionally NOT set here. The Anthropic
    # adapter gives a service-level system_instruction absolute priority over
    # any system message in the context (base_llm_adapter._resolve_system_
    # instruction), which would stop the IVRNavigator from swapping in its
    # classifier / navigation / hand-off prompts. Instead the system prompt
    # lives in the context (seeded below) and the IVRNavigator manages it on
    # real calls.
    llm = AnthropicLLMService(
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        settings=AnthropicLLMService.Settings(
            model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            enable_prompt_caching=True,
            max_tokens=int(os.getenv("ANTHROPIC_MAX_TOKENS", "512")),
        ),
    )

    # IVR navigator: detects whether we reached an automated phone menu or a live
    # human. For a menu it actively navigates with DTMF (this is what makes the
    # bot ACT on menus — the bare LLM never even ran on continuous menu audio
    # because turn-taking treats a recording as "never finished"). When a human
    # is reached it fires on_conversation_detected and we hand off to Hailey's
    # normal conversation (below).
    ivr_goal = (
        f"You are calling {lead.company or 'a school'} on behalf of RunScout, and you "
        "need to reach a LIVE PERSON. Navigate the phone menu to the front office, "
        "main office, reception, or operator — whatever option connects you to a real "
        "person who can help or transfer you. Prefer options worded like 'front "
        "office', 'main office', 'operator', 'reception', 'to speak with someone', or "
        "'all other matters'. NEVER choose attendance, absence reporting, the "
        "registrar, counseling, fees or payments, special education, food services, or "
        "a staff directory. If the only path is a recorded directory with no way to "
        "reach a person, respond with <ivr>stuck</ivr>."
    )
    ivr_navigator = IVRNavigator(llm=llm, ivr_prompt=ivr_goal)

    async def save_contact_info(
        params: FunctionCallParams,
        name: str,
        role: str,
        phone: str = "",
        extension: str = "",
        email: str = "",
        best_time: str = "",
    ):
        """Save the school/district security decision maker's contact information.

        Call this EXACTLY ONCE, near the END of the call, and ONLY after you
        have actually collected the contact's name AND at least a phone number
        or an email from the person you're speaking with. Do NOT call it during
        the greeting, before anyone has given you contact details, or with empty
        or made-up values — that produces a wrong "let me jot that down" moment.
        Read the email back to confirm spelling before calling this.

        Args:
            name: The security decision maker's full name.
            role: Their role, e.g. "Director of Safety and Security" or "Principal".
            phone: Their direct or main phone number, if given.
            extension: The phone extension, if the number goes through a switchboard.
            email: Their email address, if given.
            best_time: A good day and time, during school hours, for a senior
                rep to call the security person (e.g. "Tuesday 10am"). Leave
                blank if no time was given.
        """
        if not phone and not email:
            await params.result_callback(
                {"status": "error", "message": "Need at least a phone number or an email."}
            )
            return
        result.contact = {
            "name": name,
            "role": role,
            "phone": phone,
            "extension": extension,
            "email": email,
            "best_time": best_time,
        }
        logger.info(f"Call {call_id}: saved contact info for {name} ({role})")
        await params.result_callback({"status": "saved"})

    async def end_call(params: FunctionCallParams, reason: str, notes: str = ""):
        """End the phone call. Only call this after you have said goodbye.

        Args:
            reason: Why the call is ending. One of: "contact_captured",
                "transferred_no_info", "refused", "wrong_number", "voicemail",
                "closed_for_summer", "other". Use "closed_for_summer" when the
                school's recording says it is closed for summer/an extended
                break (so we hold off re-calling for a while).
            notes: Optional one-line note about how the call went.
        """
        result.end_reason = reason
        result.notes = notes
        if reason == "contact_captured" and not result.contact:
            # The model wrapped up as if it captured a contact but never called
            # save_contact_info, so there's nothing to persist or enroll. Surface
            # it loudly instead of recording a hollow "contact_captured" row.
            logger.warning(
                f"Call {call_id}: ended as 'contact_captured' but save_contact_info "
                f"was never called — no contact data saved."
            )
        logger.info(f"Call {call_id}: ending call ({reason})")
        # Don't run the LLM again; the goodbye was already spoken before this call.
        await params.result_callback(
            {"status": "ending"}, properties=FunctionCallResultProperties(run_llm=False)
        )
        # Drain the in-flight goodbye and function-call events first: the eval
        # websocket server closes its connection as soon as the EndFrame passes
        # the input transport, so anything still queued would be lost.
        await worker.flush_pipeline()
        # EndWorkerFrame flows upstream and shuts the pipeline down gracefully.
        result.ending = True
        await params.llm.push_frame(EndWorkerFrame(), FrameDirection.UPSTREAM)

    # (No canned "let me jot that down" filler on save_contact_info: it collided
    # with Hailey's own natural acknowledgment and the closing line, producing a
    # doubled, awkward wrap-up. The brief save round-trip is left unmasked.)
    # (Phone-menu / DTMF navigation is handled by the IVRNavigator below, not a
    # tool, so there's no press_keys function here anymore.)

    # Seed Hailey's system prompt into the context (since it's not a service-level
    # system_instruction). On eval runs the bare LLM uses this directly; on real
    # calls the IVRNavigator swaps it for its classifier/IVR prompts and restores
    # Hailey's on hand-off. Direct functions in the context register automatically.
    context = LLMContext(
        messages=[{"role": "system", "content": system_prompt(lead)}],
        tools=[save_contact_info, end_call],
    )
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_turn_strategies=UserTurnStrategies(
                stop=[
                    TurnAnalyzerUserTurnStopStrategy(
                        # Smart Turn's default silence fallback is 3s. When it
                        # judges an utterance "incomplete" (e.g. a bare
                        # "Hello?"), the bot would sit silent that whole time.
                        # 1s keeps its don't-interrupt judgement with a phone
                        # friendly worst case.
                        turn_analyzer=LocalSmartTurnAnalyzerV3(
                            params=SmartTurnParams(stop_secs=1.0)
                        )
                    )
                ]
            ),
        ),
    )

    # Pipeline - assembled from reusable components. The person who answers
    # speaks first ("Hello" / "Hello, Lincoln Elementary"); Hailey's first reply
    # is generated by the LLM so she can adapt — introduce herself and either
    # confirm the school or go straight to the security question (see the system
    # prompt). No canned first turn, so nothing to clip on PSTN early-media.
    turn_limiter = TurnLimiter(MAX_LLM_TURNS)  # on-limit callback set after worker exists
    usage_totals = {
        "llm_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
    usage_tracker = UsageTracker(usage_totals, MAX_LLM_CALLS)  # on-limit set after worker exists
    # Real calls go through the IVRNavigator (classify menu-vs-human, navigate
    # menus with DTMF, hand off to Hailey on a person). Eval runs use the bare
    # LLM so the scenarios exercise Hailey's conversation directly.
    llm_stage = ivr_navigator if dialout_settings is not None else llm
    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            # Drop stock STT hallucinations / robot auto-greeters on connect so
            # Hailey waits for a real human greeting before replying.
            StartupHallucinationFilter(),
            user_aggregator,
            # Cost guard: cap LLM completions per call (stuck-call runaway).
            turn_limiter,
            llm_stage,
            # Tally LLM token usage (incl. cache hits) for per-call cost logging,
            # and enforce a mode-independent hard cap on total completions.
            usage_tracker,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
        ),
        # Logs exact user-stopped-speaking to bot-started-speaking latency
        observers=[UserBotLatencyObserver()],
        # Honor the runner's idle timeout (the dial-out handlers below still
        # drive normal call teardown; this is the backstop for a stuck call).
        idle_timeout_secs=runner_args.pipeline_idle_timeout_secs,
    )

    async def guard_end(reason: str):
        """Cost guard: force-end a call that's run too long or churned too many
        LLM turns, so a stuck call can't rack up unbounded API spend."""
        if result.ending or result.end_reason:
            return
        result.end_reason = reason
        logger.warning(f"Call {call_id}: cost guard force-ending call ({reason})")
        await worker.cancel()

    turn_limiter.set_on_limit(lambda: guard_end("max_turns"))
    usage_tracker.set_on_limit(lambda: guard_end("max_turns"))

    @ivr_navigator.event_handler("on_conversation_detected")
    async def on_conversation_detected(ivr_processor, conversation_history):
        # A live person answered (or the menu handed us to one). Switch the LLM
        # from IVR-navigation mode to Hailey's conversation: load her system
        # prompt plus everything heard so far, and run so she responds to them.
        logger.info(f"Call {call_id}: live person reached — handing off to Hailey")
        result.reached_human = True
        messages = [{"role": "developer", "content": system_prompt(lead)}, *conversation_history]
        await ivr_processor.push_frame(
            LLMMessagesUpdateFrame(messages=messages, run_llm=True),
            FrameDirection.UPSTREAM,
        )

    @ivr_navigator.event_handler("on_ivr_status_changed")
    async def on_ivr_status_changed(ivr_processor, status):
        logger.info(f"Call {call_id}: IVR status — {getattr(status, 'value', status)}")
        # Stuck = no menu path to a live person (recorded directory, dead end).
        # Don't burn the call sitting in the menu; end it as no_answer. But if we
        # ALREADY reached a human (conversation hand-off happened), a later STUCK
        # must not relabel the call no_answer — that would re-dial someone we just
        # spoke to. Leave the outcome to the human-conversation path.
        if (
            status == IVRStatus.STUCK
            and not result.ending
            and not result.end_reason
            and not result.reached_human
        ):
            result.end_reason = "no_answer"
            result.notes = "IVR navigation stuck — no path to a live person"
            await worker.cancel()

    async def call_watchdog():
        """Absolute per-call time cap — backstop for guard cases the turn
        counter misses (e.g. long silences with few transcriptions)."""
        try:
            await asyncio.sleep(MAX_CALL_SECONDS)
        except asyncio.CancelledError:
            return
        await guard_end("timeout")

    # Dial-out only applies to real calls; eval runs get minimal handlers below.
    if dialout_settings is not None:
        # Initialize dialout manager
        dialout_manager = DialoutManager(transport, dialout_settings)

        @transport.event_handler("on_joined")
        async def on_joined(transport, data):
            await dialout_manager.attempt_dialout()

        @transport.event_handler("on_dialout_answered")
        async def on_dialout_answered(transport, data):
            logger.debug(f"Dial-out answered: {data}")
            # Record that the callee picked up (so on_dialout_stopped doesn't
            # misreport a connected call as no_answer). Hailey doesn't speak
            # first — the person says "Hello" and the LLM replies.
            dialout_manager.mark_successful()

        @transport.event_handler("on_dialout_stopped")
        async def on_dialout_stopped(transport, data):
            logger.debug(f"Dial-out stopped: {data}")
            # Stopped before being answered means busy or no answer.
            if not dialout_manager.is_successful:
                result.end_reason = "no_answer"
            if not result.ending:
                await worker.cancel()

        @transport.event_handler("on_dialout_error")
        async def on_dialout_error(transport, data: Any):
            logger.error(f"Dial-out error, retrying: {data}")

            if dialout_manager.should_retry():
                await dialout_manager.attempt_dialout()
            else:
                logger.error("No more retries allowed, stopping bot.")
                result.end_reason = "dialout_error"
                await worker.cancel()

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport, client):
            logger.info("Client disconnected")
            # If Hailey already ended the call, the pipeline is shutting down
            # gracefully; cancelling now would cut off that shutdown.
            if not result.ending:
                await worker.cancel()

    else:

        @transport.event_handler("on_client_connected")
        async def on_client_connected(transport, client):
            logger.info("Client connected")

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport, client):
            logger.info("Client disconnected")
            if result.end_reason is None:
                await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)

    await runner.add_workers(worker)
    watchdog_task = asyncio.create_task(call_watchdog())
    try:
        await runner.run()
    finally:
        watchdog_task.cancel()
        # The outcome report doubles as the "this call finished" signal for
        # dialer.py, so it must be sent no matter how the call ended. This is
        # where a real app would write to a database; the demo logs the row
        # and hands it to server.py, which keeps it in memory.
        # Extract the full conversation transcript from the LLM context so it
        # can be reviewed in the dashboard for prompt improvement.
        result.transcript = []
        for m in context.messages:
            role = m.get("role")
            if role not in ("user", "assistant"):
                continue
            content = m.get("content", "")
            if isinstance(content, str):
                text = content.strip()
            elif isinstance(content, list):
                # Anthropic content blocks — extract text parts, skip tool calls
                text = " ".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ).strip()
            else:
                text = ""
            if text:
                result.transcript.append({"role": role, "text": text})

        # Per-call token summary — shows cost and whether prompt caching engaged
        # (cache reads should be most of the input after the first turn).
        result.usage = usage_totals
        u = usage_totals
        cached_in = u["cache_read_tokens"] + u["cache_creation_tokens"]
        total_in = u["prompt_tokens"] + cached_in
        cache_pct = round(100 * u["cache_read_tokens"] / total_in) if total_in else 0
        logger.info(
            f"Call {call_id}: outcome '{result.outcome}' | "
            f"{u['llm_calls']} LLM calls, in={total_in} tok "
            f"(cache_read={u['cache_read_tokens']}, {cache_pct}% cached), "
            f"out={u['completion_tokens']} tok"
        )

        if report_results:
            await report_result(result.to_row())


async def bot(runner_args: RunnerArguments):
    """Main bot entry point."""

    # Behavioral evals: run with `-t eval` to drive this bot via `pipecat eval`.
    # Eval runs don't dial out: the harness connects over a local WebSocket.
    if isinstance(runner_args, EvalRunnerArguments):
        transport = await create_transport(
            runner_args,
            {
                "eval": lambda: WebsocketServerParams(
                    audio_in_enabled=True,
                    audio_out_enabled=True,
                ),
            },
        )
        lead = Lead.model_validate(runner_args.body) if runner_args.body else EVAL_LEAD
        await run_bot(
            transport,
            runner_args,
            lead=lead,
            dialout_settings=None,
            call_id="eval",
            report_results=False,
        )
        return

    try:
        request = AgentRequest.model_validate(runner_args.body)

        transport = DailyTransport(
            request.room_url,
            request.token,
            "Hailey (RunScout School Safety)",
            params=DailyParams(
                api_key=os.getenv("DAILY_API_KEY"),
                audio_in_enabled=True,
                audio_out_enabled=True,
            ),
        )

        await run_bot(
            transport,
            runner_args,
            lead=request.lead,
            dialout_settings=request.dialout_settings,
            call_id=request.call_id,
            report_results=True,
        )

    except Exception as e:
        logger.error(f"Error running bot: {e}")
        raise e


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
