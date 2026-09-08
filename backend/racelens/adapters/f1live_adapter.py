"""Direct F1 SignalR feed → Event adapter (no fastf1 session reconstruction).

fastf1's livedata path cannot build laps from a live/mid-join recording (it
drops keyframes and its lap builder needs archive-grade data), so for LIVE we
map the raw recorded feed lines to our Event timeline ourselves. The recording
is what `fastf1.livetiming.client.SignalRClient` writes: one python-ish list
per line — [category, payload, iso_timestamp] — where keyframes (subscribe
snapshots) carry payload as a JSON string and an EMPTY timestamp, while live
increments carry payload as a dict and a real timestamp.

Post-session replays keep using the fastf1 adapter (archives parse fine);
this adapter's job is the live tower: positions, gaps, laps, pits, status.
"""
from __future__ import annotations

import ast
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from racelens.adapters._common import message_to_status
from racelens.events.models import WEATHER_BOUNDS, Event, event

# SessionStatus.Status → our SessionStatusChanged payload status
_STATUS_MAP = {
    "Started": "started",
    "Finished": "finished",
    "Ends": "finished",
    "Finalised": "finished",
    "Aborted": "red_flag",
}

_LAPTIME_RE = re.compile(r"^(?:(\d+):)?(\d{1,2})\.(\d{3})$")
_GMT_OFFSET_RE = re.compile(r"^([+-]?)(\d{1,2}):(\d{2}):(\d{2})$")
_RESTART_RE = re.compile(r"\bRACE WILL RESUME AT\s+(\d{1,2}):(\d{2})\b", re.IGNORECASE)
_RETIRE_CONFIRM_MS = 5_000
_WEATHER_FIELDS = {
    "AirTemp": "air_temp_c",
    "TrackTemp": "track_temp_c",
    "Humidity": "humidity_percent",
    "Pressure": "pressure_mbar",
    "WindDirection": "wind_direction_deg",
    "WindSpeed": "wind_speed_mps",
}


def _parse_iso(ts: str) -> float | None:
    """ISO UTC timestamp → posix seconds. Feed uses varying precision."""
    ts = ts.strip()
    if not ts:
        return None
    ts = ts.removesuffix("Z")
    # normalize fractional part to 6 digits (feed emits 1..7)
    if "." in ts:
        head, frac = ts.split(".", 1)
        ts = f"{head}.{frac[:6].ljust(6, '0')}"
    try:
        return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _parse_laptime_ms(value: str | None) -> int | None:
    if not value:
        return None
    m = _LAPTIME_RE.match(value.strip())
    if not m:
        return None
    minutes = int(m.group(1) or 0)
    return (minutes * 60 + int(m.group(2))) * 1000 + int(m.group(3))


def _parse_gmt_offset(value: object) -> int | None:
    if not isinstance(value, str) or not (match := _GMT_OFFSET_RE.match(value.strip())):
        return None
    hours, minutes, seconds = map(int, match.groups()[1:])
    if hours > 23 or minutes > 59 or seconds > 59:
        return None
    sign = -1 if match.group(1) == "-" else 1
    return sign * (hours * 3600 + minutes * 60 + seconds)


def _restart_at_ms(
    message: str,
    message_posix: float | None,
    session_start_posix: float,
    gmt_offset_s: int | None,
) -> int | None:
    match = _RESTART_RE.search(message)
    if match is None or message_posix is None or gmt_offset_s is None:
        return None
    hour, minute = map(int, match.groups())
    if hour > 23 or minute > 59:
        return None
    local_message = datetime.fromtimestamp(message_posix, tz=timezone.utc) + timedelta(
        seconds=gmt_offset_s,
    )
    local_restart = local_message.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local_restart < local_message - timedelta(minutes=1):
        local_restart += timedelta(days=1)
    restart_posix = (local_restart - timedelta(seconds=gmt_offset_s)).timestamp()
    restart_ms = round((restart_posix - session_start_posix) * 1000)
    return restart_ms if restart_ms >= 0 else None


def _parse_gap_s(value: Any) -> float | None:
    """'+1.234' → 1.234; lapped ('1L') / markers ('LAP 12') → None."""
    if isinstance(value, dict):
        value = value.get("Value")
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip().lstrip("+")
    try:
        gap = float(v)
        return gap if math.isfinite(gap) else None
    except ValueError:
        return None


def _parse_weather(payload: dict[str, Any]) -> dict[str, float | bool]:
    weather: dict[str, float | bool] = {}
    for source, target in _WEATHER_FIELDS.items():
        value = payload.get(source)
        if isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        lower, upper = WEATHER_BOUNDS[target]
        if math.isfinite(number) and lower <= number <= upper:
            weather[target] = number
    rainfall = payload.get("Rainfall")
    if rainfall in (0, 1, "0", "1", False, True):
        weather["rainfall"] = str(rainfall).lower() in {"1", "true"}
    return weather


def _lines(feed_files: tuple[str, ...]) -> Iterator[tuple[str, Any, str]]:
    """Yield (category, payload-as-dict, ts) for every parseable line."""
    for path in feed_files:
        with open(path, encoding="utf-8-sig") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw.startswith("["):
                    continue
                try:
                    row = ast.literal_eval(raw)
                except (ValueError, SyntaxError):
                    continue
                if not isinstance(row, list) or len(row) < 3:
                    continue
                cat, payload, ts = row[0], row[1], row[2]
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                if isinstance(payload, dict):
                    yield cat, payload, ts


def _session_identity(payload: dict[str, Any]) -> tuple[object, ...] | None:
    meeting = payload.get("Meeting")
    if not isinstance(meeting, dict) or not payload.get("Name"):
        return None
    return (
        meeting.get("Key") or meeting.get("Number") or meeting.get("Name"),
        payload.get("Key") or payload.get("Path") or payload.get("StartDate"),
        payload.get("Name"),
    )


def current_f1live_session_info(*feed_files: str) -> dict[str, Any] | None:
    """Return the latest identified SessionInfo keyframe in a recording."""
    current = None
    for category, payload, _ in _lines(feed_files):
        if category == "SessionInfo" and _session_identity(payload) is not None:
            current = payload
    return current


def _current_session_rows(
    rows: list[tuple[str, Any, str]],
) -> list[tuple[str, Any, str]]:
    """Drop a stale session segment once the public feed switches identity."""
    identities = [
        (index, _session_identity(payload))
        for index, (category, payload, _) in enumerate(rows)
        if category == "SessionInfo" and _session_identity(payload) is not None
    ]
    distinct = {identity for _, identity in identities}
    if len(distinct) <= 1:
        return rows
    latest = identities[-1][1]
    start = next(index for index, identity in identities if identity == latest)
    driver_list = next(
        (
            rows[index]
            for index in range(start - 1, -1, -1)
            if rows[index][0] == "DriverList"
        ),
        None,
    )
    return ([driver_list] if driver_list else []) + rows[start:]


def _find_t0(rows: list[tuple[str, Any, str]]) -> tuple[float | None, bool]:
    """Session-start posix time: StatusSeries 'Started' (keyframe replays
    history, so this works mid-join), else first live SessionStatus Started,
    else first timestamped line (last resort)."""
    for cat, payload, _ in rows:
        if cat == "SessionData":
            series = payload.get("StatusSeries") or {}
            items = series if isinstance(series, list) else series.values()
            for item in items:
                if isinstance(item, dict) and item.get("SessionStatus") == "Started":
                    t = _parse_iso(item.get("Utc") or "")
                    if t is not None:
                        return t, True
    for cat, payload, ts in rows:
        if cat == "SessionStatus" and payload.get("Status") == "Started":
            t = _parse_iso(ts)
            if t is not None:
                return t, True
    for _, _, ts in rows:
        t = _parse_iso(ts)
        if t is not None:
            return t, False
    return None, False


def ingest_f1live(*feed_files: str, session_id: str = "f1live") -> list[Event]:
    """Map recorded SignalR feed file(s) to the Event timeline.

    Deterministic full-snapshot conversion (LiveRunner's fetch contract):
    same recording → same events; the runner dedupes by event_id across polls.
    """
    rows = _current_session_rows(list(_lines(feed_files)))
    t0, has_started = _find_t0(rows)
    if t0 is None:
        return []

    def to_ms(posix: float) -> int:
        return max(0, round((posix - t0) * 1000))

    row_times: list[float | None] = [None] * len(rows)
    next_time: float | None = None
    for index in range(len(rows) - 1, -1, -1):
        parsed = _parse_iso(rows[index][2])
        if parsed is not None:
            next_time = parsed
        row_times[index] = next_time

    # Session badge text ("SILVERSTONE · RACE"): SessionInfo is a keyframe
    # (full history resent on every re-parse), so the first occurrence in the
    # file is stable across polls once the feed has connected.
    session_name: str | None = None
    gmt_offset_s: int | None = None
    for cat, payload, _ in rows:
        if cat == "SessionInfo":
            location = (payload.get("Meeting") or {}).get("Location")
            name = payload.get("Name")
            if location and name:
                session_name = f"{location} · {name}".upper()
            gmt_offset_s = _parse_gmt_offset(payload.get("GmtOffset"))
            break

    sid = session_id
    initial_payload: dict[str, Any] = {}
    if session_name:
        initial_payload["session_name"] = session_name
    if not has_started:
        initial_payload["formation"] = True
    total_laps = next(
        (
            payload.get("TotalLaps")
            for category, payload, _ in reversed(rows)
            if category == "LapCount" and isinstance(payload.get("TotalLaps"), int)
        ),
        None,
    )
    if total_laps:
        initial_payload["total_laps"] = total_laps
    events: list[Event] = [event(sid, "SessionStarted", 0, source="f1live", **initial_payload)]

    num_to_abbr: dict[str, str] = {}
    # per-driver last seen values, to emit only real transitions
    pos: dict[str, int] = {}
    laps: dict[str, int] = {}
    in_pit: dict[str, bool] = {}
    last_lap_ms: dict[str, int | None] = {}
    retirement_candidates: dict[str, tuple[int, int]] = {}
    stopped: dict[str, bool] = {}
    yellow_times: list[int] = []
    last_status: str | None = None  # dedupe SessionStatus vs RCM-derived statuses
    session_path: str | None = None  # SessionInfo "Path", to build absolute radio audio_url

    def emit_status(status: str, t_ms: int) -> None:
        nonlocal last_status
        if status != last_status:
            last_status = status
            events.append(event(sid, "SessionStatusChanged", t_ms, status=status))

    def drv(num: str) -> str:
        return num_to_abbr.get(num, str(num))

    def apply_driver_list(payload: dict) -> None:
        for num, info in payload.items():
            if isinstance(info, dict) and info.get("Tla"):
                num_to_abbr[str(num)] = str(info["Tla"])

    def apply_timing(lines: dict, t_ms: int, *, emit_activity: bool, timestamped: bool) -> None:
        for num, patch in lines.items():
            if not isinstance(patch, dict):
                continue
            d = drv(str(num))

            p = patch.get("Position")
            if p is not None:
                try:
                    p_int = int(p)
                except (TypeError, ValueError):
                    p_int = None
                if p_int is not None and pos.get(d) != p_int:
                    pos[d] = p_int
                    events.append(event(sid, "PositionChanged", t_ms, d, position=p_int))

            if "GapToLeader" in patch:
                gap = _parse_gap_s(patch["GapToLeader"])
                events.append(event(sid, "GapUpdated", t_ms, d, gap_s=gap))

            if "IntervalToPositionAhead" in patch:
                value = patch["IntervalToPositionAhead"]
                if not isinstance(value, dict) or "Value" in value:
                    iv = _parse_gap_s(value)
                    events.append(event(sid, "IntervalUpdated", t_ms, d, interval_s=iv))

            if "LastLapTime" in patch:
                lt = patch["LastLapTime"]
                last_lap_ms[d] = _parse_laptime_ms(lt.get("Value") if isinstance(lt, dict) else lt)

            n = patch.get("NumberOfLaps")
            if type(n) is int and n >= 0 and (d not in laps or n > laps[d]):
                laps[d] = n
                if emit_activity and n > 0:
                    events.append(event(sid, "LapCompleted", t_ms, d, lap=n,
                                        lap_time_ms=last_lap_ms.get(d)))

            sectors = patch.get("Sectors")
            if emit_activity and timestamped and isinstance(sectors, (dict, list)):
                if isinstance(sectors, list):
                    sectors = {str(i): value for i, value in enumerate(sectors)}
                finish = sectors.get("2")
                finish_value = finish.get("Value") if isinstance(finish, dict) else None
                completed_here = (
                    type(n) is int and n > 0 and isinstance(finish_value, str)
                    and (_parse_laptime_ms(finish_value) or 0) > 0
                )
                for index in range(3):
                    sector = sectors.get(str(index))
                    if not isinstance(sector, dict) or not isinstance(sector.get("Value"), str):
                        continue
                    value = sector["Value"]
                    duration = _parse_laptime_ms(value)
                    if value != "" and (duration is None or duration <= 0):
                        continue
                    # S3 normally accompanies NumberOfLaps increment. Without
                    # that count its lap is unknown, not blindly completed+1.
                    sector_lap = n if completed_here else (
                        laps[d] + 1 if index < 2 and d in laps else None
                    )
                    events.append(event(
                        sid, "SectorTimeUpdated", t_ms, d, lap=sector_lap,
                        source="f1live", sector=index + 1, time_ms=duration,
                    ))

            if "InPit" in patch:
                now_in = bool(patch["InPit"])
                was_in = in_pit.get(d)
                if was_in is None or now_in != was_in:
                    in_pit[d] = now_in
                    cur_lap = laps.get(d, 0) + 1  # NumberOfLaps = completed
                    if emit_activity and now_in:
                        events.append(event(sid, "PitIn", t_ms, d, lap=cur_lap))
                    elif emit_activity and was_in:  # only a real out after a seen in
                        events.append(event(sid, "PitOut", t_ms, d, lap=cur_lap))

            if "Retired" in patch:
                if emit_activity and patch["Retired"]:
                    retirement_candidates.setdefault(d, (t_ms, laps.get(d, 0) + 1))
                else:
                    retirement_candidates.pop(d, None)

            if "Stopped" in patch:
                now_stopped = bool(patch["Stopped"])
                was_stopped = stopped.get(d)
                stopped[d] = now_stopped
                if emit_activity and (now_stopped or was_stopped is not None):
                    if was_stopped != now_stopped:
                        events.append(event(
                            sid, "DriverStoppedChanged", t_ms, d, stopped=now_stopped,
                        ))

    def apply_tyres(lines: dict, t_ms: int) -> None:
        for num, patch in lines.items():
            if not isinstance(patch, dict):
                continue
            stints = patch.get("Stints")
            # Feed duality: keyframes send Stints as a LIST (full history),
            # increments as a DICT of patches. Accept both, else starting
            # compounds are lost on mid-join and TYR stays empty until a stop.
            if isinstance(stints, dict):
                ordered = [
                    stints[key]
                    for key in sorted(
                        stints,
                        key=lambda key: (0, int(key)) if str(key).isdigit() else (1, str(key)),
                    )
                ]
            elif isinstance(stints, list):
                ordered = stints
            else:
                continue
            current = next(
                (stint for stint in reversed(ordered)
                 if isinstance(stint, dict) and stint.get("Compound")),
                None,
            )
            if current:
                events.append(event(
                    sid, "TyreStintUpdated", t_ms, drv(str(num)), source="f1live",
                    compound=str(current["Compound"]).capitalize(),
                    age_laps=int(current.get("TotalLaps") or 0),
                ))

    # Pass 1: driver names from ALL DriverList payloads (keyframes included),
    # so events emitted from early lines already carry abbreviations.
    for cat, payload, _ in rows:
        if cat == "DriverList":
            apply_driver_list(payload)

    # Pass 2: chronological event emission.
    latest_ms = 0
    for index, (cat, payload, ts) in enumerate(rows):
        t_posix = _parse_iso(ts)
        effective_posix = t_posix if t_posix is not None else row_times[index]
        if effective_posix is None:
            # Keyframe (no timestamp): baseline snapshot. Anchor it at the join
            # moment if known, else at 0.
            t_ms = 0
        else:
            t_ms = to_ms(effective_posix)
        latest_ms = max(latest_ms, t_ms)
        emit_activity = has_started and effective_posix is not None and effective_posix > t0

        if cat == "SessionInfo":
            path = payload.get("Path")
            if isinstance(path, str) and path:
                session_path = path
        elif cat == "SessionStatus":
            status = _STATUS_MAP.get(str(payload.get("Status")))
            # A join/reconnect keyframe says the session is running, not that
            # SC/VSC ended now. Only timestamped Started rows are transitions.
            if status == "started" and t_posix is None:
                continue
            if status:
                if status == "started" and last_status == "red_flag":
                    status = "formation"
                emit_status(status, t_ms)
        elif cat == "TimingData":
            lines = payload.get("Lines")
            if isinstance(lines, dict):
                apply_timing(lines, t_ms, emit_activity=emit_activity, timestamped=t_posix is not None)
        elif cat == "TimingAppData":
            lines = payload.get("Lines")
            if isinstance(lines, dict):
                apply_tyres(lines, t_ms)
        elif cat == "WeatherData":
            if weather := _parse_weather(payload):
                events.append(event(sid, "WeatherUpdated", t_ms, source="f1live", **weather))
        elif cat == "TrackStatus":
            if emit_activity and str(payload.get("Message", "")).lower() == "yellow":
                yellow_times.append(t_ms)
        elif cat == "RaceControlMessages":
            msgs = payload.get("Messages")
            items = msgs.values() if isinstance(msgs, dict) else (msgs or [])
            for m in items:
                if not isinstance(m, dict) or not m.get("Message"):
                    continue
                text = str(m["Message"])
                lap_no = m.get("Lap") if isinstance(m.get("Lap"), int) else None
                message_posix = _parse_iso(str(m.get("Utc") or ""))
                if not has_started or message_posix is None or message_posix < t0:
                    continue
                message_ms = to_ms(message_posix) if message_posix is not None else t_ms
                race_control_payload: dict[str, Any] = {
                    "category": str(m.get("Category", "")),
                    "message": text,
                }
                restart_at_ms = _restart_at_ms(text, message_posix, t0, gmt_offset_s)
                if restart_at_ms is not None:
                    race_control_payload["restart_at_ms"] = restart_at_ms
                events.append(event(
                    sid,
                    "RaceControlMessage",
                    message_ms,
                    lap=lap_no,
                    **race_control_payload,
                ))
                text_upper = text.upper()
                if "EXTRA FORMATION LAP" in text_upper:
                    status = "formation"
                elif (
                    last_status == "formation"
                    and any(
                        start in text_upper
                        for start in ("STANDING START", "ROLLING START")
                    )
                ):
                    status = "started"
                else:
                    status = message_to_status(text)
                    if status == "started":
                        status = None
                if "TRACK CLEAR" in text.upper() and last_status in {"safety_car", "vsc"}:
                    status = "started"
                if status is not None:
                    emit_status(status, message_ms)
        elif cat == "TeamRadio":
            caps = payload.get("Captures")
            items = caps.values() if isinstance(caps, dict) else (caps or [])
            for c in items:
                if not isinstance(c, dict) or not c.get("Path"):
                    continue
                # Keyframes contain the full radio history. Their row timestamp
                # is the join time, not the clip time; each capture carries its
                # own UTC timestamp and must be placed on the replay with that.
                radio_posix = _parse_iso(str(c.get("Utc") or ""))
                if not has_started or radio_posix is None or radio_posix < t0:
                    continue
                radio_ms = to_ms(radio_posix) if radio_posix is not None else t_ms
                d = drv(str(c.get("RacingNumber", "")))
                radio_payload: dict[str, Any] = {
                    "category": "Radio",
                    "message": f"RADIO: {d}",
                    "audio_path": str(c["Path"]),
                }
                if session_path:
                    radio_payload["audio_url"] = (
                        f"https://livetiming.formula1.com/static/{session_path}{c['Path']}"
                    )
                events.append(event(sid, "RaceControlMessage", radio_ms, d,
                                    lap=laps.get(d, 0) + 1,
                                    **radio_payload))

    for driver_id, (retired_at, lap) in retirement_candidates.items():
        if latest_ms - retired_at >= _RETIRE_CONFIRM_MS:
            events.append(event(sid, "RetirementDetected", retired_at, driver_id, lap=lap))

    pit_times: dict[str, list[int]] = {}
    for item in events:
        if item.type == "PitIn" and item.driver_id:
            pit_times.setdefault(item.driver_id, []).append(item.session_time_ms)
    for driver_id in pos:
        changes = [
            item for item in events
            if item.type == "PositionChanged" and item.driver_id == driver_id
        ]
        anchor = None
        for change in changes:
            current = change.payload["position"]
            if (
                anchor is None
                or change.session_time_ms - anchor.session_time_ms > 10_000
                or current <= anchor.payload["position"]
            ):
                anchor = change
                continue
            if (
                current >= 15
                and current - anchor.payload["position"] >= 8
                and any(abs(change.session_time_ms - yellow) <= 5_000 for yellow in yellow_times)
                and not any(
                    change.session_time_ms - 10_000 <= pit <= change.session_time_ms
                    for pit in pit_times.get(driver_id, [])
                )
            ):
                events.append(event(
                    sid, "DriverTroubleDetected", change.session_time_ms, driver_id,
                    lap=laps.get(driver_id, 0) + 1,
                    from_position=anchor.payload["position"], to_position=current,
                ))
                break

    events.sort(key=lambda e: (e.session_time_ms, e.event_id))
    return events
