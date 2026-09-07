"""Undercut risk: the car behind can pit first and jump the car ahead (PLAN.md §12.3).

MVP rule: rivals are close (interval within striking range), both on old-ish
tyres so a stop is plausible, and the attacker is not slower. Fresh-tyre
outlap gain at most tracks is ~1.5-2.5s, so the closer the interval, the
higher the risk for the defender.
"""
from __future__ import annotations

import itertools
from typing import Any

from racelens.insights._base import mk_insight
from racelens.replay.engine import RECENT_LAPS_WINDOW

STRIKE_RANGE_S = 3.5      # within this, a good outlap usually closes the gap
HIGH_RISK_RANGE_S = 1.0
MIN_TYRE_AGE_LAPS = 10    # nobody undercuts on fresh tyres
MAX_PACE_DEFICIT_MS = 300 # attacker must not be clearly slower


def detect_undercut_risk(state: dict[str, Any]) -> list[dict[str, Any]]:
    if state["lap"] < 3:
        return []

    drivers = state["drivers"]
    order = state["classification"]
    insights = []

    for ahead_id, behind_id in itertools.pairwise(order):
        ahead, behind = drivers[ahead_id], drivers[behind_id]
        if ahead.get("retired") or behind.get("retired"):
            continue
        if any(
            len(driver.get("recent_laps_ms", [])) < RECENT_LAPS_WINDOW
            for driver in (ahead, behind)
        ):
            continue
        interval = behind["interval_s"]
        if interval is None or interval > STRIKE_RANGE_S:
            continue
        if behind["in_pit"] or ahead["in_pit"]:
            continue
        if (behind["tyre_age_laps"] or 0) < MIN_TYRE_AGE_LAPS:
            continue
        ahead_lap_ms = ahead["recent_laps_ms"][-1]
        behind_lap_ms = behind["recent_laps_ms"][-1]
        if behind_lap_ms - ahead_lap_ms > MAX_PACE_DEFICIT_MS:
            continue

        severity = "high" if interval <= HIGH_RISK_RANGE_S else "medium"
        insights.append(mk_insight(
            insight_id=f"undercut:{behind_id}:{state['at_ms']}",
            type_=f"UNDERCUT_RISK_{severity.upper()}",
            driver_ids=[behind_id, ahead_id],  # attacker, defender
            severity=severity,
            confidence="medium",  # static outlap-gain model
            evidence={
                "interval_s": interval,
                "attacker_tyre_age_laps": behind["tyre_age_laps"],
                "defender_tyre_age_laps": ahead["tyre_age_laps"],
                "pace_delta_ms": ahead_lap_ms - behind_lap_ms,
            },
            state=state,
        ))
    return insights
