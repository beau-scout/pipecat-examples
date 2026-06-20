"""Threaded frame capture from RTSP / video file / webcam.

A background thread continuously reads the source and keeps only the most recent
frame, so consumers (the inference loop and the MJPEG stream) never block on or
queue up behind a slow reader. RTSP drops are handled by reconnecting.
"""

from __future__ import annotations

import os
import threading
import time

import cv2
import numpy as np

from .config import CameraSettings


def _resolve_source(source: str) -> int | str:
    """Webcam indexes are given as strings like "0"; everything else is a URL/path."""
    return int(source) if source.isdigit() else source


class FrameGrabber:
    def __init__(self, settings: CameraSettings):
        self._settings = settings
        self._source = _resolve_source(settings.source)
        self._is_rtsp = isinstance(self._source, str) and self._source.lower().startswith("rtsp")

        # "synthetic"/"test"/"demo" generates frames in-process so the app can
        # run with no camera at all (great with --mock).
        self._synthetic = isinstance(self._source, str) and self._source.lower() in (
            "synthetic", "test", "demo",
        )

        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._frame_ts: float = 0.0
        self._connected = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        if self._is_rtsp:
            # Force a reliable transport for Axis cameras over LAN. Must be set
            # before VideoCapture opens the FFmpeg backend.
            os.environ.setdefault(
                "OPENCV_FFMPEG_CAPTURE_OPTIONS",
                f"rtsp_transport;{settings.rtsp_transport}",
            )

    # -- lifecycle -------------------------------------------------------
    def start(self) -> "FrameGrabber":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="frame-grabber", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    # -- accessors -------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._connected

    def latest(self) -> tuple[np.ndarray | None, float]:
        """Return a copy of the most recent frame and its capture timestamp."""
        with self._lock:
            if self._frame is None:
                return None, 0.0
            return self._frame.copy(), self._frame_ts

    # -- internals -------------------------------------------------------
    def _open(self) -> cv2.VideoCapture:
        backend = cv2.CAP_FFMPEG if isinstance(self._source, str) else cv2.CAP_ANY
        cap = cv2.VideoCapture(self._source, backend)
        # Keep latency low: don't let frames pile up in the driver buffer.
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _run_synthetic(self) -> None:
        import time as _t

        w, h, i = 960, 540, 0
        while not self._stop.is_set():
            i += 1
            frame = np.full((h, w, 3), 30, dtype=np.uint8)
            frame[:, :, 0] = (np.linspace(20, 90, w).astype(np.uint8))[None, :]
            cx = int((0.5 + 0.4 * np.sin(i / 30.0)) * w)
            cv2.rectangle(frame, (cx - 40, h - 240), (cx + 40, h - 20), (60, 160, 60), -1)
            cv2.putText(frame, "SYNTHETIC SOURCE", (20, h - 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (220, 220, 220), 2, cv2.LINE_AA)
            self._connected = True
            with self._lock:
                self._frame, self._frame_ts = frame, _t.time()
            self._stop.wait(1.0 / 15.0)

    def _run(self) -> None:
        if self._synthetic:
            self._run_synthetic()
            return
        cap = self._open()
        while not self._stop.is_set():
            if not cap.isOpened():
                self._connected = False
                time.sleep(self._settings.reconnect_delay_s)
                cap.release()
                cap = self._open()
                continue

            ok, frame = cap.read()
            if not ok or frame is None:
                # End of file -> loop it (handy for testing with a clip).
                if not self._is_rtsp and isinstance(self._source, str):
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok, frame = cap.read()
                if not ok or frame is None:
                    self._connected = False
                    time.sleep(self._settings.reconnect_delay_s)
                    cap.release()
                    cap = self._open()
                    continue

            self._connected = True
            with self._lock:
                self._frame = frame
                self._frame_ts = time.time()

        cap.release()
