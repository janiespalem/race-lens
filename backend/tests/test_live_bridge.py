import copy
import asyncio
import json
import os
from datetime import UTC, datetime, timedelta

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import racelens.object_storage as storage  # noqa: E402
from racelens.adapters.f1live_adapter import ingest_f1live  # noqa: E402
from racelens.events.models import WEATHER_BOUNDS  # noqa: E402
from racelens.object_storage import StorageError, publish_session  # noqa: E402
from racelens.recorder.schedule import ScheduledSession  # noqa: E402
from racelens.recorder.state import Phase  # noqa: E402
from racelens.recorder.worker import Config, Recorder, fixture_stem  # noqa: E402
from tests.test_object_storage import MemoryStore  # noqa: E402


NOW = datetime(2026, 8, 23, 13, 5, tzinfo=UTC)
SESSION = ScheduledSession(2026, 15, "Dutch Grand Prix", "R", NOW - timedelta(minutes=5))


def _config(tmp_path):
    return Config(
        state_dir=tmp_path / "state",
        raw_dir=tmp_path / "raw",
        data_dir=tmp_path / "data",
        interval_seconds=120,
        capture_poll_seconds=5,
        raw_retention_days=14,
        publish_sessions=frozenset({"R"}),
        transcribe_radio=True,
        race_core=tmp_path / "race-core",
        git_publication=False,
    )


def _line(category, payload, timestamp=""):
    return repr([category, payload, timestamp])


def _feed_prefix():
    info = {
        "Meeting": {
            "Key": 15,
            "Number": 15,
            "Name": "Dutch Grand Prix",
            "Location": "Zandvoort",
        },
        "Key": 99,
        "Name": "Race",
        "StartDate": "2026-08-23T13:00:00Z",
        "Path": "2026/Dutch/Race/",
    }
    return [
        _line("SessionInfo", json.dumps(info)),
        _line(
            "SessionData",
            {"StatusSeries": [{"SessionStatus": "Started", "Utc": "2026-08-23T13:00:00Z"}]},
        ),
        _line("DriverList", {"1": {"Tla": "VER"}, "4": {"Tla": "NOR"}}),
        _line("SessionStatus", {"Status": "Started"}, "2026-08-23T13:00:00Z"),
        _line(
            "TimingData",
            {"Lines": {"1": {"Position": "1"}, "4": {"Position": "2", "GapToLeader": "+1.0"}}},
            "2026-08-23T13:00:01Z",
        ),
    ]


def _write_feed(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))


def _valid_records(now=NOW):
    canonical = SESSION.session_id
    replay = fixture_stem(SESSION)
    generated = now.isoformat().replace("+00:00", "Z")
    expires = (now + timedelta(seconds=20)).isoformat().replace("+00:00", "Z")
    key = f"live/{canonical}/snapshot.json"
    pointer = {
        "schema_version": 1,
        "canonical_session_id": canonical,
        "replay_session_id": replay,
        "status": "live",
        "snapshot_key": key,
        "created_at": generated,
        "updated_at": generated,
        "failure": None,
    }
    state = {
        "session_id": replay,
        "at_ms": 1_000,
        "lap": 1,
        "session_status": "started",
        "session_name": "ZANDVOORT · RACE",
        "status_since_ms": 0,
        "total_laps": 72,
        "classification": ["VER"],
        "drivers": {"VER": {
            "position": 1,
            "rank": 1,
            "grid_position": 1,
            "laps_completed": 1,
            "last_lap_ms": 80_000,
            "best_lap_ms": 80_000,
            "gap_s": 0.0,
            "interval_s": None,
            "tyre_compound": "Medium",
            "tyre_age_laps": 1,
            "pit_count": 0,
            "in_pit": False,
            "recent_laps_ms": [80_000],
            "retired": False,
            "x": None,
            "y": None,
            "progress": None,
        }},
        "data_quality": {
            "status": "good",
            "last_event_ms": 1_000,
            "events_applied": 2,
            "duplicates_dropped": 0,
        },
        "frame_source": "live",
        "viewbox": None,
    }
    snapshot = {
        "schema_version": 1,
        "canonical_session_id": canonical,
        "replay_session_id": replay,
        "sequence": 1,
        "generated_at": generated,
        "expires_at": expires,
        "race_state": state,
        "battles": [],
        "active_insights": [],
        "recent_passes": [],
        "feed": {"en": [], "ru": []},
        "commentary": {
            "en": {"beginner": [], "pro": []},
            "ru": {"beginner": [], "pro": []},
        },
        "radio": [],
        "capture_freshness": {
            "raw_size": 100,
            "raw_updated_at": generated,
            "seconds_since_growth": 0.0,
            "transport_growing": True,
        },
        "data_quality": "good",
    }
    return pointer, snapshot


def test_live_records_reject_invalid_oversized_stale_and_mismatched_data():
    store = MemoryStore()
    pointer, snapshot = _valid_records()
    storage.write_live_snapshot(store, pointer, snapshot, now=NOW)
    assert storage.load_live(store, now=NOW)["snapshot"]["sequence"] == 1

    bad_pointer = {**pointer, "status": "starting"}
    with pytest.raises(storage.LiveRecordError):
        storage.validate_live_pointer(bad_pointer)

    oversized = copy.deepcopy(snapshot)
    oversized["race_state"]["padding"] = "x" * storage.MAX_LIVE_SNAPSHOT_BYTES
    with pytest.raises(storage.LiveRecordError, match="size"):
        storage.validate_live_snapshot(oversized, pointer=pointer, now=NOW)

    stale = copy.deepcopy(snapshot)
    stale["generated_at"] = (NOW - timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    stale["expires_at"] = (NOW - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
    with pytest.raises(storage.LiveRecordError, match="stale"):
        storage.validate_live_snapshot(stale, pointer=pointer, now=NOW)

    mismatched = copy.deepcopy(snapshot)
    mismatched["replay_session_id"] = "other_2026_race"
    with pytest.raises(storage.LiveRecordError, match="identity"):
        storage.validate_live_snapshot(mismatched, pointer=pointer, now=NOW)


def test_live_records_reject_inner_identity_malformed_values_and_path_text():
    pointer, snapshot = _valid_records()
    inner = copy.deepcopy(snapshot)
    inner["race_state"]["session_id"] = "other_2026_race"
    with pytest.raises(storage.LiveRecordError, match="identity"):
        storage.validate_live_snapshot(inner, pointer=pointer, now=NOW)

    malformed_pointer = {**pointer, "status": []}
    with pytest.raises(storage.LiveRecordError):
        storage.validate_live_pointer(malformed_pointer)
    malformed_snapshot = copy.deepcopy(snapshot)
    malformed_snapshot["data_quality"] = []
    with pytest.raises(storage.LiveRecordError):
        storage.validate_live_snapshot(malformed_snapshot, pointer=pointer, now=NOW)

    malformed_restart = copy.deepcopy(snapshot)
    malformed_restart["race_state"]["restart_at_ms"] = "soon"
    with pytest.raises(storage.LiveRecordError):
        storage.validate_live_snapshot(malformed_restart, pointer=pointer, now=NOW)

    leaked = copy.deepcopy(snapshot)
    leaked["feed"]["en"] = [{
        "id": "leak",
        "at_ms": 1_000,
        "lap": 1,
        "kind": "RaceControlMessage",
        "tag": "FLAG",
        "text": "/home/recorder/.aws/credentials",
        "driver_id": None,
    }]
    with pytest.raises(storage.LiveRecordError, match="feed"):
        storage.validate_live_snapshot(leaked, pointer=pointer, now=NOW)
    with pytest.raises(storage.LiveRecordError):
        storage.validate_live_pointer({**pointer, "status": "failed", "failure": "S3 bucket secret"})

    missing = copy.deepcopy(snapshot)
    del missing["race_state"]["classification"]
    with pytest.raises(storage.LiveRecordError, match="race state"):
        storage.validate_live_snapshot(missing, pointer=pointer, now=NOW)


def test_live_records_accept_multiline_whisper_and_nullable_undercut_evidence():
    pointer, snapshot = _valid_records()
    transcript = "Brake balance endpoint\nput the tyres in the bucket"
    audio_url = "https://livetiming.formula1.com/static/2026/Dutch/Race/radio.mp3"
    radio_feed = {
        "id": "radio:ver",
        "at_ms": 1_000,
        "lap": 1,
        "kind": "RaceControlMessage",
        "tag": "FLAG",
        "text": "RADIO: VER",
        "driver_id": "VER",
        "audio_url": audio_url,
        "transcript": transcript,
    }
    snapshot["feed"] = {"en": [radio_feed], "ru": [copy.deepcopy(radio_feed)]}
    snapshot["radio"] = [{
        "audio_url": audio_url,
        "transcript": transcript,
        "driver_id": "VER",
        "at_ms": 1_000,
    }]
    snapshot["active_insights"] = [{
        "insight_id": "undercut:VER:1000",
        "type": "UNDERCUT_RISK_MEDIUM",
        "severity": "medium",
        "confidence": "medium",
        "created_at_ms": 1_000,
        "lap": 1,
        "driver_ids": ["VER", "NOR"],
        "evidence": {
            "interval_s": 1.5,
            "attacker_tyre_age_laps": 10,
            "defender_tyre_age_laps": None,
            "pace_delta_ms": 100,
        },
    }]

    assert storage.validate_live_snapshot(snapshot, pointer=pointer, now=NOW) == snapshot


def test_live_records_accept_official_stewards_feed_tag():
    pointer, snapshot = _valid_records()
    steward_item = {
        "id": "stewards:per",
        "at_ms": 1_000,
        "lap": 30,
        "kind": "RaceControlMessage",
        "tag": "STEWARDS",
        "text": "FIA STEWARDS: 5 SECOND TIME PENALTY FOR CAR 11 (PER)",
        "driver_id": None,
    }
    snapshot["feed"] = {
        "en": [steward_item],
        "ru": [copy.deepcopy(steward_item)],
    }

    assert storage.validate_live_snapshot(snapshot, pointer=pointer, now=NOW) == snapshot


def test_live_records_accept_source_backed_weather():
    pointer, snapshot = _valid_records()
    snapshot["race_state"]["weather"] = {
        "air_temp_c": 18.7,
        "track_temp_c": 32.9,
        "humidity_percent": 56.2,
        "pressure_mbar": 1024.6,
        "rainfall": False,
        "wind_direction_deg": 96.0,
        "wind_speed_mps": 2.2,
    }

    assert storage.validate_live_snapshot(snapshot, pointer=pointer, now=NOW) == snapshot


@pytest.mark.parametrize("weather", [
    {"unknown": 1.0},
    {"air_temp_c": float("inf")},
    {},
    {"rainfall": "no"},
])
def test_live_records_reject_malformed_weather(weather):
    pointer, snapshot = _valid_records()
    snapshot["race_state"]["weather"] = weather

    with pytest.raises(storage.LiveRecordError, match="race state"):
        storage.validate_live_snapshot(snapshot, pointer=pointer, now=NOW)


@pytest.mark.parametrize(("field", "value"), [
    ("air_temp_c", -50.1), ("air_temp_c", 70.1),
    ("track_temp_c", -50.1), ("track_temp_c", 100.1),
    ("humidity_percent", -0.1), ("humidity_percent", 100.1),
    ("pressure_mbar", 699.9), ("pressure_mbar", 1100.1),
    ("wind_direction_deg", -0.1), ("wind_direction_deg", 360.1),
    ("wind_speed_mps", -0.1), ("wind_speed_mps", 100.1),
])
def test_live_records_reject_out_of_range_weather(field, value):
    pointer, snapshot = _valid_records()
    snapshot["race_state"]["weather"] = {field: value}

    with pytest.raises(storage.LiveRecordError, match="race state"):
        storage.validate_live_snapshot(snapshot, pointer=pointer, now=NOW)


@pytest.mark.parametrize(("field", "lower", "upper"), [
    ("air_temp_c", -50.0, 70.0),
    ("track_temp_c", -50.0, 100.0),
    ("humidity_percent", 0.0, 100.0),
    ("pressure_mbar", 700.0, 1100.0),
    ("wind_direction_deg", 0.0, 360.0),
    ("wind_speed_mps", 0.0, 100.0),
])
def test_weather_bounds_accept_endpoints_and_representative_integer(field, lower, upper):
    assert WEATHER_BOUNDS[field] == (lower, upper)
    pointer, snapshot = _valid_records()
    for value in (lower, upper, int((lower + upper) / 2)):
        snapshot["race_state"]["weather"] = {field: value}
        assert storage.validate_live_snapshot(snapshot, pointer=pointer, now=NOW) == snapshot


@pytest.mark.parametrize(
    "path",
    ["/root/private", "/srv/race-lens/secret", r"C:\private\secret"],
)
def test_live_transcript_rejects_absolute_path_on_later_line(path):
    pointer, snapshot = _valid_records()
    snapshot["radio"] = [{
        "audio_url": "https://livetiming.formula1.com/static/2026/Dutch/Race/radio.mp3",
        "transcript": f"Box this lap\n{path}",
        "driver_id": "VER",
        "at_ms": 1_000,
    }]

    with pytest.raises(storage.LiveRecordError, match="radio"):
        storage.validate_live_snapshot(snapshot, pointer=pointer, now=NOW)


def test_recorder_snapshot_handles_partial_append_sc_vsc_and_late_transcript(tmp_path):
    store = MemoryStore()
    recorder = Recorder(_config(tmp_path), now=lambda: NOW, object_store=store)
    feed = recorder._paths(SESSION)["raw"]
    wrong = _feed_prefix()
    wrong[0] = wrong[0].replace("Dutch Grand Prix", "Belgian Grand Prix")
    _write_feed(feed, wrong)
    assert not recorder._publish_live_snapshot(SESSION, feed)
    assert "live/current.json" not in store.objects

    lines = _feed_prefix() + [
        _line(
            "RaceControlMessages",
            {"Messages": [{"Utc": "2026-08-23T13:01:00Z", "Category": "Flag", "Message": "SAFETY CAR DEPLOYED"}]},
            "2026-08-23T13:01:00Z",
        ),
        _line(
            "TeamRadio",
            {"Captures": [{"Utc": "2026-08-23T13:01:01Z", "RacingNumber": "1", "Path": "TeamRadio/ver.mp3"}]},
            "2026-08-23T13:01:01Z",
        ),
    ]
    _write_feed(feed, lines)
    with feed.open("a", encoding="utf-8") as handle:
        handle.write("['TimingData', {'Lines':")
    os.utime(feed, (NOW.timestamp(), NOW.timestamp()))

    class Transcripts:
        calls = 0

        def get(self, _url):
            self.calls += 1
            return "box now" if self.calls > 1 else None

    recorder._transcripts = Transcripts()
    assert recorder._publish_live_snapshot(SESSION, feed)
    first = store.objects[f"live/{SESSION.session_id}/snapshot.json"]
    assert first["race_state"]["session_status"] == "safety_car"
    assert first["radio"][0]["audio_url"].startswith("https://")
    assert first["radio"][0]["transcript"] is None

    with feed.open("a", encoding="utf-8") as handle:
        handle.write(" {'1': {'Position': '1'}}}, '2026-08-23T13:01:02Z']\n")
        handle.write(_line(
            "RaceControlMessages",
            {"Messages": [{"Utc": "2026-08-23T13:01:03Z", "Category": "Flag", "Message": "VIRTUAL SAFETY CAR DEPLOYED"}]},
            "2026-08-23T13:01:03Z",
        ) + "\n")
    os.utime(feed, (NOW.timestamp(), NOW.timestamp()))

    assert recorder._publish_live_snapshot(SESSION, feed)
    second = store.objects[f"live/{SESSION.session_id}/snapshot.json"]
    assert second["sequence"] > first["sequence"]
    assert second["race_state"]["session_status"] == "vsc"
    assert second["radio"][0]["transcript"] == "box now"


def test_capture_quality_uses_transport_growth_not_modeled_events(tmp_path):
    recorder = Recorder(_config(tmp_path), now=lambda: NOW, object_store=MemoryStore())
    feed = recorder._paths(SESSION)["raw"]
    _write_feed(feed, _feed_prefix())

    first = recorder._capture_freshness(SESSION, feed)
    with feed.open("a", encoding="utf-8") as handle:
        handle.write("ignored transport heartbeat\n")
    os.utime(feed, ((NOW + timedelta(seconds=10)).timestamp(),) * 2)
    recorder.now = lambda: NOW + timedelta(seconds=10)
    growing = recorder._capture_freshness(SESSION, feed)
    recorder.now = lambda: NOW + timedelta(seconds=31)
    frozen = recorder._capture_freshness(SESSION, feed)

    assert first["data_quality"] == "good"
    assert growing["data_quality"] == "good"
    assert growing["capture_freshness"]["transport_growing"] is True
    assert frozen["data_quality"] == "stalled"
    assert frozen["capture_freshness"]["transport_growing"] is False


def test_finishing_then_verified_ready_or_safe_failed_keeps_snapshot(tmp_path):
    store = MemoryStore()
    recorder = Recorder(_config(tmp_path), now=lambda: NOW, object_store=store)
    feed = recorder._paths(SESSION)["raw"]
    _write_feed(feed, _feed_prefix())
    assert recorder._publish_live_snapshot(SESSION, feed)
    snapshot_key = f"live/{SESSION.session_id}/snapshot.json"
    snapshot = copy.deepcopy(store.objects[snapshot_key])

    recorder._set_live_status(SESSION, "finishing")
    assert store.objects["live/current.json"]["status"] == "finishing"
    with pytest.raises(storage.LiveRecordError, match="manifest"):
        recorder._set_live_status(SESSION, "replay_ready")

    archive = tmp_path / "archive"
    archive.mkdir()
    replay = fixture_stem(SESSION)
    files = []
    for suffix in (".jsonl", ".track.json", ".positions.json"):
        path = archive / f"{replay}{suffix}"
        path.write_text("{}\n", encoding="utf-8")
        files.append(path)
    publish_session(store, SESSION.session_id, replay, *files, event_count=1)
    recorder._set_live_status(SESSION, "replay_ready")
    assert store.objects["live/current.json"]["status"] == "replay_ready"

    with pytest.raises(storage.LiveRecordError, match="transition"):
        recorder._set_live_status(SESSION, "failed", failure="Archive preparation failed")
    assert store.objects["live/current.json"]["status"] == "replay_ready"
    assert store.objects[snapshot_key] == snapshot


def test_live_status_enforces_lifecycle_graph_and_verified_retry(tmp_path):
    store = MemoryStore()
    pointer, snapshot = _valid_records()
    storage.write_live_snapshot(store, pointer, snapshot, now=NOW)
    with pytest.raises(storage.LiveRecordError, match="transition"):
        storage.write_live_status(
            store, SESSION.session_id, fixture_stem(SESSION), "replay_ready", now=NOW,
        )
    storage.write_live_status(
        store, SESSION.session_id, fixture_stem(SESSION), "failed",
        failure="Archive preparation failed", now=NOW,
    )
    with pytest.raises(storage.LiveRecordError, match="manifest"):
        storage.write_live_status(
            store, SESSION.session_id, fixture_stem(SESSION), "replay_ready", now=NOW,
        )

    archive = tmp_path / "retry"
    archive.mkdir()
    replay = fixture_stem(SESSION)
    files = []
    for suffix in (".jsonl", ".track.json", ".positions.json"):
        path = archive / f"{replay}{suffix}"
        path.write_text("{}\n", encoding="utf-8")
        files.append(path)
    publish_session(store, SESSION.session_id, replay, *files, event_count=1)
    storage.write_live_status(
        store, SESSION.session_id, replay, "replay_ready", now=NOW,
    )
    with pytest.raises(storage.LiveRecordError, match="transition"):
        storage.write_live_status(
            store, SESSION.session_id, replay, "finishing", now=NOW,
        )


def test_live_snapshot_preserves_created_at_for_same_identity_and_resets_for_new_race():
    store = MemoryStore()
    pointer, snapshot = _valid_records()
    storage.write_live_snapshot(store, pointer, snapshot, now=NOW)

    later = NOW + timedelta(seconds=5)
    next_pointer = {
        **pointer,
        "created_at": later.isoformat().replace("+00:00", "Z"),
        "updated_at": later.isoformat().replace("+00:00", "Z"),
    }
    next_snapshot = copy.deepcopy(snapshot)
    next_snapshot.update(
        sequence=2,
        generated_at=later.isoformat().replace("+00:00", "Z"),
        expires_at=(later + timedelta(seconds=20)).isoformat().replace("+00:00", "Z"),
    )
    storage.write_live_snapshot(store, next_pointer, next_snapshot, now=later)
    assert store.objects["live/current.json"]["created_at"] == pointer["created_at"]

    new_pointer = copy.deepcopy(next_pointer)
    new_pointer.update(
        canonical_session_id="2026-16-r",
        replay_session_id="italian_2026_race",
        snapshot_key="live/2026-16-r/snapshot.json",
    )
    new_snapshot = copy.deepcopy(next_snapshot)
    new_snapshot.update(
        canonical_session_id="2026-16-r",
        replay_session_id="italian_2026_race",
    )
    new_snapshot["race_state"]["session_id"] = "italian_2026_race"
    storage.write_live_snapshot(store, new_pointer, new_snapshot, now=later)
    assert store.objects["live/current.json"]["created_at"] == next_pointer["created_at"]


def test_live_snapshot_writer_cannot_publish_ready_or_overwrite_terminal_state():
    pointer, snapshot = _valid_records()
    store = MemoryStore()
    with pytest.raises(storage.LiveRecordError, match="snapshot pointer"):
        storage.write_live_snapshot(
            store, {**pointer, "status": "replay_ready"}, snapshot, now=NOW,
        )
    assert "live/current.json" not in store.objects

    storage.write_live_snapshot(store, pointer, snapshot, now=NOW)
    storage.write_live_status(
        store, SESSION.session_id, fixture_stem(SESSION), "finishing", now=NOW,
    )
    terminal_pointer = copy.deepcopy(store.objects["live/current.json"])
    terminal_snapshot = copy.deepcopy(store.objects[pointer["snapshot_key"]])
    with pytest.raises(storage.LiveRecordError, match="snapshot lifecycle"):
        storage.write_live_snapshot(store, pointer, snapshot, now=NOW)
    assert store.objects["live/current.json"] == terminal_pointer
    assert store.objects[pointer["snapshot_key"]] == terminal_snapshot

    store.objects["live/current.json"] = {
        **terminal_pointer,
        "status": "replay_ready",
    }
    with pytest.raises(storage.LiveRecordError, match="snapshot lifecycle"):
        storage.write_live_snapshot(store, pointer, snapshot, now=NOW)


def test_capture_loop_restarts_and_publishes_every_five_seconds_only_with_storage(
    tmp_path, monkeypatch,
):
    import racelens.recorder.worker as worker

    monkeypatch.setattr(worker, "FINISH_GRACE", timedelta(seconds=5))
    monkeypatch.setattr(storage.StorageConfig, "from_env", classmethod(lambda cls: None))

    def run_capture(root, object_store):
        clock = [NOW]
        config = _config(root)
        processes = []
        raw = config.raw_dir / f"{SESSION.session_id}.f1live"
        _write_feed(raw, _feed_prefix())

        class Process:
            def __init__(self, first):
                self.first = first
                self.stopped = False

            def poll(self):
                return 1 if self.first and clock[0] >= NOW + timedelta(seconds=5) else None

            def send_signal(self, _signal):
                self.stopped = True

            def wait(self, timeout=None):
                return 0

            def kill(self):
                self.stopped = True

        def popen(*_args, **_kwargs):
            process = Process(not processes)
            processes.append(process)
            return process

        def sleep(seconds):
            clock[0] += timedelta(seconds=seconds)
            if clock[0] == NOW + timedelta(seconds=5):
                with raw.open("a", encoding="utf-8") as handle:
                    handle.write(_line(
                        "SessionStatus", {"Status": "Finished"},
                        "2026-08-23T13:05:05Z",
                    ) + "\n")

        recorder = Recorder(
            config, now=lambda: clock[0], sleep=sleep, object_store=object_store,
        )
        publishes = []
        monkeypatch.setattr(
            recorder,
            "_publish_live_snapshot",
            lambda *_args: publishes.append(clock[0]) or True,
        )
        monkeypatch.setattr(recorder, "_set_live_status", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(worker.subprocess, "Popen", popen)
        recorder.capture(SESSION)
        return publishes, len(processes)

    publishes, starts = run_capture(tmp_path / "stored", MemoryStore())
    assert publishes == [NOW, NOW + timedelta(seconds=5), NOW + timedelta(seconds=10)]
    assert starts == 2
    publishes, _ = run_capture(tmp_path / "local", None)
    assert publishes == []


def test_remote_live_concurrent_readers_share_one_storage_refresh(monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    import racelens.api as api

    class SlowStore(MemoryStore):
        reads = 0

        def get_json(self, key, *, limit):
            self.reads += 1
            time.sleep(0.05)  # Allow concurrent callers while storage is in flight.
            return super().get_json(key, limit=limit)

    store = SlowStore()
    pointer, snapshot = _valid_records()
    storage.write_live_snapshot(store, pointer, snapshot, now=NOW)
    store.reads = 0
    monkeypatch.setattr(api, "_object_store", lambda: store)
    monkeypatch.setattr(api, "_utcnow", lambda: NOW)
    monkeypatch.setattr(api, "_remote_live_cache", None)
    start = Barrier(4)

    def read(_):
        start.wait(timeout=5)
        return api._remote_live_required()

    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(read, range(4)))

    assert all(value["snapshot"]["sequence"] == 1 for value in values)
    assert len({id(value) for value in values}) == 1
    assert store.reads == 2  # One pointer and one snapshot, not two per viewer.


def test_remote_live_api_fallback_cache_and_sanitized_storage_failure(monkeypatch):
    import racelens.api as api

    class CountingStore(MemoryStore):
        reads = 0

        def get_json(self, key, *, limit):
            self.reads += 1
            return super().get_json(key, limit=limit)

    store = CountingStore()
    pointer, snapshot = _valid_records()
    storage.write_live_snapshot(store, pointer, snapshot, now=NOW)
    store.reads = 0
    monkeypatch.setattr(api, "_object_store", lambda: store)
    monkeypatch.setattr(api, "_live", None)
    monkeypatch.setattr(api, "_remote_live_cache", None)
    monkeypatch.setattr(api, "_utcnow", lambda: NOW)
    client = TestClient(api.app)

    status = client.get("/api/live/status")
    assert status.status_code == 200
    assert status.json()["status"] == "live"
    assert client.get("/api/live/feed").json() == []
    assert client.get("/api/live/battles").json() == {"at_ms": 1000, "battles": []}
    assert client.get("/api/live/forecast").status_code == 200
    pit = client.get("/api/live/simulate-pit", params={"driver": "VER"})
    assert pit.status_code == 200 and pit.json()["driver"] == "VER"
    assert store.reads == 2

    storage.write_live_status(
        store, SESSION.session_id, fixture_stem(SESSION), "finishing", now=NOW,
    )
    monkeypatch.setattr(api, "_remote_live_cache", None)
    response = asyncio.run(api.live_stream(tick_s=0.5, lang="en", level="pro"))
    assert "event: end" in asyncio.run(anext(response.body_iterator))
    monkeypatch.setattr(api, "READONLY", True)
    assert client.post(
        "/api/live/start", params={"year": 2026, "country": "Zandvoort"},
    ).status_code == 403
    assert client.post("/api/live/stop").status_code == 403

    class BrokenStore:
        def get_json(self, *_args, **_kwargs):
            raise StorageError("secret endpoint and bucket")

    monkeypatch.setattr(api, "_object_store", lambda: BrokenStore())
    monkeypatch.setattr(api, "_remote_live_cache", None)
    failed = client.get("/api/live/status")
    assert failed.status_code == 503
    assert "secret" not in failed.text


def test_active_remote_sse_closes_without_terminal_end_on_transient_or_stale(
    monkeypatch,
):
    import racelens.api as api

    async def collect_after(initial_store, replacement_store):
        monkeypatch.setattr(api, "_object_store", lambda: initial_store)
        monkeypatch.setattr(api, "_live", None)
        monkeypatch.setattr(api, "_remote_live_cache", None)
        monkeypatch.setattr(api, "_utcnow", lambda: NOW)
        response = await api.live_stream(tick_s=0.5, lang="en", level="pro")
        monkeypatch.setattr(api, "_object_store", lambda: replacement_store)
        api._remote_live_cache = None
        return [chunk async for chunk in response.body_iterator]

    healthy = MemoryStore()
    pointer, snapshot = _valid_records()
    storage.write_live_snapshot(healthy, pointer, snapshot, now=NOW)

    class BrokenStore:
        def get_json(self, *_args, **_kwargs):
            raise StorageError("provider endpoint secret")

    assert asyncio.run(collect_after(healthy, BrokenStore())) == []

    stale = MemoryStore()
    storage.write_live_snapshot(stale, pointer, snapshot, now=NOW)
    stale.objects[pointer["snapshot_key"]]["expires_at"] = (
        NOW - timedelta(seconds=1)
    ).isoformat().replace("+00:00", "Z")
    assert asyncio.run(collect_after(healthy, stale)) == []


def test_live_status_reports_idle_when_no_live_pointer(monkeypatch):
    import racelens.api as api

    monkeypatch.setattr(api, "_object_store", lambda: MemoryStore())
    monkeypatch.setattr(api, "_live", None)
    monkeypatch.setattr(api, "_remote_live_cache", None)
    client = TestClient(api.app)

    body = client.get("/api/live/status").json()
    assert body["status"] == "idle"
    assert body["source"] == "none"
    assert body["is_running"] is False
    assert body["last_error"] is None

    diagnostics = client.get("/api/diagnostics").json()
    assert diagnostics["live"] == {"source": "none", "freshness": "idle"}


def test_live_status_does_not_hide_invalid_pointer_as_idle(monkeypatch):
    import racelens.api as api

    store = MemoryStore()
    store.put_json("live/current.json", {"invalid": True})
    monkeypatch.setattr(api, "STORAGE_CONFIG", object())
    monkeypatch.setattr(api, "_object_store", lambda: store)
    monkeypatch.setattr(api, "_live", None)
    monkeypatch.setattr(api, "_remote_live_cache", None)
    client = TestClient(api.app)

    response = client.get("/api/live/status")
    assert response.status_code == 502
    assert response.json()["detail"] == "Current live data is invalid"
    assert client.get("/api/diagnostics").json()["live"] == {
        "source": "remote",
        "freshness": "record-invalid",
    }


def test_diagnostics_reports_terminal_remote_lifecycle(monkeypatch):
    import racelens.api as api

    monkeypatch.setattr(api, "STORAGE_CONFIG", object())
    monkeypatch.setattr(api, "_remote_cache", lambda: None)
    monkeypatch.setattr(api, "_live", None)
    monkeypatch.setattr(api, "_remote_live", lambda: {
        "pointer": {"status": "replay_ready"},
        "snapshot": None,
    })

    assert api.diagnostics()["live"] == {
        "source": "remote",
        "freshness": "replay-ready",
    }


def _finished_segment_feed():
    other = {
        "Meeting": {
            "Key": 16,
            "Number": 16,
            "Name": "Belgian Grand Prix",
            "Location": "Spa",
        },
        "Key": 100,
        "Name": "Race",
        "StartDate": "2026-08-30T13:00:00Z",
        "Path": "2026/Belgian/Race/",
    }
    return _feed_prefix() + [
        _line("SessionStatus", {"Status": "Finished"}, "2026-08-23T13:05:00Z"),
        _line("SessionInfo", json.dumps(other)),
    ]


def _finished_feed():
    return _feed_prefix() + [
        _line("SessionStatus", {"Status": "Finished"}, "2026-08-23T13:05:00Z"),
    ]


def test_remote_live_status_reports_stalled_live_not_502_when_snapshot_expired(monkeypatch):
    import racelens.api as api

    store = MemoryStore()
    pointer, snapshot = _valid_records()
    storage.write_live_snapshot(store, pointer, snapshot, now=NOW)
    # Expire the snapshot while keeping the pointer status == "live".
    snapshot = store.objects[pointer["snapshot_key"]]
    snapshot["generated_at"] = (NOW - timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    snapshot["expires_at"] = (NOW - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
    monkeypatch.setattr(api, "_object_store", lambda: store)
    monkeypatch.setattr(api, "_live", None)
    monkeypatch.setattr(api, "_remote_live_cache", None)
    monkeypatch.setattr(api, "_utcnow", lambda: NOW)
    client = TestClient(api.app)

    response = client.get("/api/live/status")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "live"
    assert body["data_quality"] == "stalled"
    assert body["is_running"] is True


def test_capture_resume_finishes_live_pointer_when_final_publish_is_blocked(tmp_path):
    store = MemoryStore()
    recorder = Recorder(_config(tmp_path), now=lambda: NOW, object_store=store)
    feed = recorder._paths(SESSION)["raw"]
    _write_feed(feed, _feed_prefix())
    assert recorder._publish_live_snapshot(SESSION, feed)
    assert store.objects["live/current.json"]["status"] == "live"

    # A following foreign SessionInfo blocks the final snapshot publish.
    _write_feed(feed, _finished_segment_feed())
    clean = recorder.capture(SESSION)
    assert clean.exists()
    assert store.objects["live/current.json"]["status"] == "finishing"


def test_capture_resume_tolerates_pointer_already_finishing(tmp_path):
    store = MemoryStore()
    recorder = Recorder(_config(tmp_path), now=lambda: NOW, object_store=store)
    feed = recorder._paths(SESSION)["raw"]
    _write_feed(feed, _feed_prefix())
    assert recorder._publish_live_snapshot(SESSION, feed)
    recorder._set_live_status(SESSION, "finishing")
    assert store.objects["live/current.json"]["status"] == "finishing"

    # Finished feed without a foreign marker still attempts the final publish,
    # which write_live_snapshot rejects because the pointer is already finishing.
    _write_feed(feed, _finished_feed())
    clean = recorder.capture(SESSION)
    assert clean.exists()
    assert store.objects["live/current.json"]["status"] == "finishing"


def test_object_store_construction_failure_maps_to_503(monkeypatch):
    import racelens.api as api

    api._object_store.cache_clear()
    monkeypatch.setattr(api, "STORAGE_CONFIG", object())
    monkeypatch.setattr(api, "_live", None)
    monkeypatch.setattr(api, "_remote_live_cache", None)
    monkeypatch.setattr(
        api, "S3Store",
        lambda _config: (_ for _ in ()).throw(storage.StorageError("secret bucket")),
    )
    client = TestClient(api.app)

    response = client.get("/api/live/status")
    assert response.status_code == 503
    assert "secret" not in response.text


def test_live_stream_sets_sse_buffering_headers(monkeypatch):
    import racelens.api as api
    from racelens.live.runner import LiveRunner
    from tests.test_replay import mini_race

    runner = LiveRunner(lambda: mini_race(), poll_interval_s=5.0)
    runner._poll_once()
    monkeypatch.setattr(api, "_live", runner)
    response = asyncio.run(api.live_stream(tick_s=0.5, lang="en", level="pro"))
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"


def test_local_live_stream_replaces_same_size_pass_cache_and_keeps_frame_consistent(monkeypatch):
    import racelens.api as api
    from racelens.events.models import event
    from racelens.live.runner import LiveRunner

    baseline = [
        event("test", "SessionStarted", 0),
        event("test", "PositionChanged", 0, "A", position=1),
        event("test", "PositionChanged", 0, "B", position=2),
        event("test", "LapCompleted", 90_000, "A", lap=1, lap_time_ms=90_000),
    ]
    snapshots = iter([
        baseline + [
            event("test", "PositionChanged", 200_000, "B", position=1),
            event("test", "PositionChanged", 200_000, "A", position=2),
        ],
        baseline + [
            event("test", "PositionChanged", 200_000, "A", position=1),
            event("test", "PositionChanged", 200_000, "B", position=2),
        ],
    ])
    runner = LiveRunner(lambda: next(snapshots))
    runner._poll_once()
    monkeypatch.setattr(api, "_live", runner)
    detect_passes = api.detect_passes
    first = True

    def detect_during_correction(events):
        nonlocal first
        result = detect_passes(events)
        if first:
            first = False
            runner._poll_once()
        return result

    monkeypatch.setattr(api, "detect_passes", detect_during_correction)

    async def read_frames():
        runner._task = asyncio.current_task()
        response = await api.live_stream(tick_s=0, lang="en", level="pro")
        try:
            return [json.loads((await anext(response.body_iterator))[6:]) for _ in range(2)]
        finally:
            await response.body_iterator.aclose()
            runner._task = None

    before, after = asyncio.run(read_frames())
    assert before["recent_passes"] and before["classification"] == ["B", "A"]
    assert after["recent_passes"] == []
    assert after["classification"] == ["A", "B"]


@pytest.mark.parametrize(("endpoint", "params"), [
    ("live_feed", {"lang": "en", "limit": 30}),
    ("live_stints", {}),
    ("live_forecast", {"laps": 10}),
    ("live_win_prob", {}),
    ("live_battles", {}),
    ("live_simulate_pit", {"driver": "VER"}),
])
def test_local_live_endpoints_keep_captured_engine_during_empty_poll(
    monkeypatch, endpoint, params,
):
    import racelens.api as api
    from racelens.live.runner import LiveRunner
    from tests.test_replay import mini_race

    runner = LiveRunner(lambda: mini_race())
    runner._poll_once()
    reads = [runner.engine]
    monkeypatch.setattr(api, "_live", runner)
    monkeypatch.setattr(
        LiveRunner, "engine", property(lambda self: reads.pop() if reads else None),
        raising=False,
    )

    result = getattr(api, endpoint)(**params)
    if endpoint != "live_stints":
        result = asyncio.run(result)
    assert isinstance(result, list if endpoint == "live_feed" else dict)


@pytest.mark.parametrize(("messages", "expected_statuses"), [
    pytest.param(
        ["SAFETY CAR DEPLOYED", "SAFETY CAR IN THIS LAP", "TRACK CLEAR"],
        [(0, "started"), (60_000, "safety_car"), (80_000, "started")],
        id="safety-car",
    ),
    pytest.param(
        ["VSC DEPLOYED", "VSC ENDING", "TRACK CLEAR"],
        [(0, "started"), (60_000, "vsc"), (80_000, "started")],
        id="vsc-ending",
    ),
    pytest.param(
        ["VSC DEPLOYED", "VIRTUAL SAFETY CAR ENDING", "TRACK CLEAR"],
        [(0, "started"), (60_000, "vsc"), (80_000, "started")],
        id="virtual-safety-car-ending",
    ),
    pytest.param(
        ["RED FLAG", "TRACK CLEAR"],
        [(0, "started"), (60_000, "red_flag")],
        id="red-flag",
    ),
])
def test_live_race_control_resumes_only_on_track_clear(
    tmp_path,
    messages,
    expected_statuses,
):
    lines = _feed_prefix()
    for index, message in enumerate(messages):
        timestamp = f"2026-08-23T13:01:{index * 10:02d}Z"
        lines.append(_line(
            "RaceControlMessages",
            {"Messages": [{"Utc": timestamp, "Category": "Flag", "Message": message}]},
            timestamp,
        ))
    feed = tmp_path / "status.txt"
    _write_feed(feed, lines)

    events = ingest_f1live(str(feed), session_id="test")

    assert [
        (item.session_time_ms, item.payload["status"])
        for item in events
        if item.type == "SessionStatusChanged"
    ] == expected_statuses
    assert [
        item.payload["message"]
        for item in events
        if item.type == "RaceControlMessage"
    ] == messages


def test_red_flag_restart_exposes_repeat_formation_laps(tmp_path):
    lines = _feed_prefix() + [
        _line(
            "RaceControlMessages",
            {"Messages": [{"Utc": "2026-08-23T13:01:00Z", "Category": "Flag",
                           "Message": "RED FLAG"}]},
            "2026-08-23T13:01:00Z",
        ),
        _line("SessionStatus", {"Status": "Started"}, "2026-08-23T13:20:00Z"),
        _line(
            "RaceControlMessages",
            {"Messages": [{"Utc": "2026-08-23T13:21:00Z", "Category": "Other",
                           "Message": "STANDING START"}]},
            "2026-08-23T13:21:00Z",
        ),
        _line(
            "RaceControlMessages",
            {"Messages": [{"Utc": "2026-08-23T13:22:00Z", "Category": "Other",
                           "Message": "EXTRA FORMATION LAP"}]},
            "2026-08-23T13:22:00Z",
        ),
        _line(
            "RaceControlMessages",
            {"Messages": [{"Utc": "2026-08-23T13:23:00Z", "Category": "Other",
                           "Message": "STANDING START"}]},
            "2026-08-23T13:23:00Z",
        ),
    ]
    feed = tmp_path / "restart-formation.txt"
    _write_feed(feed, lines)

    statuses = [
        (item.session_time_ms, item.payload["status"])
        for item in ingest_f1live(str(feed), session_id="test")
        if item.type == "SessionStatusChanged"
    ]

    assert statuses == [
        (0, "started"),
        (60_000, "red_flag"),
        (1_200_000, "formation"),
        (1_260_000, "started"),
        (1_320_000, "formation"),
        (1_380_000, "started"),
    ]


def test_complete_live_weekend_lifecycle_smoke(tmp_path, monkeypatch):
    """One deterministic race covers the browser Live contract end to end."""
    import racelens.api as api

    clock = [NOW]
    store = MemoryStore()
    recorder = Recorder(_config(tmp_path), now=lambda: clock[0], object_store=store)
    practice = ScheduledSession(
        SESSION.year,
        SESSION.round_number,
        SESSION.event_name,
        "FP1",
        SESSION.starts_at - timedelta(days=2),
    )
    recorder._schedule = [practice, SESSION]
    for phase in (Phase.RECORDING, Phase.CAPTURED, Phase.PROCESSING, Phase.COMPLETE):
        recorder.store.transition(practice.session_id, phase, NOW - timedelta(days=1))

    archive = recorder.config.data_dir / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    practice_replay = fixture_stem(practice)
    fixture = archive / f"{practice_replay}.jsonl"
    track = archive / f"{practice_replay}.track.json"
    positions = archive / f"{practice_replay}.positions.json"
    fixture.write_text("{}\n", encoding="utf-8")
    track.write_text(json.dumps({
        "session_id": practice_replay,
        "viewbox": [600, 400],
        "points": [[0, 0], [100, 0], [100, 100]],
    }), encoding="utf-8")
    positions.write_text("{}\n", encoding="utf-8")
    publish_session(
        store, practice.session_id, practice_replay,
        fixture, track, positions, event_count=1,
    )

    raw = recorder._paths(SESSION)["raw"]
    session_info = {
        "Meeting": {
            "Key": 15,
            "Number": 15,
            "Name": "Dutch Grand Prix",
            "Location": "Zandvoort",
        },
        "Key": 99,
        "Name": "Race",
        "StartDate": "2026-08-23T13:00:00Z",
        "GmtOffset": "02:00:00",
        "Path": "2026/Dutch/Race/",
    }
    lines = [
        _line("SessionInfo", json.dumps(session_info)),
        _line("DriverList", {"1": {"Tla": "VER"}, "4": {"Tla": "NOR"}}),
        _line("LapCount", {"CurrentLap": 0, "TotalLaps": 4}, "2026-08-23T12:50:00Z"),
        _line("TimingAppData", {"Lines": {
            "1": {"Stints": [{"Compound": "MEDIUM", "TotalLaps": 0}]},
            "4": {"Stints": [{"Compound": "SOFT", "TotalLaps": 0}]},
        }}),
        _line("TimingData", {"Lines": {
            "1": {"Position": "1"}, "4": {"Position": "2"},
        }}, "2026-08-23T12:59:01Z"),
    ]
    _write_feed(raw, lines)

    assert recorder._publish_live_snapshot(SESSION, raw)
    snapshot = store.objects[f"live/{SESSION.session_id}/snapshot.json"]
    assert snapshot["race_state"]["session_status"] == "formation"
    assert snapshot["race_state"]["lap"] == 0
    assert not any(item["kind"] == "SessionStarted" for item in snapshot["feed"]["en"])
    assert snapshot["stints"] == {"session_id": fixture_stem(SESSION), "total_laps": 4, "stints": {}}
    assert store.objects["live/current.json"]["track_replay_session_id"] == practice_replay

    formation_at_ms = snapshot["race_state"]["at_ms"]
    clock[0] += timedelta(seconds=45)
    assert recorder._publish_live_snapshot(SESSION, raw)
    quiet_formation = store.objects[f"live/{SESSION.session_id}/snapshot.json"]
    assert quiet_formation["race_state"]["session_status"] == "formation"
    assert quiet_formation["race_state"]["at_ms"] >= formation_at_ms + 45_000

    lines.extend([
        _line("SessionData", {"StatusSeries": [{
            "SessionStatus": "Started", "Utc": "2026-08-23T13:00:00Z",
        }]}),
        _line("SessionStatus", {"Status": "Started"}, "2026-08-23T13:00:00Z"),
        _line("TimingData", {"Lines": {
            "1": {"Position": "2"}, "4": {"Position": "1"},
        }}, "2026-08-23T13:00:01Z"),
        _line("TimingData", {"Lines": {
            "1": {"NumberOfLaps": 1, "LastLapTime": {"Value": "1:20.000"}},
            "4": {"NumberOfLaps": 1, "LastLapTime": {"Value": "1:21.000"}},
        }}, "2026-08-23T13:01:21Z"),
        _line("RaceControlMessages", {"Messages": [{
            "Utc": "2026-08-23T13:01:30Z", "Category": "Flag", "Message": "RED FLAG",
        }]}, "2026-08-23T13:01:30Z"),
        _line("RaceControlMessages", {"Messages": [{
            "Utc": "2026-08-23T13:01:35Z", "Category": "Other",
            "Message": "RACE WILL RESUME AT 15:03",
        }]}, "2026-08-23T13:01:35Z"),
        _line("RaceControlMessages", {"Messages": [{
            "Utc": "2026-08-23T13:01:40Z", "Category": "Flag",
            "Message": "GREEN LIGHT - PIT EXIT OPEN",
        }]}, "2026-08-23T13:01:40Z"),
    ])
    _write_feed(raw, lines)
    assert recorder._publish_live_snapshot(SESSION, raw)
    red = copy.deepcopy(store.objects[f"live/{SESSION.session_id}/snapshot.json"])
    assert red["race_state"]["session_status"] == "red_flag"
    assert red["race_state"]["at_ms"] == 100_000
    assert red["race_state"]["restart_at_ms"] == 180_000
    assert red["stints"]["stints"]["VER"][0]["compound"] == "Medium"
    assert any("RACE WILL RESUME" in item["text"] for item in red["feed"]["en"])

    clock[0] += timedelta(seconds=45)
    assert recorder._publish_live_snapshot(SESSION, raw)
    quiet_red = store.objects[f"live/{SESSION.session_id}/snapshot.json"]
    assert quiet_red["race_state"]["session_status"] == "red_flag"
    assert quiet_red["race_state"]["restart_at_ms"] == 180_000
    assert quiet_red["race_state"]["at_ms"] >= red["race_state"]["at_ms"] + 45_000

    lines.extend([
        _line("TimingData", {"Lines": {
            "1": {"InPit": False, "Position": "1"},
            "4": {"InPit": False, "Position": "2"},
        }}, "2026-08-23T13:02:25Z"),
        _line("SessionStatus", {"Status": "Started"}, "2026-08-23T13:02:30Z"),
        _line("RaceControlMessages", {"Messages": [{
            "Utc": "2026-08-23T13:02:30.500Z", "Category": "Other",
            "Message": "STANDING START",
        }]}, "2026-08-23T13:02:30.500Z"),
        _line("TimingData", {"Lines": {
            "1": {"Position": "2"}, "4": {"Position": "1"},
        }}, "2026-08-23T13:02:31Z"),
    ])
    _write_feed(raw, lines)
    clock[0] += timedelta(seconds=5)
    assert recorder._publish_live_snapshot(SESSION, raw)
    restarted = store.objects[f"live/{SESSION.session_id}/snapshot.json"]
    assert restarted["race_state"]["session_status"] == "started"
    assert restarted["race_state"]["restart_at_ms"] is None
    assert restarted["recent_passes"] == []

    lines.extend([
        _line("TimingData", {"Lines": {
            "1": {"NumberOfLaps": 2}, "4": {"NumberOfLaps": 2},
        }}, "2026-08-23T13:03:51Z"),
        _line("RaceControlMessages", {"Messages": [{
            "Utc": "2026-08-23T13:04:00Z", "Category": "Flag", "Message": "VSC DEPLOYED",
        }]}, "2026-08-23T13:04:00Z"),
        _line("RaceControlMessages", {"Messages": [{
            "Utc": "2026-08-23T13:04:10Z", "Category": "Flag", "Message": "VSC ENDING",
        }]}, "2026-08-23T13:04:10Z"),
        _line("RaceControlMessages", {"Messages": [{
            "Utc": "2026-08-23T13:04:11Z", "Category": "Flag", "Message": "TRACK CLEAR",
        }]}, "2026-08-23T13:04:11Z"),
        _line("RaceControlMessages", {"Messages": [{
            "Utc": "2026-08-23T13:04:12Z", "Category": "Flag",
            "Message": "BLACK AND WHITE FLAG FOR CAR 4 - DRIVING ERRATICALLY",
        }]}, "2026-08-23T13:04:12Z"),
        _line("TimingData", {"Lines": {"1": {"Stopped": True}}},
              "2026-08-23T13:04:15Z"),
    ])
    _write_feed(raw, lines)
    clock[0] += timedelta(seconds=5)
    assert recorder._publish_live_snapshot(SESSION, raw)
    active = store.objects[f"live/{SESSION.session_id}/snapshot.json"]
    assert active["race_state"]["session_status"] == "started"
    assert active["race_state"]["drivers"]["VER"]["stopped"] is True
    assert any("BLACK AND WHITE" in item["text"] for item in active["feed"]["en"])

    lines.extend([
        _line("TimingData", {"Lines": {"1": {"Stopped": False, "Retired": True}}},
              "2026-08-23T13:04:20Z"),
        _line("TimingData", {"Lines": {"4": {
            "NumberOfLaps": 4, "LastLapTime": {"Value": "1:20.500"},
        }}}, "2026-08-23T13:05:41Z"),
        _line("SessionStatus", {"Status": "Finished"}, "2026-08-23T13:05:42Z"),
    ])
    _write_feed(raw, lines)
    clock[0] += timedelta(seconds=5)
    assert recorder._publish_live_snapshot(SESSION, raw)
    finished = store.objects[f"live/{SESSION.session_id}/snapshot.json"]
    assert finished["race_state"]["session_status"] == "finished"
    assert finished["race_state"]["drivers"]["VER"]["retired"] is True

    monkeypatch.setattr(api, "_object_store", lambda: store)
    monkeypatch.setattr(api, "_live", None)
    monkeypatch.setattr(api, "_remote_live_cache", None)
    monkeypatch.setattr(api, "_utcnow", lambda: clock[0])
    assert api.live_stints()["total_laps"] == 4

    recorder._finish_live(SESSION)
    assert store.objects["live/current.json"]["status"] == "finishing"

    monkeypatch.setattr(api, "_remote_live_cache", None)
    monkeypatch.setattr(api, "REMOTE_CACHE_DIR", tmp_path / "remote-cache")
    api._remote_cache.cache_clear()
    assert api.live_track()["session_id"] == practice_replay

    replay = fixture_stem(SESSION)
    replay_files = []
    for suffix, content in (
        (".jsonl", "{}\n"),
        (".track.json", track.read_text(encoding="utf-8")),
        (".positions.json", "{}\n"),
    ):
        path = archive / f"{replay}{suffix}"
        path.write_text(content, encoding="utf-8")
        replay_files.append(path)
    publish_session(store, SESSION.session_id, replay, *replay_files, event_count=1)
    recorder._set_live_status(SESSION, "replay_ready")
    assert store.objects["live/current.json"]["status"] == "replay_ready"
