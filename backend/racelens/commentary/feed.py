"""Event feed for the frontend: spoiler-free, newest-first, human-readable.

render_feed() converts raw normalized events into a compact list suitable for
a live-ticker UI. Only meaningful event types are included; noise (position
changes, gaps, intervals) is suppressed.
"""
from __future__ import annotations

import bisect
from typing import Any

from racelens.events.models import Event
from racelens.insights.passes import KIND_UNDERCUT, detect_passes

# Compound display names — handle both full names and abbreviations
_COMPOUND_EN = {
    "SOFT": "softs", "S": "softs",
    "MEDIUM": "mediums", "M": "mediums",
    "HARD": "hards", "H": "hards",
    "INTERMEDIATE": "intermediates", "I": "intermediates",
    "WET": "wets", "W": "wets",
}
_COMPOUND_RU = {
    "SOFT": "софте", "S": "софте",
    "MEDIUM": "медиуме", "M": "медиуме",
    "HARD": "харде", "H": "харде",
    "INTERMEDIATE": "интермедиате", "I": "интермедиате",
    "WET": "дожде", "W": "дожде",
}

# Session status → display text
_STATUS_EN: dict[str, str] = {
    "red_flag": "Red flag — session stopped",
    "safety_car": "Safety car",
    "vsc": "VSC",
    "finished": "Chequered flag — race complete",
}
_STATUS_RU: dict[str, str] = {
    "red_flag": "Красный флаг — гонка остановлена",
    "safety_car": "Safety car",
    "vsc": "VSC",
    "finished": "Клетчатый флаг — финиш",
}


def _fmt_ms(ms: int) -> str:
    """Format milliseconds as m:ss.mmm."""
    total_s, ms_part = divmod(ms, 1000)
    minutes, secs = divmod(total_s, 60)
    return f"{minutes}:{secs:02d}.{ms_part:03d}"


def render_feed(
    events: list[Event],
    until_ms: int,
    lang: str = "en",
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Return feed items from events up to until_ms, newest first."""

    visible = [e for e in events if e.session_time_ms <= until_ms]
    visible_sorted = sorted(visible, key=lambda e: (e.session_time_ms, e.event_id))

    # Pre-build index: driver_id -> sorted list of (session_time_ms, compound)
    # for TyreStintUpdated events, used for O(log n) PitOut↔tyre pairing.
    _stint_index: dict[str, list[tuple[int, str]]] = {}
    for _e in visible_sorted:
        if _e.type == "TyreStintUpdated" and _e.driver_id:
            compound_val = _e.payload.get("compound")
            if compound_val is not None:
                _stint_index.setdefault(_e.driver_id, []).append(
                    (_e.session_time_ms, compound_val)
                )
    # Lists are already in ascending order (visible_sorted is sorted)

    items: list[dict[str, Any]] = []

    # Track absolute fastest lap for fastest-lap feed items
    best_ms: int | None = None
    # Race distance (from SessionStarted) so we can flag finish-line crossings on
    # the final lap regardless of until_ms; and count finishers for placings.
    race_total_laps: int | None = next(
        (e.payload.get("total_laps") for e in visible_sorted if e.type == "SessionStarted"),
        None,
    )
    finish_count = 0
    # Track previous session status to detect "Race resumed"
    prev_status: str | None = None
    # Track which status transitions already produced a feed item (avoid duplicates with RCM)
    emitted_status: set[str] = set()

    for e in visible_sorted:
        text: str | None = None
        driver_id: str | None = e.driver_id
        audio_url: str | None = None
        transcript: str | None = None

        if e.type == "SessionStarted":
            if e.payload.get("formation"):
                continue
            text = "Свет погас — старт!" if lang == "ru" else "Lights out — race start!"
            driver_id = None

        elif e.type == "LapCompleted":
            lap_ms = e.payload.get("lap_time_ms")
            # Finish: crossing the line on the final lap. Winner first, then placings.
            if race_total_laps and e.lap == race_total_laps:
                finish_count += 1
                drv = e.driver_id or "?"
                if finish_count == 1:
                    text = f"{drv} забирает победу!" if lang == "ru" else f"{drv} takes the win!"
                else:
                    text = (
                        f"{drv} финиширует P{finish_count}"
                        if lang == "ru"
                        else f"{drv} finishes P{finish_count}"
                    )
                driver_id = e.driver_id
            elif lap_ms is not None and (best_ms is None or lap_ms < best_ms):
                best_ms = lap_ms
                drv = e.driver_id or "?"
                text = f"Fastest lap: {drv} {_fmt_ms(lap_ms)}"
                driver_id = e.driver_id

        elif e.type == "PitIn":
            if prev_status == "red_flag":
                continue
            drv = e.driver_id or "?"
            text = f"{drv} заезжает в боксы" if lang == "ru" else f"{drv} pits"
            driver_id = e.driver_id

        elif e.type == "PitOut":
            if prev_status == "red_flag":
                continue
            drv = e.driver_id or "?"
            # Look for TyreStintUpdated from the same driver within 5 s after PitOut
            # using pre-built index + bisect for O(log n) lookup.
            compound: str | None = None
            stints = _stint_index.get(e.driver_id or "", [])
            if stints:
                t0 = e.session_time_ms
                t1 = t0 + 5_000
                lo = bisect.bisect_left(stints, (t0,))
                if lo < len(stints) and stints[lo][0] <= t1:
                    compound = stints[lo][1]
            if compound and compound.upper() == "UNKNOWN":
                compound = None  # feed sometimes ships UNKNOWN — plain "rejoins" reads better
            if compound:
                cname_en = _COMPOUND_EN.get(compound.upper(), compound.lower() + "s")
                cname_ru = _COMPOUND_RU.get(compound.upper(), compound.lower())
                if lang == "ru":
                    text = f"{drv} выезжает на {cname_ru}"
                else:
                    text = f"{drv} rejoins on {cname_en}"
            else:
                text = f"{drv} выезжает из боксов" if lang == "ru" else f"{drv} rejoins"
            driver_id = e.driver_id

        elif e.type == "SessionStatusChanged":
            status = e.payload.get("status", "")
            if status == "started" and prev_status is not None and prev_status != "started":
                text = "Гонка возобновлена" if lang == "ru" else "Race resumed"
                emitted_status.add(status)
            elif status in _STATUS_EN:
                text = _STATUS_RU.get(status, status) if lang == "ru" else _STATUS_EN[status]
                emitted_status.add(status)
            prev_status = status
            driver_id = None

        elif e.type == "RetirementDetected":
            drv = e.driver_id or "?"
            text = f"{drv} сходит с дистанции" if lang == "ru" else f"{drv} retires"
            driver_id = e.driver_id

        elif e.type == "DriverStoppedChanged" and e.payload.get("stopped"):
            drv = e.driver_id or "?"
            text = f"{drv} остановился на трассе" if lang == "ru" else f"{drv} stopped on track"
            driver_id = e.driver_id

        elif e.type == "DriverTroubleDetected":
            drv = e.driver_id or "?"
            start = e.payload.get("from_position")
            end = e.payload.get("to_position")
            text = (
                f"{drv} резко откатывается: P{start} → P{end} под жёлтым флагом"
                if lang == "ru"
                else f"{drv} drops sharply: P{start} → P{end} under yellow"
            )
            driver_id = e.driver_id

        elif e.type == "RaceControlMessage" and e.payload.get("category") == "Radio":
            # Team-radio capture — message is "RADIO: XXX", audio_path in payload.
            text = e.payload.get("message", "")
            driver_id = e.driver_id
            audio_url = e.payload.get("audio_url")
            transcript = e.payload.get("transcript")

        elif e.type == "RaceControlMessage":
            msg = e.payload.get("message", "")
            category = e.payload.get("category", "")
            msg_up = msg.upper()
            has_incident = "INCIDENT" in msg_up or "INVESTIGATION" in msg_up
            has_restart = "RACE WILL RESUME AT" in msg_up
            has_driver_flag = "Flag" in category and any(
                marker in msg_up
                for marker in (
                    "BLACK AND WHITE FLAG", "BLACK FLAG", "PENALTY",
                    "DISQUALIFIED", "DRIVING ERRATICALLY",
                )
            )
            # For flag category, only include race-level flags (red flag, SC, VSC) —
            # exclude per-sector yellow/clear/green messages which are noise
            has_flag = (
                "Flag" in category
                and (
                    "RED FLAG" in msg_up
                    or "SAFETY CAR" in msg_up
                    or "VIRTUAL SAFETY CAR" in msg_up
                )
            )
            if not (has_flag or has_incident or has_restart or has_driver_flag):
                continue
            # Avoid duplicating SessionStatusChanged items that already cover SC/VSC/red flag
            dup = False
            if ("RED" in msg_up and "red_flag" in emitted_status) or ("SAFETY CAR" in msg_up and "VIRTUAL" not in msg_up and "safety_car" in emitted_status) or ("VIRTUAL" in msg_up and "vsc" in emitted_status):
                dup = True
            if dup:
                continue
            text = msg
            driver_id = None

        if text is None:
            continue

        # Determine tag for frontend chip
        if e.type in ("PitIn", "PitOut"):
            tag = "PIT"
        elif e.type in ("SessionStarted", "SessionStatusChanged", "RaceControlMessage"):
            tag = "FLAG"
        elif e.type == "LapCompleted":
            tag = "FINISH" if (race_total_laps and e.lap == race_total_laps) else "FASTEST"
        else:
            tag = "INFO"

        # Lights-out opens lap 1; SessionStarted carries no lap of its own.
        item_lap = 1 if e.type == "SessionStarted" else e.lap
        item: dict[str, Any] = {
            "id": e.event_id,
            "at_ms": e.session_time_ms,
            "lap": item_lap,
            "kind": e.type,
            "tag": tag,
            "text": text,
            "driver_id": driver_id,
        }
        if audio_url:
            item["audio_url"] = audio_url
        if transcript:
            item["transcript"] = transcript
        items.append(item)

    # Overtakes / undercuts — detected from raw PositionChanged, not events
    # the loop above renders.
    for p in detect_passes(visible):
        if p.kind == KIND_UNDERCUT:
            text = (
                f"Андеркат: {p.ahead} перепрыгивает {p.behind}"
                if lang == "ru"
                else f"Undercut: {p.ahead} jumps {p.behind}"
            )
        else:
            text = (
                f"{p.ahead} обгоняет {p.behind} — P{p.position}"
                if lang == "ru"
                else f"{p.ahead} passes {p.behind} for P{p.position}"
            )
        items.append({
            "id": (
                f"pass:{p.kind}:{p.at_ms}:{p.ahead}:{p.behind}:{p.position}"
            ),
            "at_ms": p.at_ms,
            "lap": p.lap,
            "kind": p.kind,
            "tag": "PASS",
            "text": text,
            "driver_id": p.ahead,
        })

    # Newest first, then apply limit
    items.sort(key=lambda x: x["at_ms"], reverse=True)
    return items[:limit]
