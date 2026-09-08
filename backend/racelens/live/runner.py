"""LiveRunner — polling loop that feeds the replay engine.

Architectural invariant (by design):
    The engine is NEVER mutated between polls.  Each poll rebuilds it from
    scratch using the latest full snapshot. init(5 k events) ≈ 3 ms → negligible.
    This gives us deduplication and determinism for free.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from racelens.events.models import Event
from racelens.replay.engine import ReplayEngine

_ACTIVE_SESSION_STATUSES = {"started", "safety_car", "vsc"}


class LiveRunner:
    """Polls *fetch_events* on a fixed interval and keeps a live ReplayEngine.

    Parameters
    ----------
    fetch_events:
        Callable ``() -> list[Event]`` that returns the **full session snapshot**
        seen so far.  In production this is
        ``lambda: ingest_openf1(session_key)``; in tests a fake.
    poll_interval_s:
        Seconds between polls (default 2.0).
        OpenF1 publishes data with ~3 s source-side delay — that is the floor;
        our poll merely avoids adding extra latency on top.  Do not go below
        1.5 s — polling faster than the source refresh cadence is pointless.
    """

    # Auto-stop: once a fetched snapshot shows the session finished, keep
    # polling (post-race messages/classification still trickle in) for this
    # long, then stop itself — see _finish_seen_at / _loop.
    AUTO_STOP_DELAY_S: float = 300.0

    def __init__(
        self,
        fetch_events: Callable[[], list[Event]],
        poll_interval_s: float = 2.0,
    ) -> None:
        self._fetch = fetch_events
        self._interval = poll_interval_s

        # Latest full snapshot: event_id → Event
        self._all: dict[str, Event] = {}
        self._next_seq: int = 0        # monotonic ingest_seq counter across polls

        # Stats
        self._polls: int = 0
        self._new_last_poll: int = 0
        self._consecutive_failures: int = 0
        self._last_poll_unix: float | None = None
        self._last_new_event_unix: float | None = None
        self._last_error: str | None = None

        # Wall-clock time.time() when a "finished" SessionStatusChanged was
        # first observed in a fetched snapshot. None until then. Drives the
        # auto-stop countdown in _loop.
        self._finish_seen_at: float | None = None
        # True once the loop has stopped ITSELF via that countdown — distinct
        # from is_running=False, which can also mean "not started yet" or
        # "task cancelled/finished for some other reason" (e.g. an explicit
        # .stop() call). api.py uses this flag (not is_running) to decide
        # whether to also reap the now-orphaned capture subprocess.
        self._auto_stopped: bool = False

        # Engine (None until first successful poll)
        self.engine: ReplayEngine | None = None

        # Asyncio task handle
        self._task: asyncio.Task | None = None  # type: ignore[type-arg]

    # ── Public interface ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Launch the background polling loop."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        """Cancel the background polling loop."""
        if self._task and not self._task.done():
            self._task.cancel()

    @property
    def is_running(self) -> bool:
        """True if the background polling task exists and has not finished."""
        return self._task is not None and not self._task.done()

    @property
    def auto_stopped(self) -> bool:
        """True once the loop has stopped itself (finished + AUTO_STOP_DELAY_S)."""
        return self._auto_stopped

    @property
    def polls(self) -> int:
        return self._polls

    def status(self) -> dict[str, Any]:
        engine = self.engine
        stale_after = max(30.0, self._interval * 3)
        no_new_for = (
            time.time() - self._last_new_event_unix
            if self._last_new_event_unix is not None else None
        )
        session_running = False
        if engine is not None and engine.events:
            for event in reversed(engine.events):
                if event.type == "SessionStatusChanged":
                    session_running = event.payload.get("status") in _ACTIVE_SESSION_STATUSES
                    break
                if event.type == "SessionStarted":
                    session_running = True
                    break
        if self._consecutive_failures >= 5 or (self._polls >= 3 and engine is None) or (
            session_running and no_new_for is not None and no_new_for > stale_after
        ):
            dq = "stalled"
        elif self._consecutive_failures >= 2:
            dq = "degraded"
        else:
            dq = "good"
        return {
            "polls": self._polls,
            "events_total": len(self._all),
            "new_last_poll": self._new_last_poll,
            "consecutive_failures": self._consecutive_failures,
            "last_poll_unix": self._last_poll_unix,
            "last_new_event_unix": self._last_new_event_unix,
            "last_error": self._last_error,
            "data_quality": dq,
        }

    def state_now(self) -> dict[str, Any]:
        """Return engine state at the latest known session time."""
        engine = self.engine
        if engine is None or not engine.events:
            return {"error": "no data yet", "status": self.status()}
        at_ms = engine.events[-1].session_time_ms
        state = engine.state_at(at_ms)
        state["live_status"] = self.status()
        return state

    # ── Core poll logic (sync, extracted for testability) ────────────────────

    def _poll_once(self) -> None:
        """Execute one poll cycle synchronously.  Errors increment failure counter."""
        try:
            events = self._fetch()
        except Exception as exc:
            self._consecutive_failures += 1
            self._polls += 1
            self._last_poll_unix = time.time()
            self._new_last_poll = 0
            self._last_error = type(exc).__name__
            return

        snapshot = {e.event_id: e for e in events}
        new_count = 0
        for e in snapshot.values():
            previous = self._all.get(e.event_id)
            if previous is None:
                e.ingest_seq = self._next_seq
                self._next_seq += 1
                new_count += 1
            else:
                e.ingest_seq = previous.ingest_seq

        changed = snapshot != self._all
        self._all = snapshot
        if changed:
            self.engine = ReplayEngine(snapshot.values()) if snapshot else None
            self._last_new_event_unix = time.time()

        # First sighting of a "finished" status starts the auto-stop countdown
        # (checked once per poll in _loop, no extra thread/timer needed).
        finished = any(
            e.type == "SessionStatusChanged" and e.payload.get("status") == "finished"
            for e in events
        )
        if not finished:
            self._finish_seen_at = None
        elif self._finish_seen_at is None:
            self._finish_seen_at = time.time()

        self._consecutive_failures = 0
        self._last_error = None
        self._new_last_poll = new_count
        self._polls += 1
        self._last_poll_unix = time.time()

    # ── Async loop ────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        while True:
            await asyncio.to_thread(self._poll_once)
            if (
                self._finish_seen_at is not None
                and time.time() - self._finish_seen_at >= self.AUTO_STOP_DELAY_S
            ):
                self._auto_stopped = True
                self.stop()
                return
            await asyncio.sleep(self._interval)
