"""Draw people / group / door overlays onto frames for the dashboard preview."""

from __future__ import annotations

import cv2
import numpy as np

from .analysis import PeopleAnalysis
from .doors import Door

# BGR colors
_GREEN = (80, 200, 80)
_ORANGE = (40, 140, 240)
_RED = (60, 60, 230)
_GRAY = (170, 170, 170)
_YELLOW = (40, 210, 230)
_WHITE = (255, 255, 255)
_BLACK = (0, 0, 0)


def _label(img: np.ndarray, text: str, x: int, y: int, color, scale=0.5) -> None:
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    y_top = max(0, y - th - 6)
    cv2.rectangle(img, (x, y_top), (x + tw + 6, y_top + th + 6), color, -1)
    cv2.putText(img, text, (x + 3, y_top + th + 1),
                cv2.FONT_HERSHEY_SIMPLEX, scale, _BLACK, 1, cv2.LINE_AA)


def draw_overlay(
    frame_bgr: np.ndarray,
    people: PeopleAnalysis,
    doors: list[Door],
    fps: float | None = None,
) -> np.ndarray:
    img = frame_bgr.copy()

    for p in people.persons:
        x, y, w, h = int(p.x), int(p.y), int(p.width), int(p.height)
        color = {"adult": _GREEN, "child": _RED, "unknown": _GRAY}[p.category]
        cv2.rectangle(img, (x, y), (x + w, y + h), color, 2)
        bits = [p.category]
        if p.age_label:
            bits.append(p.age_label)
        bits.append("alone" if p.is_alone else f"group {p.group_id}")
        _label(img, " | ".join(bits), x, y, color)

    for d in doors:
        x, y, w, h = (int(v) for v in d.rect)
        color = {"open": _RED, "closed": _GREEN, "unknown": _YELLOW}[d.state]
        cv2.rectangle(img, (x, y), (x + w, y + h), color, 2)
        _label(img, f"{d.name}: {d.state.upper()}", x, max(y, 18), color)

    # Header banner with live counts.
    banner = (
        f"People {people.total}  |  Adults {people.adults}  |  "
        f"Children {people.children}  |  Groups {people.groups}  |  Alone {people.alone}"
    )
    open_doors = sum(1 for d in doors if d.is_open)
    if doors:
        banner += f"  |  Doors open {open_doors}"
    if fps is not None:
        banner += f"  |  {fps:.1f} fps"
    cv2.rectangle(img, (0, 0), (img.shape[1], 26), _BLACK, -1)
    cv2.putText(img, banner, (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, _WHITE, 1, cv2.LINE_AA)
    return img
