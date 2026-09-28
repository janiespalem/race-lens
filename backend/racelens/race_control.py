"""Interpret FIA race-control evidence as independent phase, control and local flags."""
from __future__ import annotations

import re
import math
from numbers import Integral, Real
from dataclasses import dataclass
from typing import Literal, Mapping, TypedDict


class FinishCondition(TypedDict):
    at_ms: int
    control_mode: str
    sector_flags: dict[str, str]


class RaceControlProjection(TypedDict):
    session_phase: str
    control_mode: str
    control_since_ms: int
    sector_flags: dict[str, str]
    finish_condition: FinishCondition | None


@dataclass(frozen=True, slots=True)
class RaceControlAction:
    kind: Literal["phase", "finish", "control", "sector", "clear_sector", "track_clear", "restart", "noop", "unknown"]
    value: str = ""
    sector: int | None = None


_SECTOR = re.compile(r"^(DOUBLE YELLOW|YELLOW|CLEAR|GREEN(?: FLAG)?) IN TRACK SECTOR (\d+)$")
_SECTOR_GREEN = re.compile(r"^GREEN FLAG (?:IN|FOR) (?:TRACK )?SECTOR (\d+)$")
_DRIVER_FLAG = re.compile(r"^(?:WAVED BLUE FLAG|BLACK AND WHITE FLAG) FOR CAR \d+\b")
STATUS_TABLE: tuple[tuple[str, str], ...] = (
    ("CHEQUERED FLAG", "finished"),
    ("VIRTUAL SAFETY CAR DEPLOYED", "vsc"),
    ("VSC DEPLOYED", "vsc"),
    ("SAFETY CAR DEPLOYED", "safety_car"),
    ("RED FLAG", "red_flag"),
)
_CONTROL_MESSAGE_MODES = dict(STATUS_TABLE)


def normalized_source_fields(source: Mapping[str, object]) -> dict[str, object]:
    """Keep valid structured FIA fields without changing the original message."""
    fields: dict[str, object] = {}
    for target in ("flag", "scope"):
        value = source.get(target, source.get(target.title()))
        if isinstance(value, str) and value:
            fields[target] = value
    sector = source.get("sector", source.get("Sector"))
    if isinstance(sector, str) and re.fullmatch(r"[1-9][0-9]*", sector):
        fields["sector"] = int(sector)
    elif isinstance(sector, Integral) and not isinstance(sector, bool) and sector > 0:
        fields["sector"] = int(sector)
    elif isinstance(sector, Real) and math.isfinite(sector) and sector > 0 and sector == int(sector):
        fields["sector"] = int(sector)
    return fields


def initial_race_control() -> RaceControlProjection:
    return {"session_phase": "unknown", "control_mode": "green", "control_since_ms": 0,
            "sector_flags": {}, "finish_condition": None}


def classify_race_control(payload: Mapping[str, object]) -> RaceControlAction:
    text = payload.get("message", "")
    if not isinstance(text, str):
        return RaceControlAction("unknown")
    upper = " ".join(text.upper().split())
    flag = str(payload.get("flag") or "").upper()
    scope = str(payload.get("scope") or "").upper()
    raw_sector = payload.get("sector")
    if raw_sector is not None and (type(raw_sector) is not int or raw_sector <= 0):
        return RaceControlAction("unknown")
    if upper == "CHEQUERED FLAG" or flag == "CHEQUERED":
        return RaceControlAction("finish")
    sector_match = _SECTOR.fullmatch(upper)
    green_match = _SECTOR_GREEN.fullmatch(upper)
    if sector_match or green_match:
        sector = int((sector_match or green_match).group(2 if sector_match else 1))
        if raw_sector is not None and raw_sector != sector:
            return RaceControlAction("unknown")
        color = sector_match.group(1) if sector_match else "GREEN"
        return RaceControlAction("clear_sector", sector=sector) if color in {"CLEAR", "GREEN", "GREEN FLAG"} else RaceControlAction("sector", "double_yellow" if color == "DOUBLE YELLOW" else "yellow", sector)
    if scope == "SECTOR" and type(raw_sector) is int:
        if flag in {"YELLOW", "DOUBLE YELLOW"}:
            return RaceControlAction("sector", "double_yellow" if flag == "DOUBLE YELLOW" else "yellow", raw_sector)
        if flag in {"CLEAR", "GREEN"}:
            return RaceControlAction("clear_sector", sector=raw_sector)
    if upper in _CONTROL_MESSAGE_MODES:
        return RaceControlAction("control", _CONTROL_MESSAGE_MODES[upper])
    if upper == "TRACK CLEAR":
        return RaceControlAction("track_clear")
    if upper in {"STANDING START", "ROLLING START", "GREEN FLAG"} and scope in {"", "TRACK"}:
        return RaceControlAction("restart")
    if upper == "EXTRA FORMATION LAP" or upper == "FORMATION LAP":
        return RaceControlAction("phase", "formation")
    if upper in {"SAFETY CAR IN THIS LAP", "SC IN THIS LAP", "VSC ENDING", "VIRTUAL SAFETY CAR ENDING", "GREEN LIGHT - PIT EXIT OPEN"} or _DRIVER_FLAG.match(upper):
        return RaceControlAction("noop")
    if upper == "GREEN FLAG" and scope not in {"", "TRACK"}:
        return RaceControlAction("noop")
    if flag in {"RED", "SAFETY CAR", "VIRTUAL SAFETY CAR", "VSC"} and scope in {"", "TRACK"}:
        return RaceControlAction("control", {"RED": "red_flag", "SAFETY CAR": "safety_car", "VIRTUAL SAFETY CAR": "vsc", "VSC": "vsc"}[flag])
    return RaceControlAction("unknown")


def reduce_race_control(previous: RaceControlProjection | None, action: RaceControlAction, at_ms: int) -> RaceControlProjection:
    old = previous or initial_race_control()
    state: RaceControlProjection = {**old, "sector_flags": dict(old["sector_flags"])}
    if action.kind == "finish":
        if state["session_phase"] != "finished":
            state["session_phase"] = "finished"
            state["finish_condition"] = {"at_ms": at_ms, "control_mode": state["control_mode"], "sector_flags": dict(state["sector_flags"])}
    elif action.kind == "phase" and state["session_phase"] != "finished":
        state["session_phase"] = action.value
    elif action.kind == "sector" and action.sector is not None:
        state["sector_flags"][str(action.sector)] = action.value
    elif action.kind == "clear_sector" and action.sector is not None:
        state["sector_flags"].pop(str(action.sector), None)
    elif action.kind == "track_clear":
        state["sector_flags"].clear()
        if state["control_mode"] in {"safety_car", "vsc"}:
            state["control_mode"] = "green"
            if state["session_phase"] == "unknown":
                state["session_phase"] = "running"
    elif action.kind == "control":
        state["control_mode"] = action.value
    elif action.kind == "restart":
        state["control_mode"] = "green"
        if state["session_phase"] != "finished":
            state["session_phase"] = "running"
    if state["control_mode"] != old["control_mode"]:
        state["control_since_ms"] = at_ms
    return state


def legacy_session_status(projection: RaceControlProjection) -> str:
    if projection["session_phase"] == "finished":
        return "finished"
    if projection["control_mode"] != "green":
        return projection["control_mode"]
    return {"running": "started", "formation": "formation"}.get(projection["session_phase"], "unknown")
