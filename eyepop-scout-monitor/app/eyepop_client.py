"""EyePop.ai inference wrappers.

``EyePopInferer`` wraps a connected synchronous Worker endpoint and turns a BGR
frame into EyePop's standard prediction dict. ``MockInferer`` fabricates the same
shape so the dashboard and pipeline can run with no camera, no API key, and no
spend — useful for development and for trying the UI.
"""

from __future__ import annotations

import io
import json
import logging
import random
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

from eyepop import EyePopSdk
from eyepop.worker.worker_types import Pop

from .config import Settings

log = logging.getLogger("scout.eyepop")


class Inferer(Protocol):
    def infer(self, frame_bgr: np.ndarray) -> dict: ...
    def close(self) -> None: ...


def _encode_jpeg(frame_bgr: np.ndarray, quality: int = 85) -> bytes:
    ok, buf = cv2.imencode(".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return buf.tobytes()


class EyePopInferer:
    """Synchronous EyePop worker endpoint, kept connected for the process life.

    Not thread-safe: call ``infer`` from a single worker thread.
    """

    def __init__(
        self,
        secret_key: str,
        pop_id: str | None,
        url: str | None = None,
        pop_json: str | None = None,
        name: str = "person",
    ):
        self._name = name
        self._endpoint = EyePopSdk.workerEndpoint(
            pop_id=pop_id,
            secret_key=secret_key,
            eyepop_url=url,
            is_async=False,
        )
        self._endpoint.connect()
        log.info("[%s] connected to EyePop (pop_id=%s)", name, pop_id)

        if pop_json:
            pop = Pop(**json.loads(Path(pop_json).read_text(encoding="utf-8")))
            self._endpoint.set_pop(pop)
            log.info("[%s] applied Pop from %s", name, pop_json)

    def infer(self, frame_bgr: np.ndarray) -> dict:
        data = _encode_jpeg(frame_bgr)
        job = self._endpoint.upload_stream(io.BytesIO(data), "image/jpeg")
        result = job.predict() or {}
        # Guarantee source dimensions so downstream geometry never divides by zero.
        h, w = frame_bgr.shape[:2]
        result.setdefault("source_width", w)
        result.setdefault("source_height", h)
        return result

    def close(self) -> None:
        try:
            self._endpoint.disconnect()
        except Exception:  # noqa: BLE001 - best-effort shutdown
            pass


class MockInferer:
    """Synthetic person detections for offline development / UI demos."""

    _AGE_LABELS = ["3-9", "10-17", "18-24", "25-34", "35-49", "50-64"]

    def __init__(self, seed: int | None = None):
        self._rng = random.Random(seed)
        self._t = 0.0

    def infer(self, frame_bgr: np.ndarray) -> dict:
        h, w = frame_bgr.shape[:2]
        self._t += 1
        n = self._rng.choice([0, 1, 1, 2, 2, 3])
        objects = []
        for _ in range(n):
            bw = self._rng.uniform(0.08, 0.18) * w
            bh = bw * self._rng.uniform(2.0, 2.8)
            x = self._rng.uniform(0, max(1.0, w - bw))
            y = self._rng.uniform(0, max(1.0, h - bh))
            age = self._rng.choice(self._AGE_LABELS)
            objects.append(
                {
                    "classLabel": "person",
                    "confidence": round(self._rng.uniform(0.7, 0.98), 3),
                    "x": x,
                    "y": y,
                    "width": bw,
                    "height": bh,
                    "classes": [
                        {"category": "age-range", "classLabel": age,
                         "confidence": round(self._rng.uniform(0.6, 0.95), 3)},
                    ],
                }
            )
        return {"source_width": w, "source_height": h, "objects": objects}

    def close(self) -> None:  # noqa: D401 - nothing to release
        pass


def build_person_inferer(settings: Settings) -> Inferer:
    if settings.inference.mock:
        log.warning("Inference running in MOCK mode (no EyePop calls).")
        return MockInferer()
    if not settings.eyepop.secret_key:
        raise RuntimeError(
            "EYEPOP_SECRET_KEY is not set. Set it in .env, or enable "
            "inference.mock: true in config.yaml to try the app without an account."
        )
    return EyePopInferer(
        secret_key=settings.eyepop.secret_key,
        pop_id=settings.eyepop.pop_id,
        url=settings.eyepop.url,
        pop_json=settings.inference.person_pop_json,
        name="person",
    )
