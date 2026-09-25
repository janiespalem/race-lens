"""Child-process entry point for one replay preparation job."""
from __future__ import annotations

import sys
from collections.abc import Sequence
from datetime import datetime

from racelens.recorder.schedule import ScheduledSession


def parse_session(argv: Sequence[str]) -> ScheduledSession:
    if len(argv) != 5:
        raise ValueError("preparation job requires five session arguments")
    year, round_number, event_name, kind, starts_at = argv
    start = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
    if start.tzinfo is None:
        raise ValueError("session timestamp must include a timezone")
    return ScheduledSession(int(year), int(round_number), event_name, kind, start)


def main(argv: Sequence[str] | None = None) -> None:
    from racelens.recorder.worker import Config, Recorder

    session = parse_session(sys.argv[1:] if argv is None else argv)
    Recorder(Config.from_env(), owns_coordinator_heartbeat=False).process(session)


if __name__ == "__main__":
    main()
