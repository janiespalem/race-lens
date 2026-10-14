import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from racelens.object_storage import publish_session
from racelens.events.models import dump_jsonl, event
from racelens.insights.passes import KIND_ON_TRACK, Pass
from racelens.radio.archive_maintenance import MaintenanceBudget, MaintenanceResult
from racelens.radio.models import CURRENT_RADIO_POLICY, CURRENT_RADIO_PROFILE, FinalTranscriptDecision
from racelens.recorder.preparation import PreparationOutcome
from racelens.recorder.schedule import ScheduledSession
from racelens.recorder.worker import Config, Recorder, fixture_stem
from racelens.recorder.state import Phase
from tests.test_object_storage import MemoryStore


SESSION = ScheduledSession(
    2026, 13, "Belgian Grand Prix", "R", datetime(2026, 7, 19, 13, tzinfo=UTC)
)


class FakePreparationRunner:
    def __init__(self):
        self.active_session_id = None
        self.starts = []
        self.outcome = None
        self.stop_calls = []
        self.closed = False
        self.start_error = None

    def start(self, session):
        if self.start_error is not None:
            raise self.start_error
        if self.active_session_id is not None:
            return False
        self.active_session_id = session.session_id
        self.starts.append(session.session_id)
        return True

    def poll(self):
        outcome, self.outcome = self.outcome, None
        if outcome is not None:
            self.active_session_id = None
        return outcome

    def stop(self, reason):
        if self.active_session_id is None:
            return None
        self.stop_calls.append((self.active_session_id, reason))
        outcome = PreparationOutcome(self.active_session_id, reason)
        self.active_session_id = None
        return outcome

    def close(self):
        self.closed = True
        self.stop("coordinator stopped")


class FakeRadioMaintainer:
    def __init__(self, result=None):
        self.result = result or MaintenanceResult("idle")
        self.budgets = []

    def run_once(self, budget):
        self.budgets.append(budget)
        return self.result


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
    monkeypatch.setattr("racelens.recorder.worker.merge_captured_live_data", lambda *_args: None)
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


def test_radio_maintenance_defaults_off(tmp_path, monkeypatch):
    monkeypatch.setenv("RACELENS_RECORDER_DATA", str(tmp_path))
    monkeypatch.delenv("RECORDER_RADIO_MAINTENANCE", raising=False)

    assert Config.from_env().radio_maintenance_mode == "off"


@pytest.mark.parametrize("mode", ["off", "dry-run", "enabled"])
@pytest.mark.parametrize("transcribe", ["0", "1"])
def test_radio_maintenance_mode_is_independent_from_transcription(
    tmp_path, monkeypatch, mode, transcribe,
):
    monkeypatch.setenv("RACELENS_RECORDER_DATA", str(tmp_path))
    monkeypatch.setenv("RECORDER_RADIO_MAINTENANCE", mode)
    monkeypatch.setenv("RECORDER_TRANSCRIBE_RADIO", transcribe)

    config = Config.from_env()

    assert config.radio_maintenance_mode == mode
    assert config.transcribe_radio is (transcribe == "1")


@pytest.mark.parametrize("mode", ["", "true", "ENABLED", " dry-run", "enabled "])
def test_radio_maintenance_rejects_unknown_mode(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("RACELENS_RECORDER_DATA", str(tmp_path))
    monkeypatch.setenv("RECORDER_RADIO_MAINTENANCE", mode)

    with pytest.raises(ValueError, match="RECORDER_RADIO_MAINTENANCE"):
        Config.from_env()


@pytest.mark.parametrize(("mode", "publish"), [("dry-run", False), ("enabled", True)])
def test_radio_maintenance_constructs_durable_adapter(tmp_path, mode, publish):
    from racelens.radio.archive_maintenance import RadioArchiveMaintainer
    from racelens.radio.transcribe import transcribe_final

    storage = MemoryStore()
    recorder = Recorder(
        replace(_config(tmp_path), radio_maintenance_mode=mode), object_store=storage,
    )

    assert isinstance(recorder.radio_maintainer, RadioArchiveMaintainer)
    assert recorder.radio_maintainer.store is storage
    assert recorder.radio_maintainer.workspace.is_relative_to(recorder.config.state_dir)
    assert recorder.radio_maintainer.transcribe is transcribe_final
    assert recorder.radio_maintainer.publish is publish


@pytest.mark.parametrize("mode", ["off", "enabled"])
def test_radio_maintenance_requires_storage_and_non_off_mode(tmp_path, monkeypatch, mode):
    monkeypatch.setattr("racelens.recorder.worker.StorageConfig.from_env", lambda: None)
    recorder = Recorder(
        replace(_config(tmp_path), radio_maintenance_mode=mode),
        object_store=MemoryStore() if mode == "off" else None,
    )

    assert recorder.radio_maintainer is None


@pytest.mark.parametrize(
    "work",
    ["capture", "recording", "capture_retry", "guard", "captured", "processing_retry",
     "preparation_outcome", "remote"],
)
def test_radio_maintenance_yields_to_higher_priority_work(tmp_path, monkeypatch, work):
    now = datetime(2026, 7, 19, 15, tzinfo=UTC)
    session = replace(SESSION, starts_at=now - timedelta(hours=4))
    if work == "capture":
        session = replace(session, starts_at=now)
    elif work == "guard":
        session = replace(session, starts_at=now + timedelta(hours=3))
    maintainer = FakeRadioMaintainer()
    runner = FakePreparationRunner()
    recorder = Recorder(
        replace(_config(tmp_path), radio_maintenance_mode="enabled"),
        now=lambda: now, object_store=MemoryStore(), preparation_runner=runner,
        radio_maintainer=maintainer,
    )
    monkeypatch.setattr("racelens.recorder.worker.load_fastf1_schedule", lambda _year: [session])
    captures, requested = [], []
    monkeypatch.setattr(recorder, "capture", lambda item: captures.append(item.session_id))
    monkeypatch.setattr(
        recorder, "process_requested", lambda item, _replay: requested.append(item.session_id),
    )
    if work in {"recording", "capture_retry", "captured", "processing_retry",
                "preparation_outcome"}:
        recorder.store.transition(session.session_id, Phase.RECORDING, now)
    if work in {"captured", "processing_retry", "preparation_outcome"}:
        recorder.store.transition(session.session_id, Phase.CAPTURED, now)
    if work in {"processing_retry", "preparation_outcome"}:
        recorder.store.transition(session.session_id, Phase.PROCESSING, now)
    if work in {"capture_retry", "processing_retry"}:
        recorder.store.transition(
            session.session_id, Phase.FAILED, now, error="retry required", retry_at=now,
        )
    if work == "preparation_outcome":
        runner.outcome = PreparationOutcome(session.session_id, None)
    if work == "remote":
        recorder.remote_queue.enqueue(session.session_id, fixture_stem(session))

    result = recorder.run_once()

    assert maintainer.budgets == []
    if work in {"capture", "recording", "capture_retry"}:
        assert result == f"captured: {session.session_id}"
        assert captures == [session.session_id]
    elif work == "guard":
        assert result.endswith("(approaching)")
    elif work in {"captured", "processing_retry"}:
        assert result == f"processing started: {session.session_id}"
        assert runner.starts == [session.session_id]
    elif work == "preparation_outcome":
        assert result == f"complete: {session.session_id}"
    else:
        assert result == f"requested archive complete: {session.session_id}"
        assert requested == [session.session_id]


@pytest.mark.parametrize("mode", ["dry-run", "enabled"])
def test_idle_radio_maintenance_uses_bounded_pass_before_awards(tmp_path, monkeypatch, mode):
    now = datetime(2026, 7, 19, 15, tzinfo=UTC)
    session = replace(SESSION, starts_at=now + timedelta(hours=3, seconds=1))
    maintainer = FakeRadioMaintainer(MaintenanceResult(
        "published", "belgian_2026_race", 3, 2, 1, 1, 2,
    ))
    recorder = Recorder(
        replace(_config(tmp_path), radio_maintenance_mode=mode), now=lambda: now,
        object_store=MemoryStore(), radio_maintainer=maintainer,
        preparation_runner=FakePreparationRunner(),
    )
    monkeypatch.setattr("racelens.recorder.worker.load_fastf1_schedule", lambda _year: [session])
    awards = []
    monkeypatch.setattr(recorder, "_sync_completed_awards", lambda _sessions: awards.append(True))

    result = recorder.run_once()

    assert maintainer.budgets == [MaintenanceBudget(1, 3, 60.0)]
    assert result == (
        "radio maintenance published: belgian_2026_race "
        "scanned=3 processed=2 reused=1 accepted=1 rejected=2"
    )
    assert awards == []


def test_idle_radio_maintenance_allows_lower_priority_awards(tmp_path, monkeypatch):
    now = SESSION.capture_until + timedelta(days=1)
    maintainer = FakeRadioMaintainer()
    recorder = Recorder(
        replace(_config(tmp_path), radio_maintenance_mode="enabled"), now=lambda: now,
        object_store=MemoryStore(), radio_maintainer=maintainer,
        preparation_runner=FakePreparationRunner(),
    )
    monkeypatch.setattr("racelens.recorder.worker.load_fastf1_schedule", lambda _year: [SESSION])
    award_budgets = []
    monkeypatch.setattr(
        recorder, "_sync_completed_awards",
        lambda _sessions: award_budgets.append(list(maintainer.budgets)),
    )

    assert recorder.run_once() == "idle"
    assert maintainer.budgets == [MaintenanceBudget(1, 3, 60.0)]
    assert award_budgets == [[MaintenanceBudget(1, 3, 60.0)]]


def test_off_radio_maintenance_never_invokes_injected_adapter(tmp_path, monkeypatch):
    maintainer = FakeRadioMaintainer()
    recorder = Recorder(
        _config(tmp_path), now=lambda: SESSION.capture_until + timedelta(days=1),
        object_store=MemoryStore(), radio_maintainer=maintainer,
        preparation_runner=FakePreparationRunner(),
    )
    monkeypatch.setattr("racelens.recorder.worker.load_fastf1_schedule", lambda _year: [SESSION])

    assert recorder.run_once() == "idle"
    assert maintainer.budgets == []


@pytest.mark.parametrize("replay", [None, "bad\ntranscript text", "https://audio.example/secret"])
def test_radio_maintenance_status_sanitizes_replay_identity(tmp_path, monkeypatch, replay):
    maintainer = FakeRadioMaintainer(MaintenanceResult("quarantined", replay))
    recorder = Recorder(
        replace(_config(tmp_path), radio_maintenance_mode="enabled"),
        now=lambda: SESSION.capture_until + timedelta(days=1),
        object_store=MemoryStore(), radio_maintainer=maintainer,
        preparation_runner=FakePreparationRunner(),
    )
    monkeypatch.setattr("racelens.recorder.worker.load_fastf1_schedule", lambda _year: [SESSION])

    assert recorder.run_once() == (
        "radio maintenance quarantined: - scanned=0 processed=0 reused=0 accepted=0 rejected=0"
    )


def _archive_commands(recorder, session, monkeypatch, *, deferred=False):
    from racelens.radio.transcribe import enrich_fixture

    replay = fixture_stem(session)
    paths = recorder._paths(session)
    paths["clean"].touch()
    laps = [
        event(replay, "LapCompleted", lap * 60_000, driver, lap=lap)
        for lap in range(1, 4) for driver in ("VER", "HAM")
    ]
    radio = [
        event(replay, "RaceControlMessage", 1000, "VER", category="Radio",
              audio_url="https://audio.example/accepted"),
        event(replay, "RaceControlMessage", 2000, "HAM", category="Radio",
              audio_url="https://audio.example/rejected"),
        event(replay, "RaceControlMessage", 3000, "VER", category="Radio",
              audio_url="https://audio.example/manual", transcript="  Manual text  "),
    ]
    inference = []

    def transcribe(url):
        inference.append(url)
        if deferred:
            return FinalTranscriptDecision("deferred", None, "processing_failed", {})
        accepted = url.endswith("accepted")
        status, reason = ("accepted", None) if accepted else ("rejected", "low_confidence_words")
        return FinalTranscriptDecision(
            status, "Box this lap" if accepted else None, reason,
            {"profile": CURRENT_RADIO_PROFILE, "policy": CURRENT_RADIO_POLICY,
             "status": status, "reason": reason},
        )

    monkeypatch.setattr("racelens.radio.transcribe.transcribe_final", transcribe)
    commands = []

    def run(argv, **_kwargs):
        commands.append(argv)
        if "ingest" in argv:
            rows = sorted(laps + radio, key=lambda row: (row.session_time_ms, row.event_id))
            paths["fixture"].write_text(dump_jsonl(rows), encoding="utf-8")
        elif "ingest-live" in argv:
            paths["provisional"].write_text(dump_jsonl(radio), encoding="utf-8")
        elif "radio-transcribe" in argv:
            enrich_fixture(paths["fixture"])
        elif "track" in argv:
            paths["track"].write_text(json.dumps({
                "session_id": replay, "points": [[0, 0], [1, 1]],
                "progress_points": [[0, 0], [1, 1]], "viewbox": [600, 400],
            }), encoding="utf-8")
        elif argv[0] == str(recorder.config.race_core):
            paths["positions"].write_text(json.dumps({
                "session_id": replay, "tick_ms": 1000, "viewbox": [600, 400],
                "drivers": {"VER": [[0, 0]] * 300}, "progress": {"VER": [0.0] * 300},
            }), encoding="utf-8")

    monkeypatch.setattr(recorder, "_run", run)
    return inference, commands


@pytest.mark.parametrize(
    ("transcribe", "captured", "schema"), [(True, True, 2), (False, True, 1), (True, False, 1)],
)
def test_initial_archive_radio_publication_provenance(
    tmp_path, monkeypatch, transcribe, captured, schema,
):
    from racelens.radio.archive_maintenance import RadioArchiveMaintainer

    class ArchiveStore(MemoryStore):
        def list_common_prefixes(self, prefix, *, limit=1000):
            return sorted({
                prefix + key[len(prefix):].split("/", 1)[0] + "/"
                for key in self.list_keys(prefix, limit=limit)
            })

    storage = ArchiveStore()
    session = replace(SESSION, kind="Q")
    recorder = Recorder(
        replace(_config(tmp_path, frozenset({"Q"})), transcribe_radio=transcribe,
                git_publication=False), object_store=storage,
    )
    inference, commands = _archive_commands(recorder, session, monkeypatch)
    publication_summaries = []

    def publish(*args, **kwargs):
        publication_summaries.append(kwargs.get("radio_summary"))
        return publish_session(*args, **kwargs)

    monkeypatch.setattr("racelens.recorder.worker.publish_session", publish)

    if captured:
        recorder.process(session)
    else:
        recorder.process_requested(session, fixture_stem(session))

    manifest = storage.objects[f"sessions/{fixture_stem(session)}/manifest.json"]
    assert manifest["schema_version"] == schema
    assert manifest["event_count"] == 9
    if schema == 2:
        assert publication_summaries == [{
            "profile": "medium-int8-word-timestamps-v1", "policy": "f1-confidence-gate-v1",
            "total": 3, "accepted": 1, "rejected": 1, "manual": 1,
        }]
        assert manifest["radio_profile"] == "medium-int8-word-timestamps-v1"
        assert manifest["radio_policy"] == "f1-confidence-gate-v1"
        assert manifest["radio_summary"] == {"total": 3, "accepted": 1, "rejected": 1, "manual": 1}
        assert manifest["parent_revision"] is None
        assert len(inference) == 2
        before = list(storage.operations)
        maintainer = RadioArchiveMaintainer(
            storage, tmp_path / "maintenance",
            lambda _url: pytest.fail("a newly transcribed archive must not repeat inference"),
        )
        assert maintainer.run_once(MaintenanceBudget(1, 3, 60.0)).status == "idle"
        assert storage.operations == before
    else:
        assert publication_summaries == [None]
        assert "radio_profile" not in manifest
        assert inference == []
        assert not any("radio-transcribe" in command for command in commands)


def test_deferred_initial_radio_transcription_never_publishes(tmp_path, monkeypatch):
    from racelens.radio.transcribe import IncompleteTranscriptionError

    storage = MemoryStore()
    session = replace(SESSION, kind="Q")
    recorder = Recorder(
        replace(_config(tmp_path, frozenset({"Q"})), transcribe_radio=True,
                git_publication=False), object_store=storage,
    )
    _archive_commands(recorder, session, monkeypatch, deferred=True)

    with pytest.raises(IncompleteTranscriptionError):
        recorder.process(session)

    assert storage.operations == []


def test_initial_radio_summary_requires_final_archive_validation(tmp_path, monkeypatch):
    session = replace(SESSION, kind="Q")
    storage = MemoryStore()
    recorder = Recorder(
        replace(_config(tmp_path, frozenset({"Q"})), transcribe_radio=True,
                git_publication=False), object_store=storage,
    )
    _archive_commands(recorder, session, monkeypatch)
    monkeypatch.setattr(
        "racelens.recorder.worker.validate_archive",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("invalid archive")),
    )
    monkeypatch.setattr(
        "racelens.radio.transcribe.summarize_fixture",
        lambda _path: pytest.fail("summary must not certify an invalid archive"),
    )

    with pytest.raises(RuntimeError, match="invalid archive"):
        recorder.process(session)

    assert storage.operations == []


def test_running_preparation_is_preempted_when_capture_becomes_due(tmp_path, monkeypatch):
    clock = [datetime(2026, 7, 19, 8, tzinfo=UTC)]
    older = ScheduledSession(
        2026, 12, "Dutch Grand Prix", "FP1", clock[0] - timedelta(hours=3),
    )
    later = ScheduledSession(
        2026, 13, "Belgian Grand Prix", "Q", clock[0] + timedelta(hours=4),
    )
    runner = FakePreparationRunner()
    recorder = Recorder(
        _config(tmp_path), now=lambda: clock[0], preparation_runner=runner,
    )
    recorder.store.transition(older.session_id, Phase.RECORDING, clock[0])
    recorder.store.transition(older.session_id, Phase.CAPTURED, clock[0])
    monkeypatch.setattr(
        "racelens.recorder.worker.load_fastf1_schedule", lambda _year: [older, later]
    )
    captures = []
    monkeypatch.setattr(recorder, "capture", lambda item: captures.append(item.session_id))

    assert recorder.run_once() == f"processing started: {older.session_id}"
    assert runner.active_session_id == older.session_id

    clock[0] = later.capture_from
    assert recorder.run_once() == f"captured: {later.session_id}"
    assert captures == [later.session_id]
    assert runner.stop_calls == [
        (older.session_id, "preempted by scheduled capture"),
    ]
    older_state = recorder.store.load().sessions[older.session_id]
    assert older_state.phase is Phase.FAILED
    assert older_state.retry_phase is Phase.PROCESSING


def test_preparation_success_is_reconciled_without_blocking_due_capture(
    tmp_path, monkeypatch,
):
    clock = [datetime(2026, 7, 19, 8, tzinfo=UTC)]
    older = ScheduledSession(
        2026, 12, "Dutch Grand Prix", "FP1", clock[0] - timedelta(hours=3),
    )
    later = ScheduledSession(
        2026, 13, "Belgian Grand Prix", "Q", clock[0] + timedelta(hours=4),
    )
    runner = FakePreparationRunner()
    recorder = Recorder(
        _config(tmp_path), now=lambda: clock[0], preparation_runner=runner,
    )
    recorder.store.transition(older.session_id, Phase.RECORDING, clock[0])
    recorder.store.transition(older.session_id, Phase.CAPTURED, clock[0])
    monkeypatch.setattr(
        "racelens.recorder.worker.load_fastf1_schedule", lambda _year: [older, later]
    )
    captures = []
    monkeypatch.setattr(recorder, "capture", lambda item: captures.append(item.session_id))

    assert recorder.run_once() == f"processing started: {older.session_id}"
    runner.outcome = PreparationOutcome(older.session_id, None)
    clock[0] = later.capture_from

    assert recorder.run_once() == f"captured: {later.session_id}"
    assert captures == [later.session_id]
    assert runner.stop_calls == []
    assert recorder.store.load().sessions[older.session_id].phase is Phase.COMPLETE


@pytest.mark.parametrize(
    ("outcome", "phase"),
    [
        (PreparationOutcome(SESSION.session_id, None), Phase.COMPLETE),
        (PreparationOutcome(SESSION.session_id, "archive unavailable"), Phase.FAILED),
    ],
)
def test_preparation_outcome_is_persisted_by_coordinator(
    tmp_path, monkeypatch, outcome, phase,
):
    now = datetime(2026, 7, 19, 15, tzinfo=UTC)
    session = replace(SESSION, starts_at=now - timedelta(hours=3))
    runner = FakePreparationRunner()
    recorder = Recorder(_config(tmp_path), now=lambda: now, preparation_runner=runner)
    recorder.store.transition(session.session_id, Phase.RECORDING, now)
    recorder.store.transition(session.session_id, Phase.CAPTURED, now)
    monkeypatch.setattr(
        "racelens.recorder.worker.load_fastf1_schedule", lambda _year: [session]
    )

    assert recorder.run_once() == f"processing started: {session.session_id}"
    runner.outcome = outcome
    result = recorder.run_once()

    assert result.startswith("complete:" if phase is Phase.COMPLETE else "processing failed:")
    stored = recorder.store.load().sessions[session.session_id]
    assert stored.phase is phase
    if phase is Phase.FAILED:
        assert stored.retry_phase is Phase.PROCESSING


def test_preparation_start_failure_becomes_retryable(tmp_path, monkeypatch):
    now = datetime(2026, 7, 19, 15, tzinfo=UTC)
    session = replace(SESSION, starts_at=now - timedelta(hours=3))
    runner = FakePreparationRunner()
    runner.start_error = OSError("process table full")
    recorder = Recorder(_config(tmp_path), now=lambda: now, preparation_runner=runner)
    recorder.store.transition(session.session_id, Phase.RECORDING, now)
    recorder.store.transition(session.session_id, Phase.CAPTURED, now)
    monkeypatch.setattr(
        "racelens.recorder.worker.load_fastf1_schedule", lambda _year: [session]
    )

    assert recorder.run_once() == (
        f"processing failed: {session.session_id}: process table full"
    )
    stored = recorder.store.load().sessions[session.session_id]
    assert stored.phase is Phase.FAILED
    assert stored.retry_phase is Phase.PROCESSING


def test_starting_preparation_releases_live_transcription_without_waiting(
    tmp_path, monkeypatch,
):
    now = datetime(2026, 7, 19, 15, tzinfo=UTC)
    session = replace(SESSION, starts_at=now - timedelta(hours=3))
    runner = FakePreparationRunner()
    recorder = Recorder(_config(tmp_path), now=lambda: now, preparation_runner=runner)

    class Transcripts:
        def __init__(self):
            self.close_calls = []

        def close(self, *, wait=True):
            self.close_calls.append(wait)

    transcripts = Transcripts()
    recorder._transcripts = transcripts
    recorder.store.transition(session.session_id, Phase.RECORDING, now)
    recorder.store.transition(session.session_id, Phase.CAPTURED, now)
    monkeypatch.setattr(
        "racelens.recorder.worker.load_fastf1_schedule", lambda _year: [session]
    )

    assert recorder.run_once() == f"processing started: {session.session_id}"

    assert transcripts.close_calls == [False]
    assert recorder._transcripts is None


@pytest.mark.parametrize("with_live_worker", [False, True])
def test_preparation_releases_maintenance_loaded_model_before_start(
    tmp_path, monkeypatch, with_live_worker,
):
    import io
    import sys
    import weakref
    from types import SimpleNamespace

    from racelens.radio import transcribe as radio

    models = []

    class Model:
        def __init__(self, *args, **kwargs):
            self.cycle = self
            models.append(weakref.ref(self))

        def transcribe(self, *args, **kwargs):
            return [], None

    class Runner(FakePreparationRunner):
        def start(self, session):
            assert radio._model.cache_info().currsize == 0
            assert models[0]() is None
            return super().start(session)

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Model))
    monkeypatch.setattr(radio.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"audio"))
    radio._model.cache_clear()
    now = datetime(2026, 7, 19, 15, tzinfo=UTC)
    session = replace(SESSION, starts_at=now - timedelta(hours=3))
    recorder = Recorder(_config(tmp_path), now=lambda: now, preparation_runner=Runner())
    if with_live_worker:
        recorder._transcripts = radio.TranscriptWorker()
    recorder.store.transition(session.session_id, Phase.RECORDING, now)
    recorder.store.transition(session.session_id, Phase.CAPTURED, now)
    monkeypatch.setattr("racelens.recorder.worker.load_fastf1_schedule", lambda _year: [session])
    try:
        assert radio.transcribe_final("https://audio.example/radio.mp3").status == "rejected"
        assert radio._model.cache_info().currsize == 1
        assert models[0]() is not None

        assert recorder.run_once() == f"processing started: {session.session_id}"

        assert recorder._transcripts is None
    finally:
        if recorder._transcripts is not None:
            recorder._transcripts.close()
        radio._model.cache_clear()


def test_active_preparation_prevents_second_start(tmp_path, monkeypatch):
    now = datetime(2026, 7, 19, 15, tzinfo=UTC)
    first = replace(SESSION, starts_at=now - timedelta(hours=4))
    second = ScheduledSession(
        2026, 12, "Dutch Grand Prix", "FP1", now - timedelta(hours=3),
    )
    runner = FakePreparationRunner()
    recorder = Recorder(_config(tmp_path), now=lambda: now, preparation_runner=runner)
    for session in (first, second):
        recorder.store.transition(session.session_id, Phase.RECORDING, now)
        recorder.store.transition(session.session_id, Phase.CAPTURED, now)
    monkeypatch.setattr(
        "racelens.recorder.worker.load_fastf1_schedule", lambda _year: [first, second]
    )

    assert recorder.run_once().startswith("processing started:")
    owner = runner.active_session_id
    assert owner in {first.session_id, second.session_id}
    assert recorder.run_once() == f"processing: {owner}"
    assert runner.starts == [owner]


def test_restart_relaunches_persisted_processing_once(tmp_path, monkeypatch):
    now = datetime(2026, 7, 19, 15, tzinfo=UTC)
    session = replace(SESSION, starts_at=now - timedelta(hours=3))
    first = Recorder(_config(tmp_path), now=lambda: now)
    first.store.transition(session.session_id, Phase.RECORDING, now)
    first.store.transition(session.session_id, Phase.CAPTURED, now)
    first.store.transition(session.session_id, Phase.PROCESSING, now)
    runner = FakePreparationRunner()
    restarted = Recorder(_config(tmp_path), now=lambda: now, preparation_runner=runner)
    monkeypatch.setattr(
        "racelens.recorder.worker.load_fastf1_schedule", lambda _year: [session]
    )

    assert restarted.run_once() == f"processing started: {session.session_id}"
    assert restarted.run_once() == f"processing: {session.session_id}"
    assert runner.starts == [session.session_id]


def test_preparation_failure_does_not_replace_another_session_live_pointer(
    tmp_path, monkeypatch,
):
    now = datetime(2026, 7, 19, 15, tzinfo=UTC)
    session = replace(SESSION, starts_at=now - timedelta(hours=3))
    runner = FakePreparationRunner()
    store = MemoryStore()
    pointer = {"canonical_session_id": "2026-99-r", "status": "live"}
    store.objects["live/current.json"] = pointer.copy()
    recorder = Recorder(
        _config(tmp_path), now=lambda: now, object_store=store,
        preparation_runner=runner,
    )
    recorder.store.transition(session.session_id, Phase.RECORDING, now)
    recorder.store.transition(session.session_id, Phase.CAPTURED, now)
    monkeypatch.setattr(
        "racelens.recorder.worker.load_fastf1_schedule", lambda _year: [session]
    )

    recorder.run_once()
    runner.outcome = PreparationOutcome(session.session_id, "archive unavailable")
    recorder.run_once()

    assert store.objects["live/current.json"] == pointer


def test_run_forever_closes_preparation_runner_when_interrupted(tmp_path, monkeypatch):
    runner = FakePreparationRunner()
    recorder = Recorder(
        _config(tmp_path),
        preparation_runner=runner,
        sleep=lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(recorder, "run_once", lambda: "idle")

    with pytest.raises(KeyboardInterrupt):
        recorder.run_forever()

    assert runner.closed


def test_processing_retry_keeps_live_finishing_and_never_recaptures(tmp_path, monkeypatch):
    clock = [datetime(2026, 7, 19, 12, 55, tzinfo=UTC)]
    session = ScheduledSession(2026, 13, "Belgian Grand Prix", "R", clock[0])
    runner = FakePreparationRunner()
    recorder = Recorder(
        _config(tmp_path), now=lambda: clock[0], preparation_runner=runner,
    )
    monkeypatch.setattr(
        "racelens.recorder.worker.load_fastf1_schedule", lambda year: [session]
    )
    captures = []
    live_statuses = []
    monkeypatch.setattr(recorder, "capture", lambda item: captures.append(item.session_id))
    monkeypatch.setattr(
        recorder,
        "_set_live_status",
        lambda _session, status, **_kwargs: live_statuses.append(status),
    )

    assert recorder.run_once().startswith("captured:")
    assert recorder.run_once().startswith("processing started:")
    runner.outcome = PreparationOutcome(session.session_id, "archive not ready")
    assert recorder.run_once().startswith("processing failed:")
    failed = recorder.store.load().sessions[session.session_id]
    assert failed.phase is Phase.FAILED
    assert failed.retry_phase is Phase.PROCESSING

    clock[0] += timedelta(minutes=16)
    assert recorder.run_once().startswith("processing started:")
    runner.outcome = PreparationOutcome(session.session_id, None)
    assert recorder.run_once().startswith("complete:")
    assert captures == [session.session_id]
    assert runner.starts == [session.session_id, session.session_id]
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


def test_due_processing_retry_precedes_approaching_capture(tmp_path, monkeypatch):
    now = datetime(2026, 7, 19, 12, 55, tzinfo=UTC)
    retry_session = ScheduledSession(
        2026, 12, "Dutch Grand Prix", "FP1", now - timedelta(hours=3),
    )
    later = ScheduledSession(
        2026, 13, "Belgian Grand Prix", "FP1", now + timedelta(hours=1, minutes=10),
    )
    runner = FakePreparationRunner()
    recorder = Recorder(_config(tmp_path), now=lambda: now, preparation_runner=runner)
    recorder.store.transition(retry_session.session_id, Phase.RECORDING, now)
    recorder.store.transition(retry_session.session_id, Phase.CAPTURED, now)
    recorder.store.transition(retry_session.session_id, Phase.PROCESSING, now)
    recorder.store.transition(
        retry_session.session_id,
        Phase.FAILED,
        now,
        error="archive not ready",
        retry_at=now,
    )
    monkeypatch.setattr(
        "racelens.recorder.worker.load_fastf1_schedule",
        lambda year: [retry_session, later],
    )
    assert recorder.run_once() == f"processing started: {retry_session.session_id}"
    assert runner.starts == [retry_session.session_id]
    assert recorder.store.load().sessions[retry_session.session_id].phase is Phase.PROCESSING


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
    runner = FakePreparationRunner()
    recorder = Recorder(_config(tmp_path), now=lambda: now, preparation_runner=runner)
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
    assert recorder.run_once().startswith("processing started:")
    assert protected.exists()
    assert not orphan.exists()


def test_restart_resumes_persisted_processing_phase(tmp_path, monkeypatch):
    now = datetime(2026, 7, 19, 15, tzinfo=UTC)
    session = ScheduledSession(2026, 13, "Belgian Grand Prix", "R", now)
    first = Recorder(_config(tmp_path), now=lambda: now)
    first.store.transition(session.session_id, Phase.RECORDING, now)
    first.store.transition(session.session_id, Phase.CAPTURED, now)
    first.store.transition(session.session_id, Phase.PROCESSING, now)

    runner = FakePreparationRunner()
    restarted = Recorder(
        _config(tmp_path), now=lambda: now, preparation_runner=runner,
    )
    monkeypatch.setattr(
        "racelens.recorder.worker.load_fastf1_schedule", lambda year: [session]
    )
    assert restarted.run_once().startswith("processing started:")
    assert runner.starts == [session.session_id]


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
    runner = FakePreparationRunner()
    recorder = Recorder(_config(tmp_path), now=lambda: now, preparation_runner=runner)
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
    assert recorder.run_once().startswith(f"capture failed: {SESSION.session_id}:")
    assert recorder.store.load().sessions[SESSION.session_id].retry_at > now
    assert recorder.run_once() == f"processing started: {older.session_id}"
    assert runner.starts == [older.session_id]
    assert recorder.store.load().sessions[older.session_id].phase is Phase.PROCESSING
