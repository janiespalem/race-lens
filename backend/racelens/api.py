"""Replay API: serve race state from ingested event files.

    uvicorn racelens.api:app --reload

Sessions are .jsonl files in RACELENS_FIXTURES (default: ./fixtures).
Timestamp-scoped state and insight responses use no future events. Endpoints
without a cutoff, such as full-race highlights, are explicitly opt-in.
"""
import asyncio
import importlib.util
import json
import logging
import os
import re
import time
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Any, Iterator, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from racelens.adapters.openf1_adapter import (
    OpenF1IncrementalIngester,
    find_session,
    list_sessions as _openf1_list_sessions,
)
from racelens.commentary.feed import render_feed
from racelens.catalog import (
    FIRST_SEASON,
    build_catalog,
    expected_replay_id,
    find_session as find_catalog_session,
    public_job,
    ready_job,
)
from racelens.events_significant import significant_events
from racelens.commentary.renderer import render_all
from racelens.driver_of_day import (
    award_key,
    driver_of_day as _driver_of_day,
    load_official_award,
)
from racelens.events.models import load_jsonl
from racelens.highlights import highlights as _highlights
from racelens.forecast.overtake import overtake_probability
from racelens.forecast.pit_sim import simulate_pit
from racelens.forecast.projection import project_order
from racelens.forecast.what_if import VALID_SCENARIOS, what_if
from racelens.forecast.win_prob import win_probability
from racelens.insights.battles import detect_battles
from racelens.insights.passes import Pass, detect_passes
from racelens.insights.registry import detect_all
from racelens.live.runner import LiveRunner
from racelens.live.signalr import SignalRCapture, make_signalr_fetch
from racelens.object_storage import (
    LiveRecordError,
    ManifestError,
    ObjectPreparationQueue,
    REPLAY_ID,
    RemoteSessionCache,
    S3Store,
    StorageConfig,
    StorageError,
    load_live,
)
from racelens.preparations import PreparationQueue, QueueFullError, SESSION_ID
from racelens.replay.engine import ReplayEngine
from racelens.tyre_stints import stint_timeline

FIXTURES_DIR = Path(os.environ.get("RACELENS_FIXTURES", "fixtures"))
READONLY = os.environ.get("RACELENS_READONLY", "").lower() in {"1", "true", "yes"}
CATALOG_CACHE_DIR = Path(
    os.environ.get("RACELENS_CATALOG_CACHE", FIXTURES_DIR.parent / "catalog-cache")
)
PREPARATION_QUEUE_DIR = Path(
    os.environ.get("RACELENS_PREPARATION_QUEUE", FIXTURES_DIR.parent / "preparations")
)
try:
    PREPARATION_QUEUE_MAX = int(os.environ.get("RACELENS_PREPARATION_QUEUE_MAX", "8"))
except ValueError:
    PREPARATION_QUEUE_MAX = 8
try:
    PREPARATION_DAILY_MAX = int(os.environ.get("RACELENS_PREPARATION_DAILY_MAX", "4"))
except ValueError:
    PREPARATION_DAILY_MAX = 4
REMOTE_CACHE_DIR = Path(os.environ.get("RACELENS_REMOTE_CACHE", "/tmp/race-lens-sessions"))
_CGROUP_MEMORY_CURRENT = Path("/sys/fs/cgroup/memory.current")
_CGROUP_MEMORY_MAX = Path("/sys/fs/cgroup/memory.max")
try:
    REMOTE_CACHE_MAX = int(
        os.environ.get("RACELENS_REMOTE_CACHE_MAX_BYTES", str(160 * 1024 * 1024))
    )
except ValueError:
    REMOTE_CACHE_MAX = 160 * 1024 * 1024
STORAGE_CONFIG = StorageConfig.from_env()

logger = logging.getLogger(__name__)

# maxsize=1 bounds parsed memory. Cached values are path-free, while every
# filesystem read reacquires a lease, so directory eviction cannot stale them.
SESSION_CACHE_SIZE = 1

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'self'; connect-src 'self'; "
        "font-src 'self' https://fonts.gstatic.com; frame-ancestors 'none'; "
        "img-src 'self' data:; media-src 'self' https:; object-src 'none'; "
        "script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com"
    ),
    "Permissions-Policy": "camera=(), geolocation=(), microphone=()",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}

# Global live runner — None when no live session is active.
_live: Optional[LiveRunner] = None

# Capture subprocess for source=signalr live sessions (None otherwise).
_capture: Optional[SignalRCapture] = None

# session_id for the active source=signalr live session (same formula as
# make_signalr_fetch), used by the /api/live/* predictive mirrors to look up
# track params. None for source=openf1 sessions or when no live session runs.
_live_session_id: Optional[str] = None

# Two-second process cache: remote Live is one current pointer plus one bounded
# snapshot, so there is no reason to hit object storage on every GET/SSE tick.
_remote_live_cache: tuple[float, dict | None] | None = None
_remote_live_lock = Lock()

# Lock to prevent concurrent start requests.
_start_lock: asyncio.Lock = asyncio.Lock()

# lru_cache may compute the same missing key concurrently. Fixture parsing is
# memory-heavy, so serialize cold loads and let later callers hit the cache.
_engine_load_lock = Lock()
_ping_lock = Lock()
_ping_stats: dict[str, Any] = {
    "requests": 0,
    "last_seen": None,
    "previous_interval_seconds": None,
}


@lru_cache(maxsize=SESSION_CACHE_SIZE)
def _radio_worker():
    """Lazy singleton: whisper model loads only when live radio actually shows up."""
    from racelens.radio.transcribe import TranscriptWorker

    return TranscriptWorker()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    if _live is not None:
        _live.stop()
    if _capture is not None:
        _capture.stop()


app = FastAPI(
    title="Race Lens",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None if READONLY else "/docs",
    redoc_url=None if READONLY else "/redoc",
    openapi_url=None if READONLY else "/openapi.json",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://race-lens.onrender.com",
        "http://tauri.localhost",
        "tauri://localhost",
        "http://localhost:5173",
    ],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Accept", "Content-Type"],
)


class _LeasedFileResponse(FileResponse):
    def __init__(self, *args, lease: AbstractContextManager[Path], **kwargs) -> None:
        self._lease = lease
        super().__init__(*args, **kwargs)

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._lease.__exit__(None, None, None)


@app.middleware("http")
async def add_security_headers(
    request: Request, call_next: RequestResponseEndpoint
) -> Response:
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    return response


def _require_writable() -> None:
    if READONLY:
        raise HTTPException(403, "This deployment is read-only")


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    if not slug:
        raise HTTPException(422, "country and session must contain letters or digits")
    return slug


@lru_cache(maxsize=1)
def _object_store() -> S3Store | None:
    return S3Store(STORAGE_CONFIG) if STORAGE_CONFIG is not None else None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _remote_live() -> dict | None:
    global _remote_live_cache
    # One refresh serves concurrent status/feed/SSE readers of the current Live.
    with _remote_live_lock:
        now = time.monotonic()
        if _remote_live_cache is not None and now - _remote_live_cache[0] < 2:
            return _remote_live_cache[1]
        try:
            store = _object_store()
            value = load_live(store, now=_utcnow()) if store is not None else None
        except LiveRecordError as exc:
            raise HTTPException(502, "Current live data is invalid") from exc
        except StorageError as exc:
            raise HTTPException(503, "Live storage is temporarily unavailable") from exc
        _remote_live_cache = (now, value)
        return value


def _remote_live_required(*, snapshot: bool = True) -> dict:
    value = _remote_live()
    if value is None or snapshot and value["snapshot"] is None:
        raise HTTPException(404, "No live session active")
    return value


def _remote_live_status(value: dict) -> dict[str, Any]:
    pointer = value["pointer"]
    snapshot = value["snapshot"]
    sequence = snapshot["sequence"] if snapshot is not None else 0
    state = snapshot["race_state"] if snapshot is not None else {}
    freshness = snapshot["capture_freshness"] if snapshot is not None else None
    quality = snapshot["data_quality"] if snapshot is not None else "stalled"
    generated = snapshot["generated_at"] if snapshot is not None else pointer["updated_at"]
    return {
        "polls": sequence,
        "events_total": state.get("data_quality", {}).get("events_applied", 0),
        "new_last_poll": 0,
        "consecutive_failures": 0,
        "last_poll_unix": datetime.fromisoformat(generated.replace("Z", "+00:00")).timestamp(),
        "last_new_event_unix": (
            datetime.fromisoformat(freshness["raw_updated_at"].replace("Z", "+00:00")).timestamp()
            if freshness else None
        ),
        "last_error": pointer["failure"],
        "data_quality": quality,
        "last_poll_ok": quality != "stalled",
        "is_running": pointer["status"] == "live",
        "poll_count": sequence,
        "source": "remote",
        "status": pointer["status"],
        "canonical_session_id": pointer["canonical_session_id"],
        "replay_session_id": pointer["replay_session_id"],
        "generated_at": generated,
        "expires_at": snapshot["expires_at"] if snapshot is not None else None,
        "capture_freshness": freshness,
        "failure": pointer["failure"],
    }


def _idle_live_status() -> dict[str, Any]:
    """Honest 'no live session' body for /api/live/status.

    Reaching this means storage is readable and simply holds no live pointer,
    which must stay distinct from a storage failure (503).
    """
    return {
        "status": "idle",
        "source": "remote" if STORAGE_CONFIG is not None else "none",
        "is_running": False,
        "poll_count": 0,
        "events_total": 0,
        "last_poll_ok": False,
        "last_poll_unix": None,
        "last_new_event_unix": None,
        "last_error": None,
        "data_quality": "stalled",
        "generated_at": None,
        "expires_at": None,
        "capture_freshness": None,
        "failure": None,
    }


@lru_cache(maxsize=1)
def _object_queue() -> ObjectPreparationQueue | None:
    store = _object_store()
    return (
        ObjectPreparationQueue(
            store,
            max_jobs=PREPARATION_QUEUE_MAX,
            daily_max=PREPARATION_DAILY_MAX,
        )
        if store is not None
        else None
    )


def _preparation_queue() -> PreparationQueue | ObjectPreparationQueue:
    return _object_queue() or PreparationQueue(
        PREPARATION_QUEUE_DIR, PREPARATION_QUEUE_MAX,
    )


def _preparation_enabled() -> bool:
    return STORAGE_CONFIG is not None or not READONLY


def _local_ready(session_id: str, record: dict) -> dict | None:
    """Return the ready job when the record's replay already exists on disk.

    A completed file beats stale queue state: the worker can crash after
    writing the archive but before marking the record ready.
    """
    seen: set[str] = set()
    for replay_id in (record.get("replay_session_id"), record.get("fixture_stem")):
        if not replay_id or replay_id in seen:
            continue
        seen.add(replay_id)
        path = FIXTURES_DIR / f"{replay_id}.jsonl"
        if path.is_file():
            return ready_job(session_id, replay_id, path)
    return None


def _preparation_response(session_id: str, record: dict) -> JSONResponse:
    """Envelope shared by fresh enqueues and existing active/ready records."""
    status_code = 202 if record["status"] in {"queued", "processing", "running"} else 200
    headers = {"Location": f"/api/preparations/{session_id}"}
    if status_code == 202:
        headers["Retry-After"] = "3"
    return JSONResponse(public_job(record), status_code=status_code, headers=headers)


def _catalog(season: int) -> dict:
    try:
        return build_catalog(
            season,
            FIXTURES_DIR,
            _preparation_queue(),
            CATALOG_CACHE_DIR,
            preparation_enabled=_preparation_enabled(),
        )
    except StorageError as exc:
        raise HTTPException(
            503, "Archive preparation storage is temporarily unavailable",
        ) from exc


@lru_cache(maxsize=1)
def _remote_cache() -> RemoteSessionCache | None:
    store = _object_store()
    return (
        RemoteSessionCache(store, REMOTE_CACHE_DIR, max_bytes=REMOTE_CACHE_MAX)
        if store is not None
        else None
    )


@contextmanager
def _fixture_lease(session_id: str, suffix: str = ".jsonl") -> Iterator[Path]:
    """Yield a local root or hold a remote root until the current read finishes."""
    if (FIXTURES_DIR / f"{session_id}.jsonl").is_file():
        yield FIXTURES_DIR
        return
    local_component = (FIXTURES_DIR / f"{session_id}{suffix}").is_file()
    cache = _remote_cache()
    if cache is None or not REPLAY_ID.fullmatch(session_id):
        if local_component:
            yield FIXTURES_DIR
            return
        raise HTTPException(404, f"session '{session_id}' not found")
    lease = cache.lease(session_id)
    try:
        root = lease.__enter__()
    except ManifestError as exc:
        if local_component:
            yield FIXTURES_DIR
            return
        raise HTTPException(404, f"session '{session_id}' is not ready") from exc
    except StorageError as exc:
        if local_component:
            yield FIXTURES_DIR
            return
        raise HTTPException(503, "Replay storage is temporarily unavailable") from exc
    try:
        yield root
    finally:
        lease.__exit__(None, None, None)


# Formation-lap lead. Race events are shifted forward by this so the formation
# lap occupies display time [0, LIGHTS_OUT_MS) and lights-out = LIGHTS_OUT_MS.
# Keeps the whole timeline non-negative. Must match PRE_START_MS (positions/track.py) and
# LEAD_MS (rust race-core) so the map telemetry and events line up at lights-out.
LIGHTS_OUT_MS = 180_000


@lru_cache(maxsize=SESSION_CACHE_SIZE)
def _engine_cached(session_id: str, fixtures_dir: str, mtime: float) -> ReplayEngine:
    # mtime is part of the cache key: regenerating a fixture on disk must not
    # keep serving the stale engine (bit us when re-ingesting live recordings).
    fixtures_dir_path = Path(fixtures_dir)
    path = fixtures_dir_path / f"{session_id}.jsonl"
    if not path.is_file():
        raise HTTPException(404, f"session '{session_id}' not found")
    events = load_jsonl(path.read_text(encoding="utf-8"))
    # Only sessions with formation-lap telemetry (a positions.json) get the lead
    # shift; others keep lights-out at 0 (no empty pre-roll).
    if (fixtures_dir_path / f"{session_id}.positions.json").is_file():
        for e in events:
            e.session_time_ms += LIGHTS_OUT_MS
    return ReplayEngine(events)


def _engine(session_id: str) -> ReplayEngine:
    """Thin wrapper so the cache key includes FIXTURES_DIR + file mtime.

    Keeps monkeypatched FIXTURES_DIR (tests) from colliding with real cache
    entries, and picks up regenerated fixture files without a restart.
    """
    with _fixture_lease(session_id) as fixtures_dir:
        path = fixtures_dir / f"{session_id}.jsonl"
        mtime = path.stat().st_mtime if path.is_file() else 0.0
        with _engine_load_lock:
            return _engine_cached(session_id, str(fixtures_dir), mtime)


@lru_cache(maxsize=1)  # One parsed payload keeps Render's 512 MB instance bounded.
def _positions_data_cached(session_id: str, fixtures_dir: str, mtime: float) -> dict | None:
    path = Path(fixtures_dir) / f"{session_id}.positions.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _positions_data(session_id: str) -> dict | None:
    """Thin wrapper so the cache key includes FIXTURES_DIR (see _engine)."""
    with _fixture_lease(session_id, ".positions.json") as fixtures_dir:
        path = fixtures_dir / f"{session_id}.positions.json"
        mtime = path.stat().st_mtime if path.is_file() else 0.0
        with _engine_load_lock:
            return _positions_data_cached(session_id, str(fixtures_dir), mtime)


def _positions_window(pos: dict, at_ms: int) -> dict:
    """Return 30 seconds behind and 120 seconds ahead of the requested frame."""
    tick_ms = max(1, int(pos.get("tick_ms") or 500))
    start_ms = int(pos.get("start_ms") or 0)
    drivers = pos.get("drivers") or {}
    frame_count = max((len(frames) for frames in drivers.values()), default=0)
    first = min(frame_count, max(0, (at_ms - 30_000 - start_ms) // tick_ms))
    last = min(frame_count, max(first, (at_ms + 120_000 - start_ms) // tick_ms + 1))
    result = {
        key: pos[key]
        for key in ("session_id", "tick_ms", "viewbox")
        if key in pos
    }
    result["start_ms"] = start_ms + first * tick_ms
    result["drivers"] = {
        driver: frames[first:last] for driver, frames in drivers.items()
    }
    progress = pos.get("progress")
    if isinstance(progress, dict):
        result["progress"] = {
            driver: frames[first:last] for driver, frames in progress.items()
        }
    return result


def _attach_frame(state: dict, session_id: str | None = None) -> dict:
    """Merge per-tick track telemetry into the driver frame in place.

    Single source of x/y+progress: the resampled positions.json (replay). rank
    comes from the engine's official classification (stable, correct race order);
    telemetry `progress` is too noisy to rank by (it can throw a P2 car to P7).
    Live mode (no positions.json) leaves x/y null and the map dead-reckons.

    `session_id` is the fixture stem (used to find positions.json) — NOT
    state["session_id"], which is the internal event session id and differs.
    """
    pos = _positions_data(session_id) if session_id else None
    if pos is None:
        for d in state["drivers"].values():
            d.setdefault("x", None)
            d.setdefault("y", None)
            d.setdefault("progress", None)
        state["frame_source"] = "live"
        state["viewbox"] = None
        return state

    tick_ms = int(pos.get("tick_ms") or 500)
    start_ms = int(pos.get("start_ms") or 0)
    # Both axes are DISPLAY time (events lead-shifted by LIGHTS_OUT_MS, grid already
    # display-time), so no re-shift here — just index the tick.
    tick = round((int(state["at_ms"]) - start_ms) / tick_ms)
    xy = pos.get("drivers", {})
    prog = pos.get("progress", {})
    for drv, d in state["drivers"].items():
        frames = xy.get(drv)
        pt = frames[tick] if frames and 0 <= tick < len(frames) else None
        d["x"], d["y"] = (pt[0], pt[1]) if pt else (None, None)
        pframes = prog.get(drv)
        d["progress"] = pframes[tick] if pframes and 0 <= tick < len(pframes) else None
    state["frame_source"] = "replay"
    state["viewbox"] = pos.get("viewbox", [600, 400])
    return state


# Overtake-attention window: a pass stays "recent" (flashed on the map) for
# this many ms of session time after it happened.
RECENT_PASS_WINDOW_MS = 20_000
POST_RACE_RADIO_WINDOW_MS = 5 * 60_000


def _recent_passes(passes: list[Pass], cur_ms: int, window_ms: int = RECENT_PASS_WINDOW_MS) -> list[dict]:
    """Passes with at_ms in (cur_ms - window_ms, cur_ms] as plain dicts for the frame."""
    lo = cur_ms - window_ms
    return [
        {"ahead": p.ahead, "behind": p.behind, "kind": p.kind, "at_ms": p.at_ms}
        for p in passes
        if lo < p.at_ms <= cur_ms
    ]


def _race_end_ms(eng: ReplayEngine) -> int:
    """End at the chequered flag plus a short, source-backed radio cooldown.

    Late steward messages stay outside the scrubber, while immediate post-race
    team radio remains playable.
    """
    finished = [
        e.session_time_ms for e in eng.events
        if e.type == "SessionStatusChanged" and e.payload.get("status") == "finished"
    ]
    if finished:
        finish = max(finished)
        radio = [
            e.session_time_ms for e in eng.events
            if e.type == "RaceControlMessage"
            and e.payload.get("category") == "Radio"
            and any(e.payload.get(key) for key in ("audio_url", "audio_path", "transcript"))
            and finish < e.session_time_ms <= finish + POST_RACE_RADIO_WINDOW_MS
        ]
        return max([finish, *radio])
    laps = [e.session_time_ms for e in eng.events if e.type == "LapCompleted"]
    if laps:
        return max(laps)
    return eng.events[-1].session_time_ms if eng.events else 0

def _clamp_at_ms(at_ms: int) -> int:
    """Negative at_ms = formation lap (telemetry-only, before lights-out).

    There are no events yet at negative timestamps, so callers clamp to 0
    when querying engine state, while the response still echoes the raw
    requested at_ms so the frontend scrubber playhead stays correct.
    """
    return max(0, at_ms)


def _revision() -> str:
    value = os.environ.get("RACELENS_REVISION") or os.environ.get("RENDER_GIT_COMMIT", "")
    return value if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value) else "unknown"


def _cache_stats(cache) -> dict[str, int]:
    info = cache.cache_info()
    return {
        "hits": info.hits,
        "misses": info.misses,
        "size": info.currsize,
        "max_size": info.maxsize,
    }


def _record_ping() -> None:
    now = _utcnow()
    with _ping_lock:
        previous = _ping_stats["last_seen"]
        _ping_stats["requests"] += 1
        _ping_stats["last_seen"] = now
        if isinstance(previous, datetime):
            _ping_stats["previous_interval_seconds"] = round(
                max(0.0, (now - previous).total_seconds()), 3,
            )


def _keepalive_stats() -> dict[str, Any]:
    now = _utcnow()
    with _ping_lock:
        last_seen = _ping_stats["last_seen"]
        return {
            "requests": _ping_stats["requests"],
            "last_seen": last_seen.isoformat().replace("+00:00", "Z")
            if isinstance(last_seen, datetime) else None,
            "previous_interval_seconds": _ping_stats["previous_interval_seconds"],
            "age_seconds": round(max(0.0, (now - last_seen).total_seconds()), 3)
            if isinstance(last_seen, datetime) else None,
        }


def _memory_stats() -> dict[str, int | float | None]:
    def read(path: Path) -> int | None:
        try:
            raw = path.read_text(encoding="ascii").strip()
            return None if raw == "max" else max(0, int(raw))
        except (OSError, UnicodeError, ValueError):
            return None

    current = read(_CGROUP_MEMORY_CURRENT)
    limit = read(_CGROUP_MEMORY_MAX)
    pressure = round(current * 100 / limit, 1) if current is not None and limit else None
    return {
        "current_bytes": current,
        "limit_bytes": limit,
        "pressure_percent": pressure,
    }


@app.get("/api/ping")
def ping():
    _record_ping()
    return {"status": "ok"}


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/diagnostics")
def diagnostics() -> dict:
    cache = _remote_cache()
    remote = cache.stats() if cache is not None else {
        "materializations": 0,
        "hits": 0,
        "misses": 0,
        "leases": 0,
        "evictions": 0,
        "bytes": 0,
        "disk_bytes": 0,
        "max_bytes": REMOTE_CACHE_MAX,
    }
    if _live is None:
        try:
            value = _remote_live()
        except HTTPException as exc:
            live = {
                "source": "remote",
                "freshness": (
                    "storage-unavailable" if exc.status_code == 503 else "record-invalid"
                ),
            }
        else:
            status = value["pointer"]["status"] if value is not None else None
            live = {
                "source": "remote" if STORAGE_CONFIG is not None else "none",
                "freshness": (
                    "idle" if status is None
                    else "active" if status == "live"
                    else status.replace("_", "-")
                ),
            }
    else:
        quality = _live_health_status().get("data_quality")
        freshness = {"good": "fresh", "degraded": "degraded", "stalled": "stale"}.get(
            quality, "unknown",
        )
        live = {
            "source": "signalr" if _live_session_id is not None else "openf1",
            "freshness": freshness,
        }
    return {
        "revision": _revision(),
        "parsed_cache": {
            "engine": _cache_stats(_engine_cached),
            "positions": _cache_stats(_positions_data_cached),
        },
        "remote_cache": remote,
        "memory": _memory_stats(),
        "keepalive": _keepalive_stats(),
        "live": live,
    }


@app.get("/api/capabilities")
def capabilities() -> dict:
    return {
        "readonly": READONLY,
        "signalr_available": importlib.util.find_spec("fastf1") is not None,
        "catalog_available": True,
        "preparation_enabled": _preparation_enabled(),
    }


@app.get("/api/sessions")
def list_sessions() -> list[dict]:
    out = {}
    files = sorted(
        (f for f in FIXTURES_DIR.glob("*.jsonl") if not f.stem.endswith(".positions_raw")),
        key=lambda f: (
            not (FIXTURES_DIR / f"{f.stem}.positions.json").is_file(),
            f.stem,
        ),
    )
    for f in files:
        source = "unknown"
        try:
            with f.open(encoding="utf-8") as handle:
                first = next(line for line in handle if line.strip())
            source = str(json.loads(first).get("source") or "unknown")
        except (OSError, StopIteration, json.JSONDecodeError):
            pass
        out[f.stem] = {"session_id": f.stem, "source": source}
    remote = _object_queue()
    if remote is not None:
        try:
            for record in remote.records():
                if record["status"] == "ready":
                    replay_id = record["replay_session_id"]
                    out.setdefault(
                        replay_id,
                        {"session_id": replay_id, "source": "object-storage"},
                    )
        except StorageError:
            pass
    return sorted(
        out.values(),
        key=lambda item: (
            not (
                (FIXTURES_DIR / f"{item['session_id']}.positions.json").is_file()
                or item["source"] == "object-storage"
            ),
            item["session_id"],
        ),
    )


@app.get("/api/catalog")
def catalog(
    season: Optional[int] = Query(default=None, ge=FIRST_SEASON, le=2100),
) -> dict:
    current_season = datetime.now(UTC).year
    if season is not None and season > current_season:
        raise HTTPException(422, "Season cannot be in the future")
    return _catalog(season or current_season)


@app.post("/api/catalog/{session_id}/prepare")
def prepare_replay(session_id: str):
    if not SESSION_ID.fullmatch(session_id):
        raise HTTPException(404, "Session is not in the supported catalog")
    season = int(session_id[:4])
    if season < FIRST_SEASON or season > datetime.now(UTC).year:
        raise HTTPException(404, "Session is not in the supported catalog")
    if STORAGE_CONFIG is None:
        # Preparation writes queue state, including failed retries, so a
        # read-only deployment rejects every POST regardless of existing records.
        _require_writable()
    queue = _preparation_queue()
    try:
        current = queue.get(session_id)
    except StorageError:
        # Storage outage: the catalog can still establish a ready local replay.
        current = None
    if current is not None:
        if current["status"] == "failed":
            # Retry through the queue's own failed -> queued semantics; the
            # queue is authoritative for the fixture stem of its own records.
            try:
                record, _created = queue.enqueue(session_id, current["fixture_stem"])
            except QueueFullError as exc:
                raise HTTPException(429, str(exc), headers={"Retry-After": "60"}) from exc
            except StorageError as exc:
                raise HTTPException(
                    503, "Archive preparation storage is temporarily unavailable",
                ) from exc
            return _preparation_response(session_id, record)
        ready = _local_ready(session_id, current)
        if ready is not None:
            return ready
        return _preparation_response(session_id, current)

    catalog_data = _catalog(season)
    if not catalog_data["catalog_available"]:
        raise HTTPException(503, "Session catalog is temporarily unavailable")
    match = find_catalog_session(catalog_data, session_id)
    if match is None:
        raise HTTPException(404, "Session is not in the supported catalog")
    event, session = match
    replay_id = session.get("replay_session_id") or expected_replay_id(season, event, session)
    replay_path = FIXTURES_DIR / f"{replay_id}.jsonl"
    if replay_path.is_file():
        return ready_job(session_id, replay_id, replay_path)
    try:
        record, _created = queue.enqueue(session_id, replay_id)
    except QueueFullError as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": "60"}) from exc
    except StorageError as exc:
        raise HTTPException(
            503, "Archive preparation storage is temporarily unavailable",
        ) from exc
    return _preparation_response(session_id, record)


@app.get("/api/preparations/{session_id}")
def preparation_status(session_id: str) -> dict:
    if not SESSION_ID.fullmatch(session_id):
        raise HTTPException(404, "Preparation not found")
    season = int(session_id[:4])
    if season < FIRST_SEASON or season > datetime.now(UTC).year:
        raise HTTPException(404, "Preparation not found")
    queue = _preparation_queue()
    try:
        record = queue.get(session_id)
    except StorageError:
        # Storage outage: the catalog can still establish a ready local replay.
        record = None
    if record is not None:
        if record["status"] != "failed":
            ready = _local_ready(session_id, record)
            if ready is not None:
                return ready
        return public_job(record)

    catalog_data = _catalog(season)
    if not catalog_data["catalog_available"]:
        raise HTTPException(503, "Session catalog is temporarily unavailable")
    match = find_catalog_session(catalog_data, session_id)
    if match is None:
        raise HTTPException(404, "Preparation not found")
    event, session = match
    replay_id = session.get("replay_session_id") or expected_replay_id(season, event, session)
    replay_path = FIXTURES_DIR / f"{replay_id}.jsonl"
    if replay_path.is_file():
        return ready_job(session_id, replay_id, replay_path)
    raise HTTPException(404, "Preparation not found")


@app.get("/api/sessions/{session_id}/state")
def state(session_id: str, at_ms: int = Query()) -> dict:
    # Negative at_ms = formation lap (telemetry-only, before lights-out). There are
    # no events yet, so show the grid (state at 0) but echo the requested time so
    # the scrubber playhead stays in the pre-start window.
    result = _engine(session_id).state_at(_clamp_at_ms(at_ms))
    result["at_ms"] = at_ms
    return _attach_frame(result, session_id)


@app.get("/api/sessions/{session_id}/insights")
def insights(session_id: str, at_ms: int = Query()) -> dict:
    """Active insights at a timestamp, computed from state <= at_ms only."""
    state = _engine(session_id).state_at(_clamp_at_ms(at_ms))
    return {"at_ms": at_ms, "insights": detect_all(state)}


@app.get("/api/sessions/{session_id}/battles")
def battles(session_id: str, at_ms: int = Query()) -> dict:
    """On-track battles at a timestamp: pairs of neighbouring drivers in a real fight."""
    state = _engine(session_id).state_at(_clamp_at_ms(at_ms))
    return {"at_ms": at_ms, "battles": detect_battles(state)}


@app.get("/api/sessions/{session_id}/commentary")
def commentary(session_id: str, at_ms: int = Query(), lang: str = "en", level: str = "pro") -> dict:
    """Active insights rendered as text. lang: en|ru, level: beginner|pro."""
    state = _engine(session_id).state_at(_clamp_at_ms(at_ms))
    return {"at_ms": at_ms, "items": render_all(detect_all(state), lang, level)}


@app.get("/api/sessions/{session_id}/stream")
async def stream(
    session_id: str,
    speed: float = Query(default=10.0, ge=0.1, le=100.0),
    from_ms: int = 0,
    tick_ms: int = Query(default=1000, ge=250, le=10_000),
    lang: str = "en", level: str = "pro",
) -> StreamingResponse:
    """Simulated live: replay the session as an SSE stream of states.

    One message per `tick_ms` of session time, paced at `speed`x real time.
    Each message carries full state + active insights, so the frontend
    treats replay and live identically.
    """
    eng = _engine(session_id)
    end_ms = _race_end_ms(eng)
    # Fixed event list for a replay connection — compute passes once, filter per tick.
    passes = detect_passes(eng.events)

    async def gen():
        t = from_ms
        while True:
            cur = min(t, end_ms)  # always emit the final state exactly at end_ms
            state = _attach_frame(eng.state_at(cur), session_id)
            state["active_insights"] = detect_all(state)
            state["commentary"] = render_all(state["active_insights"], lang, level)
            state["recent_passes"] = _recent_passes(passes, cur)
            yield f"data: {json.dumps(state)}\n\n"
            if cur >= end_ms:
                break
            await asyncio.sleep(tick_ms / 1000.0 / speed)
            t += tick_ms
        yield "event: end\ndata: {}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Near-live endpoints ───────────────────────────────────────────────────────


@app.post("/api/live/start")
async def live_start(
    year: int = Query(..., ge=1950, le=2100),
    country: str = Query(..., min_length=1, max_length=80),
    session: str = Query(default="Race", min_length=1, max_length=40),
    poll_s: float = Query(default=12.0, ge=6.0, le=60.0),
    source: str = Query(default="openf1", pattern="^(openf1|signalr)$"),
    auth: int = Query(default=0),
) -> dict:
    """Start the live pipeline.

    source=openf1: find the OpenF1 session and poll it (needs realtime tier
    for actually-live data). Works for historical sessions too.
    source=signalr: record the FREE official F1 SignalR feed via a capture
    subprocess and re-ingest the growing recording each poll. `country` is the
    FastF1 Grand Prix name here (e.g. "Silverstone"). The recording survives
    at fixtures/_capture_*.txt — post-race `racelens ingest-live` turns it
    into a replay fixture even if live mode misbehaves.
    """
    global _live, _capture, _live_session_id
    _require_writable()
    async with _start_lock:
        if _live is not None and _live.is_running:
            raise HTTPException(409, "A live session is already running. Stop it first.")

        # Reset any sid left over from a prior session (belt-and-braces —
        # normally cleared by live_stop already).
        _live_session_id = None

        if source == "signalr":
            if importlib.util.find_spec("fastf1") is None:
                raise HTTPException(503, "SignalR live capture requires the fastf1 extra")
            feed_path = FIXTURES_DIR / f"_capture_{year}_{_safe_slug(country)}_{_safe_slug(session)}.txt"
            # Default no_auth: anonymous feed carries full timing (verified live).
            # auth=1 uses the fastf1 token cache (scripts/f1_login.py).
            _capture = SignalRCapture(feed_path, no_auth=not auth)
            try:
                _capture.start()
            except (OSError, RuntimeError) as exc:
                _capture = None
                logger.warning("SignalR capture failed to start: %s", type(exc).__name__)
                raise HTTPException(503, "Could not start SignalR capture") from exc
            fetch = make_signalr_fetch(feed_path, year, country, session)
            _live = LiveRunner(fetch, poll_interval_s=max(poll_s, 5.0))
            # Same formula as make_signalr_fetch's internal session_id (signalr.py).
            _live_session_id = f"{country}_{year}_{session}".lower().replace(" ", "_")
            await _live.start()
            return {
                "source": "signalr", "feed_file": str(feed_path),
                "poll_interval_s": max(poll_s, 5.0), "status": "started",
            }

        try:
            session_key = await asyncio.to_thread(find_session, year, country, session)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                503, "OpenF1 session lookup is temporarily unavailable",
            ) from exc

        ingester = OpenF1IncrementalIngester(session_key)
        _live = LiveRunner(ingester.fetch, poll_interval_s=poll_s)
        await _live.start()
    return {"session_key": session_key, "poll_interval_s": poll_s, "status": "started"}


def _reap_capture_if_stopped() -> None:
    """Stop the capture subprocess once the runner has self-stopped.

    Normal `/api/live/stop` already tears down both `_live` and `_capture`
    together — this covers the runner stopping ITSELF (auto-stop 5 min after
    session finish; see LiveRunner._loop), which has no other trigger to also
    kill the now-orphaned capture subprocess. Called from the two places the
    frontend polls the runner's liveness (`live_status`, `live_stream`).

    Gated on `auto_stopped` (not `not is_running`) — `is_running` can be
    False for unrelated reasons (not started yet, externally cancelled) and
    must not be conflated with the runner's own decision to stop.
    """
    global _capture
    if _live is not None and _live.auto_stopped and _capture is not None:
        _capture.stop()
        _capture = None


def _live_health_status() -> dict[str, Any]:
    """Apply source-specific liveness once for public status and diagnostics."""
    assert _live is not None
    _reap_capture_if_stopped()
    status = _live.status()
    status["last_poll_ok"] = status["consecutive_failures"] == 0
    if _live_session_id is not None and _capture is not None:
        status["capture_alive"] = _capture.alive
        if not _capture.alive:
            status["data_quality"] = "stalled"
            status["last_poll_ok"] = False
    return status


@app.get("/api/live/status")
async def live_status() -> dict:
    if _live is not None:
        status = _live_health_status()
        # Fields the frontend contract expects (LiveStatusResult).
        status["is_running"] = _live.is_running
        status["poll_count"] = status["polls"]
        return status

    def remote_or_idle() -> dict:
        try:
            remote = _remote_live_required(snapshot=False)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            # 404 here means no live session (idle), not storage failure.
            return _idle_live_status()
        return _remote_live_status(remote)

    return await asyncio.to_thread(remote_or_idle)


@app.get("/api/live/stream")
async def live_stream(
    tick_s: float = Query(default=2.0, ge=0.5, le=30.0),
    lang: str = "en",
    level: str = "pro",
) -> StreamingResponse:
    """SSE stream: emits state_now() every *tick_s* seconds."""
    if _live is None:
        await asyncio.to_thread(_remote_live_required, snapshot=False)

    async def gen():
        # A corrected authoritative snapshot can replace events without growing.
        passes_engine = None
        passes = []
        while True:
            runner = _live
            if runner is None:
                try:
                    current = await asyncio.to_thread(
                        _remote_live_required, snapshot=False,
                    )
                except HTTPException as exc:
                    # A storage/staleness failure is not a race lifecycle event.
                    # Close so EventSource reconnects, but log the outage.
                    logger.warning("live stream remote fetch failed: %s", exc.status_code)
                    return
                pointer = current["pointer"]
                snapshot = current["snapshot"]
                if pointer["status"] in {"finishing", "replay_ready", "failed"}:
                    yield "event: end\ndata: {}\n\n"
                    return
                if snapshot is None:
                    yield "data: {}\n\n"
                else:
                    state = dict(snapshot["race_state"])
                    state["active_insights"] = snapshot["active_insights"]
                    state["commentary"] = snapshot["commentary"].get(
                        lang, snapshot["commentary"]["en"]
                    ).get(level, snapshot["commentary"]["en"]["pro"])
                    state["recent_passes"] = snapshot["recent_passes"]
                    state["battles"] = snapshot["battles"]
                    state["stints"] = snapshot.get("stints")
                    state["live_status"] = _remote_live_status(current)
                    yield f"data: {json.dumps(state)}\n\n"
                await asyncio.sleep(tick_s)
                continue
            if not runner.is_running:
                # Runner stopped (explicitly or via auto-stop) — reap the capture
                # subprocess if it's still around, signal end, close the stream.
                _reap_capture_if_stopped()
                yield "event: end\ndata: {}\n\n"
                return
            engine = runner.engine
            if engine is None or not engine.events:
                # Runner alive but no data yet — send empty heartbeat and keep waiting.
                yield "data: {}\n\n"
            else:
                events = engine.events
                if engine is not passes_engine:
                    passes = detect_passes(events)
                    passes_engine = engine
                state = _attach_frame(engine.state_at(events[-1].session_time_ms))
                state["live_status"] = runner.status()
                state["active_insights"] = detect_all(state)
                state["commentary"] = render_all(state["active_insights"], lang, level)
                state["recent_passes"] = _recent_passes(passes, state["at_ms"])
                state["battles"] = detect_battles(state)
                total_laps = state.get("total_laps") or state.get("lap") or 0
                state["stints"] = {
                    "session_id": state["session_id"],
                    "total_laps": total_laps,
                    "stints": stint_timeline(events, total_laps),
                }
                yield f"data: {json.dumps(state)}\n\n"
            await asyncio.sleep(tick_s)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/live/feed")
async def live_feed(
    lang: str = "en",
    limit: int = Query(default=30, ge=1, le=100),
) -> list:
    """Event feed for the frontend during live mode (no session_id to scope by)."""
    runner = _live
    if runner is None:
        snapshot = (await asyncio.to_thread(_remote_live_required))["snapshot"]
        return snapshot["feed"].get(lang, snapshot["feed"]["en"])[:limit]
    engine = runner.engine
    if engine is None or not engine.events:
        raise HTTPException(404, "No live session active or no data yet")
    until_ms = engine.events[-1].session_time_ms
    items = render_feed(engine.events, until_ms, lang=lang, limit=limit)
    # Whisper transcripts: queued on first sight, mixed in once the background
    # worker is done (text appears in the feed ~a minute after the clip).
    for item in items:
        url = item.get("audio_url")
        if url and not item.get("transcript"):
            text = _radio_worker().get(url)
            if text:
                item["transcript"] = text
    return items


@app.post("/api/live/stop")
async def live_stop() -> dict:
    global _live, _capture, _live_session_id
    _require_writable()
    if _live is None:
        raise HTTPException(404, "No live session active")
    final_status = _live.status()
    _live.stop()
    _live = None
    _live_session_id = None
    if _capture is not None:
        _capture.stop()
        _capture = None
    return final_status


# ── Live mirrors of the predictive endpoints ──────────────────────────────────
#
# Same pure functions as the replay /sessions/{id}/... endpoints, fed by
# _live.state_now() instead of engine.state_at(). _live_session_id (set at
# live/start for source=signalr) stands in for the replay sid — used only for
# track-params lookup (win_probability doesn't use it at all).


@app.get("/api/live/forecast")
async def live_forecast(laps: int = Query(default=10, ge=1, le=50)) -> dict:
    if _live is None:
        state = (await asyncio.to_thread(_remote_live_required))["snapshot"]["race_state"]
    else:
        state = _live.state_now()
        if "error" in state:
            raise HTTPException(404, "No live session active or no data yet")
    return project_order(state, laps_ahead=laps)


@app.get("/api/live/win-prob")
async def live_win_prob() -> dict:
    if _live is None:
        remote = await asyncio.to_thread(_remote_live_required)
        state = remote["snapshot"]["race_state"]
        session_id = remote["pointer"]["replay_session_id"]
    else:
        state = _live.state_now()
        if "error" in state:
            raise HTTPException(404, "No live session active or no data yet")
        session_id = _live_session_id or ""
    return win_probability(state, session_id)


@app.get("/api/live/battles")
async def live_battles() -> dict:
    if _live is None:
        snapshot = (await asyncio.to_thread(_remote_live_required))["snapshot"]
        return {"at_ms": snapshot["race_state"]["at_ms"], "battles": snapshot["battles"]}
    state = _live.state_now()
    if "error" in state:
        raise HTTPException(404, "No live session active or no data yet")
    return {"at_ms": state["at_ms"], "battles": detect_battles(state)}


@app.get("/api/live/simulate-pit")
async def live_simulate_pit(driver: str = Query(...)) -> dict:
    if _live is None:
        remote = await asyncio.to_thread(_remote_live_required)
        state = remote["snapshot"]["race_state"]
        session_id = remote["pointer"]["replay_session_id"]
    else:
        state = _live.state_now()
        if "error" in state:
            raise HTTPException(404, "No live session active or no data yet")
        session_id = _live_session_id or ""
    return simulate_pit(state, driver, session_id)


# ── Static frontend (single-container deploys: HF Spaces, VPS) ────────────────
# Mounted at "/" AFTER all API routes, so /api/* keeps precedence. In dev the
# vite server proxies instead and this mount simply never engages (no dist).
_DIST = Path(
    os.environ.get("RACELENS_DIST")
    or Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
)


@app.get("/api/live/sessions")
def live_sessions(
    year: int = Query(..., ge=1950, le=2100),
    country: Optional[str] = Query(default=None),
) -> list[dict]:
    """List sessions for a given year (and optional country) from OpenF1.

    Returns a list sorted by date_start.  Each item includes a `started` flag
    that is True when the current UTC time is >= date_start.
    """
    import urllib.error

    _require_writable()

    try:
        return _openf1_list_sessions(year, country)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        raise HTTPException(502, "OpenF1 unavailable") from exc


@app.get("/api/sessions/{session_id}/win-prob")
def win_prob(
    session_id: str,
    at_ms: int = Query(),
):
    """Uncalibrated gap-pressure score for every driver at a timestamp."""
    engine = _engine(session_id)
    state = engine.state_at(_clamp_at_ms(at_ms))
    return win_probability(state, session_id)


@app.get("/api/sessions/{session_id}/win-prob-series")
def win_prob_series(
    session_id: str,
    until_ms: int = Query(ge=0),
    samples: int = Query(default=20, ge=2, le=100),
):
    """Gap-pressure score series up to until_ms in N evenly-spaced samples.

    Returns [{at_ms, probs: {driver: prob}}] ascending by at_ms.
    """
    engine = _engine(session_id)
    start_ms = engine.events[0].session_time_ms if engine.events else 0
    if until_ms <= start_ms:
        return []

    step = max(1, (until_ms - start_ms) // (samples - 1))
    points = list(range(start_ms, until_ms, step))
    if not points or points[-1] < until_ms:
        points.append(until_ms)
    # Deduplicate and cap
    points = sorted(set(points))

    result = []
    for t in points:
        state = engine.state_at(t)
        wp = win_probability(state, session_id)
        result.append({"at_ms": t, "probs": wp["win_prob"]})
    return result


@app.get("/api/sessions/{session_id}/forecast")
def forecast(
    session_id: str,
    at_ms: int = Query(),
    laps: int = Query(default=10, ge=1, le=50),
):
    engine = _engine(session_id)
    state = engine.state_at(_clamp_at_ms(at_ms))
    return project_order(state, laps_ahead=laps)


@app.get("/api/sessions/{session_id}/simulate-pit")
def simulate_pit_endpoint(
    session_id: str,
    at_ms: int = Query(),
    driver: str = Query(...),
):
    engine = _engine(session_id)
    state = engine.state_at(_clamp_at_ms(at_ms))
    return simulate_pit(state, driver, session_id)


@app.get("/api/sessions/{session_id}/what-if")
def what_if_endpoint(
    session_id: str,
    at_ms: int = Query(),
    scenario: str = Query(...),
    driver: Optional[str] = Query(default=None),
):
    """Uncalibrated strategy-sensitivity comparison.

    scenario: baseline | pit_now | stay_out
    driver: required for pit_now / stay_out
    """
    if scenario not in VALID_SCENARIOS:
        raise HTTPException(422, f"Invalid scenario '{scenario}'. Valid: {sorted(VALID_SCENARIOS)}")
    if scenario in {"pit_now", "stay_out"} and not driver:
        raise HTTPException(400, f"scenario='{scenario}' requires driver parameter")
    engine = _engine(session_id)
    state = engine.state_at(_clamp_at_ms(at_ms))
    try:
        return what_if(state, session_id, scenario, driver)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/sessions/{session_id}/overtake")
def overtake_endpoint(
    session_id: str,
    at_ms: int = Query(),
    ahead: str = Query(...),
    behind: str = Query(...),
):
    engine = _engine(session_id)
    state = engine.state_at(_clamp_at_ms(at_ms))
    return overtake_probability(state, ahead, behind, session_id)


def _track_data(session_id: str) -> dict:
    """Return pre-computed track outline as {session_id, viewbox, points}."""
    with _fixture_lease(session_id, ".track.json") as root:
        path = root / f"{session_id}.track.json"
        if not path.is_file():
            raise HTTPException(404, f"track data for '{session_id}' not found")
        return json.loads(path.read_text(encoding="utf-8"))


@app.get("/api/sessions/{session_id}/track")
def track(session_id: str) -> dict:
    return _track_data(session_id)


@app.get("/api/live/track")
def live_track() -> dict:
    pointer = _remote_live_required(snapshot=False)["pointer"]
    replay_session_id = pointer.get("track_replay_session_id")
    if not replay_session_id:
        raise HTTPException(404, "Track data is not ready for this Live weekend")
    return _track_data(replay_session_id)


@app.get("/api/live/stints")
def live_stints() -> dict:
    runner = _live
    if runner is not None:
        engine = runner.engine
        if engine is None or not engine.events:
            raise HTTPException(404, "No live session active or no data yet")
        state = engine.state_at(engine.events[-1].session_time_ms)
        total_laps = state.get("total_laps") or state.get("lap") or 0
        return {
            "session_id": state["session_id"],
            "total_laps": total_laps,
            "stints": stint_timeline(engine.events, total_laps),
        }
    snapshot = _remote_live_required()["snapshot"]
    if "stints" not in snapshot:
        raise HTTPException(404, "Live tyre strategy is not available yet")
    return snapshot["stints"]


@app.get("/api/sessions/{session_id}/positions")
def positions(
    session_id: str,
    at_ms: Optional[int] = Query(default=None, ge=0),
) -> Any:
    """Return resampled car positions from Rust race-core pipeline.

    Format: {session_id, start_ms, tick_ms, viewbox, drivers: {DRV: [[x,y]|null, ...]}}
    404 if positions.json has not been generated yet.
    """
    if at_ms is not None:
        with _fixture_lease(session_id, ".positions.json") as root:
            path = root / f"{session_id}.positions.json"
            if not path.is_file():
                raise HTTPException(404, f"positions data for '{session_id}' not found — run the pipeline")
            pos = _positions_data(session_id)
            if pos is None:
                raise HTTPException(404, f"positions data for '{session_id}' not found")
            return _positions_window(pos, at_ms)
    lease = _fixture_lease(session_id, ".positions.json")
    root = lease.__enter__()
    try:
        path = root / f"{session_id}.positions.json"
        if not path.is_file():
            raise HTTPException(404, f"positions data for '{session_id}' not found — run the pipeline")
        return _LeasedFileResponse(path, lease=lease, media_type="application/json")
    except Exception:
        lease.__exit__(None, None, None)
        raise


@app.get("/api/sessions/{session_id}/feed")
def feed(
    session_id: str,
    until_ms: int = Query(),
    lang: str = "en",
    limit: int = Query(default=30, ge=1, le=100),
) -> list:
    """Event feed for the frontend: spoiler-free, newest-first human-readable items."""
    eng = _engine(session_id)
    return render_feed(eng.events, max(0, until_ms), lang=lang, limit=limit)


@app.get("/api/sessions/{session_id}/markers")
def markers(
    session_id: str,
    until_ms: Optional[int] = Query(default=None),
) -> dict:
    """Significant race events for timeline markers and highlight summaries.

    Spoiler-free: pass until_ms to restrict to events at or before that timestamp.
    Without until_ms the full race is returned.
    """
    eng = _engine(session_id)
    clamped = max(0, until_ms) if until_ms is not None else None
    return {"markers": significant_events(eng.events, until_ms=clamped, state_engine=eng)}


@app.get("/api/sessions/{session_id}/highlights")
def get_highlights(
    session_id: str,
    top_n: int = Query(default=8, ge=1, le=50),
    until_ms: Optional[int] = Query(default=None),
) -> dict:
    """Top-N most dramatic moments of the race, sorted chronologically.

    Suitable for a 'race in 60 seconds' highlight reel.
    """
    eng = _engine(session_id)
    visible = eng.events if until_ms is None else [e for e in eng.events if e.session_time_ms <= max(0, until_ms)]
    return {"highlights": _highlights(visible, ReplayEngine(visible), top_n=top_n) if visible else []}


@app.get("/api/sessions/{session_id}/driver-of-day")
def get_driver_of_day(session_id: str, at_ms: Optional[int] = Query(default=None)) -> dict:
    """Official fan result plus Race Lens' distinct algorithmic candidates.

    Pass at_ms for a spoiler-free provisional pick from the race so far (used when
    the panel unlocks in the final laps). Omit for the full-race result.
    """
    eng = _engine(session_id)
    cutoff = None if at_ms is None else _clamp_at_ms(at_ms)
    result = _driver_of_day(eng.events, eng, at_ms=cutoff)
    state = eng.state_at(cutoff if cutoff is not None else _race_end_ms(eng))
    official = None
    if state["session_status"] == "finished":
        drivers = {event.driver_id for event in eng.events if event.driver_id is not None}
        local_award = FIXTURES_DIR / award_key(session_id)
        record = load_official_award(
            FIXTURES_DIR, None if local_award.exists() else _object_store(), session_id, drivers,
        )
        if record is not None:
            official = {
                key: record[key]
                for key in ("driver", "percentage", "provider", "source_url", "fetched_at")
            }
    return {**result, "official_result": official}


@app.get("/api/sessions/{session_id}/timeline")
def timeline(session_id: str) -> dict:
    """Replay bounds + lap markers for the scrubber. No future-revealing
    detail beyond what a replay slider inherently needs."""
    with _fixture_lease(session_id) as root:
        eng = _engine(session_id)
        lap_marks = {}
        for e in eng.events:
            if e.type == "LapCompleted" and e.lap and e.lap not in lap_marks:
                lap_marks[e.lap] = e.session_time_ms
        start_ms = eng.events[0].session_time_ms if eng.events else 0
        # The formation lap lives only in telemetry. When a positions.json exists the
        # events are lead-shifted (see _engine), so lights-out = LIGHTS_OUT_MS and the
        # scrubber starts at the formation origin from positions.json. Without it there
        # is no formation: lights-out stays at 0.
        pos_path = root / f"{session_id}.positions.json"
        lights_out_ms = 0
        if pos_path.is_file():
            lights_out_ms = LIGHTS_OUT_MS
            try:
                pos_start = int(json.loads(pos_path.read_text(encoding="utf-8")).get("start_ms", 0))
                start_ms = min(start_ms, pos_start)
            except (ValueError, KeyError, TypeError):
                pass
        return {
            "session_id": session_id,
            "start_ms": start_ms,
            "end_ms": _race_end_ms(eng),
            "lights_out_ms": lights_out_ms,
            "events_total": len(eng.events),
            "lap_marks": lap_marks,
        }


@app.get("/api/sessions/{session_id}/stints")
def stints(session_id: str) -> dict:
    """Per-driver tyre stint timeline (compound + lap range) for the strategy view."""
    eng = _engine(session_id)
    end_ms = eng.events[-1].session_time_ms if eng.events else 0
    final = eng.state_at(end_ms)
    total_laps = final.get("total_laps") or final.get("lap") or 0
    return {
        "session_id": session_id,
        "total_laps": total_laps,
        "stints": stint_timeline(eng.events, total_laps),
    }


# The static mount lives at the bottom of the module so every API route above
# is already registered and wins the match (see _DIST comment up top).
@app.get("/pocket", include_in_schema=False)
def pocket_frontend() -> FileResponse:
    index = _DIST / "index.html"
    if not index.is_file():
        raise HTTPException(404, "Frontend is not built")
    return FileResponse(index)


if _DIST.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="frontend")
