import copy
import hashlib
import io
import json
from datetime import UTC, datetime, timedelta

import pytest

from racelens.events.models import dump_jsonl, event
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
    prefix = f"sessions/{replay}"
    if current:
        prefix += "/revisions/" + "a" * 64
    files = {}
    for name, data in {
        "events.jsonl": dump_jsonl(rows).encode(),
        "track.json": b'{"points":[[0,0],[1,1]]}',
        "positions.json": b'{"drivers":{"VER":[[0,0]]}}',
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
        "event_count": count,
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
    return RadioArchiveMaintainer(
        store, tmp_path, transcriber, now=lambda: datetime(2026, 10, 13, 12, tzinfo=UTC), **kwargs
    )


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

    worker = RadioArchiveMaintainer(store, tmp_path, slow_transcribe, now=lambda: current)

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
    worker = RadioArchiveMaintainer(store, tmp_path, transcriber, now=lambda: current)

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
    worker = RadioArchiveMaintainer(store, tmp_path, transcriber, now=lambda: current)
    first = worker.run_once(MaintenanceBudget())
    current += timedelta(minutes=5)

    result = RadioArchiveMaintainer(store, tmp_path, transcriber, now=lambda: current).run_once(
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
    worker = RadioArchiveMaintainer(store, tmp_path, transcriber, now=lambda: current)

    first = worker.run_once(MaintenanceBudget())
    second = RadioArchiveMaintainer(store, tmp_path, transcriber, now=lambda: current).run_once(
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
    worker = RadioArchiveMaintainer(store, tmp_path, transcriber, now=lambda: current)
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
