"""Single entry point: all insight detectors over one state."""
from typing import Any

from racelens.insights.clean_air import detect_clean_air_pace
from racelens.insights.degradation import detect_degradation
from racelens.insights.drs_train import detect_drs_train
from racelens.insights.pit_window import detect_pit_window
from racelens.insights.sc_pit import detect_sc_pit
from racelens.insights.traffic import detect_traffic_risk
from racelens.insights.undercut import detect_undercut_risk

DETECTORS = (
    detect_traffic_risk,
    detect_drs_train,
    detect_pit_window,
    detect_sc_pit,
    detect_undercut_risk,
    detect_degradation,
    detect_clean_air_pace,
)


def detect_all(state: dict[str, Any]) -> list[dict[str, Any]]:
    if state.get("session_status") in {"red_flag", "safety_car", "vsc"}:
        return detect_sc_pit(state)
    if state.get("session_status") != "started":
        return []
    out = []
    for d in DETECTORS:
        out.extend(d(state))
    return out
