"""Single-flight subprocess ownership for replay preparation."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from racelens.recorder.schedule import ScheduledSession


@dataclass(frozen=True, slots=True)
class PreparationOutcome:
    session_id: str
    error: str | None


class PreparationRunner(Protocol):
    @property
    def active_session_id(self) -> str | None: ...

    def start(self, session: ScheduledSession) -> bool: ...

    def poll(self) -> PreparationOutcome | None: ...

    def stop(self, reason: str) -> PreparationOutcome | None: ...

    def close(self) -> None: ...


class SubprocessPreparationRunner:
    def __init__(
        self,
        state_dir: Path,
        *,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        self._state_dir = Path(state_dir)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._marker = self._state_dir / "preparation-active.json"
        self._marker.unlink(missing_ok=True)
        self._popen = popen
        self._process: subprocess.Popen | None = None
        self._session_id: str | None = None

    @property
    def active_session_id(self) -> str | None:
        return self._session_id

    def _write_marker(self, session_id: str) -> None:
        value = {
            "session_id": session_id,
            "started_at": datetime.now(UTC).isoformat(),
        }
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self._state_dir,
                prefix=".preparation-active.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = handle.name
                json.dump(value, handle, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._marker)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    @staticmethod
    def _command(session: ScheduledSession) -> list[str]:
        return [
            sys.executable,
            "-m",
            "racelens.recorder.preparation_job",
            str(session.year),
            str(session.round_number),
            session.event_name,
            session.kind,
            session.starts_at.isoformat(),
        ]

    def start(self, session: ScheduledSession) -> bool:
        if self._process is not None:
            return False
        process = self._popen(self._command(session), start_new_session=True)
        self._process = process
        self._session_id = session.session_id
        try:
            self._write_marker(session.session_id)
        except Exception:
            self._terminate_process(process)
            self._clear()
            raise
        return True

    def _clear(self) -> None:
        self._marker.unlink(missing_ok=True)
        self._process = None
        self._session_id = None

    def _finish(self, error: str | None) -> PreparationOutcome:
        if self._session_id is None:
            raise RuntimeError("preparation runner lost session ownership")
        outcome = PreparationOutcome(self._session_id, error)
        self._clear()
        return outcome

    @staticmethod
    def _terminate_process(process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            process.wait(timeout=0)
            return
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)
            return
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def poll(self) -> PreparationOutcome | None:
        if self._process is None:
            return None
        status = self._process.poll()
        if status is None:
            return None
        self._terminate_process(self._process)
        error = None if status == 0 else f"preparation exited with status {status}"
        return self._finish(error)

    def stop(self, reason: str) -> PreparationOutcome | None:
        if self._process is None:
            return None
        status = self._process.poll()
        if status is not None:
            self._terminate_process(self._process)
            error = None if status == 0 else f"preparation exited with status {status}"
            return self._finish(error)
        self._terminate_process(self._process)
        return self._finish(reason)

    def close(self) -> None:
        self.stop("coordinator stopped")
