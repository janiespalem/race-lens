import copy
import hashlib
import io
import json
from datetime import UTC, datetime, timedelta

import pytest

from racelens.events.models import dump_jsonl, event, load_jsonl, make_event_id
from racelens.object_storage import MAX_MANIFEST_BYTES, ManifestError, S3Store, StorageError
from racelens.radio.archive_maintenance import MaintenanceBudget, RadioArchiveMaintainer
from racelens.radio.models import (
    CURRENT_RADIO_POLICY,
    CURRENT_RADIO_PROFILE,
    FinalTranscriptDecision,
)


class MemoryStore:
    def __init__(self):
        self.objects = {}
        self.downloads = []
        self.reads = []
        self.operations = []

    def list_keys(self, prefix, *, limit=1000):
        keys = sorted(key for key in self.objects if key.startswith(prefix))
        if len(keys) > limit:
            raise StorageError("listing exceeds bound")
        return keys

    def list_common_prefixes(self, prefix, *, limit=1000):
        return sorted(
            {
                prefix + key[len(prefix) :].split("/", 1)[0] + "/"
                for key in self.objects
                if key.startswith(prefix)
            }
        )[:limit]

    def get_json(self, key, *, limit):
        self.reads.append(key)
        return copy.deepcopy(self.objects.get(key))

    def download_verified(self, key, destination, *, size, sha256):
        self.downloads.append(key)
        data = self.objects[key]
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
            raise ManifestError("checksum mismatch")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)

    def matches(self, key, *, size, sha256):
        data = self.objects.get(key)
        return (
            isinstance(data, bytes)
            and len(data) == size
            and hashlib.sha256(data).hexdigest() == sha256
        )

    def verify(self, key, *, size, sha256):
        if not self.matches(key, size=size, sha256=sha256):
            raise ManifestError("checksum mismatch")

    def upload_file(self, key, path, *, sha256):
        data = path.read_bytes()
        assert hashlib.sha256(data).hexdigest() == sha256
        self.operations.append(("upload", key))
        self.objects[key] = data

    def put_json(self, key, value):
        self.operations.append(("put_json", key))
        self.objects[key] = copy.deepcopy(value)


class ScriptedTranscriber:
    def __init__(self, decisions=None):
        self.calls = []
        self.decisions = list(decisions or [])

    def __call__(self, url):
        self.calls.append(url)
        if self.decisions:
            return self.decisions.pop(0)
        return FinalTranscriptDecision(
            "accepted",
            "Box this lap",
            None,
            {
                "profile": CURRENT_RADIO_PROFILE,
                "policy": CURRENT_RADIO_POLICY,
                "status": "accepted",
                "reason": None,
            },
        )


class JsonManifestStore(MemoryStore):
    def get_json(self, key, *, limit):
        self.reads.append(key)
        raw = self.objects[key]

        class Client:
            def get_object(self, **kwargs):
                assert kwargs == {"Bucket": "archive-bucket", "Key": key}
                return {"ContentLength": len(raw), "Body": io.BytesIO(raw)}

        reader = S3Store.__new__(S3Store)
        reader.bucket, reader.client = "archive-bucket", Client()
        return reader.get_json(key, limit=limit)


def add_archive(store, replay="singapore_2024_race", *, count=1, current=False, payload=None):
    rows = [
        event(
            replay,
            "RaceControlMessage",
            index * 1000,
            "VER",
            category="Radio",
            audio_url=f"https://audio.example/{replay}/{index}",
            **(payload or {}),
        )
        for index in range(count)
    ]
    rows.extend(
        event(replay, "LapCompleted", lap * 60000, driver, lap=lap)
        for driver in ("VER", "HAM") for lap in range(1, 4)
    )
    prefix = f"sessions/{replay}"
    if current:
        prefix += "/revisions/" + "a" * 64
    files = {}
    for name, data in {
        "events.jsonl": dump_jsonl(rows).encode(),
        "track.json": json.dumps({
            "session_id": replay, "points": [[0, 0], [1, 1]],
            "progress_points": [[0, 0], [1, 1]], "viewbox": [600, 400],
        }).encode(),
        "positions.json": json.dumps({
            "session_id": replay, "tick_ms": 1000, "viewbox": [600, 400],
            "drivers": {"VER": [[0, 0]] * 300}, "progress": {"VER": [0.0] * 300},
        }).encode(),
    }.items():
        key = f"{prefix}/{name}"
        store.objects[key] = data
        files[name] = {"key": key, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    manifest = {
        "schema_version": 2 if current else 1,
        "canonical_session_id": "2024-18-r",
        "replay_session_id": replay,
        "source": "fastf1",
        "created_at": "2026-10-13T10:00:00Z",
        "event_count": len(rows),
        "terminal_status": "ready",
        "files": files,
    }
    if current:
        manifest.update(
            revision="a" * 64,
            parent_revision=None,
            radio_profile=CURRENT_RADIO_PROFILE,
            radio_policy=CURRENT_RADIO_POLICY,
            radio_summary={"total": count, "accepted": count, "rejected": 0, "manual": 0},
        )
    store.objects[f"sessions/{replay}/manifest.json"] = manifest
    return copy.deepcopy(manifest)


def maintainer(store, tmp_path, transcriber, **kwargs):
    kwargs.setdefault("publish", False)
    return RadioArchiveMaintainer(
        store, tmp_path, transcriber, now=lambda: datetime(2026, 10, 13, 12, tzinfo=UTC), **kwargs
    )


def add_publishable_archive(store, *, extra_rows=()):
    manifest = add_archive(store)
    replay = manifest["replay_session_id"]
    radio = [
        row for row in load_jsonl(store.objects[manifest["files"]["events.jsonl"]["key"]].decode())
        if row.payload.get("category") == "Radio"
    ]
    rows = radio + [
        event(replay, "LapCompleted", lap * 60000, driver, lap=lap)
        for driver in ("VER", "HAM")
        for lap in range(1, 4)
    ] + list(extra_rows)
    artifacts = {
        "events.jsonl": dump_jsonl(rows).encode(),
        "track.json": json.dumps({
            "session_id": replay, "points": [[0, 0], [1, 1]],
            "progress_points": [[0, 0], [1, 1]], "viewbox": [600, 400],
        }).encode(),
        "positions.json": json.dumps({
            "session_id": replay, "tick_ms": 1000, "viewbox": [600, 400],
            "drivers": {"VER": [[0, 0]] * 300}, "progress": {"VER": [0.0] * 300},
        }).encode(),
    }
    for name, data in artifacts.items():
        row = manifest["files"][name]
        row.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        store.objects[row["key"]] = data
    manifest["event_count"] = len(rows)
    store.objects[f"sessions/{replay}/manifest.json"] = manifest
    return copy.deepcopy(manifest)


def test_current_archive_performs_zero_downloads_or_inference(tmp_path):
    store, transcriber = MemoryStore(), ScriptedTranscriber()
    add_archive(store, current=True)

    result = maintainer(store, tmp_path, transcriber).run_once(MaintenanceBudget())

    assert result.status == "idle"
    assert not store.downloads
    assert not transcriber.calls


def test_legacy_archive_is_selected_once(tmp_path):
    store, transcriber = MemoryStore(), ScriptedTranscriber()
    original = add_archive(store)
    worker = maintainer(store, tmp_path, transcriber)

    first = worker.run_once(MaintenanceBudget())
    second = worker.run_once(MaintenanceBudget())

    assert first.status == "progressed"
    assert first.replay_session_id == "singapore_2024_race"
    assert second.clips_processed == 0
    assert len(transcriber.calls) == 1
    assert store.objects["sessions/singapore_2024_race/manifest.json"] == original


def test_only_one_archive_is_selected_per_run(tmp_path):
    store, transcriber = MemoryStore(), ScriptedTranscriber()
    add_archive(store, "a_race")
    add_archive(store, "b_race")

    result = maintainer(store, tmp_path, transcriber).run_once(MaintenanceBudget())

    assert result.replay_session_id == "a_race"
    assert transcriber.calls == ["https://audio.example/a_race/0"]
    assert not any("b_race" in key for key in store.downloads)


def test_manual_transcript_is_never_sent_to_transcriber(tmp_path):
    store, transcriber = MemoryStore(), ScriptedTranscriber()
    original = add_archive(store, payload={"transcript": "Leave the tyres alone"})

    result = maintainer(store, tmp_path, transcriber).run_once(MaintenanceBudget())

    assert result.status == "progressed"
    assert not transcriber.calls
    assert store.objects["sessions/singapore_2024_race/manifest.json"] == original


def test_radio_clip_limit_fails_before_transcription(tmp_path):
    store, transcriber = MemoryStore(), ScriptedTranscriber()
    original = add_archive(store, count=101)

    result = maintainer(store, tmp_path, transcriber).run_once(MaintenanceBudget())

    assert result.status == "quarantined"
    assert not transcriber.calls
    assert store.objects["sessions/singapore_2024_race/manifest.json"] == original


def test_malformed_provenance_is_quarantined(tmp_path):
    store, transcriber = MemoryStore(), ScriptedTranscriber()
    original = add_archive(
        store, payload={"transcript": "Box", "transcript_metadata": {"status": "accepted"}}
    )

    result = maintainer(store, tmp_path, transcriber).run_once(MaintenanceBudget())

    assert result.status == "quarantined"
    assert not transcriber.calls
    assert store.objects["sessions/singapore_2024_race/manifest.json"] == original


def test_corrupt_checkpoint_is_quarantined(tmp_path):
    store, transcriber = MemoryStore(), ScriptedTranscriber()
    original = add_archive(store)
    worker = maintainer(store, tmp_path, transcriber)
    worker.run_once(MaintenanceBudget())
    checkpoint = next(tmp_path.glob("jobs/*/checkpoint.json"))
    checkpoint.write_text('{"decisions": broken')
    transcriber.calls.clear()

    result = maintainer(store, tmp_path, transcriber).run_once(MaintenanceBudget())

    assert result.status == "quarantined"
    assert not transcriber.calls
    assert store.objects["sessions/singapore_2024_race/manifest.json"] == original


def test_restart_reuses_terminal_clip_checkpoints(tmp_path):
    store, transcriber = MemoryStore(), ScriptedTranscriber()
    add_archive(store, count=3)
    first = maintainer(store, tmp_path, transcriber).run_once(MaintenanceBudget(max_clips=1))

    second = maintainer(store, tmp_path, transcriber).run_once(MaintenanceBudget())

    assert first.clips_processed == 1
    assert second.status == "progressed"
    assert second.clips_processed == 2
    assert second.clips_reused == 1
    assert second.accepted == 3
    assert transcriber.calls == [
        f"https://audio.example/singapore_2024_race/{index}" for index in range(3)
    ]


def test_three_clip_budget_stops_between_clips(tmp_path):
    store, transcriber = MemoryStore(), ScriptedTranscriber()
    add_archive(store, count=4)
    worker = maintainer(store, tmp_path, transcriber)

    first = worker.run_once(MaintenanceBudget())
    second = worker.run_once(MaintenanceBudget())

    assert first.clips_processed == 3
    assert first.clips_scanned == 4
    assert second.clips_processed == 1
    assert second.clips_reused == 3
    assert len(transcriber.calls) == 4


def test_soft_deadline_is_checked_between_clips(tmp_path):
    store, transcriber = MemoryStore(), ScriptedTranscriber()
    add_archive(store, count=3)
    current = datetime(2026, 10, 13, 12, tzinfo=UTC)

    def slow_transcribe(url):
        nonlocal current
        current += timedelta(seconds=61)
        return transcriber(url)

    worker = RadioArchiveMaintainer(store, tmp_path, slow_transcribe, publish=False, now=lambda: current)

    first = worker.run_once(MaintenanceBudget())
    second = worker.run_once(MaintenanceBudget())

    assert first.clips_processed == 1
    assert second.clips_processed == 1
    assert second.clips_reused == 1
    assert transcriber.calls == [
        f"https://audio.example/singapore_2024_race/{index}" for index in range(2)
    ]


def test_policy_change_creates_a_new_job_identity(tmp_path):
    store, transcriber = MemoryStore(), ScriptedTranscriber()
    add_archive(store, count=2)
    maintainer(store, tmp_path, transcriber).run_once(MaintenanceBudget(max_clips=1))
    decision = FinalTranscriptDecision(
        "accepted",
        "Box",
        None,
        {
            "profile": CURRENT_RADIO_PROFILE,
            "policy": "new-policy",
            "status": "accepted",
            "reason": None,
        },
    )
    new_transcriber = ScriptedTranscriber([decision])

    result = maintainer(store, tmp_path, new_transcriber, policy="new-policy").run_once(
        MaintenanceBudget(max_clips=1)
    )

    assert result.clips_processed == 1
    assert result.clips_reused == 0
    assert new_transcriber.calls == ["https://audio.example/singapore_2024_race/0"]
    assert len(list(tmp_path.glob("jobs/*/checkpoint.json"))) == 2


@pytest.mark.parametrize(
    "budget",
    [
        MaintenanceBudget(max_archives=0),
        MaintenanceBudget(max_archives=2),
        MaintenanceBudget(max_clips=0),
        MaintenanceBudget(max_clips=4),
        MaintenanceBudget(max_clips=True),
        MaintenanceBudget(max_seconds=0),
        MaintenanceBudget(max_seconds=61),
        MaintenanceBudget(max_seconds=float("nan")),
    ],
)
def test_invalid_budgets_fail_before_storage_access(tmp_path, budget):
    store, transcriber = MemoryStore(), ScriptedTranscriber()
    add_archive(store)

    with pytest.raises(ValueError):
        maintainer(store, tmp_path, transcriber).run_once(budget)

    assert not store.reads
    assert not store.downloads
    assert not transcriber.calls


def test_deferred_decisions_retry_after_five_then_fifteen_minutes(tmp_path):
    store, transcriber = (
        MemoryStore(),
        ScriptedTranscriber(
            [
                FinalTranscriptDecision("deferred", None, "processing_failed", {}),
                FinalTranscriptDecision("deferred", None, "processing_failed", {}),
            ]
        ),
    )
    original = add_archive(store)
    current = datetime(2026, 10, 13, 12, tzinfo=UTC)
    worker = RadioArchiveMaintainer(store, tmp_path, transcriber, publish=False, now=lambda: current)

    first = worker.run_once(MaintenanceBudget())
    waiting = worker.run_once(MaintenanceBudget())
    current += timedelta(minutes=5)
    second = worker.run_once(MaintenanceBudget())
    current += timedelta(minutes=15)
    final = worker.run_once(MaintenanceBudget())

    assert first.status == "deferred"
    assert first.retry_at == datetime(2026, 10, 13, 12, 5, tzinfo=UTC)
    assert waiting.clips_processed == 0
    assert second.status == "deferred"
    assert second.retry_at == datetime(2026, 10, 13, 12, 20, tzinfo=UTC)
    assert final.status == "progressed"
    assert final.clips_processed == 1
    assert len(transcriber.calls) == 3
    assert store.objects["sessions/singapore_2024_race/manifest.json"] == original


def test_rejected_decision_is_terminal_and_reused(tmp_path):
    store, transcriber = (
        MemoryStore(),
        ScriptedTranscriber(
            [
                FinalTranscriptDecision(
                    "rejected",
                    None,
                    "low_confidence",
                    {
                        "profile": CURRENT_RADIO_PROFILE,
                        "policy": CURRENT_RADIO_POLICY,
                        "status": "rejected",
                        "reason": "low_confidence",
                    },
                ),
            ]
        ),
    )
    add_archive(store, count=2)
    worker = maintainer(store, tmp_path, transcriber)
    worker.run_once(MaintenanceBudget(max_clips=1))

    result = worker.run_once(MaintenanceBudget())

    assert result.rejected == 1
    assert result.accepted == 1
    assert result.clips_reused == 1
    assert len(transcriber.calls) == 2


def test_structurally_invalid_checkpoint_is_quarantined(tmp_path):
    store, transcriber = MemoryStore(), ScriptedTranscriber()
    original = add_archive(store, count=2)
    worker = maintainer(store, tmp_path, transcriber)
    worker.run_once(MaintenanceBudget(max_clips=1))
    checkpoint_path = next(tmp_path.glob("jobs/*/checkpoint.json"))
    checkpoint = json.loads(checkpoint_path.read_text())
    next(iter(checkpoint["decisions"].values()))["status"] = "deferred"
    checkpoint_path.write_text(json.dumps(checkpoint))
    transcriber.calls.clear()

    result = worker.run_once(MaintenanceBudget())

    assert result.status == "quarantined"
    assert not transcriber.calls
    assert store.objects["sessions/singapore_2024_race/manifest.json"] == original


def test_cursor_continues_to_the_next_archive_after_restart(tmp_path):
    store, transcriber = MemoryStore(), ScriptedTranscriber()
    add_archive(store, "a_race")
    add_archive(store, "b_race")
    maintainer(store, tmp_path, transcriber).run_once(MaintenanceBudget())

    result = maintainer(store, tmp_path, transcriber).run_once(MaintenanceBudget())

    assert result.replay_session_id == "b_race"
    assert transcriber.calls == ["https://audio.example/a_race/0", "https://audio.example/b_race/0"]


def test_corrupt_cursor_is_quarantined_before_inference(tmp_path):
    store, transcriber = MemoryStore(), ScriptedTranscriber()
    add_archive(store)
    (tmp_path / "cursor.json").write_text('{"after": 1}')

    result = maintainer(store, tmp_path, transcriber).run_once(MaintenanceBudget())

    assert result.status == "quarantined"
    assert not store.reads
    assert not transcriber.calls


def test_changed_manifest_creates_new_checkpoints(tmp_path):
    store, transcriber = MemoryStore(), ScriptedTranscriber()
    add_archive(store)
    worker = maintainer(store, tmp_path, transcriber)
    worker.run_once(MaintenanceBudget())
    add_archive(store, count=2)

    result = worker.run_once(MaintenanceBudget())

    assert result.clips_processed == 2
    assert result.clips_reused == 0
    assert len(list(tmp_path.glob("jobs/*/checkpoint.json"))) == 2


def test_corrupt_source_is_quarantined_before_inference(tmp_path):
    store, transcriber = MemoryStore(), ScriptedTranscriber()
    original = add_archive(store)
    store.objects[original["files"]["track.json"]["key"]] = b"corrupted"

    result = maintainer(store, tmp_path, transcriber).run_once(MaintenanceBudget())

    assert result.status == "quarantined"
    assert not transcriber.calls
    assert store.objects["sessions/singapore_2024_race/manifest.json"] == original


def test_inference_exception_is_deferred_without_logging_private_data(tmp_path, capsys):
    store = MemoryStore()
    original = add_archive(store)

    def failing_transcriber(url):
        raise RuntimeError(f"private transcript from {url}")

    result = maintainer(store, tmp_path, failing_transcriber).run_once(MaintenanceBudget())

    assert result.status == "deferred"
    assert result.clips_processed == 1
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert store.objects["sessions/singapore_2024_race/manifest.json"] == original


def test_invalid_transcriber_result_is_quarantined(tmp_path):
    store = MemoryStore()
    original = add_archive(store)

    result = maintainer(store, tmp_path, lambda url: None).run_once(MaintenanceBudget())

    assert result.status == "quarantined"
    assert store.objects["sessions/singapore_2024_race/manifest.json"] == original


def test_terminal_checkpoints_survive_a_later_deferred_clip(tmp_path):
    store, transcriber = MemoryStore(), ScriptedTranscriber()
    add_archive(store, count=3)
    accepted = transcriber("fixture")
    transcriber.calls.clear()
    transcriber.decisions = [
        accepted,
        FinalTranscriptDecision("deferred", None, "processing_failed", {}),
        accepted,
    ]
    current = datetime(2026, 10, 13, 12, tzinfo=UTC)
    worker = RadioArchiveMaintainer(store, tmp_path, transcriber, publish=False, now=lambda: current)
    first = worker.run_once(MaintenanceBudget())
    current += timedelta(minutes=5)

    result = RadioArchiveMaintainer(store, tmp_path, transcriber, publish=False, now=lambda: current).run_once(
        MaintenanceBudget()
    )

    assert first.status == "deferred"
    assert result.clips_reused == 2
    assert result.clips_processed == 1
    assert transcriber.calls == [
        "https://audio.example/singapore_2024_race/0",
        "https://audio.example/singapore_2024_race/1",
        "https://audio.example/singapore_2024_race/2",
        "https://audio.example/singapore_2024_race/1",
    ]


def test_retained_revision_objects_do_not_block_manifest_discovery(tmp_path):
    store, transcriber = MemoryStore(), ScriptedTranscriber()
    add_archive(store)
    for index in range(1001):
        store.objects[f"sessions/singapore_2024_race/revisions/{index:064x}/events.jsonl"] = (
            b"retained"
        )

    result = maintainer(store, tmp_path, transcriber).run_once(MaintenanceBudget())

    assert result.status == "progressed"
    assert result.clips_processed == 1
    assert store.reads == ["sessions/singapore_2024_race/manifest.json"]


@pytest.mark.parametrize(
    "content",
    [b'{"files": invalid}', b"x" * (MAX_MANIFEST_BYTES + 1)],
    ids=["malformed-json", "oversized-manifest"],
)
def test_invalid_remote_manifest_content_is_quarantined(tmp_path, content):
    store, transcriber = JsonManifestStore(), ScriptedTranscriber()
    key = "sessions/singapore_2024_race/manifest.json"
    store.objects[key] = content

    result = maintainer(store, tmp_path, transcriber).run_once(MaintenanceBudget())

    assert result.status == "quarantined"
    assert not transcriber.calls
    assert not store.downloads
    assert store.objects[key] == content


def test_manifest_read_failure_obeys_replay_level_retry_receipt(tmp_path):
    class UnavailableStore(MemoryStore):
        def get_json(self, key, *, limit):
            self.reads.append(key)
            raise StorageError("provider unavailable")

    store, transcriber = UnavailableStore(), ScriptedTranscriber()
    add_archive(store)
    current = datetime(2026, 10, 13, 12, tzinfo=UTC)
    worker = RadioArchiveMaintainer(store, tmp_path, transcriber, publish=False, now=lambda: current)

    first = worker.run_once(MaintenanceBudget())
    second = RadioArchiveMaintainer(store, tmp_path, transcriber, publish=False, now=lambda: current).run_once(
        MaintenanceBudget()
    )

    assert first.status == second.status == "deferred"
    assert first.retry_at == second.retry_at == datetime(2026, 10, 13, 12, 5, tzinfo=UTC)
    assert store.reads == ["sessions/singapore_2024_race/manifest.json"]
    assert not transcriber.calls
    current += timedelta(minutes=5)

    retried = worker.run_once(MaintenanceBudget())

    assert len(store.reads) == 2
    assert retried.retry_at == datetime(2026, 10, 13, 12, 20, tzinfo=UTC)


def test_manifest_recovery_creates_source_specific_job_after_retry_deadline(tmp_path):
    class RecoveringStore(MemoryStore):
        unavailable = True

        def get_json(self, key, *, limit):
            if self.unavailable:
                self.reads.append(key)
                raise StorageError("provider unavailable")
            return super().get_json(key, limit=limit)

    store, transcriber = RecoveringStore(), ScriptedTranscriber()
    add_archive(store)
    current = datetime(2026, 10, 13, 12, tzinfo=UTC)
    worker = RadioArchiveMaintainer(store, tmp_path, transcriber, publish=False, now=lambda: current)
    worker.run_once(MaintenanceBudget())
    store.unavailable = False
    add_archive(store, count=2)
    current += timedelta(minutes=5)

    recovered = worker.run_once(MaintenanceBudget())

    assert recovered.status == "progressed"
    assert recovered.clips_processed == 2
    assert len(store.reads) == 2
    receipt = json.loads((tmp_path / "receipts" / "singapore_2024_race.json").read_text())
    assert isinstance(receipt["job_id"], str)
    assert receipt["retry_at"] is None
    assert len(list(tmp_path.glob("jobs/*/checkpoint.json"))) == 1


def test_reconciliation_changes_only_machine_radio_events(tmp_path):
    store, transcriber = MemoryStore(), ScriptedTranscriber()
    replay = "singapore_2024_race"
    manual = event(replay, "RaceControlMessage", 1000, "HAM", category="Radio",
                   audio_url="https://audio.example/manual", transcript="  Keep pushing!  ")
    original = add_publishable_archive(store, extra_rows=[manual])
    source_key = original["files"]["events.jsonl"]["key"]
    source_lines = store.objects[source_key].decode().splitlines()
    non_radio = json.loads(source_lines[1])
    non_radio["retained_extension"] = {"value": "untouched"}
    source_lines[1] = json.dumps(non_radio, indent=None)
    source_data = ("\n".join(source_lines) + "\n").encode()
    store.objects[source_key] = source_data
    original["files"]["events.jsonl"].update(
        size=len(source_data), sha256=hashlib.sha256(source_data).hexdigest()
    )
    store.objects[f"sessions/{replay}/manifest.json"] = original
    unrelated = tmp_path / "jobs" / ("f" * 64)
    unrelated.mkdir(parents=True)
    (unrelated / "keep").write_text("unrelated")

    result = maintainer(store, tmp_path, transcriber, publish=True).run_once(MaintenanceBudget())

    assert result.status == "published"
    target = store.objects[f"sessions/{replay}/manifest.json"]
    target_data = store.objects[target["files"]["events.jsonl"]["key"]]
    target_lines = target_data.decode().splitlines()
    target_rows = [json.loads(line) for line in target_lines]
    assert target["event_count"] == original["event_count"] == len(target_rows)
    unchanged = [line for line in source_lines if json.loads(line)["event_id"] != json.loads(source_lines[0])["event_id"]]
    assert all(line in target_lines for line in unchanged)
    changed = next(row for row in target_rows if row["session_time_ms"] == 0)
    assert changed["payload"]["transcript"] == "Box this lap"
    assert changed["event_id"] == make_event_id(
        replay, changed["type"], 0, changed["driver_id"], changed["payload"]
    )
    assert target_rows == sorted(target_rows, key=lambda row: (row["session_time_ms"], row["event_id"]))
    for name in ("track.json", "positions.json"):
        assert store.objects[target["files"][name]["key"]] == store.objects[original["files"][name]["key"]]
    assert target["radio_summary"] == {"total": 2, "accepted": 1, "rejected": 0, "manual": 1}
    receipt = json.loads((tmp_path / "receipts" / f"{replay}.json").read_text())
    assert receipt["status"] == "published"
    assert (unrelated / "keep").read_text() == "unrelated"
    assert not list(tmp_path.glob("jobs/*/checkpoint.json"))


def test_source_revision_change_aborts_publish(tmp_path):
    store, transcriber = MemoryStore(), ScriptedTranscriber()
    original = add_publishable_archive(store)

    def changed_source(url):
        store.objects["sessions/singapore_2024_race/manifest.json"]["created_at"] = "2026-10-14T10:00:00Z"
        return transcriber(url)

    result = maintainer(store, tmp_path, changed_source, publish=True).run_once(MaintenanceBudget())

    assert result.status == "quarantined"
    assert not store.operations
    assert store.objects["sessions/singapore_2024_race/manifest.json"]["files"] == original["files"]


@pytest.mark.parametrize("failure_index", [1, 2, 3])
def test_failure_at_each_revision_upload_keeps_old_manifest(tmp_path, failure_index):
    class FailingStore(MemoryStore):
        uploads = 0

        def upload_file(self, key, path, **kwargs):
            self.uploads += 1
            if self.uploads == failure_index:
                raise StorageError("injected upload failure")
            super().upload_file(key, path, **kwargs)

    store, transcriber = FailingStore(), ScriptedTranscriber()
    original = add_publishable_archive(store)

    result = maintainer(store, tmp_path, transcriber, publish=True).run_once(MaintenanceBudget())

    assert result.status == "deferred"
    assert store.objects["sessions/singapore_2024_race/manifest.json"] == original
    assert list(tmp_path.glob("jobs/*/checkpoint.json"))
    for row in original["files"].values():
        store.verify(row["key"], size=row["size"], sha256=row["sha256"])


def test_manifest_failure_keeps_old_revision_visible(tmp_path):
    class FailingStore(MemoryStore):
        def put_json(self, key, value):
            raise StorageError("manifest failure")

    store, transcriber = FailingStore(), ScriptedTranscriber()
    original = add_publishable_archive(store)

    result = maintainer(store, tmp_path, transcriber, publish=True).run_once(MaintenanceBudget())

    assert result.status == "deferred"
    assert store.objects["sessions/singapore_2024_race/manifest.json"] == original
    assert len(store.operations) == 3


@pytest.mark.parametrize("crash_point", ["manifest", "receipt", "before-cleanup", "after-cleanup"])
def test_crash_after_manifest_is_repaired_without_inference(tmp_path, monkeypatch, crash_point):
    import racelens.radio.archive_maintenance as maintenance

    class CrashingStore(MemoryStore):
        crash = True

        def put_json(self, key, value):
            super().put_json(key, value)
            if self.crash and crash_point == "manifest":
                raise SystemExit("crash after manifest")

    store, transcriber = CrashingStore(), ScriptedTranscriber()
    add_publishable_archive(store)
    original_atomic = maintenance._atomic_json
    original_cleanup = maintenance.shutil.rmtree

    def crash_receipt(path, value):
        if crash_point == "receipt" and value.get("status") == "published":
            raise SystemExit("crash before receipt")
        original_atomic(path, value)

    def crash_cleanup(path):
        if crash_point == "before-cleanup":
            raise SystemExit("crash before cleanup")
        original_cleanup(path)
        if crash_point == "after-cleanup":
            raise SystemExit("crash after cleanup")

    monkeypatch.setattr(maintenance, "_atomic_json", crash_receipt)
    monkeypatch.setattr(maintenance.shutil, "rmtree", crash_cleanup)
    with pytest.raises(SystemExit):
        maintainer(store, tmp_path, transcriber, publish=True).run_once(MaintenanceBudget())
    assert store.objects["sessions/singapore_2024_race/manifest.json"]["schema_version"] == 2
    assert bool(list(tmp_path.glob("jobs/*/checkpoint.json"))) == (crash_point != "after-cleanup")
    monkeypatch.setattr(maintenance, "_atomic_json", original_atomic)
    monkeypatch.setattr(maintenance.shutil, "rmtree", original_cleanup)
    store.crash = False
    store.operations.clear()
    transcriber.calls.clear()

    result = maintainer(store, tmp_path, transcriber, publish=True).run_once(MaintenanceBudget())

    assert result.status == "published"
    assert not transcriber.calls
    assert not store.operations
    assert not list(tmp_path.glob("jobs/*/checkpoint.json"))
    receipt = json.loads((tmp_path / "receipts" / "singapore_2024_race.json").read_text())
    assert receipt["status"] == "published"


def test_duplicate_completed_run_creates_zero_uploads(tmp_path):
    store, transcriber = MemoryStore(), ScriptedTranscriber()
    add_publishable_archive(store)
    worker = maintainer(store, tmp_path, transcriber, publish=True)
    assert worker.run_once(MaintenanceBudget()).status == "published"
    store.operations.clear()
    transcriber.calls.clear()
    store.downloads.clear()

    result = worker.run_once(MaintenanceBudget())

    assert result.status == "idle"
    assert not store.operations
    assert not store.downloads
    assert not transcriber.calls


def test_cleanup_crash_then_storage_retry_preserves_completion_proof(tmp_path, monkeypatch):
    import racelens.radio.archive_maintenance as maintenance

    class FailingVerificationStore(MemoryStore):
        fail = False

        def verify(self, key, **kwargs):
            if self.fail:
                self.fail = False
                raise StorageError("temporary verification outage")
            super().verify(key, **kwargs)

    store, transcriber = FailingVerificationStore(), ScriptedTranscriber()
    add_publishable_archive(store)
    current = datetime(2026, 10, 13, 12, tzinfo=UTC)
    worker = RadioArchiveMaintainer(store, tmp_path, transcriber, now=lambda: current)
    cleanup = maintenance.shutil.rmtree

    def crash_after_cleanup(path):
        cleanup(path)
        raise SystemExit("crash after cleanup")

    monkeypatch.setattr(maintenance.shutil, "rmtree", crash_after_cleanup)
    with pytest.raises(SystemExit):
        worker.run_once(MaintenanceBudget())
    monkeypatch.setattr(maintenance.shutil, "rmtree", cleanup)
    assert not list(tmp_path.glob("jobs/*/checkpoint.json"))
    assert list(tmp_path.glob("publications/*.json"))
    receipt_path = tmp_path / "receipts" / "singapore_2024_race.json"
    assert json.loads(receipt_path.read_text())["status"] == "published"
    store.operations.clear()
    store.downloads.clear()
    transcriber.calls.clear()
    store.fail = True

    deferred = worker.run_once(MaintenanceBudget())
    reads_after_failure = list(store.reads)
    waiting = worker.run_once(MaintenanceBudget())
    assert deferred.status == waiting.status == "deferred"
    assert deferred.retry_at == waiting.retry_at == current + timedelta(minutes=5)
    assert store.reads == reads_after_failure
    current += timedelta(minutes=5)

    repaired = worker.run_once(MaintenanceBudget())

    assert repaired.status == "published"
    assert json.loads(receipt_path.read_text())["status"] == "published"
    assert not list(tmp_path.glob("publications/*.json"))
    assert not list(tmp_path.glob("jobs/*/checkpoint.json"))
    assert not store.downloads
    assert not transcriber.calls
    assert not store.operations


def test_dry_run_retains_complete_checkpoint_without_remote_writes(tmp_path):
    store, transcriber = MemoryStore(), ScriptedTranscriber()
    original = add_publishable_archive(store)
    worker = maintainer(store, tmp_path, transcriber)

    result = worker.run_once(MaintenanceBudget())
    repeated = worker.run_once(MaintenanceBudget())

    assert result.status == repeated.status == "progressed"
    assert repeated.clips_reused == 1
    assert len(transcriber.calls) == 1
    assert not store.operations
    assert store.objects["sessions/singapore_2024_race/manifest.json"] == original
    assert list(tmp_path.glob("jobs/*/checkpoint.json"))


@pytest.mark.parametrize("artifact", ["events.jsonl", "track.json", "positions.json"])
def test_full_archive_validation_refuses_invalid_artifacts(tmp_path, artifact):
    store, transcriber = MemoryStore(), ScriptedTranscriber()
    original = add_publishable_archive(store)
    row = original["files"][artifact]
    data = b"{}" if artifact != "events.jsonl" else store.objects[row["key"]].splitlines()[0] + b"\n"
    store.objects[row["key"]] = data
    row.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    if artifact == "events.jsonl":
        original["event_count"] = 1
    store.objects["sessions/singapore_2024_race/manifest.json"] = original

    result = maintainer(store, tmp_path, transcriber, publish=True).run_once(MaintenanceBudget())

    assert result.status == "quarantined"
    assert not store.operations
    assert store.objects["sessions/singapore_2024_race/manifest.json"] == original


@pytest.mark.parametrize("mutation", ["count", "non-radio", "manual"])
def test_final_fixture_invariants_refuse_mutated_output(tmp_path, monkeypatch, mutation):
    import racelens.radio.archive_maintenance as maintenance

    store, transcriber = MemoryStore(), ScriptedTranscriber()
    replay = "singapore_2024_race"
    manual = event(replay, "RaceControlMessage", 1000, "HAM", category="Radio",
                   audio_url="https://audio.example/manual", transcript="Keep pushing!")
    original = add_publishable_archive(store, extra_rows=[manual])
    write = maintenance.atomic_write_text

    def mutate_output(path, text):
        lines = text.splitlines()
        if mutation == "count":
            lines.pop()
        else:
            index = next(
                index for index, line in enumerate(lines)
                if (json.loads(line)["type"] == "LapCompleted" if mutation == "non-radio"
                    else json.loads(line)["event_id"] == manual.event_id)
            )
            if mutation == "manual":
                lines[index] = json.dumps(json.loads(lines[index]), indent=None)
            else:
                row = json.loads(lines[index])
                row["confidence"] = "low"
                lines[index] = json.dumps(row)
        write(path, "\n".join(lines) + "\n")

    monkeypatch.setattr(maintenance, "atomic_write_text", mutate_output)

    result = maintainer(store, tmp_path, transcriber, publish=True).run_once(MaintenanceBudget())

    assert result.status == "quarantined"
    assert not store.operations
    assert store.objects[f"sessions/{replay}/manifest.json"] == original


@pytest.mark.parametrize("artifact", ["track.json", "positions.json"])
def test_source_artifact_change_during_inference_aborts_publication(tmp_path, artifact):
    store, transcriber = MemoryStore(), ScriptedTranscriber()
    original = add_publishable_archive(store)

    def mutate_source(url):
        path = next(tmp_path.glob(f"jobs/*/{artifact}"))
        path.write_bytes(path.read_bytes() + b" ")
        return transcriber(url)

    result = maintainer(store, tmp_path, mutate_source, publish=True).run_once(MaintenanceBudget())

    assert result.status == "quarantined"
    assert not store.operations
    assert store.objects["sessions/singapore_2024_race/manifest.json"] == original


def test_read_verification_failure_never_writes_completion_receipt(tmp_path):
    class FailingStore(MemoryStore):
        fail = True

        def verify(self, key, **kwargs):
            manifest = self.objects["sessions/singapore_2024_race/manifest.json"]
            if self.fail and manifest["schema_version"] == 2:
                raise StorageError("read verification unavailable")
            super().verify(key, **kwargs)

    store, transcriber = FailingStore(), ScriptedTranscriber()
    add_publishable_archive(store)
    worker = maintainer(store, tmp_path, transcriber, publish=True)

    result = worker.run_once(MaintenanceBudget())

    assert result.status == "deferred"
    assert list(tmp_path.glob("jobs/*/checkpoint.json"))
    receipt_path = tmp_path / "receipts" / "singapore_2024_race.json"
    assert json.loads(receipt_path.read_text())["status"] == "deferred"
    store.fail = False
    store.operations.clear()
    transcriber.calls.clear()

    repaired = worker.run_once(MaintenanceBudget())

    assert repaired.status == "published"
    assert not store.operations
    assert not transcriber.calls
    assert json.loads(receipt_path.read_text())["status"] == "published"
