"""Configuration models and loader.

Tunables come from ``config.yaml`` (see ``config.example.yaml``); secrets come
from environment variables / ``.env`` so they never live in the config file.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class CameraSettings(BaseModel):
    source: str = "0"
    rtsp_transport: str = "tcp"
    reconnect_delay_s: float = 3.0
    stream_fps: float = 15.0


class InferenceSettings(BaseModel):
    interval_s: float = 1.0
    confidence_threshold: float = 0.5
    mock: bool = False
    person_pop_json: str | None = None


class AgeSettings(BaseModel):
    category: str = "age-range"
    adult_min_age: int = 18
    child_labels: list[str] = Field(
        default_factory=lambda: ["child", "minor", "kid", "baby", "infant", "toddler"]
    )


class GroupingSettings(BaseModel):
    proximity_factor: float = 1.6
    min_group_size: int = 2


class DoorROI(BaseModel):
    name: str
    rect: list[int]  # [x, y, w, h] in source-frame pixels


class DoorSettings(BaseModel):
    enabled: bool = False
    mode: str = "roi_threshold"  # "roi_threshold" | "eyepop"

    # roi_threshold mode
    reference_image: str | None = None
    open_threshold: float = 18.0
    consecutive_frames: int = 3
    rois: list[DoorROI] = Field(default_factory=list)

    # eyepop mode
    door_pop_id: str | None = None
    door_pop_json: str | None = None
    door_label: str = "door"
    open_class: str = "open"
    open_category: str = "state"


class RuleSettings(BaseModel):
    enabled: bool = False
    min_size: int = 2
    cooldown_s: float | None = None


def _default_rules() -> dict[str, RuleSettings]:
    # Sensible out-of-the-box behavior so alerts work without a config.yaml.
    return {
        "child_detected": RuleSettings(enabled=True),
        "child_alone": RuleSettings(enabled=True),
        "child_with_group": RuleSettings(enabled=False),
        "group_detected": RuleSettings(enabled=False, min_size=3),
        "door_open": RuleSettings(enabled=True),
    }


class AlertSettings(BaseModel):
    default_cooldown_s: float = 20.0
    webhook_url: str | None = None
    rules: dict[str, RuleSettings] = Field(default_factory=_default_rules)


class ServerSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000


class EyePopCredentials(BaseModel):
    secret_key: str | None = None
    pop_id: str | None = None
    url: str | None = None
    door_pop_id: str | None = None


class Settings(BaseModel):
    camera: CameraSettings = CameraSettings()
    inference: InferenceSettings = InferenceSettings()
    age: AgeSettings = AgeSettings()
    grouping: GroupingSettings = GroupingSettings()
    doors: DoorSettings = DoorSettings()
    alerts: AlertSettings = AlertSettings()
    server: ServerSettings = ServerSettings()
    eyepop: EyePopCredentials = EyePopCredentials()


def load_settings(config_path: str | os.PathLike | None = None) -> Settings:
    """Load settings from YAML + environment.

    Resolution order: ``config.yaml`` (if present) for tunables, then ``.env`` /
    process environment for the EyePop secrets.
    """
    load_dotenv()  # populate os.environ from a .env file if present

    data: dict = {}
    if config_path is None:
        default = Path("config.yaml")
        config_path = default if default.exists() else None
    if config_path is not None:
        with open(config_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}

    settings = Settings(**data)

    # Overlay secrets from the environment (never read from yaml).
    settings.eyepop.secret_key = os.getenv("EYEPOP_SECRET_KEY") or settings.eyepop.secret_key
    settings.eyepop.pop_id = os.getenv("EYEPOP_POP_ID") or settings.eyepop.pop_id
    settings.eyepop.url = os.getenv("EYEPOP_URL") or settings.eyepop.url
    settings.eyepop.door_pop_id = (
        settings.doors.door_pop_id
        or os.getenv("EYEPOP_DOOR_POP_ID")
        or settings.eyepop.door_pop_id
    )
    return settings
