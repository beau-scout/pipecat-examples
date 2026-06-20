"""Alert rules, debouncing, and fan-out to the browser / console / webhooks."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field

from .analysis import PeopleAnalysis
from .config import AlertSettings
from .doors import Door

log = logging.getLogger("scout.alerts")

_SEVERITY = {
    "child_alone": "high",
    "door_open": "high",
    "child_detected": "medium",
    "child_with_group": "medium",
    "group_detected": "low",
}


@dataclass
class Alert:
    rule: str
    message: str
    severity: str = "medium"
    ts: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class AlertEngine:
    """Evaluates rules against each analysis snapshot, with per-key cooldowns."""

    def __init__(self, settings: AlertSettings):
        self._settings = settings
        self._last_fired: dict[str, float] = {}

    def _enabled(self, rule: str) -> bool:
        r = self._settings.rules.get(rule)
        return bool(r and r.enabled)

    def _cooldown(self, rule: str) -> float:
        r = self._settings.rules.get(rule)
        if r and r.cooldown_s is not None:
            return r.cooldown_s
        return self._settings.default_cooldown_s

    def _ready(self, key: str, rule: str, now: float) -> bool:
        if now - self._last_fired.get(key, 0.0) < self._cooldown(rule):
            return False
        self._last_fired[key] = now
        return True

    def evaluate(self, people: PeopleAnalysis, doors: list[Door]) -> list[Alert]:
        now = time.time()
        out: list[Alert] = []

        def fire(rule: str, key: str, message: str, data: dict | None = None) -> None:
            if self._enabled(rule) and self._ready(key, rule, now):
                out.append(Alert(rule=rule, message=message,
                                 severity=_SEVERITY.get(rule, "medium"),
                                 data=data or {}))

        children = [p for p in people.persons if p.category == "child"]
        if children:
            fire("child_detected", "child_detected",
                 f"{len(children)} child(ren) detected in view.",
                 {"count": len(children)})

        if any(c.is_alone for c in children):
            n = sum(1 for c in children if c.is_alone)
            fire("child_alone", "child_alone",
                 f"{n} child(ren) appear to be ALONE (no group nearby).",
                 {"count": n})

        if any(not c.is_alone for c in children):
            fire("child_with_group", "child_with_group",
                 "A child is part of a group.")

        rule = self._settings.rules.get("group_detected")
        min_size = rule.min_size if rule else 2
        group_sizes: dict[int, int] = {}
        for p in people.persons:
            if p.group_id is not None:
                group_sizes[p.group_id] = group_sizes.get(p.group_id, 0) + 1
        big = [g for g, sz in group_sizes.items() if sz >= min_size]
        if big:
            fire("group_detected", "group_detected",
                 f"{len(big)} group(s) of {min_size}+ people detected.",
                 {"groups": len(big)})

        for door in doors:
            if door.is_open:
                fire("door_open", f"door_open:{door.name}",
                     f"SECURITY: '{door.name}' is OPEN.",
                     {"door": door.name, "score": round(door.score, 2)})

        return out


class AlertBroker:
    """Thread-safe pub/sub bridging the (sync) pipeline thread to (async) SSE.

    The pipeline calls :meth:`publish` from its worker thread; events are pushed
    onto each subscriber's asyncio queue via the captured event loop.
    """

    def __init__(self, settings: AlertSettings, history: int = 100):
        self._settings = settings
        self._subscribers: set[asyncio.Queue] = set()
        self._history: deque[dict] = deque(maxlen=history)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def recent_alerts(self) -> list[dict]:
        return [e["alert"] for e in self._history]

    def publish_alert(self, alert: Alert) -> None:
        log.warning("ALERT [%s] %s", alert.severity.upper(), alert.message)
        event = {"type": "alert", "alert": alert.to_dict()}
        self._history.append(event)
        self._fanout(event)
        self._maybe_webhook(alert)

    def publish_stats(self, stats: dict) -> None:
        self._fanout({"type": "stats", "stats": stats})

    # -- internals -------------------------------------------------------
    def _fanout(self, event: dict) -> None:
        if self._loop is None:
            return
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            self._loop.call_soon_threadsafe(self._safe_put, q, event)

    @staticmethod
    def _safe_put(q: asyncio.Queue, event: dict) -> None:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass  # slow client: drop rather than block the pipeline

    def _maybe_webhook(self, alert: Alert) -> None:
        url = self._settings.webhook_url
        if not url:
            return

        def _post() -> None:
            try:
                import httpx

                httpx.post(url, json=alert.to_dict(), timeout=5.0)
            except Exception as exc:  # noqa: BLE001 - webhook is best-effort
                log.error("webhook post failed: %s", exc)

        threading.Thread(target=_post, daemon=True).start()
