"""Event envelope and event types — the contract everything else builds on.

Every piece of raw motorsport data (FastF1, OpenF1, fixtures) is normalized
into Event before it touches the replay engine. See PLAN.md §10.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# Raw events — produced by adapters
EVENT_TYPES = {
    "SessionStarted",
    "SessionStatusChanged",
    "LapCompleted",
    "SectorTimeUpdated",
    "PositionChanged",
    "GapUpdated",
    "IntervalUpdated",
    "PitIn",
    "PitOut",
    "TyreStintUpdated",
    "RaceControlMessage",
    "RetirementDetected",
    "DriverStoppedChanged",
    "DriverTroubleDetected",
    "WeatherUpdated",
}

WEATHER_BOUNDS = {
    "air_temp_c": (-50.0, 70.0),
    "track_temp_c": (-50.0, 100.0),
    "humidity_percent": (0.0, 100.0),
    "pressure_mbar": (700.0, 1100.0),
    "wind_direction_deg": (0.0, 360.0),
    "wind_speed_mps": (0.0, 100.0),
}


class Event(BaseModel):
    """Normalized event envelope. Stable ID, ordered by session_time_ms."""

    event_id: str
    session_id: str
    type: str
    session_time_ms: int = Field(ge=0)
    lap: Optional[int] = None
    driver_id: Optional[str] = None
    source: str = "fixture"
    confidence: str = "high"
    # Monotonic arrival order, assigned at ingestion. Replay uses session_time_ms
    # (event time); near-live watermarks use ingest_seq (processing time), so
    # late-arriving events can revise state without breaking determinism.
    ingest_seq: Optional[int] = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def validate_type(cls, value: str) -> str:
        if value not in EVENT_TYPES:
            raise ValueError(f"unknown event type: {value}")
        return value

    @model_validator(mode="after")
    def validate_sector_time(self):
        if self.type == "SectorTimeUpdated":
            sector = self.payload.get("sector")
            duration = self.payload.get("time_ms")
            if not self.driver_id or type(sector) is not int or not 1 <= sector <= 3:
                raise ValueError("sector time requires a driver and sector 1..3")
            if "time_ms" not in self.payload or (
                duration is not None and (type(duration) is not int or duration <= 0)
            ):
                raise ValueError("sector time must be positive milliseconds or explicit null")
            if self.lap is not None and self.lap < 1:
                raise ValueError("sector lap must be positive or unknown")
        return self


def make_event_id(
    session_id: str,
    type_: str,
    session_time_ms: int,
    driver_id: Optional[str],
    payload: dict[str, Any],
) -> str:
    """Deterministic event ID: same input data → same ID, across runs and sources.

    This is what makes dedupe and replay determinism possible.
    """
    raw = json.dumps(
        [session_id, type_, session_time_ms, driver_id, payload],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def event(
    session_id: str,
    type_: str,
    session_time_ms: int,
    driver_id: Optional[str] = None,
    lap: Optional[int] = None,
    source: str = "fixture",
    **payload: Any,
) -> Event:
    """Convenience constructor with auto-generated deterministic ID."""
    return Event(
        event_id=make_event_id(session_id, type_, session_time_ms, driver_id, payload),
        session_id=session_id,
        type=type_,
        session_time_ms=session_time_ms,
        driver_id=driver_id,
        lap=lap,
        source=source,
        payload=payload,
    )


def dump_jsonl(events: list[Event]) -> str:
    return "\n".join(e.model_dump_json(exclude_none=True) for e in events) + "\n"


def load_jsonl(text: str) -> list[Event]:
    return [Event.model_validate_json(line) for line in text.splitlines() if line.strip()]
