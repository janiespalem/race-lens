from datetime import UTC, datetime, timedelta, timezone
import subprocess
from types import SimpleNamespace

import pytest

from racelens.recorder.schedule import (
    HARD_DURATION,
    ScheduledSession,
    _fetch_fastf1_schedule,
    canonical_alias,
    load_fastf1_schedule,
    parse_fastf1_schedule,
    select_due_session,
)


def _at(year=2026, month=7, day=17, hour=12, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


@pytest.mark.parametrize(
    ("name", "alias"),
    [
        ("Practice 1", "FP1"), ("Practice 2", "FP2"), ("Practice 3", "FP3"),
        ("Sprint Shootout", "SQ"), ("Sprint Qualifying", "SQ"),
        ("Sprint", "Sprint"), ("Qualifying", "Q"), ("Race", "R"),
    ],
)
def test_canonical_aliases(name, alias):
    assert canonical_alias(name) == alias
    assert canonical_alias(f"  {name.upper()}  ") == alias


def test_parse_fastf1_session_columns_as_utc():
    row = {
        "EventDate": _at(2026, 7, 19),
        "RoundNumber": 12,
        "EventName": "British Grand Prix",
        "Session1": "Practice 1",
        "Session1DateUtc": datetime(2026, 7, 17, 11),  # FastF1 UTC is naive
        "Session2": "Qualifying",
        "Session2DateUtc": "2026-07-18T14:00:00Z",
        "Session3": "Race",
        "Session3DateUtc": datetime(
            2026, 7, 19, 16, tzinfo=timezone(timedelta(hours=2))
        ),
        "Session4": "Unknown ceremony",
        "Session4DateUtc": _at(),
    }

    sessions = parse_fastf1_schedule([row])

    assert [item.kind for item in sessions] == ["FP1", "Q", "R"]
    assert all(item.starts_at.tzinfo is UTC for item in sessions)
    assert sessions[-1].starts_at == _at(2026, 7, 19, 14)
    assert sessions[-1].session_id == "2026-12-r"


def test_dataframe_shape_and_fastf1_boundary(monkeypatch):
    row = {
        "EventDate": _at(), "RoundNumber": 1, "EventName": "Test GP",
        "Session1": "Race", "Session1DateUtc": _at(),
    }

    class Frame:
        def to_dict(self, orient):
            assert orient == "records"
            return [row]

    calls = []
    fake = SimpleNamespace(
        get_event_schedule=lambda year, include_testing: (
            calls.append((year, include_testing)) or Frame()
        )
    )
    monkeypatch.setitem(__import__("sys").modules, "fastf1", fake)

    assert _fetch_fastf1_schedule(2026)[0].kind == "R"
    assert calls == [(2026, False)]


def test_race_starts_hour_early_while_other_sessions_start_ten_minutes_early():
    start = _at()
    qualifying = ScheduledSession(2026, 12, "Test", "Q", start)
    race = ScheduledSession(2026, 12, "Test", "R", start)

    assert select_due_session([qualifying], start - timedelta(minutes=11)) is None
    assert select_due_session([qualifying], start - timedelta(minutes=10)) == qualifying
    assert select_due_session([race], start - timedelta(minutes=61)) is None
    assert select_due_session([race], start - timedelta(minutes=60)) == race
    assert select_due_session([qualifying], start + timedelta(minutes=90)) == qualifying
    assert select_due_session([qualifying], start + HARD_DURATION["Q"]) is None
    assert select_due_session([race], start + timedelta(hours=3, minutes=30)) == race


def test_selects_at_most_one_and_can_exclude_state_owned_session():
    now = _at()
    older = ScheduledSession(2026, 1, "A", "FP1", now - timedelta(minutes=5))
    newer = ScheduledSession(2026, 2, "B", "FP1", now)

    assert select_due_session([newer, older], now) == older
    assert select_due_session([newer, older], now, {older.session_id}) == newer
    assert select_due_session([newer, older], now, {older.session_id, newer.session_id}) is None


def test_utc_window_crosses_year_boundary():
    session = ScheduledSession(2027, 1, "New Year", "R", _at(2027, 1, 1, 0, 5))
    local_now = datetime(2027, 1, 1, 0, 56, tzinfo=timezone(timedelta(hours=1)))
    assert select_due_session([session], local_now) == session


def test_naive_now_fails_closed():
    session = ScheduledSession(2026, 1, "Test", "R", _at())
    with pytest.raises(ValueError, match="timezone-aware"):
        select_due_session([session], datetime(2026, 7, 17, 12))


def test_naive_schedule_time_is_rejected():
    with pytest.raises(ValueError, match="starts_at"):
        ScheduledSession(2026, 1, "Test", "R", datetime(2026, 7, 17, 12))


def test_schedule_fetch_has_a_killable_deadline(monkeypatch, tmp_path):
    from racelens.recorder import schedule

    (tmp_path / "fastf1.py").write_text(
        "import time\ndef get_event_schedule(*args, **kwargs): time.sleep(60)\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setattr(schedule, "SCHEDULE_TIMEOUT_SECONDS", 0.2, raising=False)
    with pytest.raises(subprocess.TimeoutExpired):
        load_fastf1_schedule(2026)


def test_schedule_child_returns_utc_sessions(monkeypatch, tmp_path):
    (tmp_path / "fastf1.py").write_text(
        "def get_event_schedule(year, include_testing):\n"
        "    assert year == 2026 and include_testing is False\n"
        "    return [{'Year': year, 'RoundNumber': 7, 'EventName': 'Barcelona',\n"
        "             'Session1': 'Race', 'Session1DateUtc': '2026-06-14T13:00:00Z'}]\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    sessions = load_fastf1_schedule(2026)
    assert sessions == [ScheduledSession(2026, 7, "Barcelona", "R", _at(2026, 6, 14, 13))]
