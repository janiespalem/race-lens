"""Tests for render_feed: event ticker for the frontend."""
from racelens.commentary.feed import render_feed
from racelens.events.models import event

from tests.test_replay import mini_race


def test_feed_newest_first():
    feed = render_feed(mini_race(), until_ms=300_000)
    assert feed == sorted(feed, key=lambda x: x["at_ms"], reverse=True)


def test_feed_ids_distinguish_same_time_radio_clips():
    events = [
        event(
            "radio",
            "RaceControlMessage",
            1_000,
            "RUS",
            category="Radio",
            message="RADIO: RUS",
            audio_url="https://audio/one.mp3",
        ),
        event(
            "radio",
            "RaceControlMessage",
            1_000,
            "RUS",
            category="Radio",
            message="RADIO: RUS",
            audio_url="https://audio/two.mp3",
        ),
    ]

    feed = render_feed(events, until_ms=1_000)

    assert len(feed) == 2
    assert len({item["id"] for item in feed}) == 2


def test_feed_spoiler_free_no_future():
    feed = render_feed(mini_race(), until_ms=100_000)
    # PitIn is at 110_000 — must not appear
    assert not any(item["kind"] == "PitIn" for item in feed)
    # All events must be <= 100_000
    assert all(item["at_ms"] <= 100_000 for item in feed)


def test_feed_pit_in_lec_present():
    feed = render_feed(mini_race(), until_ms=300_000)
    pit_items = [i for i in feed if i["kind"] == "PitIn" and i["driver_id"] == "LEC"]
    assert len(pit_items) == 1
    assert "LEC" in pit_items[0]["text"]


def test_feed_pit_out_with_compound():
    feed = render_feed(mini_race(), until_ms=300_000)
    pit_out_items = [i for i in feed if i["kind"] == "PitOut" and i["driver_id"] == "LEC"]
    assert len(pit_out_items) == 1
    text = pit_out_items[0]["text"]
    # Should mention hards
    assert "hard" in text.lower()
    assert "LEC" in text


def test_feed_pit_out_ru():
    feed = render_feed(mini_race(), until_ms=300_000, lang="ru")
    pit_out_items = [i for i in feed if i["kind"] == "PitOut" and i["driver_id"] == "LEC"]
    assert len(pit_out_items) == 1
    assert "харде" in pit_out_items[0]["text"]


def test_feed_session_finished():
    feed = render_feed(mini_race(), until_ms=300_000)
    finished = [i for i in feed if i["kind"] == "SessionStatusChanged"]
    assert len(finished) == 1
    assert "flag" in finished[0]["text"].lower() or "Chequered" in finished[0]["text"]


def test_feed_fastest_lap_lec():
    """LEC's lap 3 is 77_000 ms — should be absolute fastest in the race."""
    # Final lap (3 of 3): line crossings become finish placings, winner first.
    feed = render_feed(mini_race(), until_ms=300_000)
    fin = [i for i in feed if i["tag"] == "FINISH"]
    assert fin, "expected finish items on the final lap"
    by_time = sorted(fin, key=lambda x: x["at_ms"])
    assert "VER" in by_time[0]["text"] and "win" in by_time[0]["text"].lower()
    lec = [i for i in fin if i["driver_id"] == "LEC"]
    assert lec and "P3" in lec[0]["text"]


def test_feed_fastest_lap_before_lec_lap3():
    """Before LEC's lap 3 at 245_000 the fastest lap belongs to VER 77_500."""
    feed = render_feed(mini_race(), until_ms=244_000)
    fl_items = [i for i in feed if i["kind"] == "LapCompleted"]
    # VER's 77_500 at 160_000 should be the fastest lap
    ver_fl = [i for i in fl_items if i["driver_id"] == "VER"]
    assert any("VER" in i["text"] for i in ver_fl)


def test_feed_limit():
    feed_30 = render_feed(mini_race(), until_ms=300_000, limit=30)
    feed_2 = render_feed(mini_race(), until_ms=300_000, limit=2)
    assert len(feed_2) <= 2
    assert len(feed_30) >= len(feed_2)


def test_feed_no_position_changed_or_gap_noise():
    feed = render_feed(mini_race(), until_ms=300_000)
    noisy_kinds = {"PositionChanged", "GapUpdated", "IntervalUpdated"}
    assert not any(i["kind"] in noisy_kinds for i in feed)


def test_feed_tag_all_items_have_tag():
    feed = render_feed(mini_race(), until_ms=300_000)
    valid_tags = {"PIT", "FLAG", "FASTEST", "FINISH", "INFO", "PASS", "STEWARDS"}
    for item in feed:
        assert "tag" in item, f"Missing tag on item: {item}"
        assert item["tag"] in valid_tags, f"Unknown tag {item['tag']!r} on item: {item}"


def test_feed_tag_pit_items():
    feed = render_feed(mini_race(), until_ms=300_000)
    pit_items = [i for i in feed if i["kind"] in ("PitIn", "PitOut")]
    assert all(i["tag"] == "PIT" for i in pit_items), "PitIn/PitOut must have tag=PIT"


def test_feed_tag_fastest_lap():
    feed = render_feed(mini_race(), until_ms=300_000)
    fl_items = [i for i in feed if i["kind"] == "LapCompleted"]
    # LapCompleted is FASTEST mid-race, FINISH on the final lap.
    assert all(i["tag"] in ("FASTEST", "FINISH") for i in fl_items)


def test_feed_tag_session_status_flag():
    feed = render_feed(mini_race(), until_ms=300_000)
    status_items = [i for i in feed if i["kind"] == "SessionStatusChanged"]
    assert all(i["tag"] == "FLAG" for i in status_items), "SessionStatusChanged must have tag=FLAG"


def test_feed_status_texts_en():
    feed = render_feed(mini_race(), until_ms=300_000)
    finished = [i for i in feed if i["kind"] == "SessionStatusChanged"]
    assert any("Chequered flag" in i["text"] and "race complete" in i["text"] for i in finished)


def test_feed_status_texts_ru():
    feed = render_feed(mini_race(), until_ms=300_000, lang="ru")
    finished = [i for i in feed if i["kind"] == "SessionStatusChanged"]
    assert any("Клетчатый флаг" in i["text"] and "финиш" in i["text"] for i in finished)


def test_feed_session_started_lights_out():
    feed = render_feed(mini_race(), until_ms=300_000)
    started = [i for i in feed if i["kind"] == "SessionStarted"]
    assert len(started) == 1
    assert started[0]["text"] == "Lights out — race start!"
    assert started[0]["tag"] == "FLAG"
    assert started[0]["driver_id"] is None


def test_feed_session_started_ru():
    feed = render_feed(mini_race(), until_ms=300_000, lang="ru")
    started = [i for i in feed if i["kind"] == "SessionStarted"]
    assert len(started) == 1
    assert "старт" in started[0]["text"].lower()


def test_red_flag_feed_keeps_incident_visible_and_reports_stopped_driver():
    events = [
        event("race", "SessionStarted", 0),
        event(
            "race", "RaceControlMessage", 100_000, category="Other",
            message="TURN 2 INCIDENT INVOLVING CARS 16 (LEC) AND 44 (HAM)",
        ),
        event("race", "DriverStoppedChanged", 110_000, "LEC", stopped=True),
        event("race", "SessionStatusChanged", 120_000, status="red_flag"),
        *(event("race", "PitIn", 130_000 + i, f"D{i}", lap=3) for i in range(22)),
    ]

    feed = render_feed(events, until_ms=200_000, limit=30)

    assert not any(item["kind"] == "PitIn" for item in feed)
    assert any("INCIDENT" in item["text"] for item in feed)
    assert any(item["driver_id"] == "LEC" and "stopped" in item["text"] for item in feed)


def test_feed_keeps_official_steward_penalty_and_reason():
    message = (
        "FIA STEWARDS: 5 SECOND TIME PENALTY FOR CAR 11 (PER) - "
        "FAILING TO FOLLOW RACE DIRECTORS INSTRUCTIONS – ESCAPE ROAD INSTRUCTIONS"
    )
    events = [
        event(
            "race",
            "RaceControlMessage",
            1_000,
            lap=30,
            category="Other",
            message=message,
        ),
    ]

    feed = render_feed(events, until_ms=1_000)

    assert [(item["tag"], item["text"]) for item in feed] == [
        ("STEWARDS", message),
    ]


def test_feed_labels_official_investigation_update_as_stewards():
    message = (
        "FIA STEWARDS: INCIDENT INVOLVING CAR 63 (RUS) NO FURTHER ACTION - "
        "YELLOW FLAG INFRINGEMENT"
    )
    events = [
        event(
            "race",
            "RaceControlMessage",
            1_000,
            lap=40,
            category="Other",
            message=message,
        ),
    ]

    feed = render_feed(events, until_ms=1_000)

    assert [(item["tag"], item["text"]) for item in feed] == [
        ("STEWARDS", message),
    ]
