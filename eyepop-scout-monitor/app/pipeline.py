"""Orchestration: capture -> EyePop inference -> analysis -> alerts -> overlay.

The inference loop runs on its own thread at ``inference.interval_s`` so EyePop
round-trips never stall the video. The MJPEG stream annotates the freshest raw
frame with the most recent detections, so the preview stays smooth even though
detections update about once a second.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

from . import analysis as ana
from .alerts import AlertBroker, AlertEngine
from .annotate import draw_overlay
from .capture import FrameGrabber
from .config import Settings
from .doors import Door, build_door_detector, capture_reference
from .eyepop_client import build_person_inferer

log = logging.getLogger("scout.pipeline")


class Pipeline:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.grabber = FrameGrabber(settings.camera)
        self.broker = AlertBroker(settings.alerts)
        self._engine = AlertEngine(settings.alerts)

        self._person_inferer = None
        self._door_detector = None
        self.inference_error: str | None = None
        self.door_error: str | None = None

        self._lock = threading.Lock()
        self._people = ana.PeopleAnalysis()
        self._doors: list[Door] = []
        self._infer_fps = 0.0

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        self.grabber.start()
        try:
            self._person_inferer = build_person_inferer(self.settings)
        except Exception as exc:  # noqa: BLE001 - surface in UI, keep streaming
            self.inference_error = str(exc)
            log.error("person inferer disabled: %s", exc)
        try:
            self._door_detector = build_door_detector(self.settings)
        except Exception as exc:  # noqa: BLE001
            self.door_error = str(exc)
            log.error("door detector disabled: %s", exc)

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="inference", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        self.grabber.stop()
        for obj in (self._person_inferer, self._door_detector):
            if obj is not None:
                try:
                    obj.close()
                except Exception:  # noqa: BLE001
                    pass

    # -- inference loop --------------------------------------------------
    def _run(self) -> None:
        interval = self.settings.inference.interval_s
        prev_start = time.time()
        while not self._stop.is_set():
            t0 = time.time()
            frame, _ = self.grabber.latest()
            if frame is None:
                time.sleep(0.1)
                continue

            people = self._people
            if self._person_inferer is not None:
                try:
                    result = self._person_inferer.infer(frame)
                    people = ana.parse_people(
                        result, self.settings.age,
                        self.settings.inference.confidence_threshold,
                    )
                    ana.assign_groups(people, self.settings.grouping)
                except Exception as exc:  # noqa: BLE001 - keep loop alive
                    self.inference_error = str(exc)
                    log.exception("inference failed")

            doors = self._doors
            if self._door_detector is not None:
                try:
                    doors = self._door_detector.detect(frame)
                except Exception as exc:  # noqa: BLE001
                    self.door_error = str(exc)
                    log.exception("door detection failed")

            for alert in self._engine.evaluate(people, doors):
                self.broker.publish_alert(alert)

            # True update cadence (includes the inter-cycle sleep), so the UI
            # shows how often detections actually refresh (~1/interval_s).
            cadence = max(t0 - prev_start, 1e-6)
            prev_start = t0
            with self._lock:
                self._people = people
                self._doors = doors
                self._infer_fps = 1.0 / cadence
            self.broker.publish_stats(self.stats())

            time.sleep(max(0.0, interval - (time.time() - t0)))

    def recalibrate_doors(self, path: str) -> bool:
        """Save the current frame as the 'all closed' reference and reload the
        ROI door detector. Returns False if no frame is available yet."""
        frame, _ = self.grabber.latest()
        if frame is None:
            return False
        capture_reference(frame, path)
        self.settings.doors.reference_image = path
        self.settings.doors.enabled = True
        self.settings.doors.mode = "roi_threshold"
        try:
            self._door_detector = build_door_detector(self.settings)
            self.door_error = None
        except Exception as exc:  # noqa: BLE001
            self.door_error = str(exc)
            return False
        return True

    # -- accessors -------------------------------------------------------
    def snapshot(self) -> tuple[ana.PeopleAnalysis, list[Door], float]:
        with self._lock:
            return self._people, list(self._doors), self._infer_fps

    def annotate_latest(self) -> np.ndarray | None:
        frame, _ = self.grabber.latest()
        if frame is None:
            return None
        people, doors, fps = self.snapshot()
        return draw_overlay(frame, people, doors, fps=fps)

    def stats(self) -> dict:
        people, doors, fps = self.snapshot()
        return {
            "connected": self.grabber.connected,
            "infer_fps": round(fps, 2),
            "people": people.total,
            "adults": people.adults,
            "children": people.children,
            "groups": people.groups,
            "alone": people.alone,
            "doors_open": sum(1 for d in doors if d.is_open),
        }

    def state(self) -> dict:
        people, doors, fps = self.snapshot()
        return {
            "stats": self.stats(),
            "persons": [p.to_dict() for p in people.persons],
            "doors": [d.to_dict() for d in doors],
            "alerts": self.broker.recent_alerts(),
            "errors": {"inference": self.inference_error, "doors": self.door_error},
            "mock": self.settings.inference.mock,
        }
