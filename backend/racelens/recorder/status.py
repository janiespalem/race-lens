"""Sanitized recorder status for operators."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from racelens.recorder.state import SESSION_ID, CorruptStateError, Phase, RecorderState, StateStore

MAX_PREPARATION_MARKER_BYTES = 4096
MAX_PREPARATION_MARKER_AGE = 24 * 60 * 60


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
    try:
        state = StateStore(base / "state" / "recorder.json").load()
    except (CorruptStateError, OSError):
        return {
            "heartbeat_age_seconds": heartbeat_age,
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
