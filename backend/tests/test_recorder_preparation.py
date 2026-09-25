import json
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime

import pytest

from racelens.recorder.preparation import PreparationOutcome, SubprocessPreparationRunner
from racelens.recorder.preparation_job import parse_session
from racelens.recorder.schedule import ScheduledSession


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
    assert killed == [(processes[0].pid, signal.SIGTERM)]
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
