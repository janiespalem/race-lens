from __future__ import annotations

import pytest

from racelens.events.models import event
from racelens.race_control import classify_race_control, reduce_race_control, legacy_session_status
from racelens.replay.engine import ReplayEngine


def flag(text: str, at_ms: int, **fields):
    return event("race", "RaceControlMessage", at_ms, category="Flag", message=text, **fields)


@pytest.mark.parametrize(("messages", "phase", "mode", "sectors"), [
    ([("FORMATION LAP", 10), ("STANDING START", 20)], "running", "green", {}),
    ([("YELLOW IN TRACK SECTOR 14", 10), ("CLEAR IN TRACK SECTOR 14", 20)], "running", "green", {}),
    ([("YELLOW IN TRACK SECTOR 14", 10), ("DOUBLE YELLOW IN TRACK SECTOR 14", 20)], "running", "green", {"14": "double_yellow"}),
    ([("YELLOW IN TRACK SECTOR 14", 10), ("DOUBLE YELLOW IN TRACK SECTOR 15", 20), ("CLEAR IN TRACK SECTOR 14", 30)], "running", "green", {"15": "double_yellow"}),
    ([("SAFETY CAR DEPLOYED", 10), ("SAFETY CAR IN THIS LAP", 20)], "running", "safety_car", {}),
    ([("VSC DEPLOYED", 10), ("VSC ENDING", 20)], "running", "vsc", {}),
    ([("RED FLAG", 10), ("TRACK CLEAR", 20)], "running", "red_flag", {}),
    ([("RED FLAG", 10), ("GREEN FLAG", 20)], "running", "green", {}),
    ([("RED FLAG", 10), ("GREEN LIGHT - PIT EXIT OPEN", 20)], "running", "red_flag", {}),
    ([("RED FLAG", 10), ("GREEN FLAG", 20, {"scope": "Sector", "sector": 14})], "running", "red_flag", {}),
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
