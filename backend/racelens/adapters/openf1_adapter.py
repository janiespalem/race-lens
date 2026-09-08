"""OpenF1 → normalized events.

Second independent data source: https://api.openf1.org/v1/
No API key required. Produces the same Event envelope as fastf1_adapter.

Usage::

    from racelens.adapters.openf1_adapter import find_session, ingest_openf1

    session_key = find_session(2024, "Monaco")
    events = ingest_openf1(session_key)

Incremental usage (for live polling — reduces per-poll API load)::

    from racelens.adapters.openf1_adapter import OpenF1IncrementalIngester

    ingester = OpenF1IncrementalIngester(session_key)
    events = ingester.fetch()   # first call: full fetch
    events = ingester.fetch()   # subsequent: only new rows since last fetch
"""
from __future__ import annotations

import contextlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

from racelens.adapters._common import message_to_status
from racelens.events.models import Event, event

_BASE = "https://api.openf1.org/v1"
_TIMEOUT = 30
_POLL_TIMEOUT_S = 60
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_RETRIES = 4
# OpenF1's free tier limits to 3 req/s and 30 req/min. When exceeded it throttles
# with 429 — and, when sustained, with 401 (it has NO real auth, so a 401 here can
# only mean "you're throttled, back off"). It also 502/503/504s under load. All
# transient → retry with exponential backoff; other 4xx (404) are real errors.
_RETRYABLE_STATUS = {401, 429, 500, 502, 503, 504}
_BACKOFF_BASE_S = 1.5
_WATERMARK_OVERLAP_S = 5 * 60
_FULL_RECONCILE_POLLS = 50


def _parse_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _remaining_timeout(deadline: float | None) -> float:
    if deadline is None:
        return _TIMEOUT
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("OpenF1 poll deadline exceeded")
    return min(_TIMEOUT, remaining)


def _retry_sleep(seconds: float, deadline: float | None) -> None:
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("OpenF1 poll deadline exceeded")
        seconds = min(seconds, remaining)
    time.sleep(max(0, seconds))


def _get(
    path: str,
    params: dict[str, Any] | None = None,
    *,
    deadline: float | None = None,
) -> list[dict]:
    """GET JSON from OpenF1 with exponential-backoff retries on rate-limit /
    transient server errors (429, 502, 503, 504) and network errors."""
    url = _BASE + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})

    for attempt in range(_MAX_RETRIES + 1):
        timeout = _remaining_timeout(deadline)
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                content_length = _parse_int(
                    getattr(resp, "headers", {}).get("Content-Length")
                )
                if content_length is not None and content_length > _MAX_RESPONSE_BYTES:
                    raise ValueError("OpenF1 response exceeds byte limit")
                body = resp.read(_MAX_RESPONSE_BYTES + 1)
                if len(body) > _MAX_RESPONSE_BYTES:
                    raise ValueError("OpenF1 response exceeds byte limit")
                data = json.loads(body.decode("utf-8"))
                return data if isinstance(data, list) else []
        except urllib.error.HTTPError as exc:
            # Honour Retry-After if present, else exponential backoff. Only retry
            # transient statuses; real 4xx (404 etc.) raise immediately.
            if exc.code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    wait = float(retry_after) if retry_after else _BACKOFF_BASE_S * (2 ** attempt)
                except ValueError:
                    wait = _BACKOFF_BASE_S * (2 ** attempt)
                _retry_sleep(min(wait, 20), deadline)
                continue
            raise
        except (urllib.error.URLError, OSError):
            # Network-level errors may be transient — back off and retry.
            if attempt < _MAX_RETRIES:
                _retry_sleep(_BACKOFF_BASE_S * (2 ** attempt), deadline)
                continue
            raise
    return []


# ── Session lookup ────────────────────────────────────────────────────────────

def find_session(year: int, country_or_circuit: str, session_name: str = "Race") -> int:
    """Return OpenF1 session_key for the given year / location / session type.

    Matches on country_name or circuit_short_name (case-insensitive substring).
    """
    rows = _get("/sessions", {"year": year, "session_name": session_name})
    needle = country_or_circuit.lower()
    for row in rows:
        if (needle in str(row.get("country_name", "")).lower()
                or needle in str(row.get("circuit_short_name", "")).lower()
                or needle in str(row.get("location", "")).lower()):
            session_key = _parse_int(row.get("session_key"))
            if session_key is not None:
                return session_key
    available = sorted({str(row.get("country_name", "")) for row in rows if row.get("country_name")})
    raise ValueError(
        f"No OpenF1 session found for year={year}, location={country_or_circuit!r}, "
        f"session_name={session_name!r}. "
        f"Available countries: {available if available else '(none — check year/session_name)'}"
    )


# ── Session listing ───────────────────────────────────────────────────────────

def list_sessions(year: int, country: str | None = None) -> list[dict]:
    """List OpenF1 sessions for *year* (optionally filtered by country/circuit/city).

    Returns a list sorted by date_start. Each item includes a `started` flag
    that is True when the current UTC time is >= date_start.

    Matches like find_session: country can be a country, circuit or city
    (Miami → country "United States"/location "Miami"; Austria → location
    "Spielberg"), so filter across all three fields instead of country_name.
    """
    rows = _get("/sessions", {"year": year})

    if country:
        needle = country.lower()
        rows = [
            r for r in rows
            if needle in str(r.get("country_name", "")).lower()
            or needle in str(r.get("circuit_short_name", "")).lower()
            or needle in str(r.get("location", "")).lower()
        ]

    now_ts = datetime.now(timezone.utc).timestamp()

    result = []
    for row in rows:
        session_key = _parse_int(row.get("session_key"))
        if session_key is None:
            continue
        date_start = str(row.get("date_start") or "")
        # OpenF1 date_start can arrive WITHOUT a timezone (it's track-local wall
        # time). Glue on gmt_offset (e.g. "02:00:00" → "+02:00") so the frontend
        # parses the correct instant instead of assuming UTC (the 16:00→18:00 bug).
        gmt = str(row.get("gmt_offset") or "")
        if date_start and "+" not in date_start and not date_start.endswith("Z") and gmt:
            hh_mm = gmt[:5] if len(gmt) >= 5 else gmt  # "02:00:00" → "02:00"
            sign = "+" if not hh_mm.startswith("-") else ""
            date_start = f"{date_start}{sign}{hh_mm}"
        ts = _parse_iso(date_start) if date_start else None
        result.append({
            "session_name": str(row.get("session_name") or ""),
            "session_key": session_key,
            "session_type": str(row.get("session_type") or ""),
            "date_start": date_start,
            "gmt_offset": gmt,
            "started": ts is not None and now_ts >= ts,
        })

    result.sort(key=lambda x: x["date_start"])
    return result


# ── Time helpers ──────────────────────────────────────────────────────────────

def _parse_iso(s: str | None) -> float | None:
    """Parse ISO-8601 string → POSIX timestamp (float seconds). Returns None on failure."""
    if not s:
        return None
    # Normalise: replace +HH:MM / -HH:MM suffix or Z to make it UTC-aware
    s_norm = s.strip()
    # Handle timezone offset like +00:00 or -05:30
    if s_norm.endswith("Z"):
        s_norm = s_norm[:-1]
        tz = timezone.utc
    elif len(s_norm) > 6 and s_norm[-6] in ("+", "-") and s_norm[-3] == ":":
        sign = 1 if s_norm[-6] == "+" else -1
        try:
            offset_h = int(s_norm[-5:-3])
            offset_m = int(s_norm[-2:])
            from datetime import timedelta
            tz = timezone(timedelta(minutes=sign * (offset_h * 60 + offset_m)))
        except ValueError:
            tz = timezone.utc
        s_norm = s_norm[:-6]
    else:
        tz = timezone.utc

    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s_norm, fmt).replace(tzinfo=tz)
            return dt.timestamp()
        except ValueError:
            continue
    return None


# ── Shared row→Event transform ────────────────────────────────────────────────
# This is the single source of truth. ingest_openf1() and
# OpenF1IncrementalIngester both call it with their accumulated row sets.
#
# _rows_to_events() (bottom of this section) is the orchestrator: it builds the
# shared context (driver_map, session id, t0/to_ms rebase, mk() event factory)
# and hands each raw row list to the matching helper below, in arrival order.

def _build_driver_map(driver_rows: list[dict]) -> dict[int, str]:
    """Drivers → {driver_number: acronym}."""
    driver_map: dict[int, str] = {}
    for row in driver_rows:
        dn = _parse_int(row.get("driver_number"))
        acronym = row.get("name_acronym") or row.get("broadcast_name") or str(dn)
        if dn is not None:
            driver_map[dn] = str(acronym)
    return driver_map


def _derive_session_id(session_rows: list[dict]) -> str:
    """Session metadata → deterministic session_id."""
    if session_rows:
        s = session_rows[0]
        year = s.get("year", "unknown")
        location = str(s.get("location") or s.get("country_name") or "unknown").lower().replace(" ", "_")
        session_name = str(s.get("session_name") or "race").lower().replace(" ", "_")
    else:
        year = "unknown"
        location = "unknown"
        session_name = "race"
    return f"{year}_{location}_{session_name}"


def _compute_t0(lap_rows: list[dict]) -> float | None:
    """t0 rebase anchor: earliest date_start of lap 1 across all drivers (POSIX seconds)."""
    t0_posix: float | None = None
    for row in lap_rows:
        if _parse_int(row.get("lap_number")) == 1 and row.get("date_start"):
            ts = _parse_iso(row["date_start"])
            if ts is not None and (t0_posix is None or ts < t0_posix):
                t0_posix = ts
    return t0_posix


def _chronological(rows: list[dict], date_field: str) -> list[dict]:
    def key(row: dict) -> tuple[bool, float, str]:
        timestamp = _parse_iso(row.get(date_field))
        return (
            timestamp is None,
            timestamp or 0,
            json.dumps(row, sort_keys=True, separators=(",", ":"), default=str),
        )

    return sorted(rows, key=key)


def _laps_to_events(
    lap_rows: list[dict],
    driver_map: dict[int, str],
    sid: str,
    to_ms: Callable[[float], int],
    mk: Callable[..., Event],
) -> list[Event]:
    """Laps → LapCompleted."""
    events: list[Event] = []
    for row in lap_rows:
        dn = _parse_int(row.get("driver_number"))
        lap_no = _parse_int(row.get("lap_number"))
        if dn is None or lap_no is None:
            continue
        drv = driver_map.get(dn, str(dn))

        duration = row.get("lap_duration")
        date_start = _parse_iso(row.get("date_start"))

        # Session time = end of lap = start + duration
        if date_start is not None and duration is not None:
            try:
                t_end_ms = to_ms(date_start + float(duration))
            except (TypeError, ValueError):
                t_end_ms = None
        elif date_start is not None:
            t_end_ms = to_ms(date_start)
        else:
            t_end_ms = None

        if t_end_ms is None:
            continue

        lap_time_ms: int | None = None
        if duration is not None:
            with contextlib.suppress(TypeError, ValueError):
                lap_time_ms = round(float(duration) * 1000)

        events.append(mk(sid, "LapCompleted", t_end_ms, drv, lap=lap_no,
                         lap_time_ms=lap_time_ms))
    return events


def _position_to_events(
    pos_rows: list[dict],
    driver_map: dict[int, str],
    sid: str,
    to_ms: Callable[[float], int],
    mk: Callable[..., Event],
) -> list[Event]:
    """Position → PositionChanged (real changes only)."""
    events: list[Event] = []
    last_pos: dict[str, int] = {}  # driver → last seen position
    for row in pos_rows:
        dn = _parse_int(row.get("driver_number"))
        pos = _parse_int(row.get("position"))
        if dn is None or pos is None:
            continue
        drv = driver_map.get(dn, str(dn))
        ts = _parse_iso(row.get("date"))
        if ts is None:
            continue
        t_ms = to_ms(ts)
        if last_pos.get(drv) != pos:
            last_pos[drv] = pos
            events.append(mk(sid, "PositionChanged", t_ms, drv, position=pos))
    return events


def _pits_to_events(
    pit_rows: list[dict],
    driver_map: dict[int, str],
    sid: str,
    to_ms: Callable[[float], int],
    mk: Callable[..., Event],
) -> list[Event]:
    """Pits → PitIn / PitOut."""
    events: list[Event] = []
    for row in pit_rows:
        dn = _parse_int(row.get("driver_number"))
        lap_value = row.get("lap_number")
        lap_no = _parse_int(lap_value)
        if dn is None or (lap_value is not None and lap_no is None):
            continue
        drv = driver_map.get(dn, str(dn))
        pit_duration = row.get("pit_duration")
        date = _parse_iso(row.get("date"))
        if date is None:
            continue
        t_in_ms = to_ms(date)
        events.append(mk(sid, "PitIn", t_in_ms, drv, lap=lap_no,
                         pit_duration_s=float(pit_duration) if pit_duration else None))
        if pit_duration is not None:
            try:
                t_out_ms = to_ms(date + float(pit_duration))
                events.append(mk(sid, "PitOut", t_out_ms, drv, lap=lap_no))
            except (TypeError, ValueError):
                pass
    return events


def _stints_to_events(
    stint_rows: list[dict],
    lap_rows: list[dict],
    driver_map: dict[int, str],
    sid: str,
    to_ms: Callable[[float], int],
    mk: Callable[..., Event],
) -> list[Event]:
    """Stints → TyreStintUpdated."""
    events: list[Event] = []
    for row in stint_rows:
        dn = _parse_int(row.get("driver_number"))
        compound = row.get("compound")
        tyre_age_value = row.get("tyre_age_at_start")
        tyre_age = _parse_int(tyre_age_value)
        lap_start = _parse_int(row.get("lap_start"))

        if (
            dn is None
            or compound is None
            or lap_start is None
            or (tyre_age_value is not None and tyre_age is None)
        ):
            continue
        drv = driver_map.get(dn, str(dn))

        # Use lap_start to anchor t (best approximation without exact timestamp)
        # We'll look up the earliest LapCompleted time for this driver on lap_start
        # If lap_start == 1, t = 0 (pre-race); otherwise find it from laps
        if lap_start == 1:
            t_ms = 0
        else:
            # Find the LapCompleted for previous lap from lap_rows for this driver
            t_ms = None
            ref_lap = lap_start - 1
            for lr in lap_rows:
                if (
                    _parse_int(lr.get("driver_number")) == dn
                    and _parse_int(lr.get("lap_number")) == ref_lap
                ):
                    ds = _parse_iso(lr.get("date_start"))
                    dur = lr.get("lap_duration")
                    if ds is not None and dur is not None:
                        with contextlib.suppress(TypeError, ValueError):
                            t_ms = to_ms(ds + float(dur))
                    break
            if t_ms is None:
                continue

        events.append(mk(sid, "TyreStintUpdated", t_ms, drv,
                         lap=lap_start or None,
                         compound=str(compound),
                         age_laps=tyre_age or 0))
    return events


_INTERVAL_SAMPLE_MS = 30_000


def _parse_gap_value(x: Any) -> float | None:
    """Parse a gap/interval value: numeric, "+N.NNN", or "+N LAP" formats.

    Returns None for non-numeric values such as "+1 LAP".
    Negative values (e.g. leader or timing artefacts) are valid and returned.
    """
    if x is None:
        return None
    s = str(x).lstrip("+")
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _intervals_to_events(
    interval_rows: list[dict],
    driver_map: dict[int, str],
    sid: str,
    to_ms: Callable[[float], int],
    mk: Callable[..., Event],
) -> list[Event]:
    """Intervals → GapUpdated / IntervalUpdated (sampled ≤ 1 per driver / 30 s)."""
    events: list[Event] = []
    last_interval_t: dict[str, int] = {}  # driver → last emitted session_time_ms
    # Track the last valid row seen per driver so we can emit a final sample.
    last_interval_row: dict[str, tuple[int, Any, Any]] = {}  # driver → (t_ms, gap, interval)

    for row in interval_rows:
        dn = _parse_int(row.get("driver_number"))
        if dn is None:
            continue
        drv = driver_map.get(dn, str(dn))
        date = _parse_iso(row.get("date"))
        if date is None:
            continue
        t_ms = to_ms(date)

        gap = _parse_gap_value(row.get("gap_to_leader"))
        interval = _parse_gap_value(row.get("interval"))

        # Record last valid row for this driver (for end-of-stream flush)
        if gap is not None or interval is not None:
            last_interval_row[drv] = (t_ms, gap, interval)

        # Sampling gate
        if t_ms - last_interval_t.get(drv, -_INTERVAL_SAMPLE_MS) < _INTERVAL_SAMPLE_MS:
            continue
        last_interval_t[drv] = t_ms

        if gap is not None:
            events.append(mk(sid, "GapUpdated", t_ms, drv, gap_s=round(gap, 3)))
        if interval is not None:
            events.append(mk(sid, "IntervalUpdated", t_ms, drv, interval_s=round(interval, 3)))

    # Emit final measurement for each driver if it was not the last sampled point
    for drv, (t_ms, gap, interval) in last_interval_row.items():
        if last_interval_t.get(drv) != t_ms:
            if gap is not None:
                events.append(mk(sid, "GapUpdated", t_ms, drv, gap_s=round(gap, 3)))
            if interval is not None:
                events.append(mk(sid, "IntervalUpdated", t_ms, drv, interval_s=round(interval, 3)))
    return events


def _race_control_to_events(
    rc_rows: list[dict],
    sid: str,
    to_ms: Callable[[float], int],
    mk: Callable[..., Event],
) -> list[Event]:
    """Race control → RaceControlMessage + SessionStatusChanged."""
    events: list[Event] = []
    last_status: str | None = None
    for row in rc_rows:
        date = _parse_iso(row.get("date"))
        if date is None:
            continue
        t_ms = to_ms(date)
        message = str(row.get("message") or "")
        category = str(row.get("category") or "")
        flag = str(row.get("flag") or "")

        events.append(mk(sid, "RaceControlMessage", t_ms,
                         category=category, message=message, flag=flag))

        status = message_to_status(
            message, previous_status=last_status, flag=flag,
            scope=str(row.get("scope") or ""),
        )
        if status:
            events.append(mk(sid, "SessionStatusChanged", t_ms, status=status))
            last_status = status
    return events


def _rows_to_events(
    driver_rows: list[dict],
    session_rows: list[dict],
    lap_rows: list[dict],
    pos_rows: list[dict],
    pit_rows: list[dict],
    stint_rows: list[dict],
    interval_rows: list[dict],
    rc_rows: list[dict],
) -> list[Event]:
    """Convert raw OpenF1 row sets into a sorted Event list.

    All eight raw row lists are consumed in one pass.  The result is
    deterministic: identical accumulated inputs always produce identical outputs.
    This function has no side effects and does not call the network.
    """
    src = "openf1"
    seq = 0  # monotonic ingest_seq counter, shared across all helpers via mk()

    def mk(sid: str, type_: str, t_ms: int, drv: str | None = None,
           lap: int | None = None, **kw) -> Event:
        nonlocal seq
        if t_ms < 0:
            kw["prestart_time_ms"] = t_ms
        e = event(sid, type_, max(t_ms, 0), drv, lap=lap, source=src, **kw)
        e.ingest_seq = seq
        seq += 1
        return e

    driver_map = _build_driver_map(driver_rows)
    sid = _derive_session_id(session_rows)
    lap_rows = _chronological(lap_rows, "date_start")
    pos_rows = _chronological(pos_rows, "date")
    pit_rows = _chronological(pit_rows, "date")
    stint_rows = _chronological(stint_rows, "date_start")
    interval_rows = _chronological(interval_rows, "date")
    # Source order resolves simultaneous flag transitions; JSON ordering cannot.
    rc_rows = sorted(rc_rows, key=lambda row: _parse_iso(row.get("date")) or 0.0)

    events: list[Event] = [mk(sid, "SessionStarted", 0)]

    t0_posix = _compute_t0(lap_rows)
    if t0_posix is None:
        return events

    def to_ms(posix: float) -> int:
        return max(0, round((posix - t0_posix) * 1000))

    events.extend(_laps_to_events(lap_rows, driver_map, sid, to_ms, mk))
    events.extend(_position_to_events(pos_rows, driver_map, sid, to_ms, mk))
    events.extend(_pits_to_events(pit_rows, driver_map, sid, to_ms, mk))
    events.extend(_stints_to_events(stint_rows, lap_rows, driver_map, sid, to_ms, mk))
    events.extend(_intervals_to_events(interval_rows, driver_map, sid, to_ms, mk))
    # Keep prestart message identity; leave other helpers' sampling clocks unchanged.
    events.extend(_race_control_to_events(
        rc_rows, sid, lambda date: round((date - t0_posix) * 1000), mk,
    ))

    # TODO(live-map): OpenF1 /location endpoint streams X/Y car positions at ~3.7 Hz
    # during a live session.  Ingest that here to replace dead-reckoning in the
    # live-map view with real position data.  Endpoint:
    #   GET /location?session_key=<key>  → [{driver_number, date, x, y, z}, ...]
    # Suggested event type: CarPosition(t_ms, driver, x, y).
    # Sample at ~1 Hz for the replay engine (native 3.7 Hz would flood _all).

    # ingest_seq is already assigned in creation order (arrival order)
    events.sort(key=lambda e: (e.session_time_ms, e.event_id))
    return events


# ── Main ingestion (full fetch — unchanged public API) ────────────────────────

def ingest_openf1(session_key: int) -> list[Event]:
    """Fetch all relevant endpoints for *session_key* and return normalized Events.

    The session_id is derived deterministically so event_ids match across re-runs.
    This always fetches the complete session data (no incremental state).
    For live polling use OpenF1IncrementalIngester instead.
    """
    deadline = time.monotonic() + _POLL_TIMEOUT_S
    driver_rows = _get("/drivers", {"session_key": session_key}, deadline=deadline) or []
    session_rows = _get("/sessions", {"session_key": session_key}, deadline=deadline) or []
    lap_rows = _get("/laps", {"session_key": session_key}, deadline=deadline) or []
    pos_rows = _get("/position", {"session_key": session_key}, deadline=deadline) or []
    pit_rows = _get("/pit", {"session_key": session_key}, deadline=deadline) or []
    stint_rows = _get("/stints", {"session_key": session_key}, deadline=deadline) or []
    interval_rows = _get("/intervals", {"session_key": session_key}, deadline=deadline) or []
    rc_rows = _get("/race_control", {"session_key": session_key}, deadline=deadline) or []

    return _rows_to_events(
        driver_rows,
        session_rows,
        lap_rows,
        pos_rows,
        pit_rows,
        stint_rows,
        interval_rows,
        rc_rows,
    )


# ── Incremental ingester for live polling ─────────────────────────────────────

# Time-series endpoints that support the date>= filter for incremental fetching.
# Static endpoints (/drivers, /sessions) only need fetching once.
_TIMESERIES_ENDPOINTS = ("/laps", "/position", "/pit", "/stints", "/intervals", "/race_control")

# The date field name per endpoint (the field used for date>= filtering AND for
# tracking the latest row we've seen).
_DATE_FIELD: dict[str, str] = {
    "/laps": "date_start",
    "/position": "date",
    "/pit": "date",
    "/stints": "date_start",   # stints don't always have date_start; see note below
    "/intervals": "date",
    "/race_control": "date",
}


def _latest_date(rows: list[dict], date_field: str) -> str | None:
    """Return the lexicographically latest ISO date string seen in *rows*."""
    latest: str | None = None
    for row in rows:
        d = row.get(date_field)
        if d and isinstance(d, str):
            if latest is None or d > latest:
                latest = d
    return latest


class OpenF1IncrementalIngester:
    """Stateful ingester that only fetches new rows on each call after the first.

    Design:
    - First call: full fetch (equivalent to ingest_openf1).
    - Subsequent calls: for each time-series endpoint, only request rows with
      ``date>=`` the latest date seen so far; static endpoints (/drivers,
      /sessions) are fetched once and reused.
    - Raw rows are accumulated across polls.  Each call re-runs the full
      _rows_to_events() transform over ALL accumulated rows so the replay
      engine always gets a complete, deterministic event list.

    The ``fetch()`` return value is byte-for-byte identical to what
    ``ingest_openf1()`` would return given the same accumulated rows.
    """

    def __init__(self, session_key: int) -> None:
        self._session_key = session_key
        self._initialized = False
        self._polls = 0

        # Accumulated raw rows per endpoint
        self._driver_rows: list[dict] = []
        self._session_rows: list[dict] = []
        self._lap_rows: list[dict] = []
        self._pos_rows: list[dict] = []
        self._pit_rows: list[dict] = []
        self._stint_rows: list[dict] = []
        self._interval_rows: list[dict] = []
        self._rc_rows: list[dict] = []

        # Latest date seen per time-series endpoint (for date>= filter)
        self._latest: dict[str, str | None] = {ep: None for ep in _TIMESERIES_ENDPOINTS}
        self._seen_rows: dict[str, set[str]] = {ep: set() for ep in _TIMESERIES_ENDPOINTS}

    def _update_latest(self, endpoint: str, new_rows: list[dict]) -> None:
        """Update the latest-date bookmark for *endpoint* from *new_rows*."""
        date_field = _DATE_FIELD.get(endpoint)
        if date_field is None:
            return
        latest = _latest_date(new_rows, date_field)
        if latest is not None:
            prev = self._latest.get(endpoint)
            if prev is None or latest > prev:
                self._latest[endpoint] = latest

    def _fetch_timeseries(
        self,
        endpoint: str,
        *,
        deadline: float | None = None,
    ) -> list[dict]:
        """Fetch rows for a time-series endpoint, including the last-seen boundary."""
        params: dict[str, Any] = {"session_key": self._session_key}
        prev_latest = self._latest.get(endpoint)
        if (
            self._initialized
            and self._polls % _FULL_RECONCILE_POLLS != 0
            and prev_latest is not None
        ):
            # OpenF1 accepts "date>=" as a literal param key name.
            # Five-minute overlap catches ordinary lag; periodic full polls catch the rest.
            timestamp = _parse_iso(prev_latest)
            params["date>="] = (
                datetime.fromtimestamp(
                    timestamp - _WATERMARK_OVERLAP_S, timezone.utc,
                ).isoformat(timespec="milliseconds")
                if timestamp is not None
                else prev_latest
            )
        return _get(endpoint, params, deadline=deadline) or []

    def fetch(self) -> list[Event]:
        """Fetch (incrementally after first call) and return the full event list.

        Returns the same result as ingest_openf1() for the same total data set.
        """
        deadline = time.monotonic() + _POLL_TIMEOUT_S
        if not self._initialized:
            # First call: full fetch for all endpoints including static ones
            self._driver_rows = _get(
                "/drivers", {"session_key": self._session_key}, deadline=deadline,
            ) or []
            self._session_rows = _get(
                "/sessions", {"session_key": self._session_key}, deadline=deadline,
            ) or []
        # Always re-fetch time-series endpoints (full on first call, incremental thereafter)
        for endpoint, store_attr in (
            ("/laps", "_lap_rows"),
            ("/position", "_pos_rows"),
            ("/pit", "_pit_rows"),
            ("/stints", "_stint_rows"),
            ("/intervals", "_interval_rows"),
            ("/race_control", "_rc_rows"),
        ):
            new_rows = self._fetch_timeseries(endpoint, deadline=deadline)
            if new_rows:
                seen = self._seen_rows[endpoint]
                unique_rows = []
                for row in new_rows:
                    key = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
                    if key not in seen:
                        seen.add(key)
                        unique_rows.append(row)
                if unique_rows:
                    getattr(self, store_attr).extend(unique_rows)
                    self._update_latest(endpoint, unique_rows)

        self._initialized = True
        self._polls += 1

        return _rows_to_events(
            self._driver_rows,
            self._session_rows,
            self._lap_rows,
            self._pos_rows,
            self._pit_rows,
            self._stint_rows,
            self._interval_rows,
            self._rc_rows,
        )
