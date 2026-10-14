import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from racelens.recorder.state import Phase, StateStore
from racelens.recorder.status import recorder_status


def _write_preparation_marker(state_dir, session_id, started_at):
    (state_dir / "preparation-active.json").write_text(
        json.dumps({"session_id": session_id, "started_at": started_at.isoformat()}) + "\n",
        encoding="utf-8",
    )


def test_recorder_status_reports_active_preparation_owner(tmp_path):
    now = datetime(2026, 9, 25, 13, tzinfo=UTC)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    store = StateStore(state_dir / "recorder.json")
    store.transition("2026-15-q", Phase.RECORDING, now)
    store.transition("2026-15-q", Phase.CAPTURED, now)
    store.transition("2026-15-q", Phase.PROCESSING, now)
    _write_preparation_marker(state_dir, "2026-15-q", now)

    status = recorder_status(tmp_path, now)

    assert status["preparation"] == {
        "session_id": "2026-15-q",
        "age_seconds": 0.0,
    }


def test_recorder_status_ignores_malformed_preparation_marker(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "preparation-active.json").write_text("{" * 5000, encoding="utf-8")

    assert recorder_status(tmp_path)["preparation"] is None


def test_recorder_status_ignores_symlinked_preparation_marker(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    target = tmp_path / "foreign.json"
    target.write_text(
        '{"session_id":"2026-15-q","started_at":"2026-09-25T13:00:00Z"}\n',
        encoding="utf-8",
    )
    (state_dir / "preparation-active.json").symlink_to(target)

    assert recorder_status(tmp_path)["preparation"] is None


def test_recorder_status_ignores_marker_without_processing_owner(tmp_path):
    now = datetime(2026, 9, 25, 13, tzinfo=UTC)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _write_preparation_marker(state_dir, "2026-15-q", now)

    assert recorder_status(tmp_path, now)["preparation"] is None


def test_recorder_status_ignores_stale_preparation_marker(tmp_path):
    now = datetime(2026, 9, 25, 13, tzinfo=UTC)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    store = StateStore(state_dir / "recorder.json")
    store.transition("2026-15-q", Phase.RECORDING, now)
    store.transition("2026-15-q", Phase.CAPTURED, now)
    store.transition("2026-15-q", Phase.PROCESSING, now)
    _write_preparation_marker(state_dir, "2026-15-q", now - timedelta(days=2))

    assert recorder_status(tmp_path, now)["preparation"] is None


def test_recorder_status_reports_current_files_without_paths_or_errors(
    tmp_path, monkeypatch, capsys,
):
    now = datetime(2026, 7, 17, 12, tzinfo=UTC)
    state_dir = tmp_path / "state"
    raw_dir = tmp_path / "raw"
    publish_dir = tmp_path / "data" / "publish"
    state_dir.mkdir()
    raw_dir.mkdir()
    publish_dir.mkdir(parents=True)
    heartbeat = state_dir / "heartbeat"
    raw = raw_dir / "2026-10-fp1.f1live"
    heartbeat.write_text("", encoding="utf-8")
    raw.write_bytes(b"secret transport")
    timestamp = (now - timedelta(seconds=5)).timestamp()
    os.utime(heartbeat, (timestamp, timestamp))
    os.utime(raw, (timestamp, timestamp))
    StateStore(state_dir / "recorder.json").transition(
        "2026-10-fp1", Phase.RECORDING, now - timedelta(seconds=10),
    )
    (publish_dir / "belgian_2026_fp1.ready.json").write_text(
        json.dumps({"session": "2026-10-fp1", "files": []}), encoding="utf-8",
    )

    body = recorder_status(tmp_path, now)

    assert body == {
        "heartbeat_age_seconds": 5.0,
        "radio_maintenance": None,
        "preparation": None,
        "session": {
            "session_id": "2026-10-fp1",
            "phase": "recording",
            "updated_age_seconds": 10.0,
        },
        "raw": {"size_bytes": 16, "age_seconds": 5.0},
        "publication": "pending",
    }
    encoded = json.dumps(body)
    assert str(tmp_path) not in encoded
    assert "secret transport" not in encoded

    from racelens import cli

    monkeypatch.setenv("RACELENS_RECORDER_DATA", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["racelens", "recorder-status", "--json"])
    cli.main()
    command = json.loads(capsys.readouterr().out)
    assert command["session"]["session_id"] == "2026-10-fp1"
    assert str(tmp_path) not in json.dumps(command)


def test_recorder_status_cli_sanitizes_corrupt_state(tmp_path, monkeypatch, capsys):
    from racelens import cli

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "recorder.json").write_text("not json: /private/recorder", encoding="utf-8")
    monkeypatch.setenv("RACELENS_RECORDER_DATA", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["racelens", "recorder-status", "--json"])

    cli.main()
    output = capsys.readouterr()
    body = json.loads(output.out)

    assert output.err == ""
    assert body["error"] == "state_unavailable"
    assert str(tmp_path) not in output.out
    assert "/private/recorder" not in output.out


def test_recorder_status_sanitizes_state_permission_failure(tmp_path, monkeypatch):
    def denied(_self):
        raise PermissionError(f"denied: {tmp_path}")

    monkeypatch.setattr(StateStore, "load", denied)

    body = recorder_status(tmp_path)

    assert body["error"] == "state_unavailable"
    assert str(tmp_path) not in json.dumps(body)


def _maintenance_marker(now):
    return {
        "mode": "dry-run",
        "replay_session_id": "singapore_2024_race",
        "status": "progressed",
        "counts": {"clips_scanned": 4, "clips_processed": 3, "clips_reused": 0,
                   "accepted": 1, "rejected": 1},
        "updated_at": (now - timedelta(seconds=10)).isoformat(),
        "attempt": 1,
        "retry_at": (now + timedelta(minutes=5)).isoformat(),
    }


@pytest.mark.parametrize("status", ["current", "progressed", "deferred", "quarantined", "published"])
def test_recorder_status_exposes_only_bounded_maintenance_fields(tmp_path, status):
    now = datetime(2026, 10, 13, 12, tzinfo=UTC)
    state = tmp_path / "state"
    state.mkdir()
    marker = _maintenance_marker(now)
    marker["status"] = status
    (state / "radio-maintenance.json").write_text(json.dumps(marker))

    value = recorder_status(tmp_path, now)["radio_maintenance"]

    assert value == {
        "mode": "dry-run", "replay_session_id": "singapore_2024_race", "status": status,
        "counts": {"clips_scanned": 4, "clips_processed": 3, "clips_reused": 0,
                   "accepted": 1, "rejected": 1},
        "age_seconds": 10.0, "attempt": 1, "retry_age_seconds": -300.0,
    }


@pytest.mark.parametrize("mutation", [
    {"updated_at": "invalid"}, {"updated_at": "2026-10-13T12:00:00"},
    {"updated_at": "2026-10-11T12:00:00Z"}, {"updated_at": "2026-10-13T12:02:00Z"},
    {"replay_session_id": "../private"}, {"replay_session_id": 42},
    {"mode": "unsafe"}, {"status": "secret reason"}, {"reason": "secret reason"},
    {"job_id": "private"}, {"attempt": True}, {"attempt": -1},
    {"retry_at": "invalid"}, {"retry_at": "2026-10-13T12:00:00"},
    {"counts": {"clips_scanned": 4}},
    {"counts": {"clips_scanned": True, "clips_processed": 0, "clips_reused": 0,
                "accepted": 0, "rejected": 0}},
    {"counts": {"clips_scanned": 101, "clips_processed": 0, "clips_reused": 0,
                "accepted": 0, "rejected": 0}},
])
def test_recorder_status_rejects_unsafe_maintenance_receipts(tmp_path, mutation):
    now = datetime(2026, 10, 13, 12, tzinfo=UTC)
    state = tmp_path / "state"
    state.mkdir()
    marker = _maintenance_marker(now) | mutation
    (state / "radio-maintenance.json").write_text(json.dumps(marker))

    assert recorder_status(tmp_path, now)["radio_maintenance"] is None


@pytest.mark.parametrize("kind", ["symlink", "oversized", "unicode_oversized", "invalid", "missing"])
def test_recorder_status_ignores_unsafe_or_missing_maintenance_file(tmp_path, kind):
    now = datetime(2026, 10, 13, 12, tzinfo=UTC)
    state = tmp_path / "state"
    state.mkdir()
    path = state / "radio-maintenance.json"
    if kind == "symlink":
        target = tmp_path / "private.json"
        target.write_text(json.dumps(_maintenance_marker(now)))
        path.symlink_to(target)
    elif kind == "oversized":
        raw = json.dumps(_maintenance_marker(now)).encode()
        path.write_bytes(raw + b" " * (4097 - len(raw)))
    elif kind == "unicode_oversized":
        path.write_bytes((json.dumps(_maintenance_marker(now)) + " " * 2000).encode("utf-16"))
    elif kind == "invalid":
        path.write_bytes(b"\xff")

    assert recorder_status(tmp_path, now)["radio_maintenance"] is None


def test_recorder_status_accepts_maintenance_receipt_at_byte_limit(tmp_path):
    now = datetime(2026, 10, 13, 12, tzinfo=UTC)
    state = tmp_path / "state"
    state.mkdir()
    raw = json.dumps(_maintenance_marker(now)).encode()
    (state / "radio-maintenance.json").write_bytes(raw + b" " * (4096 - len(raw)))

    assert recorder_status(tmp_path, now)["radio_maintenance"]["status"] == "progressed"


@pytest.mark.parametrize("existing", [None, "off", "dry-run", "enabled"])
def test_recorder_installer_sets_maintenance_default_only_for_new_env(tmp_path, existing):
    commands = tmp_path / "commands"
    commands.mkdir()
    for name, body in {
        "docker": "exit 0",
        "id": "printf '1000\\n'",
        "crontab": 'if [ "${1:-}" = "-" ]; then /bin/cat >/dev/null; fi',
    }.items():
        executable = commands / name
        executable.write_text(f"#!/bin/sh\n{body}\n")
        executable.chmod(0o700)
    config = tmp_path / "config"
    env_file = config / "race-lens-recorder" / "recorder.env"
    original = f"RECORDER_RADIO_MAINTENANCE={existing}\nPRIVATE_VALUE=retained\n"
    if existing is not None:
        env_file.parent.mkdir(parents=True)
        env_file.write_text(original)
    script = Path(__file__).resolve().parents[2] / "deploy" / "recorder" / "install.sh"

    result = subprocess.run(
        ["/bin/sh", str(script)], capture_output=True, text=True,
        env=os.environ | {"PATH": f"{commands}:{os.environ['PATH']}",
                          "XDG_CONFIG_HOME": str(config), "XDG_DATA_HOME": str(tmp_path / "data")},
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    if existing is None:
        assert "RECORDER_RADIO_MAINTENANCE=off\n" in env_file.read_text()
    else:
        assert env_file.read_text() == original
    assert env_file.stat().st_mode & 0o777 == 0o600
