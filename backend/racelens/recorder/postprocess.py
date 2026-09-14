"""Pure post-capture merge, archive validation, and command planning."""
from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from racelens.events.models import WEATHER_BOUNDS, Event, dump_jsonl, load_jsonl, make_event_id


class PostprocessError(RuntimeError):
    """Input cannot be post-processed without losing or inventing data."""


class ArchiveValidationError(PostprocessError):
    """An archive artifact is malformed or too incomplete to publish."""


def validate_fixture(path: Path, *, min_laps: int = 5, min_drivers: int = 2) -> int:
    """Reject archive placeholders that contain no real completed running."""
    events = _read_events(Path(path))
    if events != sorted(events, key=lambda event_: (event_.session_time_ms, event_.event_id)):
        raise ArchiveValidationError("fixture events are not deterministically sorted")
    invalid_ids = [
        event_ for event_ in events
        if event_.event_id != make_event_id(
            event_.session_id, event_.type, event_.session_time_ms,
            event_.driver_id, event_.payload,
        )
    ]
    if invalid_ids:
        raise ArchiveValidationError("fixture contains non-deterministic event IDs")
    laps = [event_ for event_ in events if event_.type == "LapCompleted"]
    drivers = {event_.driver_id for event_ in laps if event_.driver_id}
    if len(laps) < min_laps or len(drivers) < min_drivers:
        raise ArchiveValidationError("fixture does not contain enough completed laps")
    return len(events)


@dataclass(frozen=True, slots=True)
class MergeReport:
    canonical_events: int
    captured_events: int
    radio_added: int
    radio_deduplicated: int
    output_path: Path
    written: bool

    @property
    def summary(self) -> str:
        return (
            f"canonical={self.canonical_events} captured={self.captured_events} "
            f"radio=+{self.radio_added} deduped={self.radio_deduplicated}"
        )


@dataclass(frozen=True, slots=True)
class ValidationReport:
    events: int
    track_points: int
    drivers: int
    frames: int
    position_driver_coverage: float
    progress_driver_coverage: float
    position_frame_coverage: float
    progress_frame_coverage: float

    @property
    def summary(self) -> str:
        return (
            f"events={self.events} track={self.track_points} drivers={self.drivers} "
            f"frames={self.frames} xy={self.position_driver_coverage:.0%} "
            f"progress={self.progress_driver_coverage:.0%}"
        )


@dataclass(frozen=True, slots=True)
class PlannedCommand:
    name: str
    cwd: Path
    argv: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommandPlan:
    commands: tuple[PlannedCommand, ...]
    merge_args: tuple[Path, Path, Path]
    validate_args: tuple[Path, Path, Path] | None
    retained: tuple[Path, ...]
    published: tuple[Path, ...]


def _read_events(path: Path, *, allow_empty: bool = False) -> list[Event]:
    try:
        events = load_jsonl(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PostprocessError(f"invalid event fixture: {path}") from exc
    if not events and not allow_empty:
        raise PostprocessError(f"empty event fixture: {path}")
    return events


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            tmp_name = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, path.stat().st_mode & 0o777 if path.exists() else 0o644)
        os.replace(tmp_name, path)
    finally:
        if tmp_name is not None:
            Path(tmp_name).unlink(missing_ok=True)


def _radio(event_: Event) -> bool:
    payload = event_.payload
    return (
        event_.type == "RaceControlMessage"
        and payload.get("category") == "Radio"
        and any(payload.get(key) for key in ("audio_url", "audio_path", "transcript"))
    )


_WEATHER_FIELDS = set(WEATHER_BOUNDS) | {"rainfall"}


def _weather(event_: Event) -> bool:
    payload = event_.payload
    return (
        event_.type == "WeatherUpdated"
        and event_.source == "f1live"
        and bool(payload)
        and set(payload) <= _WEATHER_FIELDS
        and all(
            isinstance(value, bool)
            if key == "rainfall"
            else (
                isinstance(value, float)
                and math.isfinite(value)
                and WEATHER_BOUNDS[key][0] <= value <= WEATHER_BOUNDS[key][1]
            )
            for key, value in payload.items()
        )
    )


def _driver_status(event_: Event) -> bool:
    if event_.source != "f1live" or not event_.driver_id:
        return False
    if event_.type == "DriverStoppedChanged":
        return set(event_.payload) == {"stopped"} and type(event_.payload["stopped"]) is bool
    return (
        event_.type == "RetirementDetected"
        and not event_.payload
        and (event_.lap is None or event_.lap > 0)
    )


def _radio_key(event_: Event) -> tuple[object, ...]:
    payload = event_.payload
    path = str(payload.get("audio_path") or "")
    url = str(payload.get("audio_url") or "")
    if not path and url:
        url_path = urlsplit(url).path.lstrip("/")
        marker = "TeamRadio/"
        path = url_path[url_path.index(marker):] if marker in url_path else ""
    if path:
        return "path", path
    if url:
        return "url", url
    return "transcript", event_.driver_id, event_.session_time_ms, str(payload["transcript"])


def _merged_radio(session_id: str, rows: list[tuple[bool, Event]]) -> Event:
    canonical = [event_ for is_canonical, event_ in rows if is_canonical]
    representative = min(
        canonical or [event_ for _, event_ in rows],
        key=lambda event_: (event_.session_time_ms, event_.event_id),
    )
    payload = dict(representative.payload)
    for field in ("audio_url", "audio_path", "transcript"):
        values = {
            str(event_.payload[field])
            for _, event_ in rows
            if event_.payload.get(field)
        }
        if values and not payload.get(field):
            payload[field] = max(values, key=lambda value: (len(value), value))
    return representative.model_copy(
        update={
            "session_id": session_id,
            "payload": payload,
            "event_id": make_event_id(
                session_id,
                representative.type,
                representative.session_time_ms,
                representative.driver_id,
                payload,
            ),
        }
    )


def merge_captured_radio(
    canonical_path: Path,
    captured_path: Path,
    output_path: Path | None = None,
) -> MergeReport:
    """Atomically enrich a canonical FastF1 fixture with captured F1 live data.

    Captured radio, validated weather, and source-backed driver status are
    retained; other non-canonical events are ignored. Radio is deduped by
    source identity and the remaining events by deterministic event identity.
    """
    canonical_path = Path(canonical_path)
    captured_path = Path(captured_path)
    destination = Path(output_path) if output_path is not None else canonical_path
    canonical = _read_events(canonical_path)
    captured = _read_events(captured_path, allow_empty=True)
    session_ids = {event_.session_id for event_ in canonical}
    if len(session_ids) != 1:
        raise PostprocessError("canonical fixture must contain exactly one session_id")
    session_id = next(iter(session_ids))
    captured_radio = [event_ for event_ in captured if _radio(event_)]
    captured_weather = [event_ for event_ in captured if _weather(event_)]
    captured_status = [event_ for event_ in captured if _driver_status(event_)]
    if not captured_radio and not captured_weather and not captured_status:
        written = destination != canonical_path
        if written:
            atomic_write_text(destination, dump_jsonl(canonical))
        return MergeReport(len(canonical), len(captured), 0, 0, destination, written)

    groups: dict[tuple[object, ...], list[tuple[bool, Event]]] = {}
    fixed = []
    for event_ in canonical:
        if _radio(event_):
            groups.setdefault(_radio_key(event_), []).append((True, event_))
        else:
            fixed.append(event_)
    fixed_ids = {event_.event_id for event_ in fixed}
    for event_ in captured_weather + captured_status:
        payload = dict(event_.payload)
        captured_event = event_.model_copy(update={
            "session_id": session_id,
            "event_id": make_event_id(
                session_id, event_.type, event_.session_time_ms, event_.driver_id, payload,
            ),
        })
        if captured_event.event_id not in fixed_ids:
            fixed.append(captured_event)
            fixed_ids.add(captured_event.event_id)
    canonical_radio_keys = set(groups)
    for event_ in captured_radio:
        groups.setdefault(_radio_key(event_), []).append((False, event_))

    lap_marks: dict[str, list[tuple[int, int]]] = {}
    for event_ in canonical:
        if event_.type == "LapCompleted" and event_.driver_id and event_.lap is not None:
            lap_marks.setdefault(event_.driver_id, []).append(
                (event_.session_time_ms, event_.lap)
            )
    radios = []
    for _, rows in sorted(groups.items(), key=str):
        radio = _merged_radio(session_id, rows)
        marks = lap_marks.get(radio.driver_id or "", [])
        if marks:
            completed = max((lap for at, lap in marks if at <= radio.session_time_ms), default=0)
            inferred = min(completed + 1, max(lap for _, lap in marks))
            radio = radio.model_copy(update={"lap": max(1, inferred)})
        radios.append(radio)
    merged = sorted(fixed + radios, key=lambda event_: (event_.session_time_ms, event_.event_id))
    atomic_write_text(destination, dump_jsonl(merged))
    return MergeReport(
        canonical_events=len(canonical),
        captured_events=len(captured),
        radio_added=len(set(groups) - canonical_radio_keys),
        radio_deduplicated=sum(len(rows) for rows in groups.values()) - len(groups),
        output_path=destination,
        written=True,
    )


def _json_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArchiveValidationError(f"invalid {label}: {path}") from exc
    if not isinstance(value, dict):
        raise ArchiveValidationError(f"{label} must be a JSON object")
    return value


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _frames(
    value: object,
    label: str,
    *,
    xy: bool,
) -> tuple[dict[str, list], int, int, int]:
    if not isinstance(value, dict) or not value:
        raise ArchiveValidationError(f"positions.{label} must be a non-empty object")
    rows: dict[str, list] = {}
    lengths: set[int] = set()
    covered = non_null = 0
    for driver, frames in value.items():
        if not isinstance(driver, str) or not driver or not isinstance(frames, list):
            raise ArchiveValidationError(f"invalid positions.{label} driver frames")
        for frame in frames:
            valid = (
                frame is None
                or (xy and isinstance(frame, list) and len(frame) == 2 and all(_number(v) for v in frame))
                or (not xy and _number(frame))
            )
            if not valid:
                raise ArchiveValidationError(f"invalid positions.{label} frame")
        rows[driver] = frames
        lengths.add(len(frames))
        present = sum(frame is not None for frame in frames)
        non_null += present
        covered += present > 0
    if len(lengths) != 1 or not lengths or next(iter(lengths)) == 0:
        raise ArchiveValidationError(f"positions.{label} frame lengths must match and be non-zero")
    return rows, next(iter(lengths)), covered, non_null


def validate_archive(
    fixture_path: Path,
    track_path: Path,
    positions_path: Path,
    *,
    min_position_driver_coverage: float = 0.9,
    min_progress_driver_coverage: float = 0.9,
    min_position_frame_coverage: float = 0.5,
    min_progress_frame_coverage: float = 0.5,
) -> ValidationReport:
    """Validate publishable archive artifacts; never repairs missing data."""
    for threshold in (
        min_position_driver_coverage, min_progress_driver_coverage,
        min_position_frame_coverage, min_progress_frame_coverage,
    ):
        if not 0 <= threshold <= 1:
            raise ValueError("coverage thresholds must be between 0 and 1")
    event_count = validate_fixture(Path(fixture_path))

    track = _json_object(Path(track_path), "track")
    points = track.get("points")
    progress_points = track.get("progress_points")
    viewbox = track.get("viewbox")
    if not isinstance(points, list) or len(points) < 2 or not all(
        isinstance(point, list) and len(point) == 2 and all(_number(v) for v in point)
        for point in points
    ):
        raise ArchiveValidationError("track.points must contain numeric x/y pairs")
    if not isinstance(progress_points, list) or len(progress_points) < 2 or not all(
        isinstance(point, list) and len(point) == 2 and all(_number(v) for v in point)
        for point in progress_points
    ):
        raise ArchiveValidationError(
            "track.progress_points must contain numeric x/y pairs"
        )
    if not isinstance(viewbox, list) or len(viewbox) != 2 or not all(
        _number(value) and value > 0 for value in viewbox
    ):
        raise ArchiveValidationError("track.viewbox must contain two positive numbers")

    positions = _json_object(Path(positions_path), "positions")
    if positions.get("session_id") != track.get("session_id"):
        raise ArchiveValidationError("track and positions session_id differ")
    if positions.get("session_id") != Path(fixture_path).stem:
        raise ArchiveValidationError("archive session_id differs from fixture name")
    tick_ms = positions.get("tick_ms")
    if tick_ms != 1000:
        raise ArchiveValidationError("positions.tick_ms must be 1000")
    if positions.get("viewbox") != viewbox:
        raise ArchiveValidationError("track and positions viewbox differ")

    drivers, frames, position_drivers, position_non_null = _frames(
        positions.get("drivers"), "drivers", xy=True,
    )
    progress, progress_frames, progress_drivers, progress_non_null = _frames(
        positions.get("progress"), "progress", xy=False,
    )
    if set(drivers) != set(progress) or frames != progress_frames:
        raise ArchiveValidationError("position and progress grids differ")
    if frames < 300:
        raise ArchiveValidationError("positions must contain at least five minutes")
    driver_count = len(drivers)
    position_coverage = position_drivers / driver_count
    progress_coverage = progress_drivers / driver_count
    if position_coverage < min_position_driver_coverage:
        raise ArchiveValidationError(f"position driver coverage is {position_coverage:.0%}")
    if progress_coverage < min_progress_driver_coverage:
        raise ArchiveValidationError(f"progress driver coverage is {progress_coverage:.0%}")
    cells = driver_count * frames
    position_frame_coverage = position_non_null / cells
    progress_frame_coverage = progress_non_null / cells
    if position_frame_coverage < min_position_frame_coverage:
        raise ArchiveValidationError(
            f"position frame coverage is {position_frame_coverage:.0%}"
        )
    if progress_frame_coverage < min_progress_frame_coverage:
        raise ArchiveValidationError(
            f"progress frame coverage is {progress_frame_coverage:.0%}"
        )
    return ValidationReport(
        events=event_count,
        track_points=len(points),
        drivers=driver_count,
        frames=frames,
        position_driver_coverage=position_coverage,
        progress_driver_coverage=progress_coverage,
        position_frame_coverage=position_frame_coverage,
        progress_frame_coverage=progress_frame_coverage,
    )


_POSITION_SESSIONS = {"SQ", "SPRINT", "Q", "R"}
_SESSION_ALIASES = {
    "SPRINT QUALIFYING": "SQ",
    "SPRINT SHOOTOUT": "SQ",
    "QUALIFYING": "Q",
    "RACE": "R",
}


def build_command_plan(
    year: int,
    gp: str,
    session: str,
    session_id: str,
    *,
    root: Path = Path("."),
    python: str = "python",
) -> CommandPlan:
    """Return, but never run, the capture/provisional/FastF1 archive commands.

    FastF1 archive availability is external: callers must keep raw/provisional
    files and run ``validate_archive`` before publishing generated artifacts.
    """
    if year < 2018 or not all(value.strip() for value in (gp, session, session_id)):
        raise ValueError("year >= 2018 and non-empty gp/session/session_id are required")
    root = Path(root).resolve()
    backend = root / "backend"
    fixtures = backend / "fixtures"
    raw = backend / "recordings" / "raw" / f"{session_id}.f1live"
    provisional = backend / "recordings" / "provisional" / f"{session_id}.jsonl"
    archive = fixtures / f"{session_id}.jsonl"
    track = fixtures / f"{session_id}.track.json"
    positions_raw = backend / "recordings" / "raw" / f"{session_id}.positions.jsonl"
    positions = fixtures / f"{session_id}.positions.json"
    kind = _SESSION_ALIASES.get(session.strip().upper(), session.strip().upper())

    commands = [
        PlannedCommand("capture", backend, (
            python, "-m", "racelens.cli", "capture-live", "-o", str(raw), "--timeout", "0",
        )),
        PlannedCommand("provisional", backend, (
            python, "-m", "racelens.cli", "ingest-live", str(raw), "--year", str(year),
            "--gp", gp, "--session", session, "-o", str(provisional),
        )),
        PlannedCommand("canonical", backend, (
            python, "-m", "racelens.cli", "ingest", str(year), gp, session,
            "-o", str(archive),
        )),
    ]
    published: list[Path] = [archive]
    validate_args = None
    if kind in _POSITION_SESSIONS:
        commands.extend((
            PlannedCommand("track", backend, (
                python, "-m", "racelens.cli", "track", str(year), gp, session,
                "-o", str(track),
            )),
            PlannedCommand("positions-raw", backend, (
                python, "-m", "racelens.cli", "positions-raw", str(year), gp, session,
                "-o", str(positions_raw),
            )),
            PlannedCommand("positions", root, (
                str(root / "rust" / "race-core" / "target" / "release" / "race-core"),
                str(positions_raw), str(track), str(positions), "1000",
            )),
            PlannedCommand("track-progress", backend, (
                python, "-m", "racelens.cli", "track-progress", str(year), gp, session,
                session_id,
            )),
        ))
        published.extend((track, positions))
        validate_args = (archive, track, positions)
    return CommandPlan(
        commands=tuple(commands),
        merge_args=(archive, provisional, archive),
        validate_args=validate_args,
        retained=(raw, provisional),
        published=tuple(published),
    )
