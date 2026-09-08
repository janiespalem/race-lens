"""FastF1 → normalized events.

Converts a loaded historical session into the Event timeline the replay
engine consumes. Telemetry is intentionally skipped in MVP — laps, positions,
stints and pits are enough for replay + strategy insights (PLAN.md §7.1).

Requires the `fastf1` extra:  pip install -e ".[fastf1]"
"""
from __future__ import annotations

import os
from pathlib import Path

from racelens.adapters._common import fastf1_lap1_start, message_to_status
from racelens.events.models import Event, event, make_event_id


def _ms(td) -> int | None:
    """pandas Timedelta → whole milliseconds, None for NaT."""
    import pandas as pd

    if td is None or pd.isna(td):
        return None
    return int(td.total_seconds() * 1000)


def _timestamp_to_session_ms(ts, session_zero) -> int | None:
    """Absolute timestamp → session-relative milliseconds."""
    import pandas as pd

    if ts is None or pd.isna(ts):
        return None
    return _ms(pd.Timestamp(ts) - session_zero)


def session_id_for(year: int, gp: str, session: str) -> str:
    return f"{year}_{gp.lower().replace(' ', '_')}_{session.lower()}"


def best_lap_position_events(
    sid: str,
    laps: list[tuple[int, str, int | None]],
    src: str = "fastf1",
) -> list[Event]:
    """Build practice/qualifying order when FastF1 has no race position column."""
    best: dict[str, int] = {}
    positions: dict[str, int] = {}
    out: list[Event] = []
    for at_ms, driver, lap_time_ms in sorted(laps):
        if lap_time_ms is None or lap_time_ms >= best.get(driver, float("inf")):
            continue
        best[driver] = lap_time_ms
        order = sorted(best, key=lambda item: (best[item], item))
        for position, item in enumerate(order, 1):
            if positions.get(item) == position:
                continue
            positions[item] = position
            out.append(
                event(sid, "PositionChanged", at_ms, item, source=src, position=position)
            )
    return out


def ingest_session(year: int, gp: str, session: str = "R") -> list[Event]:
    """Load a historical session via FastF1 and normalize it to events.

    First call downloads data into the FastF1 cache (slow, ~tens of MB);
    subsequent calls are local.
    """
    import fastf1

    cache_dir = Path(os.environ.get("FASTF1_CACHE", "fastf1_cache"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    fastf1.Cache.enable_cache(str(cache_dir))
    ses = fastf1.get_session(year, gp, session)
    ses.load(telemetry=False, weather=False, messages=True)
    return session_to_events(ses, session_id_for(year, gp, session))


def session_to_events(ses, sid: str, src: str = "fastf1") -> list[Event]:
    """Normalize a loaded FastF1 Session into the Event timeline."""
    import pandas as pd
    from fastf1.exceptions import DataNotLoadedError

    events: list[Event] = []

    # fastf1 3.8: total_laps/laps are RAISING properties (DataNotLoadedError)
    # until the (live) feed actually contains laps — normal early in a session.
    # Emit what we can; the next live poll re-parses a fuller recording.
    try:
        total_laps = ses.total_laps
    except DataNotLoadedError:
        total_laps = None
    events.append(
        event(sid, "SessionStarted", 0, source=src,
              total_laps=int(total_laps) if total_laps else None)
    )

    try:
        laps = ses.laps
    except DataNotLoadedError:
        return events  # no laps yet (pre-start / formation) — status-only frame

    by_lap: dict[int, list[tuple[int, int, str]]] = {}  # lap → [(position, t_end, driver)]
    completed: list[tuple[int, str, int | None]] = []
    for _, lap in laps.iterlaps():
        drv = str(lap["Driver"])
        lap_no = int(lap["LapNumber"])
        t_end = _ms(lap["Time"])  # session time when the lap was completed
        if t_end is None:
            continue

        lap_time_ms = _ms(lap["LapTime"])
        completed.append((t_end, drv, lap_time_ms))
        events.append(
            event(sid, "LapCompleted", t_end, drv, lap=lap_no, source=src,
                  lap_time_ms=lap_time_ms)
        )
        if not pd.isna(lap["Position"]):
            pos = int(lap["Position"])
            events.append(
                event(sid, "PositionChanged", t_end, drv, lap=lap_no, source=src,
                      position=pos)
            )
            by_lap.setdefault(lap_no, []).append((pos, t_end, drv))

        t_pit_in = _ms(lap["PitInTime"])
        if t_pit_in is not None:
            events.append(event(sid, "PitIn", t_pit_in, drv, lap=lap_no, source=src))
        t_pit_out = _ms(lap["PitOutTime"])
        if t_pit_out is not None:
            events.append(event(sid, "PitOut", t_pit_out, drv, lap=lap_no, source=src))
            if not pd.isna(lap["Compound"]):
                events.append(
                    event(sid, "TyreStintUpdated", t_pit_out, drv, lap=lap_no, source=src,
                          compound=str(lap["Compound"]),
                          age_laps=int(lap["TyreLife"]) if not pd.isna(lap["TyreLife"]) else 0)
                )

    if not by_lap:
        events.extend(best_lap_position_events(sid, completed, src))

    # Timing-screen gaps/intervals, derived from line-crossing times on the
    # same lap number. Approximation: exact for cars on the lead lap, coarse
    # for lapped cars — good enough for MVP strategy insights.
    for lap_no, rows in by_lap.items():
        rows.sort()
        leader_t = rows[0][1]
        prev_t = leader_t
        for pos, t, drv in rows:
            if pos > 1:
                events.append(event(sid, "GapUpdated", t, drv, lap=lap_no, source=src,
                                    gap_s=round((t - leader_t) / 1000, 3)))
                events.append(event(sid, "IntervalUpdated", t, drv, lap=lap_no, source=src,
                                    interval_s=round((t - prev_t) / 1000, 3)))
            prev_t = t

    # Starting tyres: first stint per driver has no preceding PitOut
    for drv in ses.laps["Driver"].unique():
        first = ses.laps.pick_drivers(drv).iloc[0]
        if not pd.isna(first["Compound"]):
            events.append(
                event(sid, "TyreStintUpdated", 0, str(drv), source=src,
                      compound=str(first["Compound"]),
                      age_laps=int(first["TyreLife"]) - 1 if not pd.isna(first["TyreLife"]) else 0)
            )

    # Starting grid at t=0, so the timing table is populated before lap 1
    if ses.results is not None:
        for _, row in ses.results.iterrows():
            if not pd.isna(row.get("GridPosition")) and row["GridPosition"] > 0:
                events.append(
                    event(sid, "PositionChanged", 0, str(row["Abbreviation"]), source=src,
                          position=int(row["GridPosition"]))
                )

    # Race control messages ride along; flag messages also become session
    # status changes so the UI can show RED FLAG / SC / VSC instead of silence
    if ses.race_control_messages is not None:
        session_zero = pd.Timestamp(ses.date) - pd.Timedelta(ses.session_start_time)
        events.extend(_race_control_to_events(ses.race_control_messages, sid, session_zero, src))

    # Rebase to race start: FastF1 session time begins with the data feed,
    # ~1.5h before lights out. LapTime can be missing on lap 1, while
    # LapStartTime remains the authoritative start anchor.
    lap1 = ses.laps[ses.laps["LapNumber"] == 1]
    t0_ms = _ms(fastf1_lap1_start(lap1)) or 0
    if t0_ms:
        for e in events:
            e.session_time_ms = max(e.session_time_ms - t0_ms, 0)
            e.event_id = make_event_id(
                e.session_id, e.type, e.session_time_ms, e.driver_id, e.payload
            )

    events.sort(key=lambda e: (e.session_time_ms, e.event_id))
    return events


def _race_control_to_events(messages, sid: str, session_zero, src: str) -> list[Event]:
    """Normalize messages in source-time order so TRACK CLEAR has prior status."""
    events: list[Event] = []
    last_status: str | None = None
    timed_messages = []
    for _, msg in messages.iterrows():
        t = _timestamp_to_session_ms(msg.get("Time"), session_zero)
        if t is not None and t >= 0:
            timed_messages.append((t, msg))
    for t, msg in sorted(timed_messages, key=lambda item: item[0]):
        text = str(msg.get("Message", ""))
        events.append(
            event(sid, "RaceControlMessage", t, source=src,
                  category=str(msg.get("Category", "")), message=text)
        )
        status = message_to_status(
            text, previous_status=last_status,
            flag=str(msg.get("Flag", "")), scope=str(msg.get("Scope", "")),
        )
        if status is not None:
            events.append(event(sid, "SessionStatusChanged", t, source=src, status=status))
            last_status = status
    for sequence, item in enumerate(events):
        item.ingest_seq = sequence
    return events
