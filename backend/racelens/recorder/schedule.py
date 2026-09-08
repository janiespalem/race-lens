"""Pure schedule parsing and due-session selection for the recorder."""
from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Collection, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

STANDARD_START_EARLY = timedelta(minutes=10)
RACE_START_EARLY = timedelta(hours=1)
SCHEDULE_TIMEOUT_SECONDS = 30
HARD_DURATION = {
    "FP1": timedelta(hours=2),
    "FP2": timedelta(hours=2),
    "FP3": timedelta(hours=2),
    "SQ": timedelta(hours=2),
    "Sprint": timedelta(hours=2),
    "Q": timedelta(hours=2),
    "R": timedelta(hours=4),
}

_ALIASES = {
    "practice 1": "FP1",
    "practice 2": "FP2",
    "practice 3": "FP3",
    "sprint shootout": "SQ",
    "sprint qualifying": "SQ",
    "sprint": "Sprint",
    "qualifying": "Q",
    "race": "R",
    "fp1": "FP1",
    "fp2": "FP2",
    "fp3": "FP3",
    "sq": "SQ",
    "q": "Q",
    "r": "R",
}


@dataclass(frozen=True, slots=True)
class ScheduledSession:
    year: int
    round_number: int
    event_name: str
    kind: str
    starts_at: datetime

    def __post_init__(self) -> None:
        if self.kind not in HARD_DURATION:
            raise ValueError(f"unsupported session kind: {self.kind}")
        if self.starts_at.tzinfo is None:
            raise ValueError("starts_at must be timezone-aware")
        object.__setattr__(self, "starts_at", self.starts_at.astimezone(UTC))

    @property
    def session_id(self) -> str:
        return f"{self.year}-{self.round_number:02d}-{self.kind.lower()}"

    @property
    def capture_from(self) -> datetime:
        return self.starts_at - (
            RACE_START_EARLY if self.kind == "R" else STANDARD_START_EARLY
        )

    @property
    def capture_until(self) -> datetime:
        return self.starts_at + HARD_DURATION[self.kind]


def canonical_alias(name: object) -> str | None:
    """Return the recorder's stable alias for a FastF1 session name."""
    if not isinstance(name, str):
        return None
    return _ALIASES.get(" ".join(name.strip().lower().split()))


def _as_utc(value: object) -> datetime | None:
    if value is None or str(value) == "NaT":
        return None
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()  # pandas Timestamp boundary
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    # FastF1's *DateUtc columns are UTC but pandas exposes them as naive.
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _records(schedule: Any) -> Iterable[Mapping[str, object]]:
    if hasattr(schedule, "to_dict"):
        return schedule.to_dict("records")
    return schedule


def parse_fastf1_schedule(schedule: Any) -> list[ScheduledSession]:
    """Parse FastF1 EventSchedule rows without importing pandas or FastF1."""
    sessions: list[ScheduledSession] = []
    for row in _records(schedule):
        try:
            year = int(row["EventDate"].year) if "EventDate" in row else int(row["Year"])
            round_number = int(row["RoundNumber"])
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if round_number <= 0:
            continue
        event_name = str(row.get("EventName") or row.get("OfficialEventName") or "Round")
        for index in range(1, 6):
            kind = canonical_alias(row.get(f"Session{index}"))
            starts_at = _as_utc(row.get(f"Session{index}DateUtc"))
            if kind is not None and starts_at is not None:
                sessions.append(
                    ScheduledSession(year, round_number, event_name, kind, starts_at)
                )
    return sorted(sessions, key=lambda item: (item.starts_at, item.session_id))


def load_fastf1_schedule(year: int) -> list[ScheduledSession]:
    """Bound the entire optional FastF1 fetch, including its upstream fallbacks."""
    result = subprocess.run(
        [sys.executable, "-m", "racelens.recorder.schedule", str(int(year))],
        capture_output=True, text=True, check=True, timeout=SCHEDULE_TIMEOUT_SECONDS,
    )
    return [
        ScheduledSession(**{**row, "starts_at": datetime.fromisoformat(row["starts_at"])})
        for row in json.loads(result.stdout)
    ]


def _fetch_fastf1_schedule(year: int) -> list[ScheduledSession]:
    """Run only in the bounded child; FastF1 HTTP calls can otherwise wait forever."""
    import fastf1

    return parse_fastf1_schedule(fastf1.get_event_schedule(year, include_testing=False))


def select_due_session(
    sessions: Iterable[ScheduledSession],
    now: datetime,
    unavailable: Collection[str] = (),
) -> ScheduledSession | None:
    """Return at most one active capture window, oldest scheduled session first."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = now.astimezone(UTC)
    due = (
        session
        for session in sessions
        if session.session_id not in unavailable
        and session.capture_from <= current < session.capture_until
    )
    return min(due, key=lambda item: (item.starts_at, item.session_id), default=None)


if __name__ == "__main__":
    print(json.dumps([
        {**asdict(item), "starts_at": item.starts_at.isoformat()}
        for item in _fetch_fastf1_schedule(int(sys.argv[1]))
    ]))
