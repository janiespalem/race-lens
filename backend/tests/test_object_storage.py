import copy
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from racelens.object_storage import (
    MAX_OBJECT_BYTES,
    ManifestError,
    ObjectPreparationQueue,
    RemoteSessionCache,
    StorageError,
    load_manifest,
    publish_session,
    validate_manifest,
)
from racelens.preparations import QueueFullError
from racelens.events.models import event
from racelens.replay.engine import ReplayEngine
from racelens.object_storage import LiveRecordError, _validate_race_state


class MemoryStore:
    def __init__(self):
        self.objects = {}
        self.operations = []

    def list_keys(self, prefix, *, limit=1000):
        keys = sorted(key for key in self.objects if key.startswith(prefix))
        if len(keys) > limit:
            raise RuntimeError("too many")
        return keys

    def get_json(self, key, *, limit):
        value = self.objects.get(key)
        return copy.deepcopy(value) if isinstance(value, dict) else None

    def put_json(self, key, value):
        self.operations.append(("put_json", key))
        self.objects[key] = copy.deepcopy(value)

    def upload_file(self, key, path, *, sha256):
        data = Path(path).read_bytes()
        assert hashlib.sha256(data).hexdigest() == sha256
        self.operations.append(("upload", key))
        self.objects[key] = data

    def verify(self, key, *, size, sha256):
        data = self.objects[key]
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
            raise ManifestError("checksum")

    def matches(self, key, *, size, sha256):
        data = self.objects.get(key)
        return (
            isinstance(data, bytes)
            and len(data) == size
            and hashlib.sha256(data).hexdigest() == sha256
        )

    def copy(self, source, destination):
        self.operations.append(("copy", destination))
        self.objects[destination] = self.objects[source]

    def delete(self, key):
        self.objects.pop(key, None)

    def download_verified(self, key, destination, *, size, sha256):
        data = self.objects[key]
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
            raise ManifestError("checksum")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)


def test_live_snapshot_accepts_old_and_new_race_control_contract():
    state = ReplayEngine([event("race", "SessionStarted", 0)]).state_at(10)
    state.update(frame_source="live", viewbox=None)
    _validate_race_state(state, "race")
    old = {key: value for key, value in state.items() if key not in {
        "session_phase", "control_mode", "control_since_ms", "sector_flags", "finish_condition",
    }}
    _validate_race_state(old, "race")
    invalid = {**state, "sector_flags": {"14": "green"}}
    with pytest.raises(LiveRecordError):
        _validate_race_state(invalid, "race")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda state: state.pop("control_mode"),
        lambda state: state.update(control_since_ms=state["at_ms"] + 1),
        lambda state: state.update(session_status="red_flag"),
        lambda state: state.update(
            session_phase="finished",
            session_status="finished",
            finish_condition=None,
        ),
        lambda state: state.update(
            session_phase="finished",
            session_status="finished",
            finish_condition={
                "at_ms": state["at_ms"] + 1,
                "control_mode": "green",
                "sector_flags": {},
            },
        ),
    ],
)
def test_live_snapshot_rejects_inconsistent_race_control_contract(mutate):
    state = ReplayEngine([event("race", "SessionStarted", 0)]).state_at(10)
    state.update(frame_source="live", viewbox=None)
    mutate(state)

    with pytest.raises(LiveRecordError):
        _validate_race_state(state, "race")


def _archive(tmp_path):
    paths = []
    for name, content in (
        ("race.jsonl", b'{"event":"lap"}\n'),
        ("race.track.json", b'{"points":[[0,0],[1,1]]}\n'),
        ("race.positions.json", b'{"drivers":{"VER":[[0,0]]}}\n'),
    ):
        path = tmp_path / name
        path.write_bytes(content)
        paths.append(path)
    return paths


def test_manifest_is_final_and_remote_cache_rejects_corruption(tmp_path):
    store = MemoryStore()
    fixture, track, positions = _archive(tmp_path)
    manifest = publish_session(
        store,
        "2024-08-r",
        "monaco_2024_race",
        fixture,
        track,
        positions,
        event_count=1,
    )

    assert store.operations[-1] == (
        "put_json",
        "sessions/monaco_2024_race/manifest.json",
    )
    assert load_manifest(store, "monaco_2024_race") == manifest
    cache = RemoteSessionCache(store, tmp_path / "cache")
    with cache.lease("monaco_2024_race") as root:
        assert (root / "monaco_2024_race.jsonl").read_bytes() == fixture.read_bytes()
        (root / "monaco_2024_race.positions.json").unlink()
    with cache.lease("monaco_2024_race") as root:
        assert root.joinpath("monaco_2024_race.positions.json").is_file()

    store.objects["sessions/monaco_2024_race/events.jsonl"] = b"corrupt"
    with cache.lease("monaco_2024_race") as root:
        (root / ".manifest-sha256").unlink()
    with pytest.raises(ManifestError, match="checksum"):
        with cache.lease("monaco_2024_race"):
            pass


def test_publish_session_reads_back_manifest(tmp_path):
    class ReadbackStore(MemoryStore):
        def get_json(self, key, *, limit):
            if key == "sessions/monaco_2024_race/manifest.json":
                return None
            return super().get_json(key, limit=limit)

    store = ReadbackStore()
    fixture, track, positions = _archive(tmp_path)
    with pytest.raises(StorageError, match="read-back"):
        publish_session(
            store,
            "2024-08-r",
            "monaco_2024_race",
            fixture,
            track,
            positions,
            event_count=1,
        )


def test_remote_cache_leases_block_eviction_and_cold_load_once(tmp_path):
    class CountingStore(MemoryStore):
        downloads = 0

        def download_verified(self, *args, **kwargs):
            self.downloads += 1
            time.sleep(0.01)
            return super().download_verified(*args, **kwargs)

    store = CountingStore()
    fixture, track, positions = _archive(tmp_path)
    for replay_id in ("alpha_2024_race", "beta_2024_race"):
        publish_session(
            store, "2024-08-r", replay_id, fixture, track, positions, event_count=1,
        )
    cache = RemoteSessionCache(store, tmp_path / "cache")
    cache.max_bytes = sum(path.stat().st_size for path in (fixture, track, positions)) + 100

    with cache.lease("alpha_2024_race") as alpha:
        with cache.lease("beta_2024_race") as beta:
            assert alpha.is_dir() and beta.is_dir()
            assert cache.stats()["leases"] == 2
        assert alpha.is_dir()
        assert not beta.exists()

    cold = RemoteSessionCache(store, tmp_path / "cold")

    def load_once(_):
        with cold.lease("alpha_2024_race") as root:
            return root.joinpath("alpha_2024_race.jsonl").read_bytes()

    before = store.downloads
    with ThreadPoolExecutor(max_workers=4) as pool:
        payloads = list(pool.map(load_once, range(4)))

    assert payloads == [fixture.read_bytes()] * 4
    assert store.downloads - before == 3
    assert cold.stats()["materializations"] == 1
    assert cold.stats()["misses"] == 1
    assert cold.stats()["hits"] == 3
    assert cold.stats()["leases"] == 0


def test_manifest_rejects_oversized_or_unexpected_objects(tmp_path):
    store = MemoryStore()
    fixture, track, positions = _archive(tmp_path)
    manifest = publish_session(
        store,
        "2024-08-r",
        "monaco_2024_race",
        fixture,
        track,
        positions,
        event_count=1,
    )
    manifest["files"]["events.jsonl"]["size"] = MAX_OBJECT_BYTES + 1
    with pytest.raises(ManifestError, match="events.jsonl"):
        validate_manifest(manifest)

    manifest = store.objects["sessions/monaco_2024_race/manifest.json"]
    manifest["files"]["extra"] = {
        "key": "sessions/monaco_2024_race/extra",
        "size": 1,
        "sha256": "0" * 64,
    }
    with pytest.raises(ManifestError, match="file set"):
        validate_manifest(manifest)


def test_object_queue_is_bounded_idempotent_and_retries_after_worker_failure():
    store = MemoryStore()
    queue = ObjectPreparationQueue(store, max_jobs=1, daily_max=1, max_attempts=2)

    first, created = queue.enqueue("2024-08-r", "monaco_2024_race")
    duplicate, created_again = queue.enqueue("2024-08-r", "monaco_2024_race")
    assert created is True
    assert created_again is False
    assert duplicate == first
    with pytest.raises(QueueFullError):
        queue.enqueue("2024-09-r", "canada_2024_race")

    claimed = queue.claim_next()
    assert claimed["status"] == "processing"
    retry = queue.finish("2024-08-r", error="secret-key-must-not-leak")
    assert retry["status"] == "queued"
    assert "secret-key" not in retry["error"]
    retry["retry_at"] = None
    store.put_json("status/2024-08-r.json", retry)
    claimed_again = queue.claim_next()
    assert claimed_again["attempts"] == 2
    failed = queue.finish("2024-08-r", error="Archive source is unavailable")
    assert failed["status"] == "failed"

    retried, retried_now = queue.enqueue("2024-08-r", "monaco_2024_race")
    assert retried_now is True
    assert retried["status"] == "queued"
    assert retried["generation"] == 2


def test_ready_queue_record_disappears_when_an_archive_object_is_corrupt(tmp_path):
    store = MemoryStore()
    fixture, track, positions = _archive(tmp_path)
    queue = ObjectPreparationQueue(store)
    queue.enqueue("2024-08-r", "monaco_2024_race")
    queue.claim_next()
    publish_session(
        store,
        "2024-08-r",
        "monaco_2024_race",
        fixture,
        track,
        positions,
        event_count=1,
    )
    queue.finish("2024-08-r", replay_session_id="monaco_2024_race")
    store.objects["sessions/monaco_2024_race/events.jsonl"] = b"corrupt"
    queue._cache = None

    record = queue.get("2024-08-r")
    assert record["status"] == "failed"
    assert record["replay_session_id"] is None
