import copy
import hashlib
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import racelens.object_storage as storage
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
from racelens.radio.models import RadioSummary, CURRENT_RADIO_PROFILE, CURRENT_RADIO_POLICY
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


def test_s3_common_prefix_discovery_uses_bounded_delimiter_listing():
    class Paginator:
        request = None
        pages_read = 0

        def paginate(self, **kwargs):
            self.request = kwargs
            self.pages_read += 1
            yield {"CommonPrefixes": [{"Prefix": "sessions/a_race/"}, {"Prefix": "sessions/b_race/"}],
                   "Contents": [{"Key": f"sessions/a_race/revisions/{index:064x}/events.jsonl"}
                                for index in range(1001)]}
            self.pages_read += 1
            yield {"CommonPrefixes": [{"Prefix": "sessions/c_race/"}]}

    class Client:
        def get_paginator(self, operation):
            assert operation == "list_objects_v2"
            return paginator

    paginator = Paginator()
    store = storage.S3Store.__new__(storage.S3Store)
    store.bucket, store.client = "archive-bucket", Client()

    assert store.list_common_prefixes("sessions/", limit=2) == ["sessions/a_race/", "sessions/b_race/"]
    assert paginator.pages_read == 1
    assert paginator.request == {"Bucket": "archive-bucket", "Prefix": "sessions/", "Delimiter": "/",
                                 "PaginationConfig": {"MaxItems": 2, "PageSize": 2}}


@pytest.mark.parametrize("content,size,limit", [
    (b'{"bad": invalid}', 16, 100), (b"[]", 2, 100), (b"\xff", 1, 100),
    (b"oversized", 9, 8), (b"changed", 6, 100), (b"{}", -1, 100),
])
def test_s3_invalid_content_is_distinct_from_transient_storage_errors(content, size, limit):
    body = io.BytesIO(content)

    class Client:
        def get_object(self, **kwargs):
            assert kwargs == {"Bucket": "archive-bucket", "Key": "sessions/race/manifest.json"}
            return {"ContentLength": size, "Body": body}

    store = storage.S3Store.__new__(storage.S3Store)
    store.bucket, store.client = "archive-bucket", Client()

    with pytest.raises(StorageError) as error:
        store.get_json("sessions/race/manifest.json", limit=limit)

    assert type(error.value) is storage.ObjectContentError
    assert body.closed


def streaming_store(content=b"archive", *, declared_size=7, failure=None):
    class Body(io.BytesIO):
        def read(self, size=-1):
            if failure == "read":
                raise TimeoutError("https://private-provider.test/path?secret=token")
            return super().read(size)

        def close(self):
            if self.closed:
                return
            super().close()
            if failure == "close":
                raise OSError("https://private-provider.test/path?secret=token")

    body = Body(content)

    class Client:
        def get_object(self, **kwargs):
            if failure == "get":
                raise TimeoutError("https://private-provider.test/path?secret=token")
            return {"ContentLength": declared_size, "Body": body}

    store = storage.S3Store.__new__(storage.S3Store)
    store.bucket, store.client = "archive-bucket", Client()
    return store, body


def stream_operation(store, operation, destination):
    if operation == "get_bytes":
        return store.get_bytes("sessions/race/events.jsonl", limit=100)
    method = getattr(store, operation)
    args = ["sessions/race/events.jsonl"]
    if operation == "download_verified":
        args.append(destination)
    return method(*args, size=7, sha256=hashlib.sha256(b"archive").hexdigest())


@pytest.mark.parametrize("operation", ["verify", "download_verified", "get_bytes"])
@pytest.mark.parametrize("failure", ["get", "read", "close"])
def test_s3_stream_transport_failure_is_sanitized(tmp_path, operation, failure):
    import traceback

    store, body = streaming_store(failure=failure)
    destination = tmp_path / "events.jsonl"
    destination.write_bytes(b"previous")

    with pytest.raises(StorageError) as error:
        stream_operation(store, operation, destination)

    assert type(error.value) is StorageError
    details = "".join(traceback.format_exception(error.value))
    assert "private-provider" not in details
    assert "secret=token" not in details
    assert destination.read_bytes() == b"previous"
    assert not list(tmp_path.glob(".download-*"))
    if failure != "get":
        assert body.closed


@pytest.mark.parametrize("operation", ["verify", "download_verified"])
@pytest.mark.parametrize("content,declared_size", [
    (b"archive", 8), (b"archive", "invalid"), (b"archive", None),
    (b"short", 7), (b"archive-extra", 7), (b"corrupt", 7),
])
def test_s3_stream_corruption_is_permanent(tmp_path, operation, content, declared_size):
    store, body = streaming_store(content, declared_size=declared_size)
    destination = tmp_path / "events.jsonl"
    destination.write_bytes(b"previous")

    with pytest.raises(storage.ObjectContentError):
        stream_operation(store, operation, destination)

    assert body.closed
    assert destination.read_bytes() == b"previous"
    assert not list(tmp_path.glob(".download-*"))


@pytest.mark.parametrize("operation", ["verify", "download_verified"])
def test_s3_close_failure_does_not_mask_permanent_corruption(tmp_path, operation):
    store, body = streaming_store(b"corrupt", failure="close")
    with pytest.raises(storage.ObjectContentError):
        stream_operation(store, operation, tmp_path / "events.jsonl")
    assert body.closed


@pytest.mark.parametrize("failure", ["mkdir", "temporary", "fsync", "replace", "cleanup"])
def test_s3_download_local_failure_is_sanitized_and_retryable(tmp_path, monkeypatch, failure):
    import traceback

    store, body = streaming_store(failure="read" if failure == "cleanup" else None)
    destination = tmp_path / "events.jsonl"
    destination.write_bytes(b"previous")

    def fail(*args, **kwargs):
        raise OSError("/private/local/path/secret")

    owner, name = {
        "mkdir": (Path, "mkdir"),
        "temporary": (storage.tempfile, "NamedTemporaryFile"),
        "fsync": (storage.os, "fsync"),
        "replace": (storage.os, "replace"),
        "cleanup": (Path, "unlink"),
    }[failure]
    monkeypatch.setattr(owner, name, fail)

    with pytest.raises(StorageError) as error:
        stream_operation(store, "download_verified", destination)

    assert type(error.value) is StorageError
    details = "".join(traceback.format_exception(error.value))
    assert "/private/local/path" not in details
    assert "private-provider" not in details
    assert body.closed
    assert destination.read_bytes() == b"previous"


def test_s3_download_replaces_destination_only_after_verified_stream_closes(tmp_path):
    data = b"archive"
    store, body = streaming_store(data)
    destination = tmp_path / "events.jsonl"
    destination.write_bytes(b"previous")
    original_close = body.close

    def close_before_replacement():
        if body.closed:
            return
        assert destination.read_bytes() == b"previous"
        original_close()

    body.close = close_before_replacement
    stream_operation(store, "download_verified", destination)

    assert body.closed
    assert destination.read_bytes() == data
    assert not list(tmp_path.glob(".download-*"))


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


def _radio_summary():
    return RadioSummary(CURRENT_RADIO_PROFILE, CURRENT_RADIO_POLICY, 1, 1, 0, 0).to_manifest()


def _manifest(version=1):
    revision = "a" * 64
    prefix = "sessions/monaco_2024_race"
    value = {
        "schema_version": version,
        "canonical_session_id": "2024-08-r",
        "replay_session_id": "monaco_2024_race",
        "source": "fastf1",
        "created_at": "2026-10-13T10:00:00Z",
        "event_count": 3,
        "terminal_status": "ready",
        "files": {
            name: {
                "key": f"{prefix}/revisions/{revision}/{name}" if version == 2
                else f"{prefix}/{name}",
                "size": 1,
                "sha256": "b" * 64,
            }
            for name in ("events.jsonl", "track.json", "positions.json")
        },
    }
    if version == 2:
        value.update(
            revision=revision, parent_revision="c" * 64,
            radio_profile=CURRENT_RADIO_PROFILE, radio_policy=CURRENT_RADIO_POLICY,
            radio_summary={"total": 3, "accepted": 1, "rejected": 1, "manual": 1},
        )
    return value


@pytest.mark.parametrize("version", [1, 2])
def test_manifest_schemas_are_independent_of_live_records(version, monkeypatch):
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 99)
    value = _manifest(version)
    assert validate_manifest(value) == value


@pytest.mark.parametrize("parent", [None, "c" * 64])
def test_manifest_v2_accepts_initial_and_migrated_archives(parent):
    value = _manifest(2)
    value["parent_revision"] = parent
    assert validate_manifest(value) == value


@pytest.mark.parametrize(
    "field", ["revision", "parent_revision", "radio_profile", "radio_policy", "radio_summary"],
)
def test_manifest_v2_requires_provenance_fields(field):
    value = _manifest(2)
    value.pop(field)
    with pytest.raises(ManifestError):
        validate_manifest(value)


@pytest.mark.parametrize("key", [
    "sessions/monaco_2024_race/events.jsonl",
    "sessions/monaco_2024_race/revisions/../events.jsonl",
    "sessions/other/revisions/" + "a" * 64 + "/events.jsonl",
    "sessions/monaco_2024_race/revisions/" + "d" * 64 + "/events.jsonl",
    "/sessions/monaco_2024_race/revisions/" + "a" * 64 + "/events.jsonl",
])
def test_manifest_v2_rejects_unsafe_or_mismatched_keys(key):
    value = _manifest(2)
    value["files"]["events.jsonl"]["key"] = key
    with pytest.raises(ManifestError):
        validate_manifest(value)


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(unexpected="secret"),
    lambda value: value.update(schema_version=True),
    lambda value: value.update(revision="../revision"),
    lambda value: value.update(parent_revision="invalid"),
    lambda value: value.update(radio_profile=""),
    lambda value: value.update(radio_profile="x" * 129),
    lambda value: value.update(radio_policy="https://secret.example"),
    lambda value: value["radio_summary"].update(total=4),
    lambda value: value["radio_summary"].update(manual=True),
    lambda value: value["radio_summary"].update(accepted=-1),
    lambda value: value["radio_summary"].update(total=101, accepted=99),
    lambda value: value["radio_summary"].update(deferred=0),
    lambda value: value.update(event_count=2),
    lambda value: value["files"]["track.json"].update(extra=1),
])
def test_manifest_v2_rejects_invalid_metadata(mutate):
    value = _manifest(2)
    mutate(value)
    with pytest.raises(ManifestError):
        validate_manifest(value)


@pytest.mark.parametrize("version", [1, 2])
def test_manifest_digest_is_canonical_and_validates(version):
    value = _manifest(version)
    expected = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
    assert storage.manifest_digest(dict(reversed(list(value.items())))) == expected.hexdigest()
    value["unexpected"] = 1
    with pytest.raises(ManifestError):
        storage.manifest_digest(value)


def _published_archive(tmp_path, store):
    paths = _archive(tmp_path)
    current = publish_session(
        store, "2024-08-r", "monaco_2024_race", *paths, event_count=1,
    )
    store.operations.clear()
    return current, paths


def test_archive_revision_ignores_keys_and_counts_but_tracks_content_and_policy():
    files = _manifest()["files"]
    summary = _radio_summary()
    payload = {
        "files": {name: {"size": 1, "sha256": "b" * 64} for name in files},
        "radio_profile": CURRENT_RADIO_PROFILE,
        "radio_policy": CURRENT_RADIO_POLICY,
    }
    expected = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    revision = storage.archive_revision(files, summary)
    assert revision == expected.hexdigest()
    changed = copy.deepcopy(files)
    for row in changed.values():
        row["key"] = "unrelated/key"
    assert storage.archive_revision(changed, {**summary, "accepted": 0, "manual": 1}) == revision
    for field, value in (("size", 2), ("sha256", "c" * 64)):
        changed = copy.deepcopy(files)
        changed["events.jsonl"][field] = value
        assert storage.archive_revision(changed, summary) != revision
    for field in ("profile", "policy"):
        assert storage.archive_revision(files, {**summary, field: "new-version"}) != revision


def test_publish_revision_verifies_immutable_objects_before_and_after_manifest(tmp_path):
    class RecordingStore(MemoryStore):
        def verify(self, key, **kwargs):
            self.operations.append(("verify", key))
            super().verify(key, **kwargs)

    store = RecordingStore()
    current, paths = _published_archive(tmp_path, store)
    paths[0].write_bytes(b'{"event":"radio","text":"Box"}\n')
    old_objects = copy.deepcopy(store.objects)
    result = storage.publish_archive_revision(
        store, current, *paths, event_count=1, radio_summary=_radio_summary(),
    )
    assert result["schema_version"] == 2
    assert result["parent_revision"] == storage.manifest_digest(current)
    assert result["radio_summary"] == {"total": 1, "accepted": 1, "rejected": 0, "manual": 0}
    assert load_manifest(store, "monaco_2024_race") == result
    manifest_write = store.operations.index(("put_json", "sessions/monaco_2024_race/manifest.json"))
    for name, row in result["files"].items():
        assert row["key"] == f"sessions/monaco_2024_race/revisions/{result['revision']}/{name}"
        assert ("upload", row["key"]) in store.operations[:manifest_write]
        assert ("verify", row["key"]) in store.operations[:manifest_write]
        assert ("verify", row["key"]) in store.operations[manifest_write + 1:]
        assert store.objects[current["files"][name]["key"]] == old_objects[current["files"][name]["key"]]
    assert len([op for op in store.operations if op[0] == "upload"]) == 3
    assert not any(op[0] == "copy" for op in store.operations)


def test_publish_revision_duplicate_is_zero_uploads_and_next_parent_is_revision(tmp_path):
    store = MemoryStore()
    current, paths = _published_archive(tmp_path, store)
    first = storage.publish_archive_revision(
        store, current, *paths, event_count=1, radio_summary=_radio_summary(),
    )
    store.operations.clear()
    assert storage.publish_archive_revision(
        store, first, *paths, event_count=1, radio_summary=_radio_summary(),
    ) == first
    assert store.operations == []
    second = storage.publish_archive_revision(
        store, first, *paths, event_count=1,
        radio_summary={**_radio_summary(), "policy": "next-policy"},
    )
    assert second["parent_revision"] == first["revision"]
    for row in first["files"].values():
        assert row["key"] in store.objects


@pytest.mark.parametrize("field", ["files", "radio_profile", "radio_policy", "radio_summary"])
def test_publish_revision_does_not_trust_revision_label_alone(tmp_path, field):
    store = MemoryStore()
    current, paths = _published_archive(tmp_path, store)
    current = storage.publish_archive_revision(
        store, current, *paths, event_count=1, radio_summary=_radio_summary(),
    )
    if field == "files":
        row = current["files"]["events.jsonl"]
        data = b"different content\n"
        row.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        store.objects[row["key"]] = data
    elif field == "radio_summary":
        current[field] = {"total": 1, "accepted": 0, "rejected": 0, "manual": 1}
    else:
        current[field] = "different-version"
    store.put_json("sessions/monaco_2024_race/manifest.json", current)
    store.operations.clear()
    with pytest.raises(ManifestError, match="metadata differs"):
        storage.publish_archive_revision(
            store, current, *paths, event_count=1, radio_summary=_radio_summary(),
        )
    assert store.operations == []


@pytest.mark.parametrize("late", [False, True])
def test_publish_revision_detects_source_manifest_conflict(tmp_path, late):
    class ConflictStore(MemoryStore):
        def verify(self, key, **kwargs):
            super().verify(key, **kwargs)
            if late and "/revisions/" in key:
                self.objects["sessions/monaco_2024_race/manifest.json"]["created_at"] = (
                    "2026-10-14T10:00:00Z"
                )

    store = ConflictStore()
    current, paths = _published_archive(tmp_path, store)
    if not late:
        store.objects["sessions/monaco_2024_race/manifest.json"]["created_at"] = (
            "2026-10-14T10:00:00Z"
        )
    with pytest.raises(ManifestError, match="conflict"):
        storage.publish_archive_revision(
            store, current, *paths, event_count=1, radio_summary=_radio_summary(),
        )
    assert not any(op[0] == "put_json" for op in store.operations)
    if not late:
        assert store.operations == []


@pytest.mark.parametrize("operation", ["upload", "verify"])
@pytest.mark.parametrize("failure_index", [1, 2, 3])
def test_publish_revision_failure_keeps_old_manifest(tmp_path, operation, failure_index):
    class FailingStore(MemoryStore):
        calls = 0

        def upload_file(self, key, path, **kwargs):
            if operation == "upload" and "/revisions/" in key:
                self.calls += 1
                if self.calls == failure_index:
                    raise StorageError("injected upload failure")
            return super().upload_file(key, path, **kwargs)

        def verify(self, key, **kwargs):
            if operation == "verify" and "/revisions/" in key:
                self.calls += 1
                if self.calls == failure_index:
                    raise StorageError("injected verify failure")
            return super().verify(key, **kwargs)

    store = FailingStore()
    current, paths = _published_archive(tmp_path, store)
    with pytest.raises(StorageError, match="injected"):
        storage.publish_archive_revision(
            store, current, *paths, event_count=1, radio_summary=_radio_summary(),
        )
    assert load_manifest(store, "monaco_2024_race") == current
    for row in current["files"].values():
        store.verify(row["key"], size=row["size"], sha256=row["sha256"])


def test_publish_revision_reuses_verified_orphan_objects_after_failure(tmp_path):
    class FailingManifestStore(MemoryStore):
        fail = False

        def put_json(self, key, value):
            if self.fail and key.endswith("manifest.json"):
                raise StorageError("manifest failure")
            return super().put_json(key, value)

    store = FailingManifestStore()
    current, paths = _published_archive(tmp_path, store)
    store.fail = True
    with pytest.raises(StorageError, match="manifest failure"):
        storage.publish_archive_revision(
            store, current, *paths, event_count=1, radio_summary=_radio_summary(),
        )
    assert load_manifest(store, "monaco_2024_race") == current
    store.fail = False
    store.operations.clear()
    storage.publish_archive_revision(
        store, current, *paths, event_count=1, radio_summary=_radio_summary(),
    )
    assert not any(op[0] == "upload" for op in store.operations)


def test_publish_revision_detects_manifest_readback_mismatch(tmp_path):
    class ReadbackStore(MemoryStore):
        def get_json(self, key, *, limit):
            value = super().get_json(key, limit=limit)
            if value and value.get("schema_version") == 2:
                value["source"] = "changed"
            return value

    store = ReadbackStore()
    current, paths = _published_archive(tmp_path, store)
    with pytest.raises(StorageError, match="read-back"):
        storage.publish_archive_revision(
            store, current, *paths, event_count=1, radio_summary=_radio_summary(),
        )


@pytest.mark.parametrize("failure_index", [1, 2, 3])
def test_publish_revision_requires_each_post_manifest_object_read_verification(tmp_path, failure_index):
    class FailingStore(MemoryStore):
        committed_verifications = 0
        fail = True

        def verify(self, key, **kwargs):
            manifest = self.objects.get("sessions/monaco_2024_race/manifest.json", {})
            if self.fail and manifest.get("schema_version") == 2:
                self.committed_verifications += 1
                if self.committed_verifications == failure_index:
                    raise StorageError("post-manifest read verification failed")
            super().verify(key, **kwargs)

    store = FailingStore()
    current, paths = _published_archive(tmp_path, store)

    with pytest.raises(StorageError, match="post-manifest"):
        storage.publish_archive_revision(
            store, current, *paths, event_count=1, radio_summary=_radio_summary(),
        )

    committed = load_manifest(store, "monaco_2024_race")
    assert committed["parent_revision"] == storage.manifest_digest(current)
    store.fail = False
    store.operations.clear()
    assert storage.publish_archive_revision(
        store, committed, *paths, event_count=1, radio_summary=_radio_summary(),
    ) == committed
    assert store.operations == []


def test_publish_session_with_radio_summary_creates_initial_v2(tmp_path):
    store = MemoryStore()
    paths = _archive(tmp_path)
    result = publish_session(
        store, "2024-08-r", "monaco_2024_race", *paths,
        event_count=1, radio_summary=_radio_summary(),
    )
    assert result["schema_version"] == 2
    assert result["parent_revision"] is None
    assert all("/revisions/" in row["key"] for row in result["files"].values())
    store.operations.clear()
    assert publish_session(
        store, "2024-08-r", "monaco_2024_race", *paths,
        event_count=1, radio_summary=_radio_summary(),
    ) == result
    assert store.operations == []
    assert publish_session(
        store, "2024-08-r", "monaco_2024_race", *paths, event_count=1,
    ) == result


@pytest.mark.parametrize("mutate", [
    lambda files, summary: files.pop("track.json"),
    lambda files, summary: files["events.jsonl"].update(size=True),
    lambda files, summary: files["events.jsonl"].update(sha256="unsafe"),
    lambda files, summary: summary.update(total=2),
])
def test_archive_revision_rejects_invalid_inputs(mutate):
    files, summary = _manifest()["files"], _radio_summary()
    mutate(files, summary)
    with pytest.raises(ManifestError):
        storage.archive_revision(files, summary)


@pytest.mark.parametrize("version", [1, 2])
def test_publish_session_radio_summary_cannot_upgrade_ready_archive(tmp_path, version):
    store = MemoryStore()
    paths = _archive(tmp_path)
    options = {"radio_summary": _radio_summary()} if version == 2 else {}
    current = publish_session(
        store, "2024-08-r", "monaco_2024_race", *paths, event_count=1, **options,
    )
    store.operations.clear()
    with pytest.raises(ManifestError, match="different ready archive"):
        publish_session(
            store, "2024-08-r", "monaco_2024_race", *paths, event_count=1,
            radio_summary={**_radio_summary(), "policy": "new-policy"},
        )
    assert load_manifest(store, "monaco_2024_race") == current
    assert store.operations == []


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
