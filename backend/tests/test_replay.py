"""Replay engine tests: correctness, determinism, dedupe, no future leakage."""
import random

from racelens.events.models import dump_jsonl, event, load_jsonl
from racelens.replay.engine import ReplayEngine

SID = "2024_mini_race"


def mini_race():
    """Synthetic 3-lap, 3-driver race: VER leads, LEC pits on lap 2 and drops to P3."""
    e = []

    # Grid + start
    e.append(event(SID, "SessionStarted", 0, total_laps=3))
    for drv, pos in [("VER", 1), ("LEC", 2), ("NOR", 3)]:
        e.append(event(SID, "PositionChanged", 0, drv, position=pos))
        e.append(event(SID, "TyreStintUpdated", 0, drv, compound="M", age_laps=0))

    # Lap 1
    e.append(event(SID, "LapCompleted", 80_000, "VER", lap=1, lap_time_ms=78_000))
    e.append(event(SID, "LapCompleted", 81_000, "LEC", lap=1, lap_time_ms=79_000))
    e.append(event(SID, "LapCompleted", 82_000, "NOR", lap=1, lap_time_ms=80_000))
    e.append(event(SID, "GapUpdated", 82_500, "LEC", gap_s=1.0))
    e.append(event(SID, "GapUpdated", 82_500, "NOR", gap_s=2.0))

    # LEC pits during lap 2, rejoins P3 on hards
    e.append(event(SID, "PitIn", 110_000, "LEC", lap=2))
    e.append(event(SID, "PitOut", 135_000, "LEC", lap=2))
    e.append(event(SID, "TyreStintUpdated", 135_000, "LEC", compound="H", age_laps=0))
    e.append(event(SID, "PositionChanged", 136_000, "LEC", position=3))
    e.append(event(SID, "PositionChanged", 136_000, "NOR", position=2))

    # Lap 2
    e.append(event(SID, "LapCompleted", 160_000, "VER", lap=2, lap_time_ms=77_500))
    e.append(event(SID, "LapCompleted", 163_000, "NOR", lap=2, lap_time_ms=80_500))
    e.append(event(SID, "LapCompleted", 168_000, "LEC", lap=2, lap_time_ms=86_000))

    # Lap 3 + finish
    e.append(event(SID, "LapCompleted", 238_000, "VER", lap=3, lap_time_ms=77_900))
    e.append(event(SID, "LapCompleted", 242_000, "NOR", lap=3, lap_time_ms=79_800))
    e.append(event(SID, "LapCompleted", 245_000, "LEC", lap=3, lap_time_ms=77_000))
    # LEC on fresh hards catches NOR — traffic scenario for the insight engine
    e.append(event(SID, "IntervalUpdated", 246_000, "LEC", interval_s=0.7))
    e.append(event(SID, "SessionStatusChanged", 250_000, status="finished"))
    return e


def test_state_before_lap_one():
    s = ReplayEngine(mini_race()).state_at(50_000)
    assert s["lap"] == 0
    assert s["session_status"] == "started"
    assert s["classification"] == ["VER", "LEC", "NOR"]
    assert all(d["tyre_compound"] == "M" for d in s["drivers"].values())
    assert s["drivers"]["LEC"]["pit_count"] == 0


def test_state_after_pit_stop():
    s = ReplayEngine(mini_race()).state_at(140_000)
    lec = s["drivers"]["LEC"]
    assert lec["pit_count"] == 1
    assert lec["in_pit"] is False          # out at 135s
    assert lec["tyre_compound"] == "H"
    assert lec["tyre_age_laps"] == 0
    assert s["classification"] == ["VER", "NOR", "LEC"]
    assert s["lap"] == 1                   # nobody finished lap 2 yet


def test_pit_out_resets_recent_stint_pace():
    before = ReplayEngine(mini_race()).state_at(120_000)
    after = ReplayEngine(mini_race()).state_at(140_000)
    assert before["drivers"]["LEC"]["recent_laps_ms"] == [79_000]
    assert after["drivers"]["LEC"]["recent_laps_ms"] == []


def test_race_restart_discards_laps_recorded_during_red_flag():
    events = [
        event(SID, "SessionStarted", 0),
        event(SID, "LapCompleted", 80_000, "VER", lap=1, lap_time_ms=80_000),
        event(SID, "SessionStatusChanged", 90_000, status="red_flag"),
        event(SID, "LapCompleted", 1_900_000, "VER", lap=2, lap_time_ms=1_820_000),
        event(SID, "SessionStatusChanged", 1_910_000, status="formation"),
        event(SID, "SessionStatusChanged", 1_920_000, status="started"),
    ]

    state = ReplayEngine(events).state_at(1_920_000)

    assert state["drivers"]["VER"]["recent_laps_ms"] == []


def test_recent_pace_ignores_restart_lap_far_slower_than_best():
    events = [
        event(SID, "SessionStarted", 0),
        event(SID, "LapCompleted", 80_000, "VER", lap=1, lap_time_ms=80_000),
        event(SID, "SessionStatusChanged", 90_000, status="red_flag"),
        event(SID, "SessionStatusChanged", 1_900_000, status="formation"),
        event(SID, "SessionStatusChanged", 1_950_000, status="started"),
        event(SID, "LapCompleted", 2_150_000, "VER", lap=2, lap_time_ms=2_070_000),
    ]

    state = ReplayEngine(events).state_at(2_150_000)

    assert state["drivers"]["VER"]["last_lap_ms"] == 2_070_000
    assert state["drivers"]["VER"]["best_lap_ms"] == 80_000
    assert state["drivers"]["VER"]["recent_laps_ms"] == []


def test_classified_leader_gap_is_always_zero():
    events = [
        event(SID, "SessionStarted", 0, total_laps=1),
        event(SID, "PositionChanged", 0, "A", position=1),
        event(SID, "PositionChanged", 0, "B", position=2),
        event(SID, "PositionChanged", 10_000, "A", position=2),
        event(SID, "PositionChanged", 10_000, "B", position=1),
        event(SID, "GapUpdated", 10_000, "B", gap_s=94.7),
    ]
    state = ReplayEngine(events).state_at(10_000)
    leader = state["classification"][0]
    assert leader == "B"
    assert state["drivers"][leader]["gap_s"] == 0.0
    assert state["drivers"][leader]["interval_s"] is None


def test_state_at_finish():
    s = ReplayEngine(mini_race()).state_at(300_000)
    assert s["session_status"] == "finished"
    assert s["lap"] == 3
    assert s["drivers"]["VER"]["best_lap_ms"] == 77_500
    assert s["drivers"]["LEC"]["best_lap_ms"] == 77_000  # fastest lap on fresh hards
    assert s["drivers"]["LEC"]["tyre_age_laps"] == 2     # laps 2 and 3 on the H set
    assert s["data_quality"]["status"] == "good"         # 50s past last event, within threshold
    late = ReplayEngine(mini_race()).state_at(400_000)
    assert late["data_quality"]["status"] == "stale"     # 150s silence → stale


def test_determinism_under_shuffle():
    events = mini_race()
    baseline = ReplayEngine(events)
    for seed in (1, 42, 1337):
        shuffled = events[:]
        random.Random(seed).shuffle(shuffled)
        engine = ReplayEngine(shuffled)
        for t in (0, 50_000, 140_000, 300_000):
            assert engine.state_hash(t) == baseline.state_hash(t)


def test_grid_position_baseline_survives_position_changes():
    """grid_position = first-known PositionChanged value; later swaps must not
    overwrite it (mid-join recordings intentionally baseline off the earliest
    seen position, not a true grid slot)."""
    s = ReplayEngine(mini_race()).state_at(300_000)
    # Grid order from mini_race(): VER=1, LEC=2, NOR=3.
    assert s["drivers"]["VER"]["grid_position"] == 1
    assert s["drivers"]["LEC"]["grid_position"] == 2
    assert s["drivers"]["NOR"]["grid_position"] == 3
    # LEC pitted and swapped with NOR at 136s — position moved, grid_position stayed.
    assert s["drivers"]["LEC"]["position"] == 3
    assert s["drivers"]["NOR"]["position"] == 2


def test_duplicate_events_dropped():
    events = mini_race()
    noisy = events + events[5:12]  # replay a chunk, as a flaky live feed would
    engine = ReplayEngine(noisy)
    clean = ReplayEngine(events)
    assert engine.duplicates_dropped == 7
    s_noisy, s_clean = engine.state_at(300_000), clean.state_at(300_000)
    assert s_noisy["drivers"] == s_clean["drivers"]      # no double-counted pits/laps
    assert s_noisy["classification"] == s_clean["classification"]


def test_no_future_leakage():
    """State at t must be identical whether or not future events exist at all."""
    events = mini_race()
    cutoff = 140_000
    full = ReplayEngine(events)
    truncated = ReplayEngine([e for e in events if e.session_time_ms <= cutoff])
    assert full.state_at(cutoff) == truncated.state_at(cutoff)


def test_retired_driver_goes_to_tail():
    """Synthetic 3-driver race: OCO retires after lap 1, leader reaches lap 6+.

    Expected: OCO is marked retired=True and appears at the end of classification;
    the two active drivers come first sorted by position.
    The gap threshold (leader_laps >= 5 and driver <= leader_laps - 5) must NOT
    trigger in mini_race (3 laps) — existing tests must still pass.
    """
    from racelens.events.models import event as mkevent

    SID2 = "2024_retired_test"
    e = []
    e.append(mkevent(SID2, "SessionStarted", 0, total_laps=10))
    for drv, pos in [("VER", 1), ("HAM", 2), ("OCO", 3)]:
        e.append(mkevent(SID2, "PositionChanged", 0, drv, position=pos))

    # OCO completes only lap 1 (retires after that)
    e.append(mkevent(SID2, "LapCompleted", 80_000, "OCO", lap=1, lap_time_ms=80_000))

    # Leader and HAM continue to lap 6
    for lap in range(1, 7):
        t = lap * 80_000
        e.append(mkevent(SID2, "LapCompleted", t, "VER", lap=lap, lap_time_ms=78_000))
        e.append(mkevent(SID2, "LapCompleted", t + 1_000, "HAM", lap=lap, lap_time_ms=79_000))

    engine = ReplayEngine(e)
    state = engine.state_at(6 * 80_000 + 5_000)

    # OCO should be retired
    assert state["drivers"]["OCO"]["retired"] is True
    assert state["drivers"]["OCO"]["retirement_inferred"] is True
    # Active drivers not retired
    assert state["drivers"]["VER"]["retired"] is False
    assert state["drivers"]["HAM"]["retired"] is False
    # OCO must appear at the tail of classification
    assert state["classification"][-1] == "OCO"
    assert "OCO" not in state["classification"][:2]


def test_no_retired_in_mini_race():
    """mini_race only has 3 laps — the 5-lap deficit never occurs; no retirements."""
    state = ReplayEngine(mini_race()).state_at(300_000)
    for drv_state in state["drivers"].values():
        assert drv_state["retired"] is False


def test_source_backed_retirement_is_not_labelled_inferred():
    events = mini_race() + [event(SID, "RetirementDetected", 200_000, "NOR", lap=3)]
    state = ReplayEngine(events).state_at(300_000)
    assert state["drivers"]["NOR"]["retired"] is True
    assert state["drivers"]["NOR"]["retirement_inferred"] is False


def test_status_since_ms():
    """status_since_ms reflects session_time_ms of the event that set the current status."""
    engine = ReplayEngine(mini_race())
    # Before any status change: SessionStarted fires at 0
    s_early = engine.state_at(50_000)
    assert s_early["session_status"] == "started"
    assert s_early["status_since_ms"] == 0

    # After finish: SessionStatusChanged fires at 250_000
    s_late = engine.state_at(300_000)
    assert s_late["session_status"] == "finished"
    assert s_late["status_since_ms"] == 250_000


def test_halted_replay_recovers_when_source_omits_restart_status():
    for halted in ("red_flag", "formation"):
        events = [
            event(SID, "SessionStarted", 0, total_laps=3),
            event(SID, "LapCompleted", 80_000, "VER", lap=1, lap_time_ms=80_000),
            event(SID, "SessionStatusChanged", 90_000, status=halted),
            event(SID, "LapCompleted", 200_000, "VER", lap=2, lap_time_ms=80_000),
        ]
        engine = ReplayEngine(events)

        assert engine.state_at(150_000)["session_status"] == halted
        resumed = engine.state_at(200_000)
        assert resumed["session_status"] == "started"
        assert resumed["status_since_ms"] == 200_000


def test_restart_time_survives_red_flag_and_clears_on_restart():
    events = [
        event(SID, "SessionStarted", 0, total_laps=3),
        event(SID, "SessionStatusChanged", 90_000, status="red_flag"),
        event(
            SID,
            "RaceControlMessage",
            100_000,
            category="Other",
            message="RACE WILL RESUME AT 15:03",
            restart_at_ms=180_000,
        ),
        event(SID, "SessionStatusChanged", 180_000, status="started"),
    ]
    engine = ReplayEngine(events)

    assert engine.state_at(150_000)["restart_at_ms"] == 180_000
    assert engine.state_at(180_000)["restart_at_ms"] is None


def test_weather_state_follows_replay_time_and_merges_source_patches():
    events = [
        event(SID, "SessionStarted", 0),
        event(SID, "WeatherUpdated", 1_000, air_temp_c=18.7, rainfall=False),
        event(SID, "WeatherUpdated", 2_000, track_temp_c=32.9),
    ]
    engine = ReplayEngine(events)

    assert engine.state_at(999)["weather"] is None
    assert engine.state_at(999)["weather_observed_at_ms"] == {}
    assert engine.state_at(1_000)["weather"] == {
        "air_temp_c": 18.7,
        "rainfall": False,
    }
    assert engine.state_at(1_000)["weather_observed_at_ms"] == {
        "air_temp_c": 1_000,
        "rainfall": 1_000,
    }
    assert engine.state_at(2_000)["weather"] == {
        "air_temp_c": 18.7,
        "rainfall": False,
        "track_temp_c": 32.9,
    }
    # A patch re-stamps only the fields it carried: earlier fields keep the
    # time the source actually reported them.
    assert engine.state_at(2_000)["weather_observed_at_ms"] == {
        "air_temp_c": 1_000,
        "rainfall": 1_000,
        "track_temp_c": 2_000,
    }


def test_jsonl_round_trip():
    events = mini_race()
    restored = load_jsonl(dump_jsonl(events))
    assert restored == events
    assert ReplayEngine(restored).state_hash(300_000) == ReplayEngine(events).state_hash(300_000)
