import json

import pytest

from racelens.events.models import dump_jsonl, event, load_jsonl
from racelens.recorder.postprocess import (
    ArchiveValidationError,
    PostprocessError,
    build_command_plan,
    merge_captured_radio,
    validate_archive,
)


def _write(path, events):
    path.write_text(dump_jsonl(sorted(events, key=lambda item: (item.session_time_ms, item.event_id))), encoding="utf-8")


def _archive(tmp_path, *, progress=True):
    fixture = tmp_path / "race.jsonl"
    track = tmp_path / "race.track.json"
    positions = tmp_path / "race.positions.json"
    events = [event("canonical", "SessionStarted", 0)]
    events.extend(
        event("canonical", "LapCompleted", lap * 60_000, driver, lap=lap)
        for driver in ("HAM", "VER") for lap in range(1, 4)
    )
    _write(fixture, events)
    track.write_text(json.dumps({
        "session_id": "race", "viewbox": [600, 400],
        "points": [[0, 0], [1, 1]],
        "progress_points": [[0, 0], [1, 1]],
        "corners": [],
    }), encoding="utf-8")
    positions.write_text(json.dumps({
        "session_id": "race", "tick_ms": 1000, "start_ms": 0,
        "viewbox": [600, 400],
        "drivers": {"HAM": [[0, 0]] * 300},
        "progress": {"HAM": ([0.0] * 300) if progress else ([None] * 300)},
    }), encoding="utf-8")
    return fixture, track, positions


def test_merge_adds_radio_and_preserves_canonical_non_radio(tmp_path):
    canonical = tmp_path / "canonical.jsonl"
    captured = tmp_path / "captured.jsonl"
    original = [
        event("canonical", "SessionStarted", 0),
        event("canonical", "RaceControlMessage", 500, category="Flag", message="YELLOW"),
    ]
    _write(canonical, original)
    _write(captured, [event(
        "live", "RaceControlMessage", 1000, "HAM", source="f1live",
        category="Radio", message="RADIO: HAM", audio_path="TeamRadio/ham.mp3",
        audio_url="https://live/TeamRadio/ham.mp3", transcript="Box, box.",
    )])

    report = merge_captured_radio(canonical, captured)
    merged = load_jsonl(canonical.read_text(encoding="utf-8"))
    radio = [item for item in merged if item.payload.get("category") == "Radio"]

    assert report.radio_added == 1
    assert len(radio) == 1
    assert radio[0].session_id == "canonical"
    assert radio[0].payload["transcript"] == "Box, box."
    assert {item.event_id for item in merged if item.payload.get("category") != "Radio"} == {
        item.event_id for item in original
    }


def test_merge_deduplicates_same_radio_and_keeps_richer_payload(tmp_path):
    canonical = tmp_path / "canonical.jsonl"
    captured = tmp_path / "captured.jsonl"
    _write(canonical, [event("canonical", "SessionStarted", 0)])
    base = dict(category="Radio", message="RADIO: HAM", audio_path="TeamRadio/ham.mp3")
    _write(captured, [
        event("live", "RaceControlMessage", 1000, "HAM", **base),
        event(
            "live", "RaceControlMessage", 1000, "HAM", transcript="Stay out.",
            audio_url="https://live/TeamRadio/ham.mp3", **base,
        ),
    ])

    report = merge_captured_radio(canonical, captured)
    radios = [item for item in load_jsonl(canonical.read_text()) if item.payload.get("category") == "Radio"]

    assert len(radios) == 1
    assert radios[0].payload["transcript"] == "Stay out."
    assert report.radio_deduplicated == 1


def test_merge_infers_radio_lap_from_canonical_timeline(tmp_path):
    canonical = tmp_path / "canonical.jsonl"
    captured = tmp_path / "captured.jsonl"
    _write(canonical, [
        event("canonical", "SessionStarted", 0),
        event("canonical", "LapCompleted", 90_000, "HAM", lap=1),
        event("canonical", "LapCompleted", 180_000, "HAM", lap=2),
    ])
    _write(captured, [event(
        "live", "RaceControlMessage", 120_000, "HAM", lap=1,
        category="Radio", message="RADIO: HAM", audio_path="TeamRadio/ham.mp3",
    )])

    merge_captured_radio(canonical, captured)
    radio = next(
        item for item in load_jsonl(canonical.read_text())
        if item.payload.get("category") == "Radio"
    )
    assert radio.lap == 2


def test_malformed_capture_does_not_replace_canonical(tmp_path):
    canonical = tmp_path / "canonical.jsonl"
    captured = tmp_path / "captured.jsonl"
    _write(canonical, [event("canonical", "SessionStarted", 0)])
    before = canonical.read_bytes()
    captured.write_text("{not-json}\n", encoding="utf-8")

    with pytest.raises(PostprocessError):
        merge_captured_radio(canonical, captured)

    assert canonical.read_bytes() == before


def test_absent_radio_leaves_canonical_untouched(tmp_path):
    canonical = tmp_path / "canonical.jsonl"
    captured = tmp_path / "captured.jsonl"
    _write(canonical, [event("canonical", "SessionStarted", 0)])
    _write(captured, [event("live", "PositionChanged", 1000, "HAM", position=1)])
    before = canonical.read_bytes()

    report = merge_captured_radio(canonical, captured)

    assert not report.written
    assert report.radio_added == 0
    assert canonical.read_bytes() == before


def test_merge_preserves_only_valid_source_backed_weather(tmp_path):
    canonical = tmp_path / "canonical.jsonl"
    captured = tmp_path / "captured.jsonl"
    _write(canonical, [event("canonical", "SessionStarted", 0)])
    sample = event(
        "live", "WeatherUpdated", 2_000, source="f1live",
        air_temp_c=18.7, track_temp_c=32.9, rainfall=False,
    )
    _write(captured, [
        sample,
        sample,
        event("live", "WeatherUpdated", 1_000, source="f1live", air_temp_c="bad"),
        event("live", "WeatherUpdated", 1_500, source="f1live", air_temp_c=70.1),
        event("live", "WeatherUpdated", 3_000, source="fixture", air_temp_c=19.0),
    ])

    merge_captured_radio(canonical, captured)
    merged = load_jsonl(canonical.read_text(encoding="utf-8"))
    weather = [item for item in merged if item.type == "WeatherUpdated"]

    assert len(weather) == 1
    assert weather[0].session_id == "canonical"
    assert weather[0].session_time_ms == 2_000
    assert weather[0].payload == {
        "air_temp_c": 18.7,
        "track_temp_c": 32.9,
        "rainfall": False,
    }
    assert merged == sorted(merged, key=lambda item: (item.session_time_ms, item.event_id))


def test_merge_preserves_only_valid_source_backed_driver_status(tmp_path):
    canonical = tmp_path / "canonical.jsonl"
    captured = tmp_path / "captured.jsonl"
    _write(canonical, [event("canonical", "SessionStarted", 0)])
    _write(captured, [
        event(
            "live", "DriverStoppedChanged", 2_000, "HAM",
            source="f1live", stopped=True,
        ),
        event(
            "live", "RetirementDetected", 12_000, "HAM", lap=2,
            source="f1live",
        ),
        event(
            "live", "DriverStoppedChanged", 3_000, "NOR",
            source="fixture", stopped=True,
        ),
        event(
            "live", "DriverStoppedChanged", 4_000, "LEC",
            source="f1live", stopped="yes",
        ),
    ])

    merge_captured_radio(canonical, captured)
    merged = load_jsonl(canonical.read_text(encoding="utf-8"))
    statuses = [
        item for item in merged
        if item.type in {"DriverStoppedChanged", "RetirementDetected"}
    ]

    assert [(item.type, item.driver_id, item.lap, item.payload) for item in statuses] == [
        ("DriverStoppedChanged", "HAM", None, {"stopped": True}),
        ("RetirementDetected", "HAM", 2, {}),
    ]
    assert all(item.session_id == "canonical" for item in statuses)
    assert all(item.source == "f1live" for item in statuses)


def test_validate_archive_reports_schema_and_coverage(tmp_path):
    report = validate_archive(*_archive(tmp_path))

    assert report.events == 7
    assert report.track_points == 2
    assert report.frames == 300
    assert report.position_driver_coverage == 1
    assert report.progress_driver_coverage == 1


def test_validate_archive_rejects_missing_progress_coverage(tmp_path):
    with pytest.raises(ArchiveValidationError, match="progress driver coverage"):
        validate_archive(*_archive(tmp_path, progress=False))


def test_validate_archive_rejects_one_sample_per_driver(tmp_path):
    fixture, track, positions = _archive(tmp_path)
    payload = json.loads(positions.read_text())
    payload["drivers"]["HAM"] = [[0, 0]] + [None] * 299
    payload["progress"]["HAM"] = [0.0] + [None] * 299
    positions.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ArchiveValidationError, match="frame coverage"):
        validate_archive(fixture, track, positions)


def test_validate_archive_rejects_a_different_session_identity(tmp_path):
    fixture, track, positions = _archive(tmp_path)
    payload = json.loads(track.read_text())
    payload["session_id"] = "other_race"
    track.write_text(json.dumps(payload), encoding="utf-8")
    payload = json.loads(positions.read_text())
    payload["session_id"] = "other_race"
    positions.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ArchiveValidationError, match="fixture name"):
        validate_archive(fixture, track, positions)


def test_command_plan_retains_every_capture_but_publishes_positions_selectively(tmp_path):
    race = build_command_plan(2026, "British", "R", "2026-12-r", root=tmp_path)
    practice = build_command_plan(2026, "British", "FP2", "2026-12-fp2", root=tmp_path)

    assert len(race.retained) == len(practice.retained) == 2
    assert race.validate_args is not None
    assert any(path.name.endswith(".positions.json") for path in race.published)
    assert practice.validate_args is None
    assert not any(path.name.endswith(".positions.json") for path in practice.published)
    assert race.commands[-2].argv[-1] == "1000"
