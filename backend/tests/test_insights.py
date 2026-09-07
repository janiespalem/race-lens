from racelens.insights.traffic import detect_traffic_risk
from racelens.insights.registry import detect_all
from racelens.replay.engine import ReplayEngine

from tests.test_replay import mini_race


def test_traffic_risk_detected_at_finish():
    state = ReplayEngine(mini_race()).state_at(247_000)
    state["drivers"]["LEC"]["recent_laps_ms"] = [77_200, 77_100, 77_000]
    state["drivers"]["NOR"]["recent_laps_ms"] = [80_000, 79_900, 79_800]
    found = detect_traffic_risk(state)
    assert len(found) == 1
    ins = found[0]
    assert ins["type"] == "TRAFFIC_RISK_HIGH"          # LEC 2.8s/lap faster than NOR
    assert ins["driver_ids"] == ["LEC", "NOR"]
    assert ins["evidence"]["interval_s"] == 0.7
    assert ins["evidence"]["pace_delta_ms"] == 2_800


def test_no_traffic_risk_without_interval_data():
    state = ReplayEngine(mini_race()).state_at(140_000)
    assert detect_traffic_risk(state) == []


def test_traffic_risk_waits_for_three_clean_laps():
    state = {
        "at_ms": 2_000_000,
        "lap": 5,
        "classification": ["NOR", "LEC"],
        "drivers": {
            "NOR": {
                "interval_s": None,
                "last_lap_ms": 82_000,
                "recent_laps_ms": [1_900_000],
                "in_pit": False,
                "retired": False,
            },
            "LEC": {
                "interval_s": 0.7,
                "last_lap_ms": 79_000,
                "recent_laps_ms": [79_000],
                "in_pit": False,
                "retired": False,
            },
        },
    }

    assert detect_traffic_risk(state) == []


def test_insights_are_deterministic_and_spoiler_free():
    engine = ReplayEngine(mini_race())
    early = detect_traffic_risk(engine.state_at(100_000))
    assert early == []                                  # no future leakage into earlier laps
    assert detect_traffic_risk(engine.state_at(247_000)) == detect_traffic_risk(
        engine.state_at(247_000)
    )


def test_registry_only_emits_neutralization_insights_under_safety_car():
    state = {
        "at_ms": 800_000,
        "lap": 20,
        "total_laps": 60,
        "session_status": "safety_car",
        "classification": ["A", "B"],
        "drivers": {
            "A": {
                "position": 1, "gap_s": None, "interval_s": None,
                "last_lap_ms": 80_000, "tyre_age_laps": 15,
                "in_pit": False, "retired": False,
            },
            "B": {
                "position": 2, "gap_s": 0.8, "interval_s": 0.8,
                "last_lap_ms": 79_000, "tyre_age_laps": 15,
                "in_pit": False, "retired": False,
            },
        },
    }

    assert {item["type"] for item in detect_all(state)} == {"SC_PIT_WINDOW"}
    state["session_status"] = "vsc"
    assert detect_all(state) == []
