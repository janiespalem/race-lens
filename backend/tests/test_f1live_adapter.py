"""Tests for the direct SignalR feed → Event adapter (live team-radio bits)."""
import json

import pytest

from racelens.adapters.f1live_adapter import ingest_f1live
from racelens.commentary.feed import render_feed
from racelens.replay.engine import ReplayEngine

_SESSION_PATH = "2026/2026-07-05_British_Grand_Prix/2026-07-05_Race/"
_RADIO_PATH = "TeamRadio/HAMILTONLEWIS_44_20260705_1505.mp3"

_LINES = [
    # Keyframe: session info carries the static-file path prefix.
    "['SessionInfo', '{\"Path\": \"%s\"}', '']" % _SESSION_PATH,
    # Timestamped SessionStatus 'Started' anchors t0.
    "['SessionStatus', {'Status': 'Started'}, '2026-07-05T15:00:00.000Z']",
    # Driver number → abbreviation.
    "['DriverList', {'44': {'Tla': 'HAM'}}, '2026-07-05T15:00:01.000Z']",
    # Team radio capture for HAM.
    (
        "['TeamRadio', {'Captures': {'0': {'Utc': '2026-07-05T15:05:00.000Z', "
        "'RacingNumber': '44', 'Path': '%s'}}}, '2026-07-05T15:05:00.500Z']" % _RADIO_PATH
    ),
]


def _write_feed(tmp_path):
    feed = tmp_path / "feed.txt"
    feed.write_text("\n".join(_LINES) + "\n", encoding="utf-8")
    return str(feed)


def test_team_radio_gets_absolute_audio_url_and_lap(tmp_path):
    events = ingest_f1live(_write_feed(tmp_path), session_id="test")
    radio = [e for e in events if e.payload.get("category") == "Radio"]
    assert len(radio) == 1
    r = radio[0]
    assert r.driver_id == "HAM"
    assert r.lap == 1
    assert r.session_time_ms == 300_000
    assert r.payload["audio_path"] == _RADIO_PATH
    assert r.payload["audio_url"] == f"https://livetiming.formula1.com/static/{_SESSION_PATH}{_RADIO_PATH}"


def test_render_feed_carries_audio_url(tmp_path):
    events = ingest_f1live(_write_feed(tmp_path), session_id="test")
    feed = render_feed(events, until_ms=10_000_000)
    radio_items = [i for i in feed if i.get("audio_url")]
    assert len(radio_items) == 1
    assert radio_items[0]["audio_url"].startswith("https://livetiming.formula1.com/static/")


# ── Session badge: Meeting.Location + session Name → state["session_name"] ────

_SESSION_INFO_PAYLOAD = {
    "Meeting": {"Name": "British Grand Prix", "Location": "Silverstone"},
    "Name": "Race",
    "Path": _SESSION_PATH,
}


def _write_session_info_feed(tmp_path):
    lines = [
        "['SessionInfo', %s, '']" % json.dumps(json.dumps(_SESSION_INFO_PAYLOAD)),
        "['SessionStatus', {'Status': 'Started'}, '2026-07-05T15:00:00.000Z']",
    ]
    feed = tmp_path / "feed.txt"
    feed.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(feed)


def test_session_started_carries_session_name(tmp_path):
    events = ingest_f1live(_write_session_info_feed(tmp_path), session_id="test")
    started = [e for e in events if e.type == "SessionStarted"]
    assert len(started) == 1
    assert started[0].payload.get("session_name") == "SILVERSTONE · RACE"


def test_session_name_lands_in_engine_state(tmp_path):
    events = ingest_f1live(_write_session_info_feed(tmp_path), session_id="test")
    state = ReplayEngine(events).state_at(0)
    assert state["session_name"] == "SILVERSTONE · RACE"


def test_session_name_absent_when_no_session_info(tmp_path):
    """Feeds without Meeting/Name (e.g. the radio fixture above, which only
    carries Path) must not crash and leave session_name unset."""
    events = ingest_f1live(_write_feed(tmp_path), session_id="test")
    state = ReplayEngine(events).state_at(0)
    assert state["session_name"] is None


def test_weather_data_is_normalized_and_malformed_values_are_ignored(tmp_path):
    lines = [
        "['SessionStatus', {'Status': 'Started'}, '2026-08-23T13:00:00Z']",
        repr([
            "WeatherData",
            {
                "AirTemp": "18.7",
                "TrackTemp": "32.9",
                "Humidity": "56.2",
                "Pressure": "1024.6",
                "Rainfall": "0",
                "WindDirection": "96",
                "WindSpeed": "2.2",
            },
            "2026-08-23T13:01:00Z",
        ]),
        repr([
            "WeatherData",
            {"AirTemp": "not-a-number", "Rainfall": "maybe"},
            "2026-08-23T13:02:00Z",
        ]),
    ]
    feed = tmp_path / "weather.txt"
    feed.write_text("\n".join(lines) + "\n", encoding="utf-8")

    weather = [
        item for item in ingest_f1live(str(feed), session_id="test")
        if item.type == "WeatherUpdated"
    ]

    assert len(weather) == 1
    assert weather[0].session_time_ms == 60_000
    assert weather[0].payload == {
        "air_temp_c": 18.7,
        "track_temp_c": 32.9,
        "humidity_percent": 56.2,
        "pressure_mbar": 1024.6,
        "rainfall": False,
        "wind_direction_deg": 96.0,
        "wind_speed_mps": 2.2,
    }


@pytest.mark.parametrize(("field", "value"), [
    ("AirTemp", "-50.1"), ("AirTemp", "70.1"),
    ("TrackTemp", "-50.1"), ("TrackTemp", "100.1"),
    ("Humidity", "-0.1"), ("Humidity", "100.1"),
    ("Pressure", "699.9"), ("Pressure", "1100.1"),
    ("WindDirection", "-0.1"), ("WindDirection", "360.1"),
    ("WindSpeed", "-0.1"), ("WindSpeed", "100.1"),
])
def test_weather_data_ignores_out_of_range_fields_but_keeps_valid_patch(
    tmp_path, field, value,
):
    fallback = {"Pressure": "1024.6"} if field != "Pressure" else {"AirTemp": "18.7"}
    expected = {"pressure_mbar": 1024.6} if field != "Pressure" else {"air_temp_c": 18.7}
    feed = tmp_path / "weather-bounds.txt"
    feed.write_text("\n".join([
        "['SessionStatus', {'Status': 'Started'}, '2026-08-23T13:00:00Z']",
        repr([
            "WeatherData",
            {field: value, **fallback},
            "2026-08-23T13:01:00Z",
        ]),
    ]) + "\n", encoding="utf-8")

    weather = next(
        item for item in ingest_f1live(str(feed), session_id="test")
        if item.type == "WeatherUpdated"
    )
    assert weather.payload == expected


def test_new_session_identity_drops_previous_session_rows(tmp_path):
    previous = {
        "Meeting": {"Key": 1, "Name": "Hungarian Grand Prix", "Location": "Budapest"},
        "Key": 10, "Name": "Race", "StartDate": "2026-07-26T15:00:00",
    }
    current = {
        "Meeting": {"Key": 2, "Name": "Belgian Grand Prix", "Location": "Spa"},
        "Key": 20, "Name": "Race", "StartDate": "2026-08-02T15:00:00",
    }
    feed = tmp_path / "switched.txt"
    feed.write_text("\n".join([
        repr(["DriverList", {"1": {"Tla": "OLD"}, "3": {"Tla": "VER"}}, ""]),
        repr(["SessionInfo", json.dumps(previous), ""]),
        repr(["SessionStatus", {"Status": "Started"}, "2026-07-26T13:00:00Z"]),
        repr(["TimingData", {"Lines": {"1": {"Position": "1"}}}, "2026-07-26T13:01:00Z"]),
        repr(["SessionInfo", json.dumps(current), ""]),
        repr(["SessionStatus", {"Status": "Started"}, "2026-08-02T13:00:00Z"]),
        repr(["TimingData", {"Lines": {"3": {"Position": "1"}}}, "2026-08-02T13:01:00Z"]),
    ]) + "\n", encoding="utf-8")

    events = ingest_f1live(str(feed), session_id="current")

    assert any(item.driver_id == "VER" for item in events)
    assert all(item.driver_id != "OLD" for item in events)


def test_keyframe_is_anchored_to_first_live_timestamp(tmp_path):
    lines = [
        "['SessionData', {\"StatusSeries\": [{\"SessionStatus\": \"Started\", "
        "\"Utc\": \"2026-07-05T15:00:00.000Z\"}]}, '']",
        "['DriverList', {'44': {'Tla': 'HAM'}}, '']",
        "['TimingAppData', {'Lines': {'44': {'Stints': "
        "[{'Compound': 'MEDIUM', 'TotalLaps': 3}]}}}, '']",
        "['TimingData', {'Lines': {'44': {'Position': '1'}}}, "
        "'2026-07-05T15:05:00.000Z']",
    ]
    feed = tmp_path / "midjoin.txt"
    feed.write_text("\n".join(lines) + "\n", encoding="utf-8")

    events = ingest_f1live(str(feed), session_id="test")
    tyre = next(e for e in events if e.type == "TyreStintUpdated")
    assert tyre.session_time_ms == 300_000


def test_midrace_timing_keyframe_restores_current_driver_state(tmp_path):
    lines = [
        "['SessionData', {\"StatusSeries\": [{\"SessionStatus\": \"Started\", "
        "\"Utc\": \"2026-07-05T15:00:00.000Z\"}]}, '']",
        "['DriverList', {'44': {'Tla': 'HAM'}}, '']",
        "['TimingData', {'Lines': {'44': {'Position': '7', 'NumberOfLaps': 12, "
        "'InPit': True, 'Stopped': True}}}, '']",
        "['Heartbeat', {'Utc': '2026-07-05T15:05:00.000Z'}, "
        "'2026-07-05T15:05:00.000Z']",
    ]
    feed = tmp_path / "midrace-keyframe.txt"
    feed.write_text("\n".join(lines) + "\n", encoding="utf-8")

    state = ReplayEngine(ingest_f1live(str(feed), session_id="test")).state_at(300_000)

    assert state["lap"] == 12
    assert state["drivers"]["HAM"]["laps_completed"] == 12
    assert state["drivers"]["HAM"]["in_pit"] is True
    assert state["drivers"]["HAM"]["stopped"] is True


def test_prestart_keyframe_immediately_before_start_is_not_race_activity(tmp_path):
    feed = tmp_path / "prestart-keyframe.txt"
    feed.write_text("\n".join([
        "['DriverList', {'44': {'Tla': 'HAM'}}, '']",
        "['TimingData', {'Lines': {'44': {'Position': '1', 'NumberOfLaps': 3, "
        "'InPit': True, 'Stopped': True}}}, '']",
        "['SessionStatus', {'Status': 'Started'}, '2026-07-05T15:00:00.000Z']",
    ]) + "\n", encoding="utf-8")

    events = ingest_f1live(str(feed), session_id="test")

    assert not any(item.type in {
        "LapCompleted", "PitIn", "PitOut", "DriverStoppedChanged",
    } for item in events)


def test_race_control_keyframe_uses_each_message_timestamp(tmp_path):
    lines = [
        "['SessionData', {\"StatusSeries\": [{\"SessionStatus\": \"Started\", "
        "\"Utc\": \"2026-07-05T15:00:00.000Z\"}]}, '']",
        "['RaceControlMessages', {'Messages': {"
        "'0': {'Utc': '2026-07-05T15:01:00.000Z', 'Category': 'Other', "
        "'Message': 'FIRST'}, "
        "'1': {'Utc': '2026-07-05T15:02:00.000Z', 'Category': 'Other', "
        "'Message': 'SECOND'}}}, '']",
        "['TimingData', {'Lines': {'44': {'Position': '1'}}}, "
        "'2026-07-05T15:05:00.000Z']",
    ]
    feed = tmp_path / "midjoin.txt"
    feed.write_text("\n".join(lines) + "\n", encoding="utf-8")

    messages = [
        event for event in ingest_f1live(str(feed), session_id="test")
        if event.type == "RaceControlMessage"
    ]
    assert [(event.session_time_ms, event.payload["message"]) for event in messages] == [
        (60_000, "FIRST"),
        (120_000, "SECOND"),
    ]


def test_prestart_activity_does_not_leak_into_live_feed(tmp_path):
    before_start = [
        "['DriverList', {'16': {'Tla': 'LEC'}}, '']",
        "['TimingData', {'Lines': {'16': {'InPit': True}}}, "
        "'2026-09-06T12:30:00Z']",
        "['RaceControlMessages', {'Messages': [{'Utc': '2026-09-06T12:40:00Z', "
        "'Category': 'Other', 'Message': 'PRESTART INCIDENT'}]}, "
        "'2026-09-06T12:40:00Z']",
        "['TeamRadio', {'Captures': [{'Utc': '2026-09-06T12:50:00Z', "
        "'RacingNumber': '16', 'Path': 'TeamRadio/prestart.mp3'}]}, "
        "'2026-09-06T12:50:00Z']",
    ]
    feed = tmp_path / "race.txt"
    feed.write_text("\n".join(before_start) + "\n", encoding="utf-8")

    assert render_feed(ingest_f1live(str(feed)), until_ms=10_000_000) == []

    feed.write_text("\n".join(before_start + [
        "['SessionStatus', {'Status': 'Started'}, '2026-09-06T13:00:00Z']",
        "['RaceControlMessages', {'Messages': [{'Utc': '2026-09-06T13:01:00Z', "
        "'Category': 'Other', 'Message': 'RACE INCIDENT'}]}, "
        "'2026-09-06T13:01:00Z']",
    ]) + "\n", encoding="utf-8")

    items = render_feed(ingest_f1live(str(feed)), until_ms=10_000_000)
    assert [item["text"] for item in items] == [
        "RACE INCIDENT",
        "Lights out — race start!",
    ]


def test_restart_announcement_uses_source_gmt_offset(tmp_path):
    session_info = {
        "Meeting": {"Name": "Dutch Grand Prix", "Location": "Zandvoort"},
        "Name": "Race",
        "GmtOffset": "02:00:00",
    }
    lines = [
        "['SessionInfo', %s, '']" % json.dumps(json.dumps(session_info)),
        "['SessionStatus', {'Status': 'Started'}, '2026-08-23T13:00:00Z']",
        "['RaceControlMessages', {'Messages': [{'Utc': '2026-08-23T13:22:29Z', "
        "'Category': 'Other', 'Message': 'RACE WILL RESUME AT 15:33'}]}, "
        "'2026-08-23T13:22:29Z']",
    ]
    feed = tmp_path / "restart.txt"
    feed.write_text("\n".join(lines) + "\n", encoding="utf-8")

    message = next(
        event for event in ingest_f1live(str(feed), session_id="test")
        if event.type == "RaceControlMessage"
    )

    assert message.payload["restart_at_ms"] == 1_980_000


def test_restart_announcement_without_source_timezone_stays_unresolved(tmp_path):
    lines = [
        "['SessionStatus', {'Status': 'Started'}, '2026-08-23T13:00:00Z']",
        "['RaceControlMessages', {'Messages': [{'Utc': '2026-08-23T13:22:29Z', "
        "'Category': 'Other', 'Message': 'RACE WILL RESUME AT 15:33'}]}, "
        "'2026-08-23T13:22:29Z']",
    ]
    feed = tmp_path / "restart-without-offset.txt"
    feed.write_text("\n".join(lines) + "\n", encoding="utf-8")

    message = next(
        event for event in ingest_f1live(str(feed), session_id="test")
        if event.type == "RaceControlMessage"
    )

    assert "restart_at_ms" not in message.payload


def test_transient_retired_pulse_is_ignored(tmp_path):
    feed = tmp_path / "retirement-pulse.txt"
    feed.write_text("\n".join([
        "['SessionStatus', {'Status': 'Started'}, '2026-07-05T15:00:00.000Z']",
        "['DriverList', {'77': {'Tla': 'BOT'}}, '2026-07-05T15:00:01.000Z']",
        "['TimingData', {'Lines': {'77': {'Retired': True, 'Stopped': True}}}, "
        "'2026-07-05T15:01:00.000Z']",
        "['TimingData', {'Lines': {'77': {'Retired': False, 'Stopped': False}}}, "
        "'2026-07-05T15:01:01.000Z']",
    ]) + "\n", encoding="utf-8")

    events = ingest_f1live(str(feed), session_id="test")

    assert not any(event.type == "RetirementDetected" for event in events)


def test_rapid_position_loss_under_yellow_reports_trouble_but_not_pit_stop(tmp_path):
    feed = tmp_path / "driver-trouble.txt"
    feed.write_text("\n".join([
        "['SessionStatus', {'Status': 'Started'}, '2026-07-05T15:00:00Z']",
        "['DriverList', {'43': {'Tla': 'COL'}, '81': {'Tla': 'PIA'}}, '']",
        "['TimingData', {'Lines': {'81': {'InPit': True}}}, "
        "'2026-07-05T15:00:59Z']",
        "['TimingData', {'Lines': {'43': {'Position': '5'}, "
        "'81': {'Position': '6'}}}, '2026-07-05T15:01:00Z']",
        "['TrackStatus', {'Status': '2', 'Message': 'Yellow'}, "
        "'2026-07-05T15:01:04Z']",
        "['TimingData', {'Lines': {'43': {'Position': '21'}, "
        "'81': {'Position': '20'}}}, '2026-07-05T15:01:05Z']",
    ]) + "\n", encoding="utf-8")

    events = ingest_f1live(str(feed), session_id="test")
    trouble = [item for item in events if item.type == "DriverTroubleDetected"]

    assert [(item.driver_id, item.payload) for item in trouble] == [
        ("COL", {"from_position": 5, "to_position": 21}),
    ]
    assert any(
        item["driver_id"] == "COL" and "P5" in item["text"] and "P21" in item["text"]
        for item in render_feed(events, until_ms=70_000)
    )
