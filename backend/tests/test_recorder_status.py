import json
import os
import sys
from datetime import UTC, datetime, timedelta

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
