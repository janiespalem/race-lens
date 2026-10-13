import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from threading import Barrier

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from racelens.events.models import dump_jsonl  # noqa: E402
from racelens.object_storage import RemoteSessionCache, publish_session  # noqa: E402
import racelens.object_storage as storage  # noqa: E402
from racelens.radio.models import RadioSummary, CURRENT_RADIO_PROFILE, CURRENT_RADIO_POLICY  # noqa: E402
from tests.test_object_storage import MemoryStore  # noqa: E402
from tests.test_replay import mini_race  # noqa: E402


def test_remote_cache_materializes_revision_once_for_concurrent_readers(tmp_path):
    class CountingStore(MemoryStore):
        downloads = []

        def download_verified(self, key, *args, **kwargs):
            self.downloads.append(key)
            return super().download_verified(key, *args, **kwargs)

    store = CountingStore()
    fixture, track, positions = (tmp_path / name for name in ("race.jsonl", "track", "positions"))
    fixture.write_bytes(b'{"event":"old"}\n')
    track.write_bytes(b'{}\n')
    positions.write_bytes(b'{}\n')
    current = publish_session(
        store, "2024-08-r", "monaco_2024_race", fixture, track, positions, event_count=1,
    )
    cache = RemoteSessionCache(store, tmp_path / "cache")
    with cache.lease("monaco_2024_race") as root:
        assert (root / "monaco_2024_race.jsonl").read_bytes() == b'{"event":"old"}\n'
    fixture.write_bytes(b'{"event":"new"}\n')
    revised = storage.publish_archive_revision(
        store, current, fixture, track, positions, event_count=1,
        radio_summary=RadioSummary(CURRENT_RADIO_PROFILE, CURRENT_RADIO_POLICY, 0, 0, 0, 0).to_manifest(),
    )
    barrier = Barrier(4)

    def read(_):
        with cache.lease("monaco_2024_race") as root:
            barrier.wait(timeout=5)
            return (root / "monaco_2024_race.jsonl").read_bytes()

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(read, range(4))) == [b'{"event":"new"}\n'] * 4
    assert store.downloads.count(revised["files"]["events.jsonl"]["key"]) == 1
    assert len(store.downloads) == 6
    assert cache.stats()["materializations"] == 2
    assert cache.stats()["leases"] == 0


def test_remote_api_reads_survive_forced_eviction(tmp_path, monkeypatch):
    import racelens.api as api

    store = MemoryStore()
    sizes = []
    for replay_id in ("alpha_2024_race", "beta_2024_race"):
        events = mini_race()
        for item in events:
            item.session_id = replay_id
        fixture = tmp_path / f"{replay_id}.jsonl"
        track = tmp_path / f"{replay_id}.track.json"
        positions = tmp_path / f"{replay_id}.positions.json"
        fixture.write_text(dump_jsonl(events), encoding="utf-8")
        track.write_text(json.dumps({"session_id": replay_id, "points": [[0, 0]]}))
        positions.write_text(json.dumps({
            "session_id": replay_id,
            "start_ms": 0,
            "tick_ms": 1000,
            "drivers": {},
        }))
        publish_session(
            store, "2024-08-r", replay_id, fixture, track, positions,
            event_count=len(events),
        )
        sizes.append(sum(path.stat().st_size for path in (fixture, track, positions)))

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    cache = RemoteSessionCache(store, tmp_path / "cache")
    cache.max_bytes = max(sizes) + 100
    monkeypatch.setattr(api, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(api, "_remote_cache", lambda: cache)
    api._engine_cached.cache_clear()
    api._positions_data_cached.cache_clear()

    def read(replay_id):
        client = TestClient(api.app)
        responses = [
            client.get(f"/api/sessions/{replay_id}/state", params={"at_ms": 0}),
            client.get(f"/api/sessions/{replay_id}/track"),
            client.get(f"/api/sessions/{replay_id}/positions"),
            client.get(f"/api/sessions/{replay_id}/timeline"),
        ]
        assert [response.status_code for response in responses] == [200] * 4
        return [response.json()["session_id"] for response in responses]

    assert read("alpha_2024_race") == ["alpha_2024_race"] * 4
    assert {path.name for path in cache.directory.iterdir()} == {"alpha_2024_race"}
    assert read("beta_2024_race") == ["beta_2024_race"] * 4
    assert {path.name for path in cache.directory.iterdir()} == {"beta_2024_race"}
    assert read("alpha_2024_race") == ["alpha_2024_race"] * 4
    assert {path.name for path in cache.directory.iterdir()} == {"alpha_2024_race"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(read, ("alpha_2024_race", "beta_2024_race")))
    assert results == [["alpha_2024_race"] * 4, ["beta_2024_race"] * 4]


def test_diagnostics_are_operational_and_sanitized(tmp_path, monkeypatch):
    import racelens.api as api

    class Cache:
        def stats(self):
            return {
                "materializations": 2,
                "hits": 3,
                "misses": 2,
                "leases": 1,
                "evictions": 1,
                "bytes": 456,
                "disk_bytes": 123,
                "max_bytes": 789,
            }

    class Live:
        auto_stopped = False

        def status(self):
            return {
                "consecutive_failures": 0,
                "data_quality": "good",
                "last_error": "SECRET_ACCESS_KEY=hidden",
            }

    class Capture:
        alive = False

    monkeypatch.setenv("RACELENS_REVISION", "abc1234")
    monkeypatch.setattr(api, "STORAGE_CONFIG", SimpleNamespace(
        bucket="private-bucket", secret_access_key="SECRET_ACCESS_KEY=hidden",
    ))
    monkeypatch.setattr(api, "REMOTE_CACHE_DIR", tmp_path / "private-bucket")
    monkeypatch.setattr(api, "_remote_cache", lambda: Cache())
    monkeypatch.setattr(api, "_live", Live())
    monkeypatch.setattr(api, "_capture", Capture())
    monkeypatch.setattr(api, "_live_session_id", "monaco_2024_race")

    response = TestClient(api.app).get("/api/diagnostics")
    body = response.json()

    assert response.status_code == 200
    assert body["revision"] == "abc1234"
    assert body["parsed_cache"]["engine"]["max_size"] == 1
    assert body["parsed_cache"]["positions"]["max_size"] == 1
    assert body["remote_cache"] == {
        "materializations": 2,
        "hits": 3,
        "misses": 2,
        "leases": 1,
        "evictions": 1,
        "bytes": 456,
        "disk_bytes": 123,
        "max_bytes": 789,
    }
    assert body["live"] == {"source": "signalr", "freshness": "stale"}
    encoded = json.dumps(body)
    assert "SECRET_ACCESS_KEY" not in encoded
    assert "private-bucket" not in encoded
    assert str(tmp_path) not in encoded


def test_diagnostics_measure_keepalive_interval_and_memory_pressure(tmp_path, monkeypatch):
    import racelens.api as api

    current = tmp_path / "memory.current"
    maximum = tmp_path / "memory.max"
    current.write_text("200\n", encoding="ascii")
    maximum.write_text("800\n", encoding="ascii")
    started = datetime(2026, 8, 14, 12, tzinfo=UTC)
    clock = iter((started, started + timedelta(minutes=5), started + timedelta(minutes=6)))
    monkeypatch.setattr(api, "_CGROUP_MEMORY_CURRENT", current)
    monkeypatch.setattr(api, "_CGROUP_MEMORY_MAX", maximum)
    monkeypatch.setattr(api, "_ping_stats", {
        "requests": 0, "last_seen": None, "previous_interval_seconds": None,
    })
    monkeypatch.setattr(api, "_utcnow", lambda: next(clock))
    monkeypatch.setattr(api, "_remote_cache", lambda: None)
    monkeypatch.setattr(api, "_live", None)
    client = TestClient(api.app)

    assert client.get("/api/health").status_code == 200
    assert client.get("/api/ping").status_code == 200
    assert client.get("/api/ping").status_code == 200
    body = client.get("/api/diagnostics").json()

    assert body["keepalive"] == {
        "requests": 2,
        "last_seen": "2026-08-14T12:05:00Z",
        "previous_interval_seconds": 300.0,
        "age_seconds": 60.0,
    }
    assert body["memory"] == {
        "current_bytes": 200,
        "limit_bytes": 800,
        "pressure_percent": 25.0,
    }


def test_diagnostics_preserve_signalr_source_after_capture_reap(monkeypatch):
    import racelens.api as api

    class Live:
        auto_stopped = True

        def status(self):
            return {"consecutive_failures": 0, "data_quality": "good"}

    class Capture:
        alive = False

        def stop(self):
            pass

    monkeypatch.setattr(api, "_remote_cache", lambda: None)
    monkeypatch.setattr(api, "_live", Live())
    monkeypatch.setattr(api, "_capture", Capture())
    monkeypatch.setattr(api, "_live_session_id", "monaco_2024_race")

    body = TestClient(api.app).get("/api/diagnostics").json()

    assert api._capture is None
    assert body["live"] == {"source": "signalr", "freshness": "fresh"}
