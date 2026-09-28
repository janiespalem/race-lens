from __future__ import annotations

import pytest
import json
from pathlib import Path

from racelens.events.models import event
from racelens.race_control import (
    classify_race_control, reduce_race_control, legacy_session_status,
    normalized_source_fields,
)
from racelens.replay.engine import ReplayEngine


def flag(text: str, at_ms: int, **fields):
    return event("race", "RaceControlMessage", at_ms, category="Flag", message=text, **fields)


@pytest.mark.parametrize(("messages", "phase", "mode", "sectors"), [
    ([("FORMATION LAP", 10), ("STANDING START", 20)], "running", "green", {}),
    ([("YELLOW IN TRACK SECTOR 14", 10), ("CLEAR IN TRACK SECTOR 14", 20)], "running", "green", {}),
    ([("YELLOW IN TRACK SECTOR 14", 10), ("DOUBLE YELLOW IN TRACK SECTOR 14", 20)], "running", "green", {"14": "double_yellow"}),
    ([("YELLOW IN TRACK SECTOR 14", 10), ("DOUBLE YELLOW IN TRACK SECTOR 14", 20), ("CLEAR IN TRACK SECTOR 14", 30)], "running", "green", {}),
    ([("YELLOW IN TRACK SECTOR 14", 10), ("DOUBLE YELLOW IN TRACK SECTOR 15", 20), ("CLEAR IN TRACK SECTOR 14", 30)], "running", "green", {"15": "double_yellow"}),
    ([("SAFETY CAR DEPLOYED", 10), ("SAFETY CAR IN THIS LAP", 20)], "running", "safety_car", {}),
    ([("SAFETY CAR DEPLOYED", 10), ("SAFETY CAR IN THIS LAP", 20), ("TRACK CLEAR", 30)], "running", "green", {}),
    ([("VSC DEPLOYED", 10), ("VSC ENDING", 20)], "running", "vsc", {}),
    ([("VSC DEPLOYED", 10), ("VSC ENDING", 20), ("TRACK CLEAR", 30)], "running", "green", {}),
    ([("RED FLAG", 10), ("TRACK CLEAR", 20)], "running", "red_flag", {}),
    ([("RED FLAG", 10), ("GREEN FLAG", 20)], "running", "green", {}),
    ([("RED FLAG", 10), ("GREEN LIGHT - PIT EXIT OPEN", 20)], "running", "red_flag", {}),
    ([("RED FLAG", 10), ("GREEN FLAG", 20, {"scope": "Sector", "sector": 14})], "running", "red_flag", {}),
    ([("RED FLAG", 10), ("STANDING START", 20, {"scope": "Sector", "sector": 14})], "running", "red_flag", {}),
])
def test_control_scenarios(messages, phase, mode, sectors):
    events = [event("race", "SessionStarted", 0)]
    events += [flag(row[0], row[1], **(row[2] if len(row) > 2 else {})) for row in messages]
    state = ReplayEngine(events).state_at(30)
    assert (state["session_phase"], state["control_mode"], state["sector_flags"]) == (phase, mode, sectors)


@pytest.mark.parametrize("mode,message", [
    ("green", "CHEQUERED FLAG"),
    ("safety_car", "SAFETY CAR DEPLOYED"),
    ("vsc", "VSC DEPLOYED"),
    ("red_flag", "RED FLAG"),
])
def test_finish_freezes_mode(mode, message):
    events = [event("race", "SessionStarted", 0)]
    if mode != "green":
        events.append(flag(message, 10))
    events += [flag("CHEQUERED FLAG", 20), flag("TRACK CLEAR", 30), flag("CHEQUERED FLAG", 40), event("race", "SessionStarted", 50)]
    state = ReplayEngine(events).state_at(50)
    assert state["session_phase"] == "finished"
    assert state["finish_condition"] == {"at_ms": 20, "control_mode": mode, "sector_flags": {}}
    assert state["session_status"] == "finished"


def test_azerbaijan_finish_keeps_independent_marshal_sectors():
    messages = [
        (5_821_289, "YELLOW IN TRACK SECTOR 14"),
        (5_821_289, "DOUBLE YELLOW IN TRACK SECTOR 15"),
        (5_831_289, "YELLOW IN TRACK SECTOR 20"),
        (5_831_289, "YELLOW IN TRACK SECTOR 21"),
        (5_881_289, "CHEQUERED FLAG"),
        (5_892_289, "CLEAR IN TRACK SECTOR 14"),
        (5_892_290, "DOUBLE YELLOW IN TRACK SECTOR 26"),
    ]
    replay = ReplayEngine([event("race", "SessionStarted", 0)] + [flag(text, at) for at, text in messages], snapshot_interval=2)
    before = replay.state_at(5_831_289)
    expected = {"14": "yellow", "15": "double_yellow", "20": "yellow", "21": "yellow"}
    assert before["sector_flags"] == expected
    assert before["finish_condition"] is None
    finish = replay.state_at(5_881_289)
    assert finish["session_phase"] == "finished"
    assert finish["finish_condition"] == {"at_ms": 5_881_289, "control_mode": "green", "sector_flags": expected}
    assert replay.state_at(5_892_290)["finish_condition"] == finish["finish_condition"]
    assert replay.state_at(5_831_289)["finish_condition"] is None


def test_classifier_is_idempotent_and_unknown_is_noop():
    action = classify_race_control({"category": "Flag", "message": "DOUBLE YELLOW IN TRACK SECTOR 15"})
    first = reduce_race_control(None, action, 10)
    second = reduce_race_control(first, action, 20)
    assert second == first
    assert legacy_session_status(second) == "unknown"
    assert reduce_race_control(second, classify_race_control({"category": "Flag", "message": "UNSEEN FLAG"}), 30) == second


def test_source_sector_accepts_decimal_provider_values_only():
    assert normalized_source_fields({"Sector": "15", "Flag": "YELLOW", "Scope": "Sector"}) == {
        "sector": 15, "flag": "YELLOW", "scope": "Sector",
    }
    assert normalized_source_fields({"Sector": 15.0}) == {"sector": 15}
    assert normalized_source_fields({"Sector": "15x"}) == {}
    assert normalized_source_fields({"Sector": float("inf")}) == {}


def test_structured_global_flag_classifies_without_legacy_message_spelling():
    assert classify_race_control({"message": "SC DEPLOYED", "flag": "SAFETY CAR", "scope": "Track"}).value == "safety_car"
    assert classify_race_control({"message": "SAFETY CAR IN THIS LAP", "flag": "SAFETY CAR", "scope": "Track"}).kind == "noop"


def test_fastf1_preserves_structured_flag_evidence(monkeypatch):
    from racelens.adapters import fastf1_adapter

    class Rows(list):
        def iterrows(self):
            return enumerate(self)

    monkeypatch.setattr(fastf1_adapter, "_timestamp_to_session_ms", lambda value, _: value)
    events = fastf1_adapter._race_control_to_events(Rows([{
        "Time": 10, "Category": "Flag", "Message": "DOUBLE YELLOW IN TRACK SECTOR 15",
        "Flag": "DOUBLE YELLOW", "Scope": "Sector", "Sector": 15,
    }]), "race", None, "fastf1")
    assert events[0].payload == {"category": "Flag", "message": "DOUBLE YELLOW IN TRACK SECTOR 15", "flag": "DOUBLE YELLOW", "scope": "Sector", "sector": 15}
    assert ReplayEngine(events).state_at(10)["sector_flags"] == {"15": "double_yellow"}


def test_openf1_preserves_structured_flag_evidence():
    from racelens.adapters.openf1_adapter import _race_control_to_events

    events = _race_control_to_events([{
        "date": "2024-01-01T00:00:01Z", "category": "Flag",
        "message": "DOUBLE YELLOW IN TRACK SECTOR 15", "flag": "DOUBLE YELLOW",
        "scope": "Sector", "sector": 15,
    }], "race", lambda _: 10, event)
    assert events[0].payload == {"category": "Flag", "message": "DOUBLE YELLOW IN TRACK SECTOR 15", "flag": "DOUBLE YELLOW", "scope": "Sector", "sector": 15}


def test_f1live_preserves_structured_flag_evidence(tmp_path):
    from racelens.adapters.f1live_adapter import ingest_f1live

    rows = [
        ["SessionStatus", {"Status": "Started"}, "2026-07-05T15:00:00Z"],
        ["RaceControlMessages", {"Messages": [{
            "Utc": "2026-07-05T15:00:01Z", "Category": "Flag",
            "Message": "DOUBLE YELLOW IN TRACK SECTOR 15", "Flag": "DOUBLE YELLOW",
            "Scope": "Sector", "Sector": 15,
        }]}, "2026-07-05T15:00:01Z"],
    ]
    source = tmp_path / "live.txt"
    source.write_text("\n".join(map(repr, rows)) + "\n", encoding="utf-8")
    events = ingest_f1live(str(source), session_id="race")
    race_control_event = next(item for item in events if item.type == "RaceControlMessage")
    payload = race_control_event.payload
    assert race_control_event.source == "f1live"
    assert payload == {"category": "Flag", "message": "DOUBLE YELLOW IN TRACK SECTOR 15", "flag": "DOUBLE YELLOW", "scope": "Sector", "sector": 15}
    assert any(item.payload.get("evidence") == "direct" for item in events if item.type == "SessionStatusChanged")


def test_fixture_flag_corpus_has_no_unclassified_patterns():
    failures = []
    patterns = set()
    for path in sorted((Path(__file__).parents[1] / "fixtures").glob("*.jsonl")):
        with path.open(encoding="utf-8") as fixture:
            for line_number, line in enumerate(fixture, 1):
                row = json.loads(line)
                if row.get("type") != "RaceControlMessage" or row.get("payload", {}).get("category") != "Flag":
                    continue
                payload = row["payload"]
                patterns.add(payload.get("message"))
                if classify_race_control(payload).kind == "unknown":
                    failures.append(f"{path.name}:{line_number}: {payload.get('message')}")
    assert patterns
    assert not failures, "\n".join(failures)


def test_duplicate_control_and_local_flags_keep_transition_timestamps():
    events = [event("race", "SessionStarted", 0), flag("SAFETY CAR DEPLOYED", 10),
              flag("SAFETY CAR DEPLOYED", 20), flag("YELLOW IN TRACK SECTOR 14", 25),
              flag("YELLOW IN TRACK SECTOR 14", 30)]
    state = ReplayEngine(events).state_at(30)
    assert state["status_since_ms"] == 10
    assert state["control_since_ms"] == 10
    assert state["sector_flags"] == {"14": "yellow"}


def test_reconnect_replay_deduplicates_race_control_events():
    deployed = flag("RED FLAG", 10)
    original = ReplayEngine([event("race", "SessionStarted", 0), deployed])
    reconnected = ReplayEngine([event("race", "SessionStarted", 0), deployed, deployed])
    assert reconnected.duplicates_dropped == 1
    fields = ("session_phase", "control_mode", "control_since_ms", "sector_flags",
              "finish_condition", "session_status", "status_since_ms")
    assert tuple(reconnected.state_at(20)[key] for key in fields) == tuple(
        original.state_at(20)[key] for key in fields
    )


def test_sector_green_is_local_and_global_control_keeps_other_sectors():
    events = [event("race", "SessionStarted", 0),
              flag("YELLOW IN TRACK SECTOR 14", 10),
              flag("DOUBLE YELLOW IN TRACK SECTOR 15", 20),
              flag("SAFETY CAR DEPLOYED", 30),
              flag("GREEN FLAG IN TRACK SECTOR 14", 40),
              flag("VSC DEPLOYED", 50),
              flag("RED FLAG", 60)]
    replay = ReplayEngine(events)
    for at_ms, mode in ((30, "safety_car"), (50, "vsc"), (60, "red_flag")):
        assert replay.state_at(at_ms)["control_mode"] == mode
        assert replay.state_at(at_ms)["sector_flags"].get("15") == "double_yellow"
    assert replay.state_at(40)["sector_flags"] == {"15": "double_yellow"}


def test_malformed_structured_message_cannot_mutate_projection():
    events = [event("race", "SessionStarted", 0),
              flag("YELLOW IN TRACK SECTOR 14", 10, sector="bad"),
              flag("YELLOW IN TRACK SECTOR 15", 20, sector=14)]
    state = ReplayEngine(events).state_at(20)
    assert state["sector_flags"] == {}
    assert state["session_status"] == "started"


def test_equivalent_source_sequences_have_identical_projection(monkeypatch, tmp_path):
    from racelens.adapters import fastf1_adapter
    from racelens.adapters.f1live_adapter import ingest_f1live
    from racelens.adapters.openf1_adapter import _race_control_to_events

    sequence = [
        ("SAFETY CAR DEPLOYED", "SAFETY CAR", "Track", None),
        ("DOUBLE YELLOW IN TRACK SECTOR 15", "DOUBLE YELLOW", "Sector", 15),
        ("TRACK CLEAR", "GREEN", "Track", None),
        ("RED FLAG", "RED", "Track", None),
        ("GREEN FLAG", "GREEN", "Track", None),
        ("CHEQUERED FLAG", "CHEQUERED", "Track", None),
    ]
    class Rows(list):
        def iterrows(self):
            return enumerate(self)

    fast_rows = Rows()
    open_rows = []
    live_rows = [["SessionStatus", {"Status": "Started"}, "2024-01-01T00:00:00Z"]]
    for second, (message, flag_name, scope, sector) in enumerate(sequence, 1):
        utc = f"2024-01-01T00:00:{second:02d}Z"
        fields = {"Flag": flag_name, "Scope": scope}
        if sector is not None:
            fields["Sector"] = sector
        fast_rows.append({"Time": second * 1000, "Category": "Flag", "Message": message, **fields})
        open_rows.append({"date": utc, "category": "Flag", "message": message,
                          "flag": flag_name, "scope": scope, **({"sector": sector} if sector else {})})
        live_rows.append(["RaceControlMessages", {"Messages": [{"Utc": utc, "Category": "Flag",
                           "Message": message, **fields}]}, utc])
    monkeypatch.setattr(fastf1_adapter, "_timestamp_to_session_ms", lambda value, _: value)
    fast_events = [event("race", "SessionStarted", 0)] + fastf1_adapter._race_control_to_events(fast_rows, "race", None, "fastf1")
    open_events = [event("race", "SessionStarted", 0)] + _race_control_to_events(
        open_rows, "race", lambda epoch: round((epoch - 1704067200) * 1000), event,
    )
    source = tmp_path / "live.txt"
    source.write_text("\n".join(map(repr, live_rows)) + "\n", encoding="utf-8")
    live_events = ingest_f1live(str(source), session_id="race")
    projections = []
    for events in (fast_events, open_events, live_events):
        state = ReplayEngine(events).state_at(6000)
        projections.append(tuple(state[key] for key in (
            "session_phase", "control_mode", "control_since_ms", "sector_flags", "finish_condition",
        )))
    assert projections[0] == projections[1] == projections[2]
