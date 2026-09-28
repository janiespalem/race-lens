"""Shared utilities for racelens adapters."""
from __future__ import annotations

from racelens.race_control import (
    STATUS_TABLE, classify_race_control, initial_race_control,
    legacy_session_status, reduce_race_control,
)

__all__ = ("STATUS_TABLE", "message_to_status", "fastf1_lap1_start")

def message_to_status(
    text: str,
    *,
    previous_status: str | None = None,
    flag: str = "",
    scope: str = "",
) -> str | None:
    """Map actual race-control evidence; ending notices remain feed-only.

    TRACK CLEAR only ends SC/VSC. A track-wide green flag can also confirm a
    restart; a pit-exit light or sector flag cannot change session status.
    """
    action = classify_race_control({"message": text, "flag": flag, "scope": scope})
    if action.kind in {"unknown", "noop", "sector", "clear_sector"}:
        return None
    previous = initial_race_control()
    if previous_status in {"red_flag", "safety_car", "vsc"}:
        previous["control_mode"] = previous_status
    elif previous_status == "formation":
        previous["session_phase"] = "formation"
    elif previous_status == "started":
        previous["session_phase"] = "running"
    elif previous_status == "finished":
        previous["session_phase"] = "finished"
    result = reduce_race_control(previous, action, 0)
    status = legacy_session_status(result)
    return status if status != "unknown" and (
        status != (previous_status or "unknown") or action.kind in {"control", "finish"}
    ) else None


def fastf1_lap1_start(lap1):
    """Return FastF1's earliest explicit lap-1 start, with legacy fallback."""
    if "LapStartTime" in lap1:
        explicit = lap1["LapStartTime"].dropna()
        if len(explicit):
            return explicit.min()
    derived = (lap1["Time"] - lap1["LapTime"]).dropna()
    if len(derived):
        return derived.min()
    raise ValueError("FastF1 lap 1 has no usable start time; refusing unrebased archive")
