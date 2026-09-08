from datetime import timedelta

import pytest

from racelens.adapters._common import fastf1_lap1_start
from racelens.adapters.fastf1_adapter import best_lap_position_events
from racelens.replay.engine import ReplayEngine


class _Series(list):
    def dropna(self):
        return _Series(value for value in self if value is not None)

    def min(self):
        return min(self)

    def __sub__(self, other):
        return _Series(
            left - right if left is not None and right is not None else None
            for left, right in zip(self, other)
        )


def test_lap1_start_uses_explicit_time_when_lap_time_is_missing():
    lap1 = {
        "LapStartTime": _Series([timedelta(seconds=42), timedelta(seconds=43)]),
        "Time": _Series([timedelta(seconds=130), timedelta(seconds=131)]),
        "LapTime": _Series([None, None]),
    }

    assert fastf1_lap1_start(lap1) == timedelta(seconds=42)


def test_lap1_start_falls_back_for_legacy_fastf1_data():
    lap1 = {
        "Time": _Series([timedelta(seconds=130), timedelta(seconds=131)]),
        "LapTime": _Series([timedelta(seconds=90), None]),
    }

    assert fastf1_lap1_start(lap1) == timedelta(seconds=40)


def test_lap1_start_refuses_an_unrebased_archive():
    lap1 = {
        "LapStartTime": _Series([None]),
        "Time": _Series([timedelta(minutes=58)]),
        "LapTime": _Series([None]),
    }

    with pytest.raises(ValueError, match="refusing unrebased archive"):
        fastf1_lap1_start(lap1)


def test_practice_positions_follow_each_drivers_best_lap():
    events = best_lap_position_events(
        "practice",
        [
            (100, "VER", 90_000),
            (110, "ANT", 89_000),
            (120, "VER", 88_000),
            (130, "ANT", 91_000),
        ],
    )

    state = ReplayEngine(events).state_at(130)
    assert state["classification"] == ["VER", "ANT"]


def test_race_control_waits_for_actual_green_and_preserves_red_flag(monkeypatch):
    import racelens.adapters.fastf1_adapter as adapter

    messages = [
        ("SAFETY CAR DEPLOYED", ""),
        ("SC IN THIS LAP", ""),
        ("TRACK CLEAR", ""),
        ("VIRTUAL SAFETY CAR DEPLOYED", ""),
        ("VIRTUAL SAFETY CAR ENDING", "GREEN"),
        ("GREEN FLAG", "GREEN"),
        ("RED FLAG", "RED"),
        ("TRACK CLEAR", "GREEN"),
        ("GREEN LIGHT - PIT EXIT OPEN", "GREEN"),
        ("GREEN FLAG", "GREEN"),
    ]

    class Rows(list):
        def iterrows(self):
            return enumerate(self)

    rows = Rows(
        {"Time": second * 1000, "Message": message, "Flag": flag,
         "Category": "Flag", "Scope": "Track"}
        for second, (message, flag) in enumerate(messages, 1)
    )
    rows.reverse()
    # This focused conversion check needs no optional pandas/FastF1 install.
    monkeypatch.setattr(adapter, "_timestamp_to_session_ms", lambda time, _: time)
    events = adapter._race_control_to_events(rows, "race", None, "fastf1")
    engine = ReplayEngine(events)
    assert [engine.state_at(second * 1000)["session_status"] for second in range(1, 11)] == [
        "safety_car", "safety_car", "started", "vsc", "vsc", "started",
        "red_flag", "red_flag", "red_flag", "started",
    ]
    assert [e.payload["message"] for e in events if e.type == "RaceControlMessage"] == [
        message for message, _ in messages
    ]


@pytest.mark.parametrize("times,rebase_ms", [
    ((1000, 1000), 0),
    ((1000, 2000), 3000),
], ids=["same-source-time", "prestart-rebased-to-zero"])
def test_race_control_preserves_source_order_when_event_times_collide(
    monkeypatch, times, rebase_ms,
):
    import racelens.adapters.fastf1_adapter as adapter
    from racelens.events.models import dump_jsonl, load_jsonl

    class Rows(list):
        def iterrows(self):
            return enumerate(self)

    rows = Rows([
        {"Time": times[0], "Message": "VSC DEPLOYED"},
        {"Time": times[1], "Message": "TRACK CLEAR"},
    ])
    monkeypatch.setattr(adapter, "_timestamp_to_session_ms", lambda time, _: time)
    events = adapter._race_control_to_events(
        rows, "2026_barcelona_grand_prix_r", None, "fastf1",
    )
    adapter._rebase_events(events, rebase_ms)
    events.sort(key=lambda item: (item.session_time_ms, item.event_id))

    restored = load_jsonl(dump_jsonl(events))
    state = ReplayEngine(restored).state_at(max(times[-1] - rebase_ms, 0))
    assert state["session_status"] == "started"
    assert all("ingest_seq" not in item.payload for item in restored)


@pytest.mark.parametrize("rebase_ms", [4000, 3000, 500])
def test_repeated_flag_transitions_survive_rebasing_and_real_duplicates_do_not(
    monkeypatch, rebase_ms, tmp_path,
):
    import racelens.adapters.fastf1_adapter as adapter
    from racelens.events.models import dump_jsonl, load_jsonl
    from racelens.recorder.postprocess import validate_fixture

    class Rows(list):
        def iterrows(self):
            return enumerate(self)

    rows = Rows([
        {"Time": 1000, "Message": "VSC DEPLOYED"},
        {"Time": 2000, "Message": "TRACK CLEAR"},
        {"Time": 3000, "Message": "VSC DEPLOYED"},
        {"Time": 3000, "Message": "VSC DEPLOYED"},  # actual provider duplicate
    ])
    monkeypatch.setattr(adapter, "_timestamp_to_session_ms", lambda time, _: time)

    def normalize():
        events = adapter._race_control_to_events(rows, "race", None, "fastf1")
        adapter._rebase_events(events, rebase_ms)
        return sorted(events, key=lambda item: (item.session_time_ms, item.event_id))

    events = normalize()
    fixture = tmp_path / "race.jsonl"
    fixture.write_text(dump_jsonl(events), encoding="utf-8")
    assert validate_fixture(fixture, min_laps=0, min_drivers=0) == len(events)
    restored = load_jsonl(dump_jsonl(events))
    engine = ReplayEngine(restored)
    assert engine.state_at(max(3000 - rebase_ms, 0))["session_status"] == "vsc"
    assert engine.duplicates_dropped == 2
    assert [e.payload["status"] for e in engine.events if e.type == "SessionStatusChanged"] == [
        "vsc", "started", "vsc",
    ]
    assert len([e for e in engine.events if e.type == "RaceControlMessage"]) == 3
    assert all(e.session_time_ms >= 0 for e in restored)
    assert [e.event_id for e in events] == [e.event_id for e in normalize()]
