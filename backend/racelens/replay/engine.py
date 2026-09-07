"""Deterministic replay engine.

Core guarantee (PLAN.md §10.5):

    same events + same timestamp = same state

The engine never looks past `at_ms` — this is what makes spoiler-free mode
possible at the API layer: serve state_at(t) and nothing else.
"""
from __future__ import annotations

import bisect
import copy
import hashlib
import json
from typing import Any, Iterable

from racelens.events.models import Event

# Number of recent laps tracked per driver — used by short-horizon insight rules.
RECENT_LAPS_WINDOW = 3

_EVENT_PRIORITY = {
    "SessionStarted": 0,
    "SessionStatusChanged": 10,
    "PitIn": 20,
    "PitOut": 21,
    "LapCompleted": 30,
    "TyreStintUpdated": 40,
    "WeatherUpdated": 45,
    "PositionChanged": 50,
    "GapUpdated": 60,
    "IntervalUpdated": 61,
    "RetirementDetected": 70,
    "DriverStoppedChanged": 71,
}


def _event_sort_key(value: Event) -> tuple[int, int, int, str]:
    return (
        value.session_time_ms,
        _EVENT_PRIORITY.get(value.type, 50),
        value.ingest_seq if value.ingest_seq is not None else -1,
        value.event_id,
    )


def _new_driver() -> dict[str, Any]:
    return {
        "position": None,
        "rank": None,         # 1-based ordering truth (= classification index), set per frame
        "grid_position": None,  # baseline = first-known position (grid, or mid-join lap for late starts)
        "laps_completed": 0,
        "last_lap_ms": None,
        "best_lap_ms": None,
        "gap_s": None,        # to leader
        "interval_s": None,   # to car ahead
        "tyre_compound": None,
        "tyre_age_laps": None,
        "pit_count": 0,
        "in_pit": False,
        "recent_laps_ms": [],
        "retired": False,
        "retirement_inferred": False,
        "stopped": False,
    }


class ReplayEngine:
    """Holds a session's normalized events; answers `state_at(t)` queries.

    Events are deduped by event_id and sorted by event time, semantic priority,
    arrival order (when available), then event_id for deterministic fallback.
    """

    def __init__(self, events: Iterable[Event], snapshot_interval: int = 200):
        seen: set[str] = set()
        unique: list[Event] = []
        duplicates = 0
        for e in events:
            if e.event_id in seen:
                duplicates += 1
                continue
            seen.add(e.event_id)
            unique.append(e)
        self.events = sorted(unique, key=_event_sort_key)
        self.duplicates_dropped = duplicates
        self.session_id = self.events[0].session_id if self.events else None
        self._times = [e.session_time_ms for e in self.events]

        # Snapshots every N applied events make state_at ~O(N) instead of
        # O(total events) — replay determinism is unaffected, the snapshot is
        # just a memoized prefix.
        self._snap_keys: list[int] = [0]
        self._snapshots: list[dict[str, Any]] = [self._initial_state()]
        if snapshot_interval > 0:
            state = copy.deepcopy(self._snapshots[0])
            for i, e in enumerate(self.events, start=1):
                self._apply(state, e)
                if i % snapshot_interval == 0:
                    self._snap_keys.append(i)
                    self._snapshots.append(copy.deepcopy(state))

    def _initial_state(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "at_ms": None,
            "lap": 0,
            "session_status": "unknown",
            "session_name": None,  # e.g. "SILVERSTONE · RACE" (live only — see SessionStarted)
            "status_since_ms": 0,
            "restart_at_ms": None,
            "total_laps": None,
            "weather": None,
            "classification": [],
            "drivers": {},
            "data_quality": {
                "status": "unknown",
                "last_event_ms": None,
                "events_applied": 0,
                "duplicates_dropped": self.duplicates_dropped,
            },
        }

    # ── State construction ────────────────────────────────────────────────

    def state_at(self, at_ms: int) -> dict[str, Any]:
        idx = bisect.bisect_right(self._times, at_ms)  # events to apply
        snap_pos = bisect.bisect_right(self._snap_keys, idx) - 1
        start = self._snap_keys[snap_pos]
        state = copy.deepcopy(self._snapshots[snap_pos])

        for e in self.events[start:idx]:
            self._apply(state, e)

        state["at_ms"] = at_ms
        last_ms = self._times[idx - 1] if idx else None
        dq = state["data_quality"]
        dq["events_applied"] = idx
        dq["last_event_ms"] = last_ms
        if last_ms is None:
            dq["status"] = "unknown"
        elif at_ms - last_ms > 120_000:
            dq["status"] = "stale"
        else:
            dq["status"] = "good"

        # Historical sources do not expose a reliable retirement transition.
        # Keep the existing conservative fallback for display classification;
        # forecast functions independently reject drivers without usable pace.
        leader_laps = max(
            (s["laps_completed"] for s in state["drivers"].values()),
            default=0,
        )
        for s in state["drivers"].values():
            if (
                not s["retired"]
                and leader_laps >= 5
                and s["laps_completed"] <= leader_laps - 5
            ):
                s["retired"] = True
                s["retirement_inferred"] = True

        active = sorted(
            (d for d, s in state["drivers"].items() if s["position"] is not None and not s["retired"]),
            key=lambda d: state["drivers"][d]["position"],
        )
        retired = sorted(
            (d for d, s in state["drivers"].items() if s["position"] is not None and s["retired"]),
            key=lambda d: state["drivers"][d]["laps_completed"],
            reverse=True,
        )
        state["classification"] = active + retired
        # rank = the single ordering truth for both map and tower. Official,
        # event-derived, 1-based. The frontend renders this; it never re-sorts.
        for i, drv in enumerate(state["classification"], start=1):
            state["drivers"][drv]["rank"] = i
        if active:
            leader = state["drivers"][active[0]]
            leader["gap_s"] = 0.0
            leader["interval_s"] = None
        return state

    def state_hash(self, at_ms: int) -> str:
        """Canonical hash of the state — used by determinism tests."""
        blob = json.dumps(self.state_at(at_ms), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # ── Event application ─────────────────────────────────────────────────

    def _driver(self, state: dict, driver_id: str) -> dict[str, Any]:
        return state["drivers"].setdefault(driver_id, _new_driver())

    def _apply(self, state: dict, e: Event) -> None:
        p = e.payload

        if e.type == "SessionStarted":
            state["session_status"] = "formation" if p.get("formation") else "started"
            state["status_since_ms"] = e.session_time_ms
            state["total_laps"] = p.get("total_laps")
            if "session_name" in p:
                state["session_name"] = p["session_name"]

        elif e.type == "SessionStatusChanged":
            new_status = p.get("status", state["session_status"])
            state["session_status"] = new_status
            state["status_since_ms"] = e.session_time_ms
            state["restart_at_ms"] = None
            if new_status in {"red_flag", "safety_car", "vsc"}:
                for drv in state["drivers"].values():
                    drv["recent_laps_ms"] = []

        elif e.type == "LapCompleted":
            if (
                state["session_status"] in {"red_flag", "formation"}
                and (e.lap or 0) > state["lap"]
                and e.session_time_ms - state["status_since_ms"] >= 60_000
            ):
                # Some canonical feeds omit the explicit red-flag restart.
                # A new global lap after a sustained stop is source-backed
                # proof that racing resumed; short pit-entry crossings are not.
                state["session_status"] = "started"
                state["status_since_ms"] = e.session_time_ms
                state["restart_at_ms"] = None
            d = self._driver(state, e.driver_id)
            d["laps_completed"] = max(d["laps_completed"], e.lap or 0)
            lap_ms = p.get("lap_time_ms")
            if lap_ms is not None:
                d["last_lap_ms"] = lap_ms
                if d["best_lap_ms"] is None or lap_ms < d["best_lap_ms"]:
                    d["best_lap_ms"] = lap_ms
                d["recent_laps_ms"] = (d["recent_laps_ms"] + [lap_ms])[-RECENT_LAPS_WINDOW:]
            if d["tyre_age_laps"] is not None:
                d["tyre_age_laps"] += 1
            state["lap"] = max(state["lap"], e.lap or 0)

        elif e.type == "PositionChanged":
            d = self._driver(state, e.driver_id)
            d["position"] = p.get("position")
            if d["position"] == 1:
                d["gap_s"] = 0.0
                d["interval_s"] = None
            if d["grid_position"] is None:
                # First known position = baseline. For a mid-join recording that
                # starts a few laps in, this is intentionally the position at
                # join time, not the true grid slot — best available baseline.
                d["grid_position"] = p.get("position")

        elif e.type == "GapUpdated":
            self._driver(state, e.driver_id)["gap_s"] = p.get("gap_s")

        elif e.type == "IntervalUpdated":
            self._driver(state, e.driver_id)["interval_s"] = p.get("interval_s")

        elif e.type == "RetirementDetected":
            driver = self._driver(state, e.driver_id)
            driver["retired"] = True
            driver["retirement_inferred"] = False

        elif e.type == "DriverStoppedChanged":
            self._driver(state, e.driver_id)["stopped"] = bool(p.get("stopped"))

        elif e.type == "PitIn":
            d = self._driver(state, e.driver_id)
            d["in_pit"] = True
            d["pit_count"] += 1

        elif e.type == "PitOut":
            d = self._driver(state, e.driver_id)
            d["in_pit"] = False
            d["recent_laps_ms"] = []

        elif e.type == "TyreStintUpdated":
            d = self._driver(state, e.driver_id)
            compound = p.get("compound")
            age = p.get("age_laps", 0)
            if d["tyre_compound"] is not None and (
                compound != d["tyre_compound"]
                or (d["tyre_age_laps"] is not None and age < d["tyre_age_laps"])
            ):
                d["recent_laps_ms"] = []
            d["tyre_compound"] = compound
            d["tyre_age_laps"] = age

        elif e.type == "WeatherUpdated":
            state["weather"] = {**(state["weather"] or {}), **p}

        elif e.type == "RaceControlMessage":
            restart_at_ms = p.get("restart_at_ms")
            if isinstance(restart_at_ms, int) and restart_at_ms >= 0:
                state["restart_at_ms"] = restart_at_ms
