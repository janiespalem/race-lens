import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from racelens.object_storage import publish_session
from racelens.insights.passes import KIND_ON_TRACK, Pass
from racelens.recorder.schedule import ScheduledSession
from racelens.recorder.worker import Config, Recorder, fixture_stem
from racelens.recorder.state import Phase
from tests.test_object_storage import MemoryStore


SESSION = ScheduledSession(
    2026, 13, "Belgian Grand Prix", "R", datetime(2026, 7, 19, 13, tzinfo=UTC)
)


def _config(tmp_path, publish=frozenset({"R"})):
    return Config(
        state_dir=tmp_path / "state", raw_dir=tmp_path / "raw",
        data_dir=tmp_path / "data", interval_seconds=120, capture_poll_seconds=5,
        raw_retention_days=14, publish_sessions=publish,
        transcribe_radio=False, race_core=Path("race-core"),
    )


def test_fixture_stem_is_stable_and_readable():
    assert fixture_stem(SESSION) == "belgian_2026_race"


def test_live_pass_requires_stable_order_for_five_seconds(tmp_path):
    clock = [datetime(2026, 7, 19, 13, tzinfo=UTC)]
    recorder = Recorder(_config(tmp_path), now=lambda: clock[0], object_store=MemoryStore())
    candidate = Pass(120_000, 2, "VER", "NOR", 1, KIND_ON_TRACK)
    stable = {"drivers": {
        "VER": {"position": 1},
        "NOR": {"position": 2},
    }}
    reverted = {"drivers": {
        "VER": {"position": 2},
        "NOR": {"position": 1},
    }}

    assert recorder._confirmed_live_passes(SESSION, [candidate], stable, clock[0]) == []
    clock[0] += timedelta(seconds=2)
    assert recorder._confirmed_live_passes(SESSION, [candidate], reverted, clock[0]) == []
    clock[0] += timedelta(seconds=1)
    assert recorder._confirmed_live_passes(SESSION, [candidate], stable, clock[0]) == []
    clock[0] += timedelta(seconds=5)
    assert recorder._confirmed_live_passes(SESSION, [candidate], stable, clock[0]) == [candidate]


def test_stage_allows_only_archive_files_and_writes_manifest_last(tmp_path):
    recorder = Recorder(_config(tmp_path))
    archive = tmp_path / "data" / "archive"
    archive.mkdir(parents=True)
    fixture = archive / "belgian_2026_race.jsonl"
    fixture.write_text("fixture", encoding="utf-8")

    recorder._stage(SESSION, [fixture])

    publish = tmp_path / "data" / "publish"
    manifest = json.loads((publish / "belgian_2026_race.ready.json").read_text())
    assert manifest == {"session": "2026-13-r", "files": [fixture.name]}
    with pytest.raises(ValueError, match="outside archive"):
        recorder._stage(SESSION, [tmp_path / "foreign.jsonl"])


def test_official_award_delay_never_fails_archive_publication(tmp_path, monkeypatch):
    recorder = Recorder(replace(_config(tmp_path), git_publication=False))
    monkeypatch.setattr(
        "racelens.driver_of_day.sync_official_award",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("not posted")),
    )

    recorder._publish(SESSION, [], 1)


def test_invalid_canonical_fixture_skips_radio_transcription(tmp_path, monkeypatch):
    recorder = Recorder(replace(_config(tmp_path), transcribe_radio=True))
    commands = []
    monkeypatch.setattr(recorder, "_run", lambda argv, **_kwargs: commands.append(argv))
    monkeypatch.setattr("racelens.recorder.worker.merge_captured_radio", lambda *_args: None)
    monkeypatch.setattr(
        "racelens.recorder.worker.validate_fixture",
        lambda _path: (_ for _ in ()).throw(RuntimeError("canonical unavailable")),
    )

    with pytest.raises(RuntimeError, match="canonical unavailable"):
        recorder._build_archive(
            SESSION, captured=tmp_path / "captured.jsonl", full=True,
        )

    assert not any("radio-transcribe" in command for command in commands)


def test_config_rejects_unknown_publish_session(tmp_path, monkeypatch):
    monkeypatch.setenv("RACELENS_RECORDER_DATA", str(tmp_path))
    monkeypatch.setenv("RECORDER_PUBLISH_SESSIONS", "R,wat")
    with pytest.raises(ValueError, match="unknown session"):
        Config.from_env()


def test_config_publishes_every_session_type_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("RACELENS_RECORDER_DATA", str(tmp_path))
    monkeypatch.delenv("RECORDER_PUBLISH_SESSIONS", raising=False)
    assert Config.from_env().publish_sessions == frozenset(
        {"FP1", "FP2", "FP3", "Q", "SQ", "Sprint", "R"}
    )


def test_processing_retry_keeps_live_finishing_and_never_recaptures(tmp_path, monkeypatch):
    clock = [datetime(2026, 7, 19, 12, 55, tzinfo=UTC)]
    session = ScheduledSession(2026, 13, "Belgian Grand Prix", "R", clock[0])
    recorder = Recorder(_config(tmp_path), now=lambda: clock[0])
    monkeypatch.setattr(
        "racelens.recorder.worker.load_fastf1_schedule", lambda year: [session]
    )
    captures = []
    processes = []
    live_statuses = []
    monkeypatch.setattr(recorder, "capture", lambda item: captures.append(item.session_id))
    monkeypatch.setattr(
        recorder,
        "_set_live_status",
        lambda _session, status, **_kwargs: live_statuses.append(status),
    )

    def process(item):
        processes.append(item.session_id)
        if len(processes) == 1:
            raise RuntimeError("archive not ready")

    monkeypatch.setattr(recorder, "process", process)

    assert recorder.run_once().startswith("captured:")
    assert recorder.run_once().startswith("processing failed:")
    failed = recorder.store.load().sessions[session.session_id]
    assert failed.phase is Phase.FAILED
    assert failed.retry_phase is Phase.PROCESSING

    clock[0] += timedelta(minutes=16)
    assert recorder.run_once().startswith("complete:")
    assert captures == [session.session_id]
    assert processes == [session.session_id, session.session_id]
    assert live_statuses == []


def test_due_capture_precedes_older_captured_archive(tmp_path, monkeypatch):
    now = datetime(2026, 7, 19, 12, 55, tzinfo=UTC)
    older = ScheduledSession(
        2026, 12, "Dutch Grand Prix", "FP1", now - timedelta(hours=3),
    )
    later = ScheduledSession(
        2026, 13, "Belgian Grand Prix", "FP1", now + timedelta(minutes=10),
    )
    recorder = Recorder(_config(tmp_path), now=lambda: now)
    recorder.store.transition(older.session_id, Phase.RECORDING, now)
    recorder.store.transition(older.session_id, Phase.CAPTURED, now)
    monkeypatch.setattr(
        "racelens.recorder.worker.load_fastf1_schedule", lambda year: [older, later]
    )
    captures = []
    processes = []
    monkeypatch.setattr(recorder, "capture", lambda item: captures.append(item.session_id))
    monkeypatch.setattr(recorder, "process", lambda item: processes.append(item.session_id))

    assert recorder.run_once() == f"captured: {later.session_id}"
    assert captures == [later.session_id]
    assert processes == []


def test_approaching_capture_precedes_older_captured_archive(tmp_path, monkeypatch):
    now = datetime(2026, 7, 19, 12, 55, tzinfo=UTC)
    older = ScheduledSession(
        2026, 12, "Dutch Grand Prix", "FP1", now - timedelta(hours=3),
    )
    later = ScheduledSession(
        2026, 13, "Belgian Grand Prix", "FP1", now + timedelta(hours=1, minutes=10),
    )
    recorder = Recorder(_config(tmp_path), now=lambda: now)
    recorder.store.transition(older.session_id, Phase.RECORDING, now)
    recorder.store.transition(older.session_id, Phase.CAPTURED, now)
    monkeypatch.setattr(
        "racelens.recorder.worker.load_fastf1_schedule", lambda year: [older, later]
    )
    captures = []
    processes = []
    monkeypatch.setattr(recorder, "capture", lambda item: captures.append(item.session_id))
    monkeypatch.setattr(recorder, "process", lambda item: processes.append(item.session_id))

    assert recorder.run_once() == (
        f"idle: next capture {later.session_id} at "
        f"{later.capture_from.isoformat()} (approaching)"
    )
    assert captures == []
    assert processes == []


def test_failed_capture_retry_precedes_older_captured_archive(tmp_path, monkeypatch):
    now = datetime(2026, 7, 19, 12, 55, tzinfo=UTC)
    older = ScheduledSession(
        2026, 12, "Dutch Grand Prix", "FP1", now - timedelta(hours=3),
    )
    retry_session = ScheduledSession(
        2026, 13, "Belgian Grand Prix", "FP1", now - timedelta(hours=1),
    )
    retry_at = now + timedelta(minutes=15)
    recorder = Recorder(_config(tmp_path), now=lambda: now)
    recorder.store.transition(older.session_id, Phase.RECORDING, now)
    recorder.store.transition(older.session_id, Phase.CAPTURED, now)
    recorder.store.transition(retry_session.session_id, Phase.RECORDING, now)
    recorder.store.transition(
        retry_session.session_id, Phase.FAILED, now,
        error="capture unavailable", retry_at=retry_at,
    )
    monkeypatch.setattr(
        "racelens.recorder.worker.load_fastf1_schedule",
        lambda year: [older, retry_session],
    )
    captures = []
    processes = []
    monkeypatch.setattr(recorder, "capture", lambda item: captures.append(item.session_id))
    monkeypatch.setattr(recorder, "process", lambda item: processes.append(item.session_id))

    assert recorder.run_once() == (
        f"idle: next capture {retry_session.session_id} at "
        f"{retry_at.isoformat()} (approaching)"
    )
    assert captures == []
    assert processes == []


def test_retention_keeps_input_for_captured_work(tmp_path, monkeypatch):
    now = datetime(2026, 7, 19, 15, tzinfo=UTC)
    recorder = Recorder(_config(tmp_path), now=lambda: now)
    recorder.store.transition(SESSION.session_id, Phase.RECORDING, now)
    recorder.store.transition(SESSION.session_id, Phase.CAPTURED, now)
    protected = recorder._paths(SESSION)["clean"]
    orphan = recorder.config.raw_dir / "old-complete.clean.f1live"
    for path in (protected, orphan):
        path.write_text("capture", encoding="utf-8")
        os.utime(path, (0, 0))
    monkeypatch.setattr(
        "racelens.recorder.worker.load_fastf1_schedule", lambda year: [SESSION]
    )
    monkeypatch.setattr(recorder, "process", lambda session: None)

    assert recorder.run_once().startswith("complete:")
    assert protected.exists()
    assert not orphan.exists()


def test_restart_resumes_persisted_processing_phase(tmp_path, monkeypatch):
    now = datetime(2026, 7, 19, 15, tzinfo=UTC)
    session = ScheduledSession(2026, 13, "Belgian Grand Prix", "R", now)
    first = Recorder(_config(tmp_path), now=lambda: now)
    first.store.transition(session.session_id, Phase.RECORDING, now)
    first.store.transition(session.session_id, Phase.CAPTURED, now)
    first.store.transition(session.session_id, Phase.PROCESSING, now)

    restarted = Recorder(_config(tmp_path), now=lambda: now)
    monkeypatch.setattr(
        "racelens.recorder.worker.load_fastf1_schedule", lambda year: [session]
    )
    processed = []
    monkeypatch.setattr(restarted, "process", lambda item: processed.append(item.session_id))

    assert restarted.run_once().startswith("complete:")
    assert processed == [session.session_id]


def test_restart_finalizes_recording_after_schedule_deadline(tmp_path, monkeypatch):
    start = datetime(2026, 7, 19, 10, tzinfo=UTC)
    now = start + timedelta(hours=5)
    session = ScheduledSession(2026, 13, "Belgian Grand Prix", "R", start)
    recorder = Recorder(_config(tmp_path), now=lambda: now)
    recorder.store.transition(session.session_id, Phase.RECORDING, start)
    monkeypatch.setattr(
        "racelens.recorder.worker.load_fastf1_schedule", lambda year: [session]
    )
    captures = []
    monkeypatch.setattr(recorder, "capture", lambda item: captures.append(item.session_id))

    assert recorder.run_once().startswith("captured:")
    assert captures == [session.session_id]


def test_idle_worker_reports_the_next_capture_window(tmp_path, monkeypatch):
    now = datetime(2026, 8, 14, 12, tzinfo=UTC)
    session = ScheduledSession(
        2026, 12, "Dutch Grand Prix", "FP1", datetime(2026, 8, 21, 10, 30, tzinfo=UTC),
    )
    recorder = Recorder(_config(tmp_path), now=lambda: now)
    monkeypatch.setattr(
        "racelens.recorder.worker.load_fastf1_schedule", lambda year: [session]
    )

    assert recorder.run_once() == (
        "idle: next capture 2026-12-fp1 at 2026-08-21T10:20:00+00:00"
    )


def test_restart_uses_persisted_schedule_when_fastf1_is_unavailable(tmp_path, monkeypatch):
    now = datetime(2026, 9, 3, 20, tzinfo=UTC)
    session = ScheduledSession(
        2026, 13, "Italian Grand Prix", "FP1", datetime(2026, 9, 4, 10, 30, tzinfo=UTC),
    )
    monkeypatch.setattr(
        "racelens.recorder.worker.load_fastf1_schedule", lambda _year: [session],
    )
    first = Recorder(_config(tmp_path), now=lambda: now)
    assert first.run_once().startswith("idle: next capture 2026-13-fp1")

    monkeypatch.setattr(
        "racelens.recorder.worker.load_fastf1_schedule",
        lambda _year: (_ for _ in ()).throw(OSError("schedule host offline")),
    )
    restarted = Recorder(_config(tmp_path), now=lambda: now)

    assert restarted.run_once().startswith("idle: next capture 2026-13-fp1")


def test_missing_schedule_cache_is_marked_for_healthcheck(tmp_path, monkeypatch):
    recorder = Recorder(_config(tmp_path), now=lambda: datetime(2026, 9, 3, tzinfo=UTC))
    monkeypatch.setattr(
        "racelens.recorder.worker.load_fastf1_schedule",
        lambda _year: (_ for _ in ()).throw(OSError("schedule host offline")),
    )

    with pytest.raises(OSError, match="schedule host offline"):
        recorder.run_once()

    assert (tmp_path / "state" / "schedule-failure").is_file()


def test_empty_current_schedule_is_not_reported_as_healthy(tmp_path, monkeypatch):
    recorder = Recorder(_config(tmp_path), now=lambda: datetime(2026, 9, 3, tzinfo=UTC))
    monkeypatch.setattr("racelens.recorder.worker.load_fastf1_schedule", lambda _year: [])

    with pytest.raises(RuntimeError, match="empty schedule for 2026"):
        recorder.run_once()

    assert (tmp_path / "state" / "schedule-failure").is_file()


def test_schedule_cache_write_failure_does_not_block_capture_selection(tmp_path, monkeypatch):
    now = datetime(2026, 9, 3, 20, tzinfo=UTC)
    session = ScheduledSession(
        2026, 13, "Italian Grand Prix", "FP1", datetime(2026, 9, 4, 10, 30, tzinfo=UTC),
    )
    recorder = Recorder(_config(tmp_path), now=lambda: now)
    monkeypatch.setattr("racelens.recorder.worker.load_fastf1_schedule", lambda _year: [session])
    monkeypatch.setattr(
        recorder, "_save_schedule_cache",
        lambda _sessions: (_ for _ in ()).throw(OSError("disk full")),
    )

    assert recorder.run_once().startswith("idle: next capture 2026-13-fp1")


def test_idle_worker_processes_one_durable_historical_request(tmp_path, monkeypatch):
    now = datetime(2026, 1, 10, tzinfo=UTC)
    historical = ScheduledSession(
        2024, 8, "Monaco Grand Prix", "R", datetime(2024, 5, 26, 13, tzinfo=UTC),
    )
    storage = MemoryStore()
    recorder = Recorder(_config(tmp_path), now=lambda: now, object_store=storage)
    recorder.remote_queue.enqueue(historical.session_id, fixture_stem(historical))
    monkeypatch.setattr(
        "racelens.recorder.worker.load_fastf1_schedule",
        lambda year: [historical] if year == 2024 else [SESSION],
    )

    def process_requested(session, replay_id):
        archive = tmp_path / "remote"
        archive.mkdir()
        fixture = archive / f"{replay_id}.jsonl"
        track = archive / f"{replay_id}.track.json"
        positions = archive / f"{replay_id}.positions.json"
        fixture.write_text("{}\n", encoding="utf-8")
        track.write_text("{}\n", encoding="utf-8")
        positions.write_text("{}\n", encoding="utf-8")
        publish_session(
            storage,
            session.session_id,
            replay_id,
            fixture,
            track,
            positions,
            event_count=1,
        )
        recorder.remote_queue.finish(session.session_id, replay_session_id=replay_id)

    monkeypatch.setattr(recorder, "process_requested", process_requested)

    assert recorder.run_once() == "requested archive complete: 2024-08-r"
    assert recorder.remote_queue.get("2024-08-r")["status"] == "ready"
    assert not recorder.remote_processing.exists()


def test_remote_request_uses_loaded_schedule_when_upstream_is_unavailable(tmp_path, monkeypatch):
    now = datetime(2026, 9, 8, tzinfo=UTC)
    recorder = Recorder(_config(tmp_path), now=lambda: now, object_store=MemoryStore())
    recorder._schedule = [SESSION]
    recorder.remote_queue.enqueue(SESSION.session_id, fixture_stem(SESSION))
    monkeypatch.setattr(
        "racelens.recorder.worker.load_fastf1_schedule",
        lambda _year: (_ for _ in ()).throw(OSError("upstream unavailable")),
    )
    processed = []
    monkeypatch.setattr(
        recorder, "process_requested", lambda session, replay_id: processed.append(session),
    )

    assert recorder._run_remote_once() == "requested archive complete: 2026-13-r"
    assert processed == [SESSION]
    assert not recorder.remote_processing.exists()


def test_due_failed_capture_recovers_after_window_closes(tmp_path, monkeypatch):
    now = SESSION.capture_until + timedelta(minutes=1)
    recorder = Recorder(_config(tmp_path), now=lambda: now)
    recorder.store.transition(SESSION.session_id, Phase.RECORDING, SESSION.starts_at)
    recorder.store.transition(
        SESSION.session_id, Phase.FAILED, SESSION.capture_until - timedelta(minutes=6),
        error="connection lost", retry_at=SESSION.capture_until - timedelta(minutes=1),
    )
    monkeypatch.setattr("racelens.recorder.worker.load_fastf1_schedule", lambda _year: [SESSION])
    recovered = []
    monkeypatch.setattr(recorder, "capture", lambda session: recovered.append(session))

    assert recorder.run_once() == "captured: 2026-13-r"
    assert recovered == [SESSION]
    assert recorder.store.load().sessions[SESSION.session_id].phase is Phase.CAPTURED


def test_expired_capture_without_matching_raw_never_starts_signalr(tmp_path, monkeypatch):
    recorder = Recorder(_config(tmp_path), now=lambda: SESSION.capture_until + timedelta(seconds=1))
    def unexpected_spawn(*_args, **_kwargs):
        pytest.fail("expired capture must not start a SignalR subprocess")
    monkeypatch.setattr("racelens.recorder.worker.subprocess.Popen", unexpected_spawn)

    with pytest.raises(RuntimeError, match="no matching recording"):
        recorder.capture(SESSION)


def test_expired_missing_capture_does_not_block_other_archive_between_retries(tmp_path, monkeypatch):
    now = SESSION.capture_until + timedelta(minutes=1)
    older = replace(SESSION, round_number=12, starts_at=SESSION.starts_at - timedelta(days=7))
    recorder = Recorder(_config(tmp_path), now=lambda: now)
    recorder.store.transition(older.session_id, Phase.RECORDING, older.starts_at)
    recorder.store.transition(older.session_id, Phase.CAPTURED, older.capture_until)
    recorder.store.transition(SESSION.session_id, Phase.RECORDING, SESSION.starts_at)
    recorder.store.transition(
        SESSION.session_id, Phase.FAILED, SESSION.capture_until,
        error="connection lost", retry_at=now,
    )
    monkeypatch.setattr(
        "racelens.recorder.worker.load_fastf1_schedule", lambda _year: [older, SESSION],
    )
    processed = []
    monkeypatch.setattr(recorder, "process", lambda session: processed.append(session.session_id))

    assert recorder.run_once().startswith(f"capture failed: {SESSION.session_id}:")
    assert recorder.store.load().sessions[SESSION.session_id].retry_at > now
    assert recorder.run_once() == f"complete: {older.session_id}"
    assert processed == [older.session_id]
    assert recorder.store.load().sessions[older.session_id].phase is Phase.COMPLETE
