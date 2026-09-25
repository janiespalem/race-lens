import os
from datetime import UTC, datetime, timedelta

import pytest

from racelens.recorder.health import MAX_PROCESSING_AGE, check
from racelens.recorder.state import Phase, StateStore


def test_health_rejects_a_stalled_recording(tmp_path):
    now = datetime(2026, 7, 17, 12, tzinfo=UTC)
    state_dir = tmp_path / "state"
    raw_dir = tmp_path / "raw"
    state_dir.mkdir()
    raw_dir.mkdir()
    heartbeat = state_dir / "heartbeat"
    heartbeat.touch()
    raw = raw_dir / "2026-10-fp1.f1live"
    raw.touch()
    StateStore(state_dir / "recorder.json").transition("2026-10-fp1", Phase.RECORDING, now)
    fresh = now.timestamp()
    os.utime(heartbeat, (fresh, fresh))
    os.utime(raw, (fresh, fresh))

    check(tmp_path, now)
    stale = (now - timedelta(minutes=6)).timestamp()
    os.utime(raw, (stale, stale))

    with pytest.raises(RuntimeError, match="stale file"):
        check(tmp_path, now)


def test_health_rejects_a_worker_without_a_schedule(tmp_path):
    now = datetime(2026, 9, 3, 20, tzinfo=UTC)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "heartbeat").touch()
    (state_dir / "schedule-failure").write_text("schedule host offline\n", encoding="utf-8")
    os.utime(state_dir / "heartbeat", (now.timestamp(), now.timestamp()))

    with pytest.raises(RuntimeError, match="schedule unavailable"):
        check(tmp_path, now)


def test_active_preparation_marker_does_not_hide_stale_coordinator(tmp_path):
    now = datetime(2026, 9, 25, 13, tzinfo=UTC)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    heartbeat = state_dir / "heartbeat"
    heartbeat.touch()
    store = StateStore(state_dir / "recorder.json")
    store.transition("2026-15-q", Phase.RECORDING, now)
    store.transition("2026-15-q", Phase.CAPTURED, now)
    store.transition("2026-15-q", Phase.PROCESSING, now)
    (state_dir / "preparation-active.json").write_text(
        '{"session_id":"2026-15-q","started_at":"2026-09-25T13:00:00+00:00"}\n',
        encoding="utf-8",
    )
    stale = now.timestamp() - MAX_PROCESSING_AGE - 1
    os.utime(heartbeat, (stale, stale))

    with pytest.raises(RuntimeError, match="stale file: heartbeat"):
        check(tmp_path, now)
