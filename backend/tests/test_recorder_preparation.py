import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from racelens.recorder.preparation import PreparationOutcome, SubprocessPreparationRunner
from racelens.recorder import preparation_job
from racelens.recorder.preparation_job import parse_session
from racelens.recorder.schedule import ScheduledSession
from racelens.recorder.worker import Config, Recorder


SESSION = ScheduledSession(
    2026, 15, "Azerbaijan Grand Prix", "Q", datetime(2026, 9, 25, 12, tzinfo=UTC)
)
OTHER_SESSION = ScheduledSession(
    2026, 15, "Azerbaijan Grand Prix", "R", datetime(2026, 9, 26, 11, tzinfo=UTC)
)


class FakeProcess:
    def __init__(self, pid):
        self.pid = pid
        self.returncode = None
        self.wait_calls = []
        self.time_out_once = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.time_out_once:
            self.time_out_once = False
            raise subprocess.TimeoutExpired("prepare", timeout)
        if self.returncode is None:
            self.returncode = -signal.SIGTERM
        return self.returncode


def make_runner(tmp_path):
    processes = []
    calls = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        process = FakeProcess(10_000 + len(processes))
        processes.append(process)
        return process

    return SubprocessPreparationRunner(tmp_path, popen=popen), processes, calls


def test_runner_starts_expected_command_and_records_owner(tmp_path):
    runner, processes, calls = make_runner(tmp_path)

    assert runner.start(SESSION)

    assert len(processes) == 1
    assert calls == [([
        sys.executable,
        "-m",
        "racelens.recorder.preparation_job",
        "2026",
        "15",
        "Azerbaijan Grand Prix",
        "Q",
        "2026-09-25T12:00:00+00:00",
    ], {"start_new_session": True})]
    marker = json.loads((tmp_path / "preparation-active.json").read_text())
    assert marker["session_id"] == SESSION.session_id
    assert datetime.fromisoformat(marker["started_at"]).tzinfo is not None


def test_runner_is_single_flight(tmp_path):
    runner, processes, _calls = make_runner(tmp_path)

    assert runner.start(SESSION)
    assert not runner.start(OTHER_SESSION)

    assert len(processes) == 1
    assert runner.active_session_id == SESSION.session_id


@pytest.mark.parametrize(
    ("returncode", "error"),
    [(0, None), (7, "preparation exited with status 7")],
)
def test_poll_reports_one_terminal_outcome_and_clears_marker(
    tmp_path, returncode, error,
):
    runner, processes, _calls = make_runner(tmp_path)
    runner.start(SESSION)
    processes[0].returncode = returncode

    assert runner.poll() == PreparationOutcome(SESSION.session_id, error)
    assert runner.poll() is None
    assert runner.active_session_id is None
    assert not (tmp_path / "preparation-active.json").exists()
    assert processes[0].wait_calls == [0]


def test_stop_terminates_process_group_and_reports_preemption(tmp_path, monkeypatch):
    runner, processes, _calls = make_runner(tmp_path)
    runner.start(SESSION)
    killed = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    outcome = runner.stop("preempted by scheduled capture")

    assert outcome == PreparationOutcome(
        SESSION.session_id, "preempted by scheduled capture"
    )
    assert killed == [
        (processes[0].pid, signal.SIGTERM),
        (processes[0].pid, 0),
        (processes[0].pid, signal.SIGKILL),
    ]
    assert runner.active_session_id is None
    assert not (tmp_path / "preparation-active.json").exists()


def test_stop_kills_process_group_after_termination_timeout(tmp_path, monkeypatch):
    runner, processes, _calls = make_runner(tmp_path)
    runner.start(SESSION)
    processes[0].time_out_once = True
    killed = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    runner.stop("preempted")

    assert killed == [
        (processes[0].pid, signal.SIGTERM),
        (processes[0].pid, signal.SIGKILL),
    ]
    assert processes[0].wait_calls == [30, 10]


def _pid_is_running(pid: int) -> bool:
    stat = Path(f"/proc/{pid}/stat")
    if not stat.exists():
        return False
    return stat.read_text(encoding="utf-8").split()[2] != "Z"


def _wait_for_file(path: Path, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {path}")
        time.sleep(0.01)


def _spawn_leader_with_stubborn_child(pid_file: Path, *, leader_exits: bool):
    child = (
        "import os,signal,time,pathlib;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()));"
        "time.sleep(60)"
    )
    leader_tail = "" if leader_exits else ";time.sleep(60)"
    leader = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{child!r}])"
        f"{leader_tail}"
    )
    return subprocess.Popen([sys.executable, "-c", leader], start_new_session=True)


def _kill_group_for_test(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def test_stop_kills_stubborn_descendant_after_leader_exits(tmp_path):
    pid_file = tmp_path / "child.pid"
    process = _spawn_leader_with_stubborn_child(pid_file, leader_exits=False)
    _wait_for_file(pid_file)
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    try:
        SubprocessPreparationRunner._terminate_process(process)
        deadline = time.monotonic() + 2
        while _pid_is_running(child_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not _pid_is_running(child_pid)
    finally:
        _kill_group_for_test(process)


def test_poll_cleans_descendants_after_unexpected_leader_exit(tmp_path):
    pid_file = tmp_path / "child.pid"
    processes = []

    def popen(_argv, **_kwargs):
        process = _spawn_leader_with_stubborn_child(pid_file, leader_exits=True)
        processes.append(process)
        return process

    runner = SubprocessPreparationRunner(tmp_path, popen=popen)
    assert runner.start(SESSION)
    _wait_for_file(pid_file)
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    processes[0].wait(timeout=5)
    try:
        assert runner.poll() == PreparationOutcome(SESSION.session_id, None)
        deadline = time.monotonic() + 2
        while _pid_is_running(child_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not _pid_is_running(child_pid)
    finally:
        _kill_group_for_test(processes[0])


def test_constructor_removes_stale_marker(tmp_path):
    marker = tmp_path / "preparation-active.json"
    marker.write_text('{"session_id":"2026-15-q"}\n', encoding="utf-8")

    runner, _processes, _calls = make_runner(tmp_path)

    assert runner.active_session_id is None
    assert not marker.exists()


def test_popen_failure_keeps_runner_idle_and_writes_no_marker(tmp_path):
    def fail(_argv, **_kwargs):
        raise OSError("process table full")

    runner = SubprocessPreparationRunner(tmp_path, popen=fail)

    with pytest.raises(OSError, match="process table full"):
        runner.start(SESSION)
    assert runner.active_session_id is None
    assert not (tmp_path / "preparation-active.json").exists()


def test_parse_session_rejects_naive_timestamp():
    with pytest.raises(ValueError, match="timezone"):
        parse_session(["2026", "15", "Azerbaijan Grand Prix", "Q", "2026-09-25T12:00:00"])


def test_parse_session_reconstructs_scheduled_session():
    assert parse_session([
        "2026",
        "15",
        "Azerbaijan Grand Prix",
        "Q",
        "2026-09-25T14:00:00+02:00",
    ]) == SESSION


def test_preparation_recorder_does_not_refresh_coordinator_heartbeat(tmp_path):
    config = Config(
        state_dir=tmp_path / "state",
        raw_dir=tmp_path / "raw",
        data_dir=tmp_path / "data",
        interval_seconds=120,
        capture_poll_seconds=5,
        raw_retention_days=14,
        publish_sessions=frozenset({"R"}),
        transcribe_radio=False,
        race_core=Path("race-core"),
    )
    recorder = Recorder(config, owns_coordinator_heartbeat=False)
    recorder.heartbeat.write_text("coordinator\n", encoding="utf-8")
    old_ns = 1_700_000_000_000_000_000
    os.utime(recorder.heartbeat, ns=(old_ns, old_ns))

    recorder._run([sys.executable, "-c", "pass"])

    assert recorder.heartbeat.stat().st_mtime_ns == old_ns


def test_preparation_job_disables_coordinator_heartbeat(monkeypatch):
    calls = []

    class FakeConfig:
        @staticmethod
        def from_env():
            return "config"

    class FakeRecorder:
        def __init__(self, config, **kwargs):
            calls.append((config, kwargs))

        def process(self, session):
            calls.append(session)

    monkeypatch.setattr("racelens.recorder.worker.Config", FakeConfig)
    monkeypatch.setattr("racelens.recorder.worker.Recorder", FakeRecorder)

    preparation_job.main([
        "2026", "15", "Azerbaijan Grand Prix", "Q", "2026-09-25T12:00:00+00:00",
    ])

    assert calls == [
        ("config", {"owns_coordinator_heartbeat": False}),
        SESSION,
    ]
