"""Tests for the OpenF1 adapter — no network, all HTTP mocked."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import racelens.adapters.openf1_adapter as _mod
from racelens.replay.engine import ReplayEngine

# ── Canned fixtures ──────────────────────────────────────────────────────────

_SESSION_KEY = 9999

_SESSIONS = [
    {
        "session_key": _SESSION_KEY,
        "session_name": "Race",
        "year": 2024,
        "location": "Monaco",
        "country_name": "Monaco",
        "circuit_short_name": "Monaco",
    }
]

_DRIVERS = [
    {"driver_number": 1, "name_acronym": "VER"},
    {"driver_number": 16, "name_acronym": "LEC"},
    {"driver_number": 4, "name_acronym": "NOR"},
]

# Three drivers, 2 laps each.
# Lap 1 starts at a common anchor.  VER is fastest each lap.
_T0 = "2024-05-26T13:00:00.000"  # session zero for rebase (lap 1 start)
_LAPS = [
    # VER lap 1: starts at T0, duration 78s
    {"driver_number": 1, "lap_number": 1, "date_start": _T0, "lap_duration": 78.0},
    # LEC lap 1: same start, 79s
    {"driver_number": 16, "lap_number": 1, "date_start": _T0, "lap_duration": 79.0},
    # NOR lap 1: same start, 80s
    {"driver_number": 4, "lap_number": 1, "date_start": _T0, "lap_duration": 80.0},
    # VER lap 2: starts after lap 1, 78s
    {"driver_number": 1, "lap_number": 2, "date_start": "2024-05-26T13:01:18.000", "lap_duration": 78.0},
    # LEC lap 2
    {"driver_number": 16, "lap_number": 2, "date_start": "2024-05-26T13:01:19.000", "lap_duration": 79.0},
    # NOR lap 2
    {"driver_number": 4, "lap_number": 2, "date_start": "2024-05-26T13:01:20.000", "lap_duration": 80.0},
]

_POSITIONS = [
    {"driver_number": 1, "position": 1, "date": _T0},
    {"driver_number": 16, "position": 2, "date": _T0},
    {"driver_number": 4, "position": 3, "date": _T0},
    # NOR overtakes LEC at lap 1 end
    {"driver_number": 4, "position": 2, "date": "2024-05-26T13:01:19.500"},
    {"driver_number": 16, "position": 3, "date": "2024-05-26T13:01:19.500"},
]

_PITS = [
    # LEC pits at lap 1, pit_duration 25s
    {"driver_number": 16, "lap_number": 1, "pit_duration": 25.0, "date": "2024-05-26T13:01:10.000"},
]

_STINTS = [
    {"driver_number": 1, "lap_start": 1, "lap_end": 2, "compound": "MEDIUM", "tyre_age_at_start": 0},
    {"driver_number": 16, "lap_start": 1, "lap_end": 1, "compound": "SOFT", "tyre_age_at_start": 0},
    {"driver_number": 16, "lap_start": 2, "lap_end": 2, "compound": "HARD", "tyre_age_at_start": 0},
    {"driver_number": 4, "lap_start": 1, "lap_end": 2, "compound": "MEDIUM", "tyre_age_at_start": 0},
]

# Interval fixture: 30 rows per driver at 3-second intervals starting 60 s
# after session zero (13:01:00 = T0 + 60 s).  Timestamps are generated via
# datetime + timedelta so seconds never exceed 59.
_INTERVAL_BASE = datetime(2024, 5, 26, 13, 1, 0, tzinfo=timezone.utc)
_INTERVAL_STEP_S = 3
_INTERVAL_COUNT = 30  # 0..87 s → 30 rows, covering a 87-second window

_INTERVALS = [
    *(
        {
            "driver_number": 16,
            "date": (_INTERVAL_BASE + timedelta(seconds=i * _INTERVAL_STEP_S)).strftime(
                "%Y-%m-%dT%H:%M:%S.000"
            ),
            "gap_to_leader": round(1.0 + i * _INTERVAL_STEP_S * 0.05, 3),
            "interval": round(0.5 + i * _INTERVAL_STEP_S * 0.02, 3),
        }
        for i in range(_INTERVAL_COUNT)
    ),
    *(
        {
            "driver_number": 4,
            "date": (_INTERVAL_BASE + timedelta(seconds=i * _INTERVAL_STEP_S)).strftime(
                "%Y-%m-%dT%H:%M:%S.000"
            ),
            "gap_to_leader": round(2.0 + i * _INTERVAL_STEP_S * 0.05, 3),
            "interval": round(1.0 + i * _INTERVAL_STEP_S * 0.02, 3),
        }
        for i in range(_INTERVAL_COUNT)
    ),
]
# Window: 0s .. 87s (step=3s, count=30).  T0 is lap-1 start = 13:00:00,
# so interval rows land at session_time_ms 60_000 .. 147_000.
# Sampling period = 30_000 ms.
# Gate emits at: 60_000 (first), 90_000 (+30s), 120_000 (+30s).
# After the loop the last valid row is at 147_000; it was not the last emitted
# sample (120_000 ≠ 147_000), so the flush adds one more → 4 emits total.
_EXPECTED_INTERVAL_EMITS = 4

_RACE_CONTROL = [
    {"date": "2024-05-26T13:00:00.500", "category": "Flag", "message": "GREEN FLAG", "flag": "GREEN"},
    {"date": "2024-05-26T13:02:40.000", "category": "Flag", "message": "CHEQUERED FLAG", "flag": "CHEQUERED"},
]


def _make_mock_get(overrides: dict | None = None):
    """Return a mock _get function that serves canned data based on path."""
    data = {
        "/sessions": _SESSIONS,
        "/drivers": _DRIVERS,
        "/laps": _LAPS,
        "/position": _POSITIONS,
        "/pit": _PITS,
        "/stints": _STINTS,
        "/intervals": _INTERVALS,
        "/race_control": _RACE_CONTROL,
    }
    if overrides:
        data.update(overrides)

    def _get(path, params=None, *, deadline=None):
        return list(data.get(path, []))

    return _get


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ingest(overrides=None):
    with patch.object(_mod, "_get", _make_mock_get(overrides)):
        return _mod.ingest_openf1(_SESSION_KEY)


# ── Tests ────────────────────────────────────────────────────────────────────

def test_get_bounds_response_body_and_deadline(monkeypatch):
    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        @staticmethod
        def read(limit):
            assert limit == 5
            return b"12345"

    monkeypatch.setattr(_mod, "_MAX_RESPONSE_BYTES", 4)
    monkeypatch.setattr(_mod.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    with pytest.raises(ValueError, match="byte limit"):
        _mod._get("/laps")

    monkeypatch.setattr(_mod.time, "monotonic", lambda: 2)
    with pytest.raises(TimeoutError, match="deadline"):
        _mod._get("/laps", deadline=1)


def test_poll_uses_one_aggregate_deadline(monkeypatch):
    deadlines = []

    def mock_get(path, params=None, *, deadline=None):
        deadlines.append(deadline)
        return []

    monkeypatch.setattr(_mod.time, "monotonic", lambda: 100)
    monkeypatch.setattr(_mod, "_get", mock_get)

    _mod.OpenF1IncrementalIngester(_SESSION_KEY).fetch()

    assert deadlines == [100 + _mod._POLL_TIMEOUT_S] * 8


def test_event_types_present():
    events = _ingest()
    types = {e.type for e in events}
    assert "SessionStarted" in types
    assert "LapCompleted" in types
    assert "PositionChanged" in types
    assert "PitIn" in types
    assert "PitOut" in types
    assert "TyreStintUpdated" in types
    assert "GapUpdated" in types
    assert "IntervalUpdated" in types
    assert "RaceControlMessage" in types
    assert "SessionStatusChanged" in types


def test_lap1_start_rebased_to_zero():
    """Earliest lap-1 date_start should map to session_time_ms=0."""
    events = _ingest()
    # The SessionStarted event is always at t=0; also, the first LapCompleted
    # for lap 1 should have session_time_ms == lap_duration_ms (78_000 for VER).
    lap_completions = [e for e in events if e.type == "LapCompleted" and e.lap == 1]
    assert lap_completions, "Expected LapCompleted events for lap 1"
    ver_lap1 = next(e for e in lap_completions if e.driver_id == "VER")
    assert ver_lap1.session_time_ms == 78_000
    assert ver_lap1.payload.get("lap_time_ms") == 78_000


def test_interval_sampling():
    """Intervals are sampled ≤ 1 per driver per 30 s, plus a final flush.

    Fixture: 30 rows × 3 s = 0..87 s window starting at session_time 60 s.
    Sampling gates emit at 60_000, 90_000, 120_000 ms.
    End-of-stream flush adds the last row (147_000 ms) → 4 GapUpdated per driver.
    """
    events = _ingest()
    gaps = [e for e in events if e.type == "GapUpdated"]
    lec_gaps = [e for e in gaps if e.driver_id == "LEC"]
    nor_gaps = [e for e in gaps if e.driver_id == "NOR"]

    assert len(lec_gaps) == _EXPECTED_INTERVAL_EMITS, (
        f"Expected {_EXPECTED_INTERVAL_EMITS} LEC GapUpdated, got {len(lec_gaps)}"
    )
    assert len(nor_gaps) == _EXPECTED_INTERVAL_EMITS, (
        f"Expected {_EXPECTED_INTERVAL_EMITS} NOR GapUpdated, got {len(nor_gaps)}"
    )


def test_ingest_seq_monotonic():
    """ingest_seq reflects arrival (creation) order — values are unique and span 0..n-1.

    After ingest, events are sorted by session_time_ms so the seq values will
    NOT appear in sorted order in the final list — that is expected.
    """
    events = _ingest()
    seqs = [e.ingest_seq for e in events if e.ingest_seq is not None]
    assert len(seqs) == len(events), "All events must have ingest_seq set"
    assert sorted(seqs) == list(range(len(seqs))), (
        "ingest_seq should be unique consecutive integers 0..n-1"
    )


def test_source_tag():
    events = _ingest()
    for e in events:
        assert e.source == "openf1", f"Unexpected source on {e}"


def test_dedup_via_replay_engine():
    """Ingesting twice and feeding all events to ReplayEngine → 0 duplicates."""
    first = _ingest()
    second = _ingest()
    engine = ReplayEngine(first + second)
    # All second-run events are exact duplicates of first-run events (same deterministic IDs)
    assert engine.duplicates_dropped == len(first)


def test_pit_events():
    events = _ingest()
    pit_ins = [e for e in events if e.type == "PitIn" and e.driver_id == "LEC"]
    pit_outs = [e for e in events if e.type == "PitOut" and e.driver_id == "LEC"]
    assert len(pit_ins) == 1
    assert len(pit_outs) == 1
    # PitOut must be after PitIn
    assert pit_outs[0].session_time_ms > pit_ins[0].session_time_ms


def test_position_changed_dedup():
    """PositionChanged should only be emitted on actual position changes."""
    events = _ingest()
    # VER stays P1 throughout; there should be only 1 PositionChanged for VER
    ver_pos = [e for e in events if e.type == "PositionChanged" and e.driver_id == "VER"]
    assert len(ver_pos) == 1


def test_position_rows_are_transformed_chronologically():
    rows = [
        {"driver_number": 1, "position": 2, "date": "2024-05-26T13:00:20.000"},
        {"driver_number": 1, "position": 1, "date": "2024-05-26T13:00:10.000"},
        {"driver_number": 1, "position": 1, "date": _T0},
    ]

    events = _ingest({"/position": rows})
    positions = [
        (event.session_time_ms, event.payload["position"])
        for event in events
        if event.type == "PositionChanged" and event.driver_id == "VER"
    ]
    assert positions == [(0, 1), (20_000, 2)]


def test_tyre_stints():
    events = _ingest()
    stint_evts = [e for e in events if e.type == "TyreStintUpdated"]
    drivers_with_stints = {e.driver_id for e in stint_evts}
    assert "VER" in drivers_with_stints
    assert "LEC" in drivers_with_stints

    # LEC has 2 stints (SOFT → HARD)
    lec_stints = [e for e in stint_evts if e.driver_id == "LEC"]
    compounds = [e.payload["compound"] for e in lec_stints]
    assert "SOFT" in compounds
    assert "HARD" in compounds


def test_session_status_from_race_control():
    events = _ingest()
    statuses = [e for e in events if e.type == "SessionStatusChanged"]
    status_values = [e.payload["status"] for e in statuses]
    assert "started" in status_values
    assert "finished" in status_values


def test_empty_endpoints_dont_crash():
    """If all endpoints return empty, ingest should return only SessionStarted."""
    empty: dict = {
        "/drivers": [],
        "/laps": [],
        "/position": [],
        "/pit": [],
        "/stints": [],
        "/intervals": [],
        "/race_control": [],
    }
    events = _ingest(overrides=empty)
    assert len(events) == 1
    assert events[0].type == "SessionStarted"


def test_malformed_provider_integer_fields_skip_only_their_rows():
    malformed = {
        "/drivers": [{"driver_number": "bad"}, *_DRIVERS],
        "/laps": [
            {"driver_number": "bad", "lap_number": 1, "date_start": _T0},
            {"driver_number": 1, "lap_number": "bad", "date_start": _T0},
            *_LAPS,
        ],
        "/position": [
            {"driver_number": "bad", "position": 1, "date": _T0},
            {"driver_number": 1, "position": "bad", "date": _T0},
            *_POSITIONS,
        ],
        "/pit": [
            {"driver_number": 1, "lap_number": "bad", "date": _T0},
            *_PITS,
        ],
        "/stints": [
            {
                "driver_number": 1, "lap_start": "bad",
                "compound": "MEDIUM", "tyre_age_at_start": 0,
            },
            {
                "driver_number": 1, "lap_start": 1,
                "compound": "MEDIUM", "tyre_age_at_start": "bad",
            },
            *_STINTS,
        ],
        "/intervals": [
            {"driver_number": "bad", "date": _T0, "gap_to_leader": 1},
            *_INTERVALS,
        ],
    }

    assert {event.event_id for event in _ingest(malformed)} == {
        event.event_id for event in _ingest()
    }


def test_find_session_mock():
    """find_session resolves case-insensitively by country_name substring."""
    with patch.object(_mod, "_get", _make_mock_get()):
        key = _mod.find_session(2024, "monaco")
    assert key == _SESSION_KEY


def test_find_session_no_match_raises():
    with patch.object(_mod, "_get", lambda path, params=None: []):
        with pytest.raises(ValueError, match="No OpenF1 session"):
            _mod.find_session(2024, "Atlantis")


def test_find_session_no_fallback_when_rows_exist():
    """find_session must NOT fall back to the first row when the needle doesn't match.

    The old code would silently return rows[0]; the fixed code must raise ValueError
    listing the available country names.
    """
    with patch.object(_mod, "_get", _make_mock_get()):
        with pytest.raises(ValueError, match="Available countries") as exc_info:
            _mod.find_session(2024, "Atlantis")
    assert "Monaco" in str(exc_info.value)


def test_find_session_404_via_api():
    """GET /api/live/start with unknown country must return 404 (not start a session)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import racelens.api as api

    api._live = None
    client = TestClient(api.app)

    with patch.object(_mod, "_get", _make_mock_get()):
        r = client.post("/api/live/start", params={"year": 2024, "country": "Atlantis"})
    assert r.status_code == 404
    assert "Available countries" in r.json()["detail"]
    assert api._live is None


# ── Incremental ingester tests ────────────────────────────────────────────────

def test_incremental_ingester_two_batches():
    """Second batch includes the date boundary; final events equal full-fetch events.

    Setup:
    - Batch 1: laps 1 only (3 drivers), positions/pits/intervals/rc from T0 only.
    - Batch 2: laps 2 only (3 drivers), remaining rows after T0.
    The mock simulates the real API: on the second call it returns only the new rows
    (as OpenF1 would after filtering by date>=).  The incremental ingester must:
    1. Include a date>= param on the second /laps (and other time-series) call.
    2. Not re-fetch /drivers or /sessions.
    3. Produce the same events as a full ingest over all rows combined.
    """
    # Split _LAPS into two batches by lap_number
    batch1_laps = [r for r in _LAPS if r["lap_number"] == 1]
    batch2_laps = [r for r in _LAPS if r["lap_number"] == 2]

    # For the other time-series endpoints we pretend batch 1 returns nothing new
    # and batch 2 returns everything (simulates that they all arrived between polls).
    # This is the simplest correct split for the test.
    batch1_data: dict[str, list] = {
        "/sessions": _SESSIONS,
        "/drivers": _DRIVERS,
        "/laps": batch1_laps,
        "/position": [],
        "/pit": [],
        "/stints": [],
        "/intervals": [],
        "/race_control": [],
    }
    batch2_data: dict[str, list] = {
        # static endpoints should NOT be called again; if they are, return empty
        "/sessions": [],
        "/drivers": [],
        "/laps": batch2_laps,
        "/position": _POSITIONS,
        "/pit": _PITS,
        "/stints": _STINTS,
        "/intervals": _INTERVALS,
        "/race_control": _RACE_CONTROL,
    }

    call_log: list[tuple[str, dict]] = []
    call_counts: dict[str, int] = {}

    def mock_get(path, params=None, *, deadline=None):
        params = dict(params) if params else {}
        call_log.append((path, params))
        n = call_counts.get(path, 0)
        call_counts[path] = n + 1
        data = batch1_data if n == 0 else batch2_data
        return list(data.get(path, []))

    ingester = _mod.OpenF1IncrementalIngester(_SESSION_KEY)

    with patch.object(_mod, "_get", mock_get):
        call_log.clear()
        call_counts.clear()
        ingester.fetch()  # batch 1

        call_log.clear()
        events_incremental = ingester.fetch()  # batch 2

    # After batch 2, the /laps call must include the last-seen boundary.
    laps_calls_batch2 = [(path, params) for path, params in call_log if path == "/laps"]
    assert laps_calls_batch2, "Expected at least one /laps call in batch 2"
    _, laps_params = laps_calls_batch2[0]
    assert "date>=" in laps_params, (
        f"Second /laps fetch must include date>= filter; got params={laps_params}"
    )

    # Static endpoints (/drivers, /sessions) must NOT be fetched again in batch 2
    static_paths_batch2 = [path for path, _ in call_log if path in ("/drivers", "/sessions")]
    assert static_paths_batch2 == [], (
        f"Static endpoints must not be re-fetched after first call; got {static_paths_batch2}"
    )

    # Final accumulated events should match a full ingest over all rows combined.
    # Build a reference by calling the full ingest with all rows in one shot.
    full_events = _ingest()
    assert len(events_incremental) == len(full_events), (
        f"Incremental ingester produced {len(events_incremental)} events, "
        f"full ingest produced {len(full_events)}"
    )

    # Event IDs must be identical (deterministic transform)
    full_ids = {e.event_id for e in full_events}
    incr_ids = {e.event_id for e in events_incremental}
    assert full_ids == incr_ids, (
        f"Event IDs differ between full ingest and incremental ingester.\n"
        f"Only in full: {full_ids - incr_ids}\n"
        f"Only in incremental: {incr_ids - full_ids}"
    )


def test_incremental_ingester_dedupes_rows_without_dates():
    ingester = _mod.OpenF1IncrementalIngester(_SESSION_KEY)
    with patch.object(_mod, "_get", _make_mock_get()):
        ingester.fetch()
        first_count = len(ingester._stint_rows)
        ingester.fetch()
    assert len(ingester._stint_rows) == first_count


def test_incremental_ingester_recovers_late_row_inside_overlap():
    first = {
        "driver_number": 1,
        "position": 1,
        "date": "2024-05-26T13:10:00.000",
    }
    late = {
        "driver_number": 16,
        "position": 2,
        "date": "2024-05-26T13:08:00.000",
    }
    position_calls = 0
    second_params = {}

    def mock_get(path, params=None, *, deadline=None):
        nonlocal position_calls, second_params
        if path == "/sessions":
            return _SESSIONS
        if path == "/drivers":
            return _DRIVERS
        if path == "/laps":
            return [r for r in _LAPS if r["lap_number"] == 1]
        if path == "/position":
            position_calls += 1
            if position_calls == 2:
                second_params = dict(params or {})
                return [late, first]
            return [first]
        return []

    ingester = _mod.OpenF1IncrementalIngester(_SESSION_KEY)
    with patch.object(_mod, "_get", mock_get):
        ingester.fetch()
        ingester.fetch()

    assert _mod._parse_iso(second_params["date>="]) == _mod._parse_iso(first["date"]) - 300
    assert ingester._pos_rows == [first, late]


def test_incremental_ingester_periodically_reconciles_full_history():
    params_seen = {}
    ingester = _mod.OpenF1IncrementalIngester(_SESSION_KEY)
    ingester._initialized = True
    ingester._polls = _mod._FULL_RECONCILE_POLLS
    ingester._latest["/position"] = "2024-05-26T13:10:00.000"

    def mock_get(path, params=None, *, deadline=None):
        params_seen.update(params or {})
        return []

    with patch.object(_mod, "_get", mock_get):
        ingester._fetch_timeseries("/position")

    assert params_seen == {"session_key": _SESSION_KEY}


def test_rows_without_lap1_anchor_do_not_use_unix_epoch():
    events = _ingest({"/laps": []})
    assert [(e.type, e.session_time_ms) for e in events] == [("SessionStarted", 0)]


def test_later_stint_without_previous_lap_anchor_is_deferred():
    orphan_stint = {
        "driver_number": 16,
        "lap_start": 4,
        "lap_end": 5,
        "compound": "HARD",
        "tyre_age_at_start": 0,
    }
    events = _ingest({"/stints": [orphan_stint]})
    assert not [e for e in events if e.type == "TyreStintUpdated"]


def test_neutralization_waits_for_actual_green_and_track_clear_does_not_restart_red():
    messages = [
        ("SAFETY CAR DEPLOYED", ""),
        ("SAFETY CAR IN THIS LAP", ""),
        ("TRACK CLEAR", ""),
        ("VIRTUAL SAFETY CAR DEPLOYED", ""),
        ("VSC ENDING", ""),
        ("GREEN FLAG", "GREEN"),
        ("RED FLAG", "RED"),
        ("TRACK CLEAR", "GREEN"),
        ("GREEN LIGHT - PIT EXIT OPEN", "GREEN"),
        ("GREEN FLAG", "GREEN"),
    ]
    rows = [
        {"date": f"2024-05-26T13:00:{second:02d}.000", "message": message,
         "flag": flag, "category": "Flag", "scope": "Track"}
        for second, (message, flag) in enumerate(messages, 1)
    ]
    events = _ingest({"/race_control": list(reversed(rows))})
    engine = ReplayEngine(events)
    assert [engine.state_at(second * 1000)["session_status"] for second in range(1, 11)] == [
        "safety_car", "safety_car", "started", "vsc", "vsc", "started",
        "red_flag", "red_flag", "red_flag", "started",
    ]
    assert [e.payload["message"] for e in events if e.type == "RaceControlMessage"] == [
        message for message, _ in messages
    ]


def test_race_control_preserves_source_order_on_equal_timestamps():
    rows = [
        {"date": "2024-05-26T13:00:02.000", "category": "Flag", "message": "RED FLAG"},
        {"date": "2024-05-26T13:00:01.000", "category": "SafetyCar", "message": "VSC DEPLOYED"},
        {"date": "2024-05-26T13:00:01.000", "category": "Flag", "message": "TRACK CLEAR"},
        {"date": "invalid", "category": "SafetyCar", "message": "SAFETY CAR DEPLOYED"},
    ]
    events = _ingest({"/race_control": rows})
    engine = ReplayEngine(events)

    assert engine.state_at(1000)["session_status"] == "started"
    assert engine.state_at(2000)["session_status"] == "red_flag"
    assert not any(e.payload.get("message") == "SAFETY CAR DEPLOYED" for e in events)
