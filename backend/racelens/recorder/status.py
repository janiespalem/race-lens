"""Sanitized recorder status for operators."""
from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from racelens.object_storage import REPLAY_ID
from racelens.recorder.state import SESSION_ID, CorruptStateError, Phase, RecorderState, StateStore

MAX_PREPARATION_MARKER_BYTES = 4096
MAX_PREPARATION_MARKER_AGE = 24 * 60 * 60
MAX_MAINTENANCE_MARKER_BYTES = 4096
MAX_MAINTENANCE_MARKER_AGE = 24 * 60 * 60


def _maintenance(path: Path, now: datetime) -> dict | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MAINTENANCE_MARKER_BYTES:
                return None
            raw = handle.read(MAX_MAINTENANCE_MARKER_BYTES + 1)
        if len(raw) > MAX_MAINTENANCE_MARKER_BYTES:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {
            "mode", "replay_session_id", "status", "counts", "updated_at", "attempt", "retry_at",
        }:
            return None
        if value["mode"] not in {"dry-run", "enabled"} or value["status"] not in {
            "current", "progressed", "deferred", "quarantined", "published",
        }:
            return None
        replay = value["replay_session_id"]
        if replay is not None and (not isinstance(replay, str) or not REPLAY_ID.fullmatch(replay)):
            return None
        counts = value["counts"]
        if not isinstance(counts, dict) or set(counts) != {
            "clips_scanned", "clips_processed", "clips_reused", "accepted", "rejected",
        } or any(type(count) is not int or not 0 <= count <= 100 for count in counts.values()):
            return None
        if type(value["attempt"]) is not int or not 0 <= value["attempt"] <= 2**31 - 1:
            return None
        updated_at = datetime.fromisoformat(value["updated_at"])
        if updated_at.tzinfo is None:
            return None
        age = (now - updated_at.astimezone(UTC)).total_seconds()
        if age < -60 or age > MAX_MAINTENANCE_MARKER_AGE:
            return None
        retry_age = None
        if value["retry_at"] is not None:
            retry_at = datetime.fromisoformat(value["retry_at"])
            if retry_at.tzinfo is None:
                return None
            retry_age = round((now - retry_at.astimezone(UTC)).total_seconds(), 3)
        return {
            "mode": value["mode"], "replay_session_id": replay, "status": value["status"],
            "counts": counts, "age_seconds": round(max(0.0, age), 3),
            "attempt": value["attempt"], "retry_age_seconds": retry_age,
        }
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _age(path: Path, now: datetime) -> float | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return round(max(0.0, now.timestamp() - path.stat().st_mtime), 3)
    except OSError:
        return None


def _publication(directory: Path, session_id: str) -> str:
    state = "none"
    for path in directory.glob("*.ready.*") if directory.is_dir() else ():
        if path.is_symlink() or not path.is_file():
            continue
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("session") != session_id:
                continue
        except (OSError, AttributeError, json.JSONDecodeError):
            continue
        if path.name.endswith(".published"):
            return "published"
        if path.name.endswith(".json"):
            state = "pending"
    return state


def _preparation(path: Path, state: RecorderState, now: datetime) -> dict | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("r", encoding="utf-8") as handle:
            raw = handle.read(MAX_PREPARATION_MARKER_BYTES + 1)
        if len(raw) > MAX_PREPARATION_MARKER_BYTES:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict):
            return None
        session_id = value.get("session_id")
        if not isinstance(session_id, str) or SESSION_ID.fullmatch(session_id) is None:
            return None
        owner = state.sessions.get(session_id)
        if owner is None or owner.phase is not Phase.PROCESSING:
            return None
        started_at = datetime.fromisoformat(str(value.get("started_at", "")).replace("Z", "+00:00"))
        if started_at.tzinfo is None:
            return None
        age = (now - started_at.astimezone(UTC)).total_seconds()
        if age < -60 or age > MAX_PREPARATION_MARKER_AGE:
            return None
        return {"session_id": session_id, "age_seconds": round(max(0.0, age), 3)}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def recorder_status(base: Path, now: datetime | None = None) -> dict:
    current = now or datetime.now(UTC)
    heartbeat_age = _age(base / "state" / "heartbeat", current)
    radio_maintenance = _maintenance(base / "state" / "radio-maintenance.json", current)
    try:
        state = StateStore(base / "state" / "recorder.json").load()
    except (CorruptStateError, OSError):
        return {
            "heartbeat_age_seconds": heartbeat_age,
            "radio_maintenance": radio_maintenance,
            "preparation": None,
            "session": None,
            "raw": None,
            "publication": "unknown",
            "error": "state_unavailable",
        }
    latest = max(state.sessions.items(), key=lambda item: item[1].updated_at, default=None)
    if latest is None:
        return {
            "heartbeat_age_seconds": heartbeat_age,
            "radio_maintenance": radio_maintenance,
            "preparation": None,
            "session": None,
            "raw": None,
            "publication": "none",
        }
    session_id, item = latest
    raw = base / "raw" / f"{session_id}.f1live"
    raw_age = _age(raw, current)
    return {
        "heartbeat_age_seconds": heartbeat_age,
        "radio_maintenance": radio_maintenance,
        "preparation": _preparation(
            base / "state" / "preparation-active.json", state, current,
        ),
        "session": {
            "session_id": session_id,
            "phase": item.phase.value,
            "updated_age_seconds": round(
                max(0.0, (current - item.updated_at).total_seconds()), 3,
            ),
        },
        "raw": {"size_bytes": raw.stat().st_size, "age_seconds": raw_age}
        if raw_age is not None else None,
        "publication": _publication(base / "data" / "publish", session_id),
    }
