"""CLI: ingest a historical session and export normalized events.

    python -m racelens.cli ingest 2024 Monaco R -o fixtures/monaco_2024_race.jsonl
    python -m racelens.cli state fixtures/monaco_2024_race.jsonl --at-ms 3600000
"""
import argparse
import json
import os
import sys
from pathlib import Path


def _write_events(events, out: str) -> None:
    """Shared tail of every ingest command: dump events, report to stderr."""
    from racelens.events.models import dump_jsonl
    from racelens.recorder.postprocess import atomic_write_text

    out_path = Path(out)
    atomic_write_text(out_path, dump_jsonl(events))
    print(f"{len(events)} events → {out_path}", file=sys.stderr)


# ── Subcommand handlers ────────────────────────────────────────────────────────

def _cmd_ingest(args: argparse.Namespace) -> None:
    from racelens.adapters.fastf1_adapter import ingest_session

    _write_events(ingest_session(args.year, args.gp, args.session), args.out)


def _cmd_ingest_openf1(args: argparse.Namespace) -> None:
    from racelens.adapters.openf1_adapter import find_session, ingest_openf1

    session_key = find_session(args.year, args.country, args.session)
    print(f"session_key={session_key}", file=sys.stderr)
    _write_events(ingest_openf1(session_key), args.out)


def _cmd_capture_live(args: argparse.Namespace) -> None:
    from fastf1.livetiming.client import SignalRClient

    if args.no_auth:
        # fastf1 3.8.3 passes access_token_factory=None when auth is disabled;
        # signalrcore rejects non-callable. Strip the key. Drop when fastf1 fixes.
        from signalrcore.hub_connection_builder import HubConnectionBuilder

        _orig_with_url = HubConnectionBuilder.with_url

        def _with_url_no_none_factory(self, url, options=None):
            if options and options.get("access_token_factory") is None:
                options = {k: v for k, v in options.items() if k != "access_token_factory"}
            return _orig_with_url(self, url, options=options)

        HubConnectionBuilder.with_url = _with_url_no_none_factory

    client = SignalRClient(
        filename=args.out,
        filemode="a" if args.append else "w",
        timeout=args.timeout,
        no_auth=args.no_auth,
    )
    print(
        f"recording live feed → {args.out} "
        f"(no_auth={args.no_auth}, timeout={args.timeout}s); Ctrl-C to stop …",
        file=sys.stderr,
    )
    client.start()  # blocks until timeout / KeyboardInterrupt


def _cmd_ingest_live(args: argparse.Namespace) -> None:
    from racelens.adapters.f1live_adapter import ingest_f1live
    from racelens.adapters.fastf1_adapter import session_id_for

    events = ingest_f1live(
        *args.feed,
        session_id=session_id_for(args.year, args.gp, args.session),
    )
    _write_events(events, args.out)


def _cmd_track(args: argparse.Namespace) -> None:
    from racelens.positions.track import build_track_outline
    from racelens.recorder.postprocess import atomic_write_text

    # Build session_id from the output file stem
    out_path = Path(args.out)
    stem = out_path.stem  # e.g. "monaco_2024_race.track" → need just "monaco_2024_race"
    session_id = stem[:-len(".track")] if stem.endswith(".track") else stem

    data = build_track_outline(args.year, args.gp, args.session, session_id)
    atomic_write_text(out_path, json.dumps(data))
    print(f"{len(data['points'])} points → {out_path}", file=sys.stderr)


def _cmd_positions_raw(args: argparse.Namespace) -> None:
    from racelens.positions.track import export_raw_positions

    out = Path(args.out)
    count = export_raw_positions(args.year, args.gp, args.session, out)
    print(f"{count} rows → {out}", file=sys.stderr)


def _cmd_mini_sectors(args: argparse.Namespace) -> None:
    import os

    from racelens.events.models import dump_jsonl, load_jsonl
    from racelens.positions.mini_sectors import compute_gap_events
    from racelens.recorder.postprocess import atomic_write_text

    fixtures_dir = Path(os.environ.get("RACELENS_FIXTURES", "fixtures"))
    fixture_path = fixtures_dir / f"{args.session_id}.jsonl"
    existing = load_jsonl(fixture_path.read_text(encoding="utf-8"))
    before = len(existing)

    new_events = compute_gap_events(args.year, args.gp, args.session, args.session_id)
    if {e.type for e in new_events} != {"GapUpdated", "IntervalUpdated"} or any(
        e.session_id != args.session_id
        or e.source != "mini_sectors"
        for e in new_events
    ):
        raise SystemExit("mini-sectors: invalid empty or mismatched replacement")

    kept = [
        e for e in existing
        if e.source != "mini_sectors"
        or e.type not in {"GapUpdated", "IntervalUpdated"}
    ]
    dropped = before - len(kept)
    combined = kept + new_events
    combined.sort(key=lambda e: (e.session_time_ms, e.event_id))

    atomic_write_text(fixture_path, dump_jsonl(combined))
    after = len(combined)
    print(
        f"before={before} dropped_gap={dropped} new_gap={len(new_events)} after={after} → {fixture_path}",
        file=sys.stderr,
    )


def _cmd_track_progress(args: argparse.Namespace) -> None:
    import os

    from racelens.positions.track_progress import compute_progress
    from racelens.recorder.postprocess import atomic_write_text

    fixtures_dir = Path(os.environ.get("RACELENS_FIXTURES", "fixtures"))
    pos_path = fixtures_dir / f"{args.session_id}.positions.json"
    pos = json.loads(pos_path.read_text(encoding="utf-8"))
    pos["progress"] = compute_progress(args.year, args.gp, args.session, args.session_id)
    atomic_write_text(pos_path, json.dumps(pos, separators=(",", ":")))
    covered = sum(1 for arr in pos["progress"].values() if any(v is not None for v in arr))
    print(f"track-progress: {covered}/{len(pos['progress'])} drivers → {pos_path}", file=sys.stderr)


def _cmd_radio_fetch(args: argparse.Namespace) -> None:
    """Merge OpenF1 team-radio clips into an existing fixture (archive sessions).

    Time anchor: earliest lap-1 date_start = lights-out = the fixture's t=0
    (both the openf1 and fastf1 adapters anchor there).
    """
    from racelens.adapters.openf1_adapter import (
        _build_driver_map, _compute_t0, _get, _parse_int, _parse_iso, find_session,
    )
    from racelens.events.models import event
    from racelens.recorder.postprocess import atomic_write_text

    path = Path(args.fixture)
    lines = path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    sid = first["session_id"]
    have_urls = {
        json.loads(ln).get("payload", {}).get("audio_url")
        for ln in lines
    } - {None}
    # Per-driver lap timeline from the fixture itself, for radio lap numbers.
    lap_marks: dict[str, list[tuple[int, int]]] = {}
    for ln in lines:
        e = json.loads(ln)
        if e["type"] == "LapCompleted" and e.get("driver_id") and e.get("lap") is not None:
            lap_marks.setdefault(e["driver_id"], []).append((e["session_time_ms"], e["lap"]))

    key = find_session(args.year, args.country)
    t0 = _compute_t0(_get("/laps", {"session_key": key, "lap_number": 1}))
    if t0 is None:
        raise SystemExit("radio-fetch: no lap-1 anchor from OpenF1")
    driver_map = _build_driver_map(_get("/drivers", {"session_key": key}))

    added = 0
    for row in _get("/team_radio", {"session_key": key}):
        url, dn = row.get("recording_url"), row.get("driver_number")
        ts = _parse_iso(row.get("date"))
        driver_number = _parse_int(dn)
        if not url or ts is None or url in have_urls or driver_number is None:
            continue
        t_ms = round((ts - t0) * 1000)
        if t_ms < 0:
            continue  # pre-race garbage
        drv = driver_map.get(driver_number, str(driver_number))
        marks = lap_marks.get(drv or "", [])
        lap = next((lp + 1 for tm, lp in reversed(marks) if tm <= t_ms), 1)
        ev = event(sid, "RaceControlMessage", t_ms, drv, lap=lap,
                   category="Radio", message=f"RADIO: {drv}", audio_url=url)
        lines.append(ev.model_dump_json(exclude_none=True))
        added += 1

    lines.sort(key=lambda ln: (json.loads(ln)["session_time_ms"], json.loads(ln)["event_id"]))
    atomic_write_text(path, "\n".join(lines) + "\n")
    print(f"radio-fetch: +{added} radio events → {path}", file=sys.stderr)


def _cmd_radio_transcribe(args: argparse.Namespace) -> None:
    from racelens.radio.transcribe import IncompleteTranscriptionError, enrich_fixture

    try:
        done = enrich_fixture(Path(args.fixture))
    except IncompleteTranscriptionError as exc:
        print(f"radio-transcribe: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    print(f"radio-transcribe: {done} transcripts → {args.fixture}", file=sys.stderr)


def _award_store():
    from racelens.object_storage import S3Store, StorageConfig

    config = StorageConfig.from_env()
    return S3Store(config) if config is not None else None


def _cmd_driver_of_day_import(args: argparse.Namespace) -> None:
    from racelens.driver_of_day import sync_official_award

    root = Path(args.fixtures)
    record = sync_official_award(
        args.year, args.meeting, args.replay_id, root, _award_store(),
    )
    print(json.dumps(record, separators=(",", ":"), sort_keys=True))


def _cmd_driver_of_day_sync_2026(args: argparse.Namespace) -> None:
    from racelens.driver_of_day import sync_completed_official_awards
    from racelens.recorder.schedule import load_fastf1_schedule

    report = sync_completed_official_awards(
        2026, load_fastf1_schedule(2026), Path(args.fixtures), _award_store(),
    )
    print(json.dumps(report, separators=(",", ":"), sort_keys=True))


def _cmd_radio_evaluate(args: argparse.Namespace) -> None:
    from racelens.radio.evaluate import format_report, evaluate_manifest

    report = evaluate_manifest(Path(args.manifest))
    print(json.dumps(report, separators=(",", ":"), sort_keys=True))
    print(format_report(report), file=sys.stderr)


def _cmd_state(args: argparse.Namespace) -> None:
    from racelens.events.models import load_jsonl
    from racelens.replay.engine import ReplayEngine

    events = load_jsonl(Path(args.events_file).read_text(encoding="utf-8"))
    engine = ReplayEngine(events)
    print(json.dumps(engine.state_at(args.at_ms), indent=2))


def _cmd_recorder_status(args: argparse.Namespace) -> None:
    from racelens.recorder.status import recorder_status

    base = Path(os.environ.get("RACELENS_RECORDER_DATA", "/var/lib/race-lens-recorder"))
    print(json.dumps(recorder_status(base), indent=None if args.json else 2, sort_keys=True))


# ── Parser / dispatch ──────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(prog="racelens")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ingest = sub.add_parser("ingest", help="ingest a historical session via FastF1")
    p_ingest.add_argument("year", type=int)
    p_ingest.add_argument("gp", help='Grand Prix name, e.g. "Monaco"')
    p_ingest.add_argument("session", nargs="?", default="R", help="R / Q / FP1 ...")
    p_ingest.add_argument("-o", "--out", required=True, help="output .jsonl path")
    p_ingest.set_defaults(func=_cmd_ingest)

    p_ingest_openf1 = sub.add_parser(
        "ingest-openf1", help="ingest a session via OpenF1 (no API key required)"
    )
    p_ingest_openf1.add_argument("year", type=int)
    p_ingest_openf1.add_argument("country", help='Country or circuit, e.g. "Monaco"')
    p_ingest_openf1.add_argument(
        "session", nargs="?", default="Race", help='Session name, e.g. "Race"'
    )
    p_ingest_openf1.add_argument("-o", "--out", required=True, help="output .jsonl path")
    p_ingest_openf1.set_defaults(func=_cmd_ingest_openf1)

    p_capture = sub.add_parser(
        "capture-live",
        help="record the live F1 SignalR feed to a file (run DURING a live session)",
    )
    p_capture.add_argument("-o", "--out", required=True, help="output raw feed path")
    p_capture.add_argument("--timeout", type=int, default=60,
                           help="exit after N seconds with no data (0 = never)")
    p_capture.add_argument("--no-auth", action="store_true",
                           help="connect without F1 auth (may return partial/empty data)")
    p_capture.add_argument("--append", action="store_true",
                           help="append instead of overwrite (resume a capture)")
    p_capture.set_defaults(func=_cmd_capture_live)

    p_ingest_live = sub.add_parser(
        "ingest-live",
        help="convert a recorded SignalR feed (capture-live) into normalized events",
    )
    p_ingest_live.add_argument("feed", nargs="+", help="recorded feed file(s)")
    p_ingest_live.add_argument("--year", type=int, required=True)
    p_ingest_live.add_argument("--gp", required=True, help='Grand Prix, e.g. "Silverstone"')
    p_ingest_live.add_argument("--session", default="R", help="R / Q / FP1 ...")
    p_ingest_live.add_argument("-o", "--out", required=True, help="output .jsonl path")
    p_ingest_live.set_defaults(func=_cmd_ingest_live)

    p_track = sub.add_parser("track", help="export track outline from FastF1 telemetry")
    p_track.add_argument("year", type=int)
    p_track.add_argument("gp", help='Grand Prix name, e.g. "Monaco"')
    p_track.add_argument("session", nargs="?", default="R", help="R / Q / FP1 ...")
    p_track.add_argument("-o", "--out", required=True, help="output .track.json path")
    p_track.set_defaults(func=_cmd_track)

    p_posraw = sub.add_parser(
        "positions-raw",
        help="export raw X/Y telemetry per driver as JSONL for Rust resampler",
    )
    p_posraw.add_argument("year", type=int)
    p_posraw.add_argument("gp", help='Grand Prix name, e.g. "Monaco"')
    p_posraw.add_argument("session", nargs="?", default="R", help="R / Q / FP1 ...")
    p_posraw.add_argument("-o", "--out", required=True, help="output .jsonl path")
    p_posraw.set_defaults(func=_cmd_positions_raw)

    p_mini = sub.add_parser(
        "mini-sectors",
        help="compute near-continuous gap events from RelativeDistance telemetry",
    )
    p_mini.add_argument("year", type=int)
    p_mini.add_argument("gp", help='Grand Prix name, e.g. "Monaco"')
    p_mini.add_argument("session", nargs="?", default="R", help="R / Q / FP1 ...")
    p_mini.add_argument("session_id", help="fixture session id, e.g. monaco_2024_race")
    p_mini.set_defaults(func=_cmd_mini_sectors)

    p_prog = sub.add_parser(
        "track-progress",
        help="write per-tick track progress into positions.json for tower ordering",
    )
    p_prog.add_argument("year", type=int)
    p_prog.add_argument("gp", help='Grand Prix name, e.g. "Monaco"')
    p_prog.add_argument("session", nargs="?", default="R", help="R / Q / FP1 ...")
    p_prog.add_argument("session_id", help="fixture session id, e.g. monaco_2024_race")
    p_prog.set_defaults(func=_cmd_track_progress)

    p_rfetch = sub.add_parser(
        "radio-fetch", help="merge OpenF1 team-radio clips into an archive fixture"
    )
    p_rfetch.add_argument("fixture", help="replay fixture .jsonl")
    p_rfetch.add_argument("year", type=int)
    p_rfetch.add_argument("country", help='Grand Prix country/circuit, e.g. "Monaco"')
    p_rfetch.set_defaults(func=_cmd_radio_fetch)

    p_radio = sub.add_parser(
        "radio-transcribe", help="whisper-transcribe a fixture's team-radio clips in place"
    )
    p_radio.add_argument("fixture", help="replay fixture .jsonl with audio_url radio events")
    p_radio.set_defaults(func=_cmd_radio_transcribe)

    p_radio_eval = sub.add_parser(
        "radio-evaluate", help="evaluate three Whisper profiles on 50 private local clips"
    )
    p_radio_eval.add_argument("manifest", help="gitignored 50-record JSONL reference manifest")
    p_radio_eval.set_defaults(func=_cmd_radio_evaluate)

    p_dotd = sub.add_parser(
        "driver-of-day-import", help="import one exact official F1 fan-vote result"
    )
    p_dotd.add_argument("year", type=int)
    p_dotd.add_argument("meeting", help="exact meeting identity, e.g. 'Hungarian Grand Prix'")
    p_dotd.add_argument("replay_id", help="existing Race replay fixture ID")
    p_dotd.add_argument(
        "--fixtures", default=os.environ.get("RACELENS_FIXTURES", "fixtures"),
    )
    p_dotd.set_defaults(func=_cmd_driver_of_day_import)

    p_dotd_sync = sub.add_parser(
        "driver-of-day-sync-2026", help="one-shot sync of completed 2026 Race replays"
    )
    p_dotd_sync.add_argument(
        "--fixtures", default=os.environ.get("RACELENS_FIXTURES", "fixtures"),
    )
    p_dotd_sync.set_defaults(func=_cmd_driver_of_day_sync_2026)

    p_state = sub.add_parser("state", help="print race state at a timestamp")
    p_state.add_argument("events_file", help="events .jsonl")
    p_state.add_argument("--at-ms", type=int, required=True)
    p_state.set_defaults(func=_cmd_state)

    p_recorder_status = sub.add_parser(
        "recorder-status", help="print sanitized recorder health and publication state",
    )
    p_recorder_status.add_argument("--json", action="store_true", help="emit compact JSON")
    p_recorder_status.set_defaults(func=_cmd_recorder_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
