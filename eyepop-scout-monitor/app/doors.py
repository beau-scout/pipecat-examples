"""Open-door detection.

Two interchangeable strategies (chosen via ``doors.mode`` in config):

* ``roi_threshold`` — no ML model. You mark door regions (ROIs) and capture a
  reference frame with every door CLOSED. Each cycle we measure how much each
  ROI differs from the reference; a large, sustained difference => the door is
  open. Self-contained and works on any camera.

* ``eyepop`` — run a dedicated EyePop Pop that detects doors and classifies
  open/closed, reading the result back per door.

NOTE: this mirrors the intent of EyePop's private "Door Thresholding" reference,
reconstructed here because that repo isn't accessible from this environment. The
exact model/ability and thresholds should be confirmed against that source.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

from .config import Settings
from .eyepop_client import EyePopInferer

log = logging.getLogger("scout.doors")


@dataclass
class Door:
    name: str
    rect: list[float]            # [x, y, w, h] in source-frame pixels
    state: str = "unknown"       # "open" | "closed" | "unknown"
    score: float = 0.0           # openness metric (roi) or confidence (eyepop)

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    def to_dict(self) -> dict:
        return {"name": self.name, "rect": self.rect,
                "state": self.state, "score": round(self.score, 2)}


class DoorDetector(Protocol):
    def detect(self, frame_bgr: np.ndarray) -> list[Door]: ...
    def close(self) -> None: ...


def _crop(frame: np.ndarray, rect: list[int]) -> np.ndarray:
    x, y, w, h = rect
    H, W = frame.shape[:2]
    x0, y0 = max(0, int(x)), max(0, int(y))
    x1, y1 = min(W, int(x + w)), min(H, int(y + h))
    return frame[y0:y1, x0:x1]


class ROIThresholdDoorDetector:
    def __init__(self, settings: Settings):
        cfg = settings.doors
        self._rois = cfg.rois
        self._threshold = cfg.open_threshold
        self._needed = max(1, cfg.consecutive_frames)
        self._open_streak: dict[str, int] = {r.name: 0 for r in cfg.rois}
        self._ref_gray: np.ndarray | None = None
        if cfg.reference_image and Path(cfg.reference_image).exists():
            ref = cv2.imread(cfg.reference_image)
            if ref is not None:
                self._ref_gray = cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY)
        if self._ref_gray is None:
            log.warning(
                "Door ROI mode enabled but no reference image loaded; doors will "
                "report 'unknown'. Capture one with all doors closed (see README)."
            )

    def detect(self, frame_bgr: np.ndarray) -> list[Door]:
        if not self._rois:
            return []
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        ref = self._ref_gray
        if ref is not None and ref.shape != gray.shape:
            ref = cv2.resize(ref, (gray.shape[1], gray.shape[0]))

        doors: list[Door] = []
        for roi in self._rois:
            if ref is None:
                doors.append(Door(name=roi.name, rect=list(roi.rect), state="unknown"))
                continue
            cur_c = _crop(gray, roi.rect)
            ref_c = _crop(ref, roi.rect)
            if cur_c.size == 0 or ref_c.size == 0:
                doors.append(Door(name=roi.name, rect=list(roi.rect), state="unknown"))
                continue
            score = float(np.mean(cv2.absdiff(cur_c, ref_c)))
            if score > self._threshold:
                self._open_streak[roi.name] = self._open_streak.get(roi.name, 0) + 1
            else:
                self._open_streak[roi.name] = 0
            state = "open" if self._open_streak[roi.name] >= self._needed else "closed"
            doors.append(Door(name=roi.name, rect=list(roi.rect), state=state, score=score))
        return doors

    def close(self) -> None:
        pass


class EyePopDoorDetector:
    def __init__(self, settings: Settings):
        cfg = settings.doors
        if not settings.eyepop.secret_key:
            raise RuntimeError("doors.mode 'eyepop' requires EYEPOP_SECRET_KEY.")
        self._open_class = cfg.open_class.lower()
        self._open_category = cfg.open_category.lower()
        self._door_label = cfg.door_label.lower()
        self._inferer = EyePopInferer(
            secret_key=settings.eyepop.secret_key,
            pop_id=settings.eyepop.door_pop_id or settings.eyepop.pop_id,
            url=settings.eyepop.url,
            pop_json=cfg.door_pop_json,
            name="door",
        )

    def _state_of(self, obj: dict) -> tuple[str, float]:
        # Prefer an explicit open/closed classification on the door object.
        for cls in obj.get("classes", []) or []:
            if str(cls.get("category", "")).lower() == self._open_category:
                is_open = str(cls.get("classLabel", "")).lower() == self._open_class
                return ("open" if is_open else "closed"), float(cls.get("confidence", 0.0))
        # Fall back to the object label itself encoding the state.
        label = str(obj.get("classLabel", "")).lower()
        if self._open_class in label:
            return "open", float(obj.get("confidence", 0.0))
        return "closed", float(obj.get("confidence", 0.0))

    def detect(self, frame_bgr: np.ndarray) -> list[Door]:
        result = self._inferer.infer(frame_bgr)
        doors: list[Door] = []
        idx = 0
        for obj in result.get("objects", []) or []:
            if self._door_label not in str(obj.get("classLabel", "")).lower():
                continue
            idx += 1
            state, score = self._state_of(obj)
            doors.append(
                Door(
                    name=f"Door {idx}",
                    rect=[float(obj.get("x", 0)), float(obj.get("y", 0)),
                          float(obj.get("width", 0)), float(obj.get("height", 0))],
                    state=state,
                    score=score,
                )
            )
        return doors

    def close(self) -> None:
        self._inferer.close()


def build_door_detector(settings: Settings) -> DoorDetector | None:
    if not settings.doors.enabled:
        return None
    if settings.doors.mode == "eyepop":
        return EyePopDoorDetector(settings)
    return ROIThresholdDoorDetector(settings)


def capture_reference(frame_bgr: np.ndarray, path: str) -> None:
    """Save the current frame as the 'all doors closed' reference image."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(path, frame_bgr)
