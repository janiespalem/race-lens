from racelens.insights.undercut import detect_undercut_risk


def _state(rows: dict[str, tuple[float | None, int, int]]) -> dict:
    """rows: driver → (interval_s, tyre_age_laps, last_lap_ms)"""
    return {
        "at_ms": 3_000_000,
        "lap": 40,
        "classification": list(rows),
        "drivers": {
            d: {
                "interval_s": iv,
                "tyre_age_laps": age,
                "last_lap_ms": lap_ms,
                "recent_laps_ms": [lap_ms, lap_ms, lap_ms],
                "in_pit": False,
            }
            for d, (iv, age, lap_ms) in rows.items()
        },
    }


def test_close_old_tyres_high_risk():
    s = _state({"NOR": (None, 22, 78_000), "VER": (0.8, 20, 78_100)})
    found = detect_undercut_risk(s)
    assert len(found) == 1
    assert found[0]["type"] == "UNDERCUT_RISK_HIGH"
    assert found[0]["driver_ids"] == ["VER", "NOR"]  # attacker first


def test_medium_risk_at_wider_interval():
    s = _state({"NOR": (None, 22, 78_000), "VER": (3.0, 20, 78_000)})
    assert detect_undercut_risk(s)[0]["severity"] == "medium"


def test_fresh_tyres_no_undercut():
    s = _state({"NOR": (None, 22, 78_000), "VER": (1.2, 4, 78_000)})
    assert detect_undercut_risk(s) == []


def test_slow_attacker_is_no_threat():
    s = _state({"NOR": (None, 22, 78_000), "VER": (1.2, 20, 79_500)})
    assert detect_undercut_risk(s) == []


def test_undercut_waits_for_three_clean_laps():
    state = _state({"NOR": (None, 22, 78_000), "VER": (0.8, 20, 78_100)})
    state["drivers"]["NOR"]["recent_laps_ms"] = [1_900_000]
    state["drivers"]["VER"]["recent_laps_ms"] = [78_100]

    assert detect_undercut_risk(state) == []
