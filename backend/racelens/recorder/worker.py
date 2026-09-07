"""Unattended recorder: schedule -> SignalR -> archive -> publish staging."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from racelens.adapters.f1live_adapter import ingest_f1live
from racelens.commentary.feed import render_feed
from racelens.commentary.renderer import render_all
from racelens.insights.battles import detect_battles
from racelens.insights.passes import Pass, detect_passes
from racelens.insights.registry import detect_all
from racelens.object_storage import (
    LIVE_SNAPSHOT_TTL_S,
    MAX_LIVE_SNAPSHOT_BYTES,
    MAX_RECORD_BYTES,
    LiveRecordError,
    ObjectPreparationQueue,
    S3Store,
    StorageConfig,
    StorageError,
    live_snapshot_key,
    publish_session,
    write_live_snapshot,
    write_live_status,
)
from racelens.recorder.feed import inspect_feed, isolate_session
from racelens.recorder.postprocess import merge_captured_radio, validate_archive, validate_fixture
from racelens.recorder.schedule import ScheduledSession, load_fastf1_schedule, select_due_session
from racelens.recorder.state import Phase, StateStore
from racelens.replay.engine import ReplayEngine
from racelens.tyre_stints import stint_timeline

FINISH_GRACE = timedelta(minutes=10)
CAPTURE_RETRY = timedelta(minutes=5)
ARCHIVE_RETRY = timedelta(minutes=15)
AWARD_SYNC_INTERVAL = timedelta(hours=6)
PROCESS_TIMEOUT = 60 * 60
SCHEDULE_REFRESH = timedelta(hours=6)
REMOTE_CAPTURE_GUARD = timedelta(hours=2)
SESSION_LABEL = {"R": "race", "Q": "qualifying", "SQ": "sprint_qualifying"}
LIVE_SESSION_KINDS = frozenset({"R", "Sprint"})

logger = logging.getLogger(__name__)


def _positive_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _slug(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def fixture_stem(session: ScheduledSession) -> str:
    venue = re.sub(r"\s+grand prix$", "", session.event_name, flags=re.IGNORECASE)
    label = SESSION_LABEL.get(session.kind, session.kind.lower())
    return f"{_slug(venue)}_{session.year}_{label}"


@dataclass(frozen=True, slots=True)
class Config:
    state_dir: Path
    raw_dir: Path
    data_dir: Path
    interval_seconds: int
    capture_poll_seconds: int
    raw_retention_days: int
    publish_sessions: frozenset[str]
    transcribe_radio: bool
    race_core: Path
    git_publication: bool = True

    @classmethod
    def from_env(cls) -> "Config":
        base = Path(os.environ.get("RACELENS_RECORDER_DATA", "/var/lib/race-lens-recorder"))
        publish = {
            item.strip()
            for item in os.environ.get(
                "RECORDER_PUBLISH_SESSIONS", "FP1,FP2,FP3,Q,SQ,Sprint,R"
            ).split(",")
            if item.strip()
        }
        valid = {"FP1", "FP2", "FP3", "SQ", "Sprint", "Q", "R"}
        if not publish <= valid:
            raise ValueError("RECORDER_PUBLISH_SESSIONS contains an unknown session")
        transcribe = os.environ.get("RECORDER_TRANSCRIBE_RADIO", "1").lower() in {
            "1", "true", "yes",
        }
        git_publication = os.environ.get("RECORDER_GIT_PUBLICATION", "1").lower() in {
            "1", "true", "yes",
        }
        return cls(
            state_dir=base / "state",
            raw_dir=base / "raw",
            data_dir=base / "data",
            interval_seconds=_positive_int("RECORDER_INTERVAL_SEC", 120, 30),
            capture_poll_seconds=_positive_int("RECORDER_CAPTURE_POLL_SEC", 5),
            raw_retention_days=_positive_int("RECORDER_RAW_RETENTION_DAYS", 14),
            publish_sessions=frozenset(publish),
            transcribe_radio=transcribe,
            race_core=Path(os.environ.get("RACELENS_RACE_CORE", "/usr/local/bin/race-core")),
            git_publication=git_publication,
        )


class Recorder:
    def __init__(
        self,
        config: Config,
        *,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        object_store: object | None = None,
    ) -> None:
        self.config = config
        self.now = now or (lambda: datetime.now(UTC))
        self.sleep = sleep
        self.store = StateStore(config.state_dir / "recorder.json")
        self.heartbeat = config.state_dir / "heartbeat"
        self.schedule_cache = config.state_dir / "schedule.json"
        self.schedule_failure = config.state_dir / "schedule-failure"
        self.remote_processing = config.state_dir / "remote-processing"
        self._schedule: list[ScheduledSession] = []
        self._schedule_loaded_at: datetime | None = None
        self._schedule_years: set[int] = set()
        if object_store is None:
            storage_config = StorageConfig.from_env()
            object_store = S3Store(storage_config) if storage_config is not None else None
        self.object_store = object_store
        self.remote_queue = (
            ObjectPreparationQueue(object_store) if object_store is not None else None
        )
        self._live_sequences: dict[str, int] = {}
        self._live_transport: dict[str, tuple[int, datetime]] = {}
        self._live_pass_pending: dict[str, dict[Pass, datetime]] = {}
        self._live_pass_confirmed: dict[str, set[Pass]] = {}
        self._transcripts = None
        self._award_sync_at: datetime | None = None
        for path in (config.state_dir, config.raw_dir, config.data_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.remote_processing.unlink(missing_ok=True)

    def _beat(self) -> None:
        self.heartbeat.touch()

    def _load_schedule_cache(self) -> list[ScheduledSession]:
        if self.schedule_cache.stat().st_size > 1024 * 1024:
            raise ValueError("schedule cache is too large")
        value = json.loads(self.schedule_cache.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError("invalid schedule cache")
        rows = value.get("sessions")
        if not isinstance(rows, list):
            raise ValueError("invalid schedule cache")
        return [
            ScheduledSession(
                int(row["year"]), int(row["round"]), str(row["event"]),
                str(row["kind"]), datetime.fromisoformat(str(row["starts_at"])),
            )
            for row in rows
        ]

    def _save_schedule_cache(self, sessions: list[ScheduledSession]) -> None:
        value = {
            "version": 1,
            "sessions": [
                {
                    "year": item.year,
                    "round": item.round_number,
                    "event": item.event_name,
                    "kind": item.kind,
                    "starts_at": item.starts_at.isoformat(),
                }
                for item in sessions
            ],
        }
        temporary = self.schedule_cache.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(value, separators=(",", ":")) + "\n", encoding="utf-8")
            os.replace(temporary, self.schedule_cache)
        finally:
            temporary.unlink(missing_ok=True)

    def _paths(self, session: ScheduledSession) -> dict[str, Path]:
        stem = fixture_stem(session)
        archive = self.config.data_dir / "archive"
        return {
            "raw": self.config.raw_dir / f"{session.session_id}.f1live",
            "clean": self.config.raw_dir / f"{session.session_id}.clean.f1live",
            "provisional": archive / f"{stem}.provisional.jsonl",
            "fixture": archive / f"{stem}.jsonl",
            "track": archive / f"{stem}.track.json",
            "positions_raw": self.config.raw_dir / f"{session.session_id}.positions.jsonl",
            "positions": archive / f"{stem}.positions.json",
            "publish": self.config.data_dir / "publish",
        }

    @staticmethod
    def _stop(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

    def _capture_freshness(self, session: ScheduledSession, raw: Path) -> dict:
        current = self.now().astimezone(UTC)
        stat = raw.stat()
        modified = datetime.fromtimestamp(stat.st_mtime, UTC)
        previous = self._live_transport.get(session.session_id)
        if previous is None:
            growth_at = min(modified, current)
        elif stat.st_size > previous[0]:
            growth_at = current
        else:
            growth_at = previous[1]
        self._live_transport[session.session_id] = (stat.st_size, growth_at)
        age = max(0.0, (current - growth_at).total_seconds())
        growing = age <= LIVE_SNAPSHOT_TTL_S
        return {
            "capture_freshness": {
                "raw_size": stat.st_size,
                "raw_updated_at": modified.isoformat().replace("+00:00", "Z"),
                "seconds_since_growth": age,
                "transport_growing": growing,
            },
            "data_quality": "good" if growing else "stalled",
        }

    def _live_track_replay_id(self, session: ScheduledSession) -> str | None:
        completed = self.store.load().sessions
        preference = {kind: index for index, kind in enumerate(("FP1", "FP2", "FP3", "SQ", "Sprint", "Q"))}
        candidates = [
            item for item in self._schedule
            if item.year == session.year
            and item.round_number == session.round_number
            and item.starts_at < session.starts_at
            and completed.get(item.session_id) is not None
            and completed[item.session_id].phase is Phase.COMPLETE
            and self._paths(item)["track"].is_file()
        ]
        if not candidates:
            return None
        return fixture_stem(min(candidates, key=lambda item: preference.get(item.kind, 99)))

    def _confirmed_live_passes(
        self,
        session: ScheduledSession,
        candidates: list[Pass],
        state: dict,
        current: datetime,
    ) -> list[Pass]:
        pending = self._live_pass_pending.setdefault(session.session_id, {})
        confirmed = self._live_pass_confirmed.setdefault(session.session_id, set())
        candidate_set = set(candidates)
        confirmed.intersection_update(candidate_set)
        drivers = state.get("drivers", {})
        stable = {
            item for item in candidates
            if drivers.get(item.ahead, {}).get("position") is not None
            and drivers.get(item.behind, {}).get("position") is not None
            and drivers[item.ahead]["position"] < drivers[item.behind]["position"]
        }
        for item in list(pending):
            if item not in stable:
                del pending[item]
        for item in stable:
            since = pending.setdefault(item, current)
            if current - since >= timedelta(seconds=5):
                confirmed.add(item)
        return [item for item in candidates if item in confirmed]

    def _publish_live_snapshot(self, session: ScheduledSession, raw: Path) -> bool:
        if self.object_store is None or session.kind not in LIVE_SESSION_KINDS:
            return False
        inspection = inspect_feed(raw, session)
        if not inspection.matched or inspection.segment_ended:
            return False
        replay_id = fixture_stem(session)
        current = self.now().astimezone(UTC)
        prior_snapshot = self.object_store.get_json(
            live_snapshot_key(session.session_id), limit=MAX_LIVE_SNAPSHOT_BYTES,
        )
        events = ingest_f1live(str(raw), session_id=replay_id)
        if not events:
            return False
        engine = ReplayEngine(events)
        at_ms = engine.events[-1].session_time_ms
        source_state = engine.state_at(at_ms)
        prior_state = (
            prior_snapshot.get("race_state") if isinstance(prior_snapshot, dict) else None
        )
        left_formation = (
            isinstance(prior_state, dict)
            and prior_state.get("session_status") == "formation"
            and source_state["session_status"] != "formation"
        )
        if (
            not left_formation
            and isinstance(prior_snapshot, dict)
            and prior_snapshot.get("canonical_session_id") == session.session_id
            and prior_snapshot.get("replay_session_id") == replay_id
            and isinstance(prior_snapshot.get("race_state"), dict)
            and isinstance(prior_snapshot["race_state"].get("at_ms"), int)
        ):
            generated = datetime.fromisoformat(
                str(prior_snapshot["generated_at"]).replace("Z", "+00:00")
            ).astimezone(UTC)
            at_ms = max(
                at_ms,
                prior_snapshot["race_state"]["at_ms"]
                + max(0, round((current - generated).total_seconds() * 1000)),
            )
        state = (
            source_state
            if at_ms == engine.events[-1].session_time_ms
            else engine.state_at(at_ms)
        )
        for driver in state["drivers"].values():
            driver.setdefault("x", None)
            driver.setdefault("y", None)
            driver.setdefault("progress", None)
        state["frame_source"] = "live"
        state["viewbox"] = None
        insights = detect_all(state)
        pass_candidates = [
            item
            for item in detect_passes(engine.events)
            if at_ms - 20_000 < item.at_ms <= at_ms
        ]
        confirmed_passes = self._confirmed_live_passes(
            session, pass_candidates, state, current,
        )
        passes = [
            {"ahead": item.ahead, "behind": item.behind, "kind": item.kind, "at_ms": item.at_ms}
            for item in confirmed_passes
        ]
        feeds = {
            language: render_feed(engine.events, at_ms, lang=language, limit=100)
            for language in ("en", "ru")
        }
        confirmed_ids = {
            f"pass:{item.kind}:{item.at_ms}:{item.ahead}:{item.behind}:{item.position}"
            for item in confirmed_passes
        }
        for language in feeds:
            feeds[language] = [
                item for item in feeds[language]
                if item["kind"] not in {"ON_TRACK", "UNDERCUT"}
                or item["id"] in confirmed_ids
            ]
        radio_text: dict[str, str | None] = {}
        if self.config.transcribe_radio:
            if self._transcripts is None:
                from racelens.radio.transcribe import TranscriptWorker

                self._transcripts = TranscriptWorker()
            for item in feeds["en"]:
                url = item.get("audio_url")
                if url:
                    radio_text[url] = item.get("transcript") or self._transcripts.get(url)
        for items in feeds.values():
            for item in items:
                url = item.get("audio_url")
                if url and radio_text.get(url):
                    item["transcript"] = radio_text[url]
        radio = [
            {
                "audio_url": item["audio_url"],
                "transcript": item.get("transcript"),
                "driver_id": item.get("driver_id"),
                "at_ms": item["at_ms"],
            }
            for item in feeds["en"]
            if item.get("audio_url")
        ][:20]
        stamp = current.isoformat().replace("+00:00", "Z")
        expires = (current + timedelta(seconds=LIVE_SNAPSHOT_TTL_S)).isoformat().replace(
            "+00:00", "Z"
        )
        prior_sequence = 0
        if (
            isinstance(prior_snapshot, dict)
            and prior_snapshot.get("canonical_session_id") == session.session_id
            and prior_snapshot.get("replay_session_id") == replay_id
            and isinstance(prior_snapshot.get("sequence"), int)
            and not isinstance(prior_snapshot.get("sequence"), bool)
        ):
            prior_sequence = prior_snapshot["sequence"]
        sequence = max(self._live_sequences.get(session.session_id, 0), prior_sequence) + 1
        quality = self._capture_freshness(session, raw)
        snapshot = {
            "schema_version": 1,
            "canonical_session_id": session.session_id,
            "replay_session_id": replay_id,
            "sequence": sequence,
            "generated_at": stamp,
            "expires_at": expires,
            "race_state": state,
            "battles": detect_battles(state)[:50],
            "active_insights": insights[:100],
            "recent_passes": passes[:100],
            "feed": feeds,
            "commentary": {
                language: {
                    level: render_all(insights, language, level)[:100]
                    for level in ("beginner", "pro")
                }
                for language in ("en", "ru")
            },
            "radio": radio,
            "stints": {
                "session_id": replay_id,
                "total_laps": state.get("total_laps") or state.get("lap") or 0,
                "stints": stint_timeline(
                    engine.events, state.get("total_laps") or state.get("lap") or 0,
                ),
            },
            **quality,
        }
        pointer = {
            "schema_version": 1,
            "canonical_session_id": session.session_id,
            "replay_session_id": replay_id,
            "status": "live",
            "snapshot_key": live_snapshot_key(session.session_id),
            "created_at": stamp,
            "updated_at": stamp,
            "failure": None,
            "track_replay_session_id": self._live_track_replay_id(session),
        }
        write_live_snapshot(self.object_store, pointer, snapshot, now=current)
        self._live_sequences[session.session_id] = sequence
        return True

    def _set_live_status(
        self,
        session: ScheduledSession,
        status: str,
        *,
        failure: str | None = None,
    ) -> None:
        if self.object_store is None or session.kind not in LIVE_SESSION_KINDS:
            return
        write_live_status(
            self.object_store,
            session.session_id,
            fixture_stem(session),
            status,
            failure=failure,
            now=self.now(),
        )

    def _finish_live(self, session: ScheduledSession) -> None:
        """Move a live pointer to ``finishing`` once capture ends.

        Idempotent: only acts when storage holds a pointer that still has
        ``status == "live"`` and matches this session. The final snapshot
        publish is best-effort, so finishing must not depend on it having
        succeeded.
        """
        if self.object_store is None or session.kind not in LIVE_SESSION_KINDS:
            return
        try:
            current = self.object_store.get_json(
                "live/current.json", limit=MAX_RECORD_BYTES,
            )
            if (
                not isinstance(current, dict)
                or current.get("canonical_session_id") != session.session_id
                or current.get("replay_session_id") != fixture_stem(session)
                or current.get("status") != "live"
            ):
                return
            self._set_live_status(session, "finishing")
        except (LiveRecordError, StorageError) as exc:
            logger.warning(
                "failed to finish live pointer for %s: %s",
                session.session_id, type(exc).__name__,
            )

    def capture(self, session: ScheduledSession) -> Path:
        paths = self._paths(session)
        raw, clean = paths["raw"], paths["clean"]
        prior = inspect_feed(raw, session)
        if prior.matched and (prior.finished or self.now() >= session.capture_until):
            try:
                self._publish_live_snapshot(session, raw)
            except (LiveRecordError, StorageError, ValueError) as exc:
                logger.warning(
                    "final live snapshot publish failed for %s: %s",
                    session.session_id, type(exc).__name__,
                )
            self._finish_live(session)
            isolate_session(raw, clean, session)
            return clean

        command = [
            sys.executable, "-m", "racelens.cli", "capture-live",
            "--no-auth", "--timeout", "0", "--append", "-o", str(raw),
        ]
        process = subprocess.Popen(command, start_new_session=True)
        finished_at: datetime | None = None
        inspection = prior
        last_live_snapshot_at: datetime | None = None
        try:
            while True:
                self._beat()
                now = self.now()
                inspection = inspect_feed(raw, session, inspection)
                if (
                    inspection.matched
                    and session.kind in LIVE_SESSION_KINDS
                    and self.object_store is not None
                    and (
                        last_live_snapshot_at is None
                        or now - last_live_snapshot_at >= timedelta(seconds=5)
                    )
                ):
                    try:
                        self._publish_live_snapshot(session, raw)
                    except (LiveRecordError, StorageError, ValueError) as exc:
                        logger.warning(
                            "live snapshot publish failed for %s: %s",
                            session.session_id, type(exc).__name__,
                        )
                    last_live_snapshot_at = now
                if inspection.finished and finished_at is None:
                    finished_at = now
                if finished_at is not None and now >= finished_at + FINISH_GRACE:
                    break
                if now >= session.capture_until:
                    break
                status = process.poll()
                if status is not None:
                    if finished_at is None:
                        raise RuntimeError(f"capture exited before session finish ({status})")
                    self.sleep(self.config.capture_poll_seconds)
                    process = subprocess.Popen(command, start_new_session=True)
                    continue
                self.sleep(self.config.capture_poll_seconds)
        finally:
            self._stop(process)

        inspection = inspect_feed(raw, session, inspection)
        if not inspection.matched:
            raise RuntimeError("live feed never matched the scheduled session")
        isolate_session(raw, clean, session)
        self._finish_live(session)
        return clean

    def _run(self, argv: list[str], *, env: dict[str, str] | None = None) -> None:
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        self._beat()
        process = subprocess.Popen(argv, env=merged_env)
        deadline = time.monotonic() + PROCESS_TIMEOUT
        try:
            while process.poll() is None:
                self._beat()
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(argv, PROCESS_TIMEOUT)
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    pass
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, argv)
        finally:
            self._stop(process)
            self._beat()

    def _stage(self, session: ScheduledSession, artifacts: list[Path]) -> None:
        destination = self._paths(session)["publish"]
        destination.mkdir(parents=True, exist_ok=True)
        names = []
        for source in artifacts:
            if source.parent != self.config.data_dir / "archive":
                raise ValueError("refusing to stage an artifact outside archive")
            target = destination / source.name
            temporary = target.with_suffix(target.suffix + ".tmp")
            shutil.copyfile(source, temporary)
            os.replace(temporary, target)
            names.append(source.name)
        manifest = destination / f"{fixture_stem(session)}.ready.json"
        temporary = manifest.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"session": session.session_id, "files": sorted(names)}) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, manifest)

    def _build_archive(
        self,
        session: ScheduledSession,
        *,
        captured: Path | None,
        full: bool,
    ) -> tuple[list[Path], int]:
        paths = self._paths(session)
        archive_dir = paths["fixture"].parent
        archive_dir.mkdir(parents=True, exist_ok=True)
        env = {
            "RACELENS_FIXTURES": str(archive_dir),
            "FASTF1_CACHE": str(self.config.data_dir / "fastf1_cache"),
        }
        self._run([
            sys.executable, "-m", "racelens.cli", "ingest", str(session.year),
            session.event_name, session.kind, "-o", str(paths["fixture"]),
        ], env=env)
        validate_fixture(paths["fixture"])
        if captured is not None:
            merge_captured_radio(paths["fixture"], captured)
        if captured is not None and self.config.transcribe_radio:
            self._run([
                sys.executable, "-m", "racelens.cli", "radio-transcribe",
                str(paths["fixture"]),
            ], env=env)
        event_count = validate_fixture(paths["fixture"])
        if not full:
            return [paths["fixture"]], event_count
        self._run([
            sys.executable, "-m", "racelens.cli", "track", str(session.year),
            session.event_name, session.kind, "-o", str(paths["track"]),
        ], env=env)
        self._run([
            sys.executable, "-m", "racelens.cli", "positions-raw", str(session.year),
            session.event_name, session.kind, "-o", str(paths["positions_raw"]),
        ], env=env)
        self._run([
            str(self.config.race_core), str(paths["positions_raw"]),
            str(paths["track"]), str(paths["positions"]), "1000",
        ], env=env)
        self._run([
            sys.executable, "-m", "racelens.cli", "track-progress", str(session.year),
            session.event_name, session.kind, fixture_stem(session),
        ], env=env)
        report = validate_archive(paths["fixture"], paths["track"], paths["positions"])
        paths["positions_raw"].unlink(missing_ok=True)
        return [paths["fixture"], paths["track"], paths["positions"]], report.events

    def _publish(
        self,
        session: ScheduledSession,
        artifacts: list[Path],
        event_count: int,
    ) -> None:
        if self.config.git_publication:
            self._stage(session, artifacts)
        if self.object_store is not None:
            replay_id = fixture_stem(session)
            publish_session(
                self.object_store,
                session.session_id,
                replay_id,
                *artifacts,
                event_count=event_count,
            )
            self.remote_queue.finish(
                session.session_id,
                replay_session_id=replay_id,
            )
            try:
                self._set_live_status(session, "replay_ready")
            except LiveRecordError as exc:
                if "pointer is missing" not in str(exc) and "identity differs" not in str(exc):
                    raise
        if session.kind == "R":
            try:
                from racelens.driver_of_day import sync_official_award

                sync_official_award(
                    session.year,
                    session.event_name,
                    fixture_stem(session),
                    self.config.data_dir / "archive",
                    self.object_store,
                )
            except Exception as exc:
                # Official results can legitimately lag archive publication.
                print(
                    f"official DOTD pending for {session.session_id}: {type(exc).__name__}",
                    file=sys.stderr,
                )

    def _sync_completed_awards(self, sessions: list[ScheduledSession]) -> None:
        current = self.now()
        if (
            self._award_sync_at is not None
            and current - self._award_sync_at < AWARD_SYNC_INTERVAL
        ):
            return
        self._award_sync_at = current
        try:
            from racelens.driver_of_day import sync_completed_official_awards

            for year in sorted({session.year for session in sessions}):
                sync_completed_official_awards(
                    year,
                    sessions,
                    self.config.data_dir / "archive",
                    self.object_store,
                    now=current,
                )
        except Exception as exc:
            print(f"official DOTD sync deferred: {type(exc).__name__}", file=sys.stderr)

    def process(self, session: ScheduledSession) -> None:
        if self._transcripts is not None:
            self._transcripts.close()
            self._transcripts = None
        paths = self._paths(session)
        clean = paths["clean"]
        if not clean.is_file():
            isolate_session(paths["raw"], clean, session)
        archive_dir = paths["fixture"].parent
        archive_dir.mkdir(parents=True, exist_ok=True)
        env = {
            "RACELENS_FIXTURES": str(archive_dir),
            "FASTF1_CACHE": str(self.config.data_dir / "fastf1_cache"),
        }
        self._run([
            sys.executable, "-m", "racelens.cli", "ingest-live", str(clean),
            "--year", str(session.year), "--gp", session.event_name,
            "--session", session.kind, "-o", str(paths["provisional"]),
        ], env=env)
        full = session.kind in self.config.publish_sessions
        artifacts, event_count = self._build_archive(
            session, captured=paths["provisional"], full=full,
        )
        if full:
            self._publish(session, artifacts, event_count)

    def process_requested(self, session: ScheduledSession, replay_id: str) -> None:
        if fixture_stem(session) != replay_id:
            raise RuntimeError("requested replay ID differs from the FastF1 schedule")
        artifacts, event_count = self._build_archive(session, captured=None, full=True)
        self._publish(session, artifacts, event_count)

    def _run_remote_once(self) -> str:
        if self.remote_queue is None:
            return "idle"
        job = self.remote_queue.claim_next(self.now())
        if job is None:
            return "idle"
        session_id = job["session_id"]
        self.remote_processing.touch()
        try:
            year = int(session_id.split("-", 1)[0])
            matches = [
                session
                for session in load_fastf1_schedule(year)
                if session.session_id == session_id
            ]
            if len(matches) != 1:
                raise RuntimeError("FastF1 schedule does not contain the requested session")
            session = matches[0]
            if session.capture_until > self.now():
                raise RuntimeError("requested session has not completed")
            self.process_requested(session, job["fixture_stem"])
        except Exception as exc:
            self.remote_queue.finish(
                session_id,
                error="Archive preparation failed on the worker",
            )
            return f"requested archive failed: {session_id}: {type(exc).__name__}"
        finally:
            self.remote_processing.unlink(missing_ok=True)
        return f"requested archive complete: {session_id}"

    def run_once(self) -> str:
        now = self.now()
        state = self.store.load()
        protected = tuple(
            f"{session_id}."
            for session_id, item in state.sessions.items()
            if item.phase in {Phase.RECORDING, Phase.CAPTURED, Phase.PROCESSING}
            or item.retry_phase in {Phase.RECORDING, Phase.PROCESSING}
        )
        cutoff = now.timestamp() - self.config.raw_retention_days * 86_400
        for path in self.config.raw_dir.iterdir():
            if (
                path.is_file()
                and not path.is_symlink()
                and not path.name.startswith(protected)
                and path.stat().st_mtime < cutoff
            ):
                path.unlink()
        years = {now.year}
        if now.month == 12:
            years.add(now.year + 1)
        years.update(int(session_id.split("-", 1)[0]) for session_id in state.sessions)
        refresh = (
            self._schedule_loaded_at is None
            or now - self._schedule_loaded_at >= SCHEDULE_REFRESH
            or self._schedule_years != years
        )
        if refresh:
            refreshed = []
            try:
                disk_cache = self._load_schedule_cache()
            except (OSError, KeyError, TypeError, ValueError):
                disk_cache = []
            for year in sorted(years):
                try:
                    loaded = load_fastf1_schedule(year)
                    if year == now.year and not loaded:
                        raise RuntimeError(f"empty schedule for {year}")
                    refreshed.extend(loaded)
                except Exception:
                    cached = [session for session in self._schedule if session.year == year]
                    if not cached:
                        cached = [session for session in disk_cache if session.year == year]
                    if cached:
                        refreshed.extend(cached)
                    elif year == now.year:
                        self.schedule_failure.write_text("schedule unavailable\n", encoding="utf-8")
                        raise
            try:
                self._save_schedule_cache(refreshed)
            except OSError as exc:
                logger.warning("failed to persist schedule cache: %s", type(exc).__name__)
            self.schedule_failure.unlink(missing_ok=True)
            self._schedule_loaded_at = now
            self._schedule_years = years
            self._schedule = refreshed
        sessions = self._schedule
        by_id = {session.session_id: session for session in sessions}

        # A crash may happen after the raw file is complete but before CAPTURED
        # is persisted. Resume/finalize RECORDING even after its schedule window.
        for session_id, item in state.sessions.items():
            if item.phase is not Phase.RECORDING:
                continue
            session = by_id.get(session_id)
            if session is None:
                raise RuntimeError(f"schedule no longer contains {session_id}")
            try:
                self.capture(session)
            except Exception as exc:
                retry = self.now() + CAPTURE_RETRY
                self.store.transition(
                    session_id, Phase.FAILED, self.now(), error=str(exc), retry_at=retry,
                )
                return f"capture failed: {session_id}: {exc}"
            self.store.transition(session_id, Phase.CAPTURED, self.now())
            return f"captured: {session_id}"

        unavailable = {
            session_id
            for session_id, item in state.sessions.items()
            if item.phase is not Phase.RECORDING
            and state.due_phase(session_id, now) is not Phase.RECORDING
        }

        # A due capture takes priority over older captured archive work.
        session = select_due_session(sessions, now, unavailable)
        if session is not None:
            self.store.transition(session.session_id, Phase.RECORDING, now)
            try:
                self.capture(session)
            except Exception as exc:
                retry = self.now() + CAPTURE_RETRY
                self.store.transition(
                    session.session_id, Phase.FAILED, self.now(), error=str(exc), retry_at=retry,
                )
                return f"capture failed: {session.session_id}: {exc}"
            self.store.transition(session.session_id, Phase.CAPTURED, self.now())
            return f"captured: {session.session_id}"

        capture_deadlines = [
            (
                item.capture_from,
                item.session_id,
            )
            for item in sessions
            if item.capture_from > now
            and item.session_id not in unavailable
        ]
        capture_deadlines.extend(
            (item.retry_at, session_id)
            for session_id, item in state.sessions.items()
            if (
                item.phase is Phase.FAILED
                and item.retry_phase is Phase.RECORDING
                and item.retry_at is not None
                and item.retry_at > now
            )
        )
        next_capture = min(capture_deadlines, default=None)
        if (
            next_capture is not None
            and next_capture[0] <= now + REMOTE_CAPTURE_GUARD
        ):
            return (
                f"idle: next capture {next_capture[1]} at "
                f"{next_capture[0].isoformat()} (approaching)"
            )

        # Outside the capture guard, archive processing precedes idle and remote work.
        for session_id, item in state.sessions.items():
            due = state.due_phase(session_id, now)
            if item.phase in {Phase.CAPTURED, Phase.PROCESSING}:
                due = Phase.PROCESSING
            if due is not Phase.PROCESSING:
                continue
            session = by_id.get(session_id)
            if session is None:
                raise RuntimeError(f"schedule no longer contains {session_id}")
            self.store.transition(session_id, Phase.PROCESSING, now)
            try:
                self.process(session)
            except Exception as exc:
                retry = self.now() + ARCHIVE_RETRY
                self.store.transition(
                    session_id, Phase.FAILED, self.now(), error=str(exc), retry_at=retry,
                )
                return f"processing failed: {session_id}: {exc}"
            self.store.transition(session_id, Phase.COMPLETE, self.now())
            return f"complete: {session_id}"

        remote = self._run_remote_once()
        if remote == "idle":
            self._sync_completed_awards(sessions)
            if next_capture is not None:
                return (
                    f"idle: next capture {next_capture[1]} at "
                    f"{next_capture[0].isoformat()}"
                )
        return remote

    def run_forever(self) -> None:
        while True:
            self._beat()
            try:
                print(f"{self.now().isoformat()} {self.run_once()}", flush=True)
            except Exception as exc:
                print(f"{self.now().isoformat()} worker error: {exc}", file=sys.stderr, flush=True)
            self._beat()
            self.sleep(self.config.interval_seconds)


def main() -> None:
    Recorder(Config.from_env()).run_forever()


if __name__ == "__main__":
    main()
