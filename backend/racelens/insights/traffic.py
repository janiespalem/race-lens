"""Traffic risk: a faster driver stuck behind a slower car (PLAN.md §12.3).

Pure function over race state — deterministic, no I/O, no AI. Insights are
structured data first; text rendering happens elsewhere.
"""
from __future__ import annotations

import itertools
from typing import Any

from racelens.insights._base import mk_insight
from racelens.replay.engine import RECENT_LAPS_WINDOW

INTERVAL_THRESHOLD_S = 1.0   # within striking distance
PACE_DELTA_MEDIUM_MS = 200   # behind car is at least this much faster per lap
PACE_DELTA_HIGH_MS = 700


def detect_traffic_risk(state: dict[str, Any]) -> list[dict[str, Any]]:
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
        if interval is None or interval > INTERVAL_THRESHOLD_S:
            continue
        if behind["in_pit"] or ahead["in_pit"]:
            continue
        ahead_lap_ms = ahead["recent_laps_ms"][-1]
        behind_lap_ms = behind["recent_laps_ms"][-1]
        pace_delta_ms = ahead_lap_ms - behind_lap_ms
        if pace_delta_ms < PACE_DELTA_MEDIUM_MS:
            continue

        severity = "high" if pace_delta_ms >= PACE_DELTA_HIGH_MS else "medium"
        insights.append(mk_insight(
            insight_id=f"traffic:{behind_id}:{state['at_ms']}",
            type_=f"TRAFFIC_RISK_{severity.upper()}",
            driver_ids=[behind_id, ahead_id],
            severity=severity,
            confidence="medium",
            evidence={
                "interval_s": interval,
                "pace_delta_ms": pace_delta_ms,
                "behind_last_lap_ms": behind_lap_ms,
                "ahead_last_lap_ms": ahead_lap_ms,
            },
            state=state,
        ))
    return insights
