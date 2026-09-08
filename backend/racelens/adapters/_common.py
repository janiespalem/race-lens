"""Shared utilities for racelens adapters."""
from __future__ import annotations

# Flag-message → session-status mapping.
# More-specific (longer) substrings MUST come before shorter ones that are
# substrings of them.  In particular "CHEQUERED FLAG" must precede "RED FLAG"
# because "CHEQUERED FLAG" contains the substring "RED FLAG"
# (chequeRED FLAG).
STATUS_TABLE: tuple[tuple[str, str], ...] = (
    ("CHEQUERED FLAG", "finished"),
    ("VIRTUAL SAFETY CAR DEPLOYED", "vsc"),
    ("VSC DEPLOYED", "vsc"),
    ("SAFETY CAR DEPLOYED", "safety_car"),
    ("RED FLAG", "red_flag"),
)


def message_to_status(
    text: str,
    table: tuple[tuple[str, str], ...] = STATUS_TABLE,
    *,
    previous_status: str | None = None,
    flag: str = "",
    scope: str = "",
) -> str | None:
    """Map actual race-control evidence; ending notices remain feed-only.

    TRACK CLEAR only ends SC/VSC. A track-wide green flag can also confirm a
    restart; a pit-exit light or sector flag cannot change session status.
    """
    upper = text.upper()
    if any(notice in upper for notice in (
        "SAFETY CAR IN THIS LAP", "SC IN THIS LAP", "VIRTUAL SAFETY CAR ENDING", "VSC ENDING",
    )):
        return None
    for needle, status in table:
        if needle in upper:
            return status
    if "TRACK CLEAR" in upper:
        return "started" if previous_status in {"safety_car", "vsc"} else None
    if flag.upper() == "GREEN" or upper.strip() == "GREEN FLAG":
        if "PIT EXIT" in upper or scope.upper() not in {"", "TRACK"}:
            return None
        return "started"
    return {
        "RED": "red_flag",
        "SAFETY CAR": "safety_car",
        "VIRTUAL SAFETY CAR": "vsc",
        "CHEQUERED": "finished",
    }.get(flag.upper())


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
