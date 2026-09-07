from racelens.commentary.renderer import render, render_all

TRAFFIC = {
    "insight_id": "traffic:LEC:1", "type": "TRAFFIC_RISK_HIGH", "severity": "high",
    "lap": 24, "driver_ids": ["LEC", "ALO"],
    "evidence": {"interval_s": 0.8, "pace_delta_ms": 460,
                 "behind_last_lap_ms": 77000, "ahead_last_lap_ms": 77460},
}
UNDERCUT = {
    "insight_id": "undercut:VER:1", "type": "UNDERCUT_RISK_MEDIUM", "severity": "medium",
    "lap": 40, "driver_ids": ["VER", "NOR"],
    "evidence": {"interval_s": 2.4, "attacker_tyre_age_laps": 18,
                 "defender_tyre_age_laps": 20, "pace_delta_ms": 100},
}
BATTLE = {
    "insight_id": "battle:NOR:VER:1", "type": "BATTLE_DETECTED",
    "severity": "medium", "lap": 20, "driver_ids": ["NOR", "VER"],
    "evidence": {"interval_s": 0.7, "positions": [1, 2]},
}


def test_en_pro_uses_evidence_numbers():
    text = render(TRAFFIC, "en", "pro")
    assert "LEC" in text and "ALO" in text
    assert "0.5s/lap" in text and "0.8s" in text


def test_ru_beginner_explains_without_jargon():
    text = render(TRAFFIC, "ru", "beginner")
    assert "LEC" in text and "заперт" in text


def test_undercut_names_attacker_and_defender():
    text = render(UNDERCUT, "ru", "pro")
    assert "VER" in text and "NOR" in text and "2.4" in text


def test_model_label_distinguishes_strategy_projection_from_observed_battle():
    assert render(UNDERCUT, "en", "pro").startswith("MODEL · ")
    assert not render(BATTLE, "en", "pro").startswith("MODEL · ")


def test_unknown_lang_falls_back_to_en_pro():
    assert render(TRAFFIC, "de", "pro") == render(TRAFFIC, "en", "pro")


def test_render_all_keeps_severity_and_lap():
    out = render_all([TRAFFIC, UNDERCUT], "en", "beginner")
    assert [o["severity"] for o in out] == ["high", "medium"]
    assert all(o["text"] for o in out)
