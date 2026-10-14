from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Literal

from racelens.events.models import Event, load_jsonl, make_event_id
from racelens.object_storage import (
    MAX_MANIFEST_BYTES,
    ObjectContentError,
    REPLAY_ID,
    StorageError,
    archive_revision,
    manifest_digest,
    publish_archive_revision,
    validate_manifest,
)
from racelens.radio.models import (
    CURRENT_RADIO_POLICY,
    CURRENT_RADIO_PROFILE,
    FinalTranscriptDecision,
)
from racelens.radio.transcribe import _radio_state
from racelens.recorder.postprocess import (
    PostprocessError,
    atomic_write_text,
    validate_archive,
    validate_fixture,
)

_BACKOFF = (timedelta(minutes=5), timedelta(minutes=15))
_MAX_CHECKPOINT_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class MaintenanceBudget:
    max_archives: int = 1
    max_clips: int = 3
    max_seconds: float = 60.0


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    status: Literal["idle", "progressed", "published", "deferred", "quarantined"]
    replay_session_id: str | None = None
    clips_scanned: int = 0
    clips_processed: int = 0
    clips_reused: int = 0
    accepted: int = 0
    rejected: int = 0
    retry_at: datetime | None = None


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _ownership(payload: dict, profile: str, policy: str) -> str:
    state = _radio_state(payload)
    metadata = payload.get("transcript_metadata")
    if state in {"manual", "unprocessed"}:
        return state
    if metadata.get("profile") == profile and metadata.get("policy") == policy:
        return metadata["status"]
    return "stale"


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".checkpoint-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read_json(path: Path) -> dict:
    if path.stat().st_size > _MAX_CHECKPOINT_BYTES:
        raise ValueError("checkpoint exceeds bound")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("checkpoint is not an object")
    return value


def _parsed_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _validate_budget(budget: MaintenanceBudget) -> None:
    if (
        type(budget.max_archives) is not int
        or budget.max_archives != 1
        or type(budget.max_clips) is not int
        or not 1 <= budget.max_clips <= 3
        or type(budget.max_seconds) not in {int, float}
        or not math.isfinite(budget.max_seconds)
        or not 0 < budget.max_seconds <= 60
    ):
        raise ValueError("maintenance budget exceeds the permitted bounds")


class RadioArchiveMaintainer:
    def __init__(
        self,
        store: Any,
        workspace: Path,
        transcribe: Callable[[str], FinalTranscriptDecision],
        *,
        profile: str = CURRENT_RADIO_PROFILE,
        policy: str = CURRENT_RADIO_POLICY,
        publish: bool = True,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.workspace = Path(workspace)
        self.transcribe = transcribe
        self.profile = profile
        self.policy = policy
        self.publish = publish
        self.now = now or (lambda: datetime.now(UTC))

    def run_once(self, budget: MaintenanceBudget) -> MaintenanceResult:
        result = self._run_once(budget)
        attempt = 0
        if result.replay_session_id is not None:
            try:
                receipt = _read_json(
                    self.workspace / "receipts" / f"{result.replay_session_id}.json"
                )
                if (
                    receipt.get("status") == result.status
                    and type(receipt.get("attempts")) is int
                    and 0 <= receipt["attempts"] <= 2**31 - 1
                ):
                    attempt = receipt["attempts"]
            except (OSError, ValueError, TypeError):
                pass
        _atomic_json(
            self.workspace.parent / "radio-maintenance.json",
            {
                "mode": "enabled" if self.publish else "dry-run",
                "replay_session_id": result.replay_session_id,
                "status": "current" if result.status == "idle" else result.status,
                "counts": {
                    "clips_scanned": result.clips_scanned,
                    "clips_processed": result.clips_processed,
                    "clips_reused": result.clips_reused,
                    "accepted": result.accepted,
                    "rejected": result.rejected,
                },
                "updated_at": self.now().isoformat(),
                "attempt": attempt,
                "retry_at": result.retry_at.isoformat() if result.retry_at else None,
            },
        )
        return result

    def _run_once(self, budget: MaintenanceBudget) -> MaintenanceResult:
        _validate_budget(budget)
        started = self.now()
        if started.tzinfo is None:
            raise ValueError("maintenance clock must include a timezone")
        deadline = started + timedelta(seconds=budget.max_seconds)
        cursor_path = self.workspace / "cursor.json"
        try:
            cursor = _read_json(cursor_path) if cursor_path.exists() else {"after": None}
            after = cursor["after"]
            if set(cursor) != {"after"} or (after is not None and not isinstance(after, str)):
                raise ValueError("invalid cursor")
        except (ValueError, KeyError, TypeError):
            return MaintenanceResult("quarantined")
        try:
            listed = self.store.list_common_prefixes("sessions/", limit=1000)
        except StorageError:
            return MaintenanceResult("deferred", retry_at=started + _BACKOFF[0])
        keys = sorted(
            prefix + "manifest.json"
            for prefix in listed[:1000]
            if prefix.startswith("sessions/")
            and prefix.endswith("/")
            and len(prefix.split("/")) == 3
        )
        after = cursor["after"]
        keys = [key for key in keys if after is None or key > after] + [
            key for key in keys if after is not None and key <= after
        ]
        waiting = MaintenanceResult("idle")
        for key in keys:
            if self.now() >= deadline:
                break
            replay = key.split("/")[1]
            if not REPLAY_ID.fullmatch(replay):
                continue
            _atomic_json(cursor_path, {"after": key})
            try:
                receipt = self._receipt(replay, None)
                retry_at = (
                    _parsed_time(receipt["retry_at"]) if receipt and receipt["retry_at"] else None
                )
                if retry_at and self.now() < retry_at:
                    waiting = MaintenanceResult("deferred", replay, retry_at=retry_at)
                    continue
            except (ValueError, KeyError, TypeError):
                return self._quarantine(replay, "invalid_retry_receipt")
            try:
                manifest = validate_manifest(self.store.get_json(key, limit=MAX_MANIFEST_BYTES))
                if manifest["replay_session_id"] != replay:
                    raise ValueError("manifest replay identity differs")
            except (ValueError, ObjectContentError):
                return self._quarantine(replay, "invalid_manifest")
            except StorageError:
                return self._defer(replay, None, "storage_unavailable")
            if self.publish:
                try:
                    repaired = self._repair_publication(manifest)
                    if repaired is not None:
                        return repaired
                except (ValueError, ObjectContentError, PostprocessError, KeyError, TypeError):
                    return self._quarantine(replay, "invalid_publication")
                except StorageError:
                    return self._defer(replay, None, "storage_unavailable")
            if (manifest.get("radio_profile"), manifest.get("radio_policy")) == (
                self.profile,
                self.policy,
            ):
                continue
            identity = [replay, manifest_digest(manifest), self.profile, self.policy]
            job_id = _digest(identity)
            job = self.workspace / "jobs" / job_id
            checkpoint_path = job / "checkpoint.json"
            try:
                receipt = self._receipt(replay, job_id)
                if receipt and receipt["status"] == "quarantined":
                    waiting = MaintenanceResult("quarantined", replay)
                    continue
                retry_at = (
                    _parsed_time(receipt["retry_at"]) if receipt and receipt["retry_at"] else None
                )
                if retry_at and self.now() < retry_at:
                    waiting = MaintenanceResult("deferred", replay, retry_at=retry_at)
                    continue
                clips, states, clip_keys = self._inventory(manifest, job)
                checkpoint = self._checkpoint(checkpoint_path, identity, clip_keys)
            except (ValueError, ObjectContentError, KeyError, TypeError):
                return self._quarantine(replay, "invalid_source_or_checkpoint", job_id)
            except StorageError:
                return self._defer(replay, job_id, "storage_unavailable")
            decisions = checkpoint["decisions"]
            reused = len(decisions)
            processed = 0
            deferred = False
            for item, state, clip_key in zip(clips, states, clip_keys):
                if state in {"manual", "accepted", "rejected"}:
                    continue
                if clip_key in decisions:
                    continue
                if processed >= budget.max_clips or self.now() >= deadline:
                    break
                url = item.payload.get("audio_url")
                if not isinstance(url, str) or not url:
                    deferred = True
                    continue
                try:
                    decision = self.transcribe(url)
                except Exception:
                    decision = FinalTranscriptDecision("deferred", None, "processing_failed", {})
                processed += 1
                if not isinstance(decision, FinalTranscriptDecision):
                    return self._quarantine(replay, "invalid_decision", job_id)
                if decision.status == "deferred":
                    deferred = True
                    continue
                try:
                    self._validate_decision(decision)
                except (ValueError, TypeError, AttributeError):
                    return self._quarantine(replay, "invalid_decision", job_id)
                decisions[clip_key] = {
                    "status": decision.status,
                    "text": decision.text,
                    "reason": decision.reason,
                    "metadata": decision.metadata,
                }
                _atomic_json(checkpoint_path, checkpoint)
            accepted = sum(state == "accepted" for state in states) + sum(
                row["status"] == "accepted" for row in decisions.values()
            )
            rejected = sum(state == "rejected" for state in states) + sum(
                row["status"] == "rejected" for row in decisions.values()
            )
            if deferred:
                result = self._defer(replay, job_id, "processing_failed")
                return MaintenanceResult(
                    "deferred",
                    replay,
                    len(clips),
                    processed,
                    reused,
                    accepted,
                    rejected,
                    result.retry_at,
                )
            complete = all(
                state in {"manual", "accepted", "rejected"} or clip_key in decisions
                for state, clip_key in zip(states, clip_keys)
            )
            if complete:
                try:
                    fixture, summary = self._final_fixture(
                        manifest, job, clips, states, clip_keys, checkpoint
                    )
                    if self.publish:
                        latest = validate_manifest(
                            self.store.get_json(key, limit=MAX_MANIFEST_BYTES)
                        )
                        if manifest_digest(latest) != identity[1]:
                            raise ValueError("archive source manifest conflict")
                        files = self._local_files(fixture, job)
                        _atomic_json(
                            self.workspace / "publications" / f"{replay}.json",
                            {
                                "job_id": job_id,
                                "source_manifest": manifest,
                                "revision": archive_revision(files, summary),
                            },
                        )
                        published = publish_archive_revision(
                            self.store, manifest, fixture, job / "track.json",
                            job / "positions.json", event_count=manifest["event_count"],
                            radio_summary=summary,
                        )
                        self._complete_publication(published, job_id)
                        return MaintenanceResult(
                            "published", replay, len(clips), processed, reused, accepted, rejected
                        )
                except (ValueError, ObjectContentError, PostprocessError, KeyError, TypeError):
                    return self._quarantine(replay, "invalid_archive_or_source_conflict", job_id)
                except StorageError:
                    return self._defer(replay, job_id, "storage_unavailable")
            _atomic_json(
                self.workspace / "receipts" / f"{replay}.json",
                {
                    "job_id": job_id,
                    "status": "progressed",
                    "reason": None,
                    "attempts": 0,
                    "retry_at": None,
                },
            )
            return MaintenanceResult(
                "progressed", replay, len(clips), processed, reused, accepted, rejected
            )
        return waiting

    def _local_files(self, fixture: Path, job: Path) -> dict:
        return {
            name: {"size": path.stat().st_size,
                   "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for name, path in (
                ("events.jsonl", fixture), ("track.json", job / "track.json"),
                ("positions.json", job / "positions.json"),
            )
        }

    def _final_fixture(
        self, manifest: dict, job: Path, clips: list[Event], states: list[str],
        clip_keys: list[str], checkpoint: dict,
    ) -> tuple[Path, dict]:
        for name, row in manifest["files"].items():
            data = (job / name).read_bytes()
            if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise ValueError("source artifact changed")
        lines = [line for line in (job / "events.jsonl").read_bytes().decode().splitlines() if line.strip()]
        rows = [json.loads(line) for line in lines]
        if len(rows) != manifest["event_count"] or len({row["event_id"] for row in rows}) != len(rows):
            raise ValueError("source event count or identity differs")
        inventory = {
            item.event_id: (state, clip_key)
            for item, state, clip_key in zip(clips, states, clip_keys)
        }
        summary = {"profile": self.profile, "policy": self.policy,
                   "total": len(clips), "accepted": 0, "rejected": 0, "manual": 0}
        output = []
        for source, line in zip(rows, lines):
            if source["event_id"] in inventory:
                state, clip_key = inventory[source["event_id"]]
                decision = checkpoint["decisions"].get(clip_key)
                if state in {"manual", "accepted", "rejected"}:
                    if decision is not None:
                        raise ValueError("checkpoint changes protected radio")
                else:
                    if decision is None:
                        raise ValueError("radio reconciliation is incomplete")
                    target = json.loads(line)
                    payload = target["payload"]
                    payload.pop("transcript", None)
                    payload["transcript_metadata"] = decision["metadata"]
                    if decision["text"] is not None:
                        payload["transcript"] = decision["text"]
                    target["event_id"] = make_event_id(
                        target["session_id"], target["type"], target["session_time_ms"],
                        target.get("driver_id"), payload,
                    )
                    self._validate_change(source, target)
                    line = json.dumps(target, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    state = decision["status"]
                summary[state] += 1
            output.append(line)
        output.sort(key=lambda line: (json.loads(line)["session_time_ms"], json.loads(line)["event_id"]))
        fixture = job / "target" / f"{manifest['replay_session_id']}.jsonl"
        text = "\n".join(output) + "\n"
        atomic_write_text(fixture, text)
        if fixture.read_bytes() != text.encode():
            raise ValueError("target fixture differs from reconciled source")
        if validate_fixture(fixture) != manifest["event_count"]:
            raise ValueError("target event count differs")
        report = validate_archive(fixture, job / "track.json", job / "positions.json")
        if report.events != manifest["event_count"]:
            raise ValueError("archive event count differs")
        for name in ("track.json", "positions.json"):
            data = (job / name).read_bytes()
            row = manifest["files"][name]
            if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise ValueError("non-radio artifact changed")
        return fixture, summary

    def _validate_change(self, source: dict, target: dict) -> None:
        if source["payload"].get("category") != "Radio" or _ownership(
            source["payload"], self.profile, self.policy
        ) not in {"unprocessed", "stale"}:
            raise ValueError("only machine radio may change")
        def protected(row: dict) -> dict:
            return {
                **{key: value for key, value in row.items() if key not in {"event_id", "payload"}},
                "payload": {key: value for key, value in row["payload"].items()
                            if key not in {"transcript", "transcript_metadata"}},
            }
        if protected(source) != protected(target):
            raise ValueError("radio source identity changed")

    def _repair_publication(self, manifest: dict) -> MaintenanceResult | None:
        replay = manifest["replay_session_id"]
        path = self.workspace / "publications" / f"{replay}.json"
        if not path.exists():
            return None
        intent = _read_json(path)
        if set(intent) != {"job_id", "source_manifest", "revision"}:
            raise ValueError("invalid publication intent")
        source = validate_manifest(intent["source_manifest"])
        identity = [replay, manifest_digest(source), self.profile, self.policy]
        job_id = _digest(identity)
        if source["replay_session_id"] != replay or intent["job_id"] != job_id:
            raise ValueError("publication identity differs")
        if manifest.get("revision") != intent["revision"]:
            return None
        if manifest.get("parent_revision") != source.get("revision", identity[1]):
            raise ValueError("publication parent differs")
        job = self.workspace / "jobs" / job_id
        receipt = self._receipt(replay, job_id)
        completed = receipt is not None and receipt["status"] == "published"
        completion_path = self.workspace / "completions" / f"{replay}.json"
        if completion_path.exists():
            proof = _read_json(completion_path)
            if set(proof) != {"job_id", "manifest_digest"}:
                raise ValueError("invalid completion proof")
            completed = proof == {"job_id": job_id, "manifest_digest": manifest_digest(manifest)}
        if not completed:
            clips, states, clip_keys = self._inventory(source, job)
            checkpoint = self._checkpoint(job / "checkpoint.json", identity, clip_keys)
            fixture, summary = self._final_fixture(source, job, clips, states, clip_keys, checkpoint)
            files = self._local_files(fixture, job)
            if archive_revision(files, summary) != intent["revision"] or any(
                manifest["files"][name][field] != row[field]
                for name, row in files.items() for field in ("size", "sha256")
            ) or manifest["event_count"] != source["event_count"] or any(
                manifest[field] != source[field]
                for field in ("canonical_session_id", "replay_session_id", "source")
            ) or manifest["radio_profile"] != self.profile or manifest["radio_policy"] != self.policy or (
                manifest["radio_summary"] != {name: summary[name] for name in ("total", "accepted", "rejected", "manual")}
            ):
                raise ValueError("publication content differs")
        self._complete_publication(manifest, job_id)
        counts = manifest["radio_summary"]
        return MaintenanceResult(
            "published", replay, counts["total"], clips_reused=counts["accepted"] + counts["rejected"],
            accepted=counts["accepted"], rejected=counts["rejected"],
        )

    def _complete_publication(self, manifest: dict, job_id: str) -> None:
        replay = manifest["replay_session_id"]
        stored = validate_manifest(self.store.get_json(f"sessions/{replay}/manifest.json", limit=MAX_MANIFEST_BYTES))
        if stored != manifest:
            raise ValueError("published manifest changed")
        for row in stored["files"].values():
            self.store.verify(row["key"], size=row["size"], sha256=row["sha256"])
        _atomic_json(
            self.workspace / "completions" / f"{replay}.json",
            {"job_id": job_id, "manifest_digest": manifest_digest(stored)},
        )
        _atomic_json(
            self.workspace / "receipts" / f"{replay}.json",
            {"job_id": job_id, "status": "published", "reason": None, "attempts": 0, "retry_at": None},
        )
        job = self.workspace / "jobs" / job_id
        if job.is_symlink():
            raise ValueError("publication workspace must not be a symlink")
        if job.exists():
            shutil.rmtree(job)
        (self.workspace / "publications" / f"{replay}.json").unlink(missing_ok=True)

    def _inventory(self, manifest: dict, job: Path) -> tuple[list[Event], list[str], list[str]]:
        for name, row in manifest["files"].items():
            path = job / name
            if (
                not path.is_file()
                or path.stat().st_size != row["size"]
                or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]
            ):
                self.store.download_verified(
                    row["key"], path, size=row["size"], sha256=row["sha256"]
                )
        events = load_jsonl((job / "events.jsonl").read_text(encoding="utf-8"))
        replay = manifest["replay_session_id"]
        if len(events) != manifest["event_count"] or any(
            item.session_id != replay for item in events
        ):
            raise ValueError("archive event identity differs")
        clips = sorted(
            (item for item in events if item.payload.get("category") == "Radio"),
            key=lambda item: (item.session_time_ms, item.event_id),
        )
        if len(clips) > 100:
            raise ValueError("too many radio clips")
        states = [_ownership(item.payload, self.profile, self.policy) for item in clips]
        clip_keys = [
            _digest(
                [item.event_id, item.session_time_ms, item.driver_id, item.payload.get("audio_url")]
            )
            for item in clips
        ]
        if len(set(clip_keys)) != len(clips):
            raise ValueError("duplicate radio clips")
        return clips, states, clip_keys

    def _checkpoint(self, path: Path, identity: list[str], clip_keys: list[str]) -> dict:
        if not path.exists():
            checkpoint = {"identity": identity, "inventory": clip_keys, "decisions": {}}
            _atomic_json(path, checkpoint)
            return checkpoint
        checkpoint = _read_json(path)
        if (
            set(checkpoint) != {"identity", "inventory", "decisions"}
            or checkpoint["identity"] != identity
            or checkpoint["inventory"] != clip_keys
            or not isinstance(checkpoint["decisions"], dict)
            or len(checkpoint["decisions"]) > 100
            or not set(checkpoint["decisions"]) <= set(clip_keys)
        ):
            raise ValueError("invalid checkpoint")
        for row in checkpoint["decisions"].values():
            if not isinstance(row, dict) or set(row) != {"status", "text", "reason", "metadata"}:
                raise ValueError("invalid checkpoint decision")
            self._validate_decision(FinalTranscriptDecision(**row))
        return checkpoint

    def _validate_decision(self, decision: FinalTranscriptDecision) -> None:
        if decision.status not in {"accepted", "rejected"}:
            raise ValueError("decision must be terminal")
        payload = {"transcript": decision.text, "transcript_metadata": decision.metadata}
        if (
            _ownership(payload, self.profile, self.policy) != decision.status
            or decision.metadata.get("status") != decision.status
            or decision.metadata.get("reason") != decision.reason
        ):
            raise ValueError("decision provenance differs")

    def _receipt(self, replay: str, job_id: str | None) -> dict | None:
        path = self.workspace / "receipts" / f"{replay}.json"
        if not path.exists():
            return None
        receipt = _read_json(path)
        if receipt.get("job_id") != job_id:
            return None
        if (
            set(receipt) != {"job_id", "status", "reason", "attempts", "retry_at"}
            or receipt["status"] not in {"progressed", "deferred", "quarantined", "published"}
            or type(receipt["attempts"]) is not int
            or receipt["attempts"] < 0
        ):
            raise ValueError("invalid retry receipt")
        if receipt["retry_at"] is not None:
            _parsed_time(receipt["retry_at"])
        return receipt

    def _defer(self, replay: str, job_id: str | None, reason: str) -> MaintenanceResult:
        receipt = self._receipt(replay, job_id)
        attempts = receipt["attempts"] + 1 if receipt else 1
        retry_at = self.now() + _BACKOFF[min(attempts - 1, len(_BACKOFF) - 1)]
        _atomic_json(
            self.workspace / "receipts" / f"{replay}.json",
            {
                "job_id": job_id,
                "status": "deferred",
                "reason": reason,
                "attempts": attempts,
                "retry_at": retry_at.isoformat(),
            },
        )
        return MaintenanceResult("deferred", replay, retry_at=retry_at)

    def _quarantine(self, replay: str, reason: str, job_id: str | None = None) -> MaintenanceResult:
        _atomic_json(
            self.workspace / "receipts" / f"{replay}.json",
            {
                "job_id": job_id,
                "status": "quarantined",
                "reason": reason,
                "attempts": 0,
                "retry_at": None,
            },
        )
        return MaintenanceResult("quarantined", replay)
