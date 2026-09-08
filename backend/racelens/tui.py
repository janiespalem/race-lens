"""Small read-only Textual client for the public Race Lens API."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Resize
from textual.widgets import DataTable, Footer, Header, Label, ListItem, ListView, Static

MIN_COLUMNS = 100
MIN_ROWS = 28

_TEXT = {
    "en": {
        "battles": "BATTLES",
        "catalog": "CATALOG",
        "feed": "FEED",
        "live": "LIVE NOW",
        "no_battles": "No active battles",
        "no_feed": "No feed items",
        "no_track": "Track data unavailable.",
        "no_live_track": "Live track telemetry unavailable.",
        "not_ready": "Replay is not ready",
        "track": "TRACK",
        "waiting": "Waiting for timing data",
        "wtw": "WHAT TO WATCH",
    },
    "ru": {
        "battles": "БОРЬБА",
        "catalog": "КАТАЛОГ",
        "feed": "ЛЕНТА",
        "live": "ЭФИР",
        "no_battles": "Нет активной борьбы",
        "no_feed": "Лента пуста",
        "no_track": "Нет данных трассы.",
        "no_live_track": "Нет телеметрии трассы в эфире.",
        "not_ready": "Повтор ещё не готов",
        "track": "ТРАССА",
        "waiting": "Ожидание тайминга",
        "wtw": "НА ЧТО СМОТРЕТЬ",
    },
}


def resize_message(width: int, height: int, lang: str = "en") -> str | None:
    if width >= MIN_COLUMNS and height >= MIN_ROWS:
        return None
    if lang == "ru":
        return f"Увеличьте терминал до 100x28 (сейчас {width}x{height})."
    return f"Resize terminal to at least 100x28 (now {width}x{height})."


def api_path(mode: str, resource: str, session_id: str | None = None) -> str | None:
    """Map viewer modes to the existing public read-only routes."""
    if mode == "catalog":
        return {"catalog": "/api/catalog", "sessions": "/api/sessions"}.get(resource)
    if mode == "live":
        return (
            f"/api/live/{resource}"
            if resource in {"status", "stream", "battles", "feed"}
            else None
        )
    if mode != "replay" or not session_id:
        return None
    if resource not in {"state", "stream", "battles", "feed", "timeline", "track", "positions"}:
        return None
    return f"/api/sessions/{quote(session_id, safe='')}/{resource}"


def reconnect_delay(attempt: int) -> float:
    """Bound reconnect pressure while continuing to recover automatically."""
    return 0.5 * 2 ** min(4, max(0, attempt))


def status_text(
    status: dict[str, Any] | None,
    *,
    connected: bool,
    ended: bool,
    lang: str = "en",
) -> str:
    status = status or {}
    phase = status.get("status")
    expires_at = status.get("expires_at")
    try:
        expired = isinstance(expires_at, str) and datetime.fromisoformat(
            expires_at.replace("Z", "+00:00")
        ) <= datetime.now(UTC)
    except ValueError:
        expired = False
    stale = (
        status.get("data_quality") == "stalled"
        or status.get("capture_alive") is False
        or expired
    )
    if lang == "ru":
        if phase == "failed":
            return "ЭФИР ЗАВЕРШЁН · ОШИБКА ПОВТОРА"
        if phase == "replay_ready":
            return "ЭФИР ЗАВЕРШЁН · ПОВТОР ГОТОВ"
        if phase == "finishing":
            return "ЭФИР ЗАВЕРШЁН · ГОТОВИТСЯ ПОВТОР"
        if ended or status.get("is_running") is False:
            return "ЭФИР ЗАВЕРШЁН"
        if stale:
            return "ДАННЫЕ УСТАРЕЛИ" + (" · ПЕРЕПОДКЛЮЧЕНИЕ" if not connected else "")
        if status.get("data_quality") == "degraded":
            return "ДАННЫЕ ЗАДЕРЖИВАЮТСЯ" + (
                " · ПЕРЕПОДКЛЮЧЕНИЕ" if not connected else ""
            )
        return "ЭФИР" if connected else "ПЕРЕПОДКЛЮЧЕНИЕ"
    if phase == "failed":
        return "ENDED · REPLAY FAILED"
    if phase == "replay_ready":
        return "ENDED · REPLAY READY"
    if phase == "finishing":
        return "ENDED · PREPARING REPLAY"
    if ended or status.get("is_running") is False:
        return "ENDED"
    if stale:
        return "STALE" + (" · RECONNECTING" if not connected else "")
    if status.get("data_quality") == "degraded":
        return "DELAYED" if connected else "DELAYED · RECONNECTING"
    return "● LIVE" if connected else "RECONNECTING"


def render_track(
    track: dict[str, Any] | None,
    drivers: dict[str, Any],
    *,
    width: int = 36,
    height: int = 12,
    ascii_only: bool = False,
    live: bool = False,
    lang: str = "en",
) -> str:
    """Rasterize API track points and current API positions, never invented XY."""
    if live and not any(
        isinstance(driver, dict)
        and isinstance(driver.get("x"), (int, float))
        and isinstance(driver.get("y"), (int, float))
        for driver in drivers.values()
    ):
        return _TEXT[lang]["no_live_track"]
    points = track.get("points") if isinstance(track, dict) else None
    viewbox = track.get("viewbox") if isinstance(track, dict) else None
    if not isinstance(points, list) or not points or not isinstance(viewbox, list) or len(viewbox) < 2:
        return _TEXT[lang]["no_track"]
    max_x, max_y = float(viewbox[0]) or 1, float(viewbox[1]) or 1

    if ascii_only:
        grid = [[" " for _ in range(width)] for _ in range(height)]
        for point in points:
            if isinstance(point, list) and len(point) >= 2:
                x = min(width - 1, max(0, round(float(point[0]) / max_x * (width - 1))))
                y = min(height - 1, max(0, round(float(point[1]) / max_y * (height - 1))))
                grid[y][x] = "#"
    else:
        dots = [[0 for _ in range(width)] for _ in range(height)]
        bit = ((1, 2, 4, 64), (8, 16, 32, 128))
        for point in points:
            if isinstance(point, list) and len(point) >= 2:
                x = min(width * 2 - 1, max(0, round(float(point[0]) / max_x * (width * 2 - 1))))
                y = min(height * 4 - 1, max(0, round(float(point[1]) / max_y * (height * 4 - 1))))
                dots[y // 4][x // 2] |= bit[x % 2][y % 4]
        grid = [[chr(0x2800 + value) if value else " " for value in row] for row in dots]

    for driver_id, driver in drivers.items():
        if not isinstance(driver, dict):
            continue
        x_value, y_value = driver.get("x"), driver.get("y")
        if not isinstance(x_value, (int, float)) or not isinstance(y_value, (int, float)):
            continue
        x = min(width - 1, max(0, round(x_value / max_x * (width - 1))))
        y = min(height - 1, max(0, round(y_value / max_y * (height - 1))))
        grid[y][x] = str(driver_id)[:1].upper()
    return "\n".join("".join(row) for row in grid)


class RaceLensTUI(App[None]):
    """Keyboard-first terminal view over the existing HTTP API."""

    TITLE = "Race Lens"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("l", "open_live", "Live"),
        ("c", "focus_catalog", "Catalog"),
        ("space", "toggle_pause", "Play/Pause"),
        ("1", "set_speed(1)", "1x"),
        ("5", "set_speed(5)", "5x"),
        ("0", "set_speed(10)", "10x"),
        Binding("left", "seek(-10000)", "-10s", priority=True),
        Binding("right", "seek(10000)", "+10s", priority=True),
    ]
    CSS = """
    Screen { background: #090b10; color: #e7eaf0; }
    #workspace { height: 1fr; }
    #catalog { width: 28; border-right: solid #3b82f6; }
    #race { width: 1fr; }
    #status { height: 3; padding: 1 2; background: #141824; color: #f5c542; }
    #main-row { height: 1fr; }
    #timing { width: 42; height: 1fr; }
    #track { width: 1fr; height: 1fr; border-left: solid #293247; padding: 0 1; }
    #lower { height: 8; border-top: solid #293247; }
    #battles, #wtw, #feed { width: 1fr; padding: 0 1; overflow: hidden; }
    #wtw, #feed { border-left: solid #293247; }
    #timeline { height: 3; padding: 0 1; border-top: solid #293247; }
    #resize { display: none; height: 1fr; content-align: center middle; text-align: center; }
    ListItem { padding: 0 1; }
    ListItem.--highlight { background: #1d4ed8; }
    DataTable { scrollbar-size: 1 1; }
    """

    def __init__(self, api_url: str, lang: str = "en") -> None:
        super().__init__()
        self.lang = lang
        self.client = httpx.AsyncClient(
            base_url=api_url.rstrip("/") + "/",
            timeout=10,
            headers={"Accept": "application/json", "User-Agent": "racelens-tui/0.1"},
        )
        self.entries: list[dict[str, str | None]] = []
        self.mode = "catalog"
        self.session_id: str | None = None
        self.speed = 5
        self.at_ms = 0
        self.timeline: dict[str, Any] = {}
        self.track: dict[str, Any] | None = None
        self.positions: dict[str, Any] | None = None
        self.live_status: dict[str, Any] | None = None
        self.connected = False
        self.ended = False
        self.paused = False
        self.stream_worker = None
        encoding = sys.stdout.encoding or "ascii"
        try:
            "⣿".encode(encoding)
            self.ascii_only = False
        except (LookupError, UnicodeEncodeError):
            self.ascii_only = True

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="resize")
        with Horizontal(id="workspace"):
            yield ListView(ListItem(Label(_TEXT[self.lang]["catalog"])), id="catalog")
            with Vertical(id="race"):
                yield Static(_TEXT[self.lang]["waiting"], id="status")
                with Horizontal(id="main-row"):
                    yield DataTable(id="timing", cursor_type="row", zebra_stripes=True)
                    yield Static(id="track")
                with Horizontal(id="lower"):
                    yield Static(id="battles")
                    yield Static(id="wtw")
                    yield Static(id="feed")
                yield Static(id="timeline")
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#timing", DataTable)
        table.add_columns("P", "DRV", "INT", "TYRE", "LAP")
        self._apply_size(self.size.width, self.size.height)
        self.run_worker(self._load_catalog(), exclusive=True, group="catalog")

    async def on_unmount(self) -> None:
        await self.client.aclose()

    def on_resize(self, event: Resize) -> None:
        self._apply_size(event.size.width, event.size.height)

    def _apply_size(self, width: int, height: int) -> None:
        message = resize_message(width, height, self.lang)
        resize = self.query_one("#resize", Static)
        workspace = self.query_one("#workspace", Horizontal)
        resize.update(message or "")
        resize.styles.display = "block" if message else "none"
        workspace.styles.display = "none" if message else "block"

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self.client.get(path, params=params)
        response.raise_for_status()
        return response.json()

    async def _optional_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            return await self._get(path, params)
        except (httpx.HTTPError, ValueError):
            return None

    async def _load_catalog(self) -> None:
        status_path = api_path("live", "status")
        catalog_path = api_path("catalog", "catalog")
        sessions_path = api_path("catalog", "sessions")
        assert status_path and catalog_path and sessions_path
        status, catalog, sessions = await asyncio.gather(
            self._optional_get(status_path),
            self._optional_get(catalog_path),
            self._optional_get(sessions_path),
        )
        self.live_status = status if isinstance(status, dict) else None
        entries: list[dict[str, str | None]] = []
        if self.live_status and (
            self.live_status.get("is_running") or self.live_status.get("status") == "live"
        ):
            entries.append({"mode": "live", "id": None, "label": _TEXT[self.lang]["live"]})
        seen: set[str] = set()
        if isinstance(catalog, dict):
            for event in catalog.get("events", []):
                if not isinstance(event, dict):
                    continue
                for session in event.get("sessions", []):
                    if not isinstance(session, dict):
                        continue
                    replay_id = session.get("replay_session_id")
                    if replay_id:
                        seen.add(str(replay_id))
                    status_name = str(session.get("status") or "")
                    entries.append({
                        "mode": "replay" if replay_id else "unavailable",
                        "id": str(replay_id) if replay_id else None,
                        "label": (
                            f"R{event.get('round')} {event.get('name')} · {session.get('name')}"
                            + ("" if status_name == "ready" else f" [{status_name}]")
                        ),
                    })
        for session in sessions if isinstance(sessions, list) else []:
            if not isinstance(session, dict) or not session.get("session_id"):
                continue
            session_id = str(session["session_id"])
            if session_id not in seen:
                entries.append({
                    "mode": "replay",
                    "id": session_id,
                    "label": session_id.replace("_", " ").upper(),
                })
        self.entries = entries
        view = self.query_one("#catalog", ListView)
        await view.clear()
        await view.extend([ListItem(Label(str(entry["label"]))) for entry in entries] or [
            ListItem(Label(_TEXT[self.lang]["no_feed"])),
        ])
        if entries and entries[0]["mode"] == "live":
            await self._open_live()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.index is None or event.index >= len(self.entries):
            return
        entry = self.entries[event.index]
        if entry["mode"] == "live":
            await self._open_live()
        elif entry["mode"] == "replay" and entry["id"]:
            await self._open_replay(str(entry["id"]))
        else:
            self.query_one("#status", Static).update(_TEXT[self.lang]["not_ready"])

    async def _open_live(self) -> None:
        self.workers.cancel_group(self, "stream")
        self.mode, self.session_id = "live", None
        self.paused = self.ended = False
        self.connected = False
        self.track = self.positions = None
        self._start_stream()

    async def _open_replay(self, session_id: str) -> None:
        self.workers.cancel_group(self, "stream")
        self.mode, self.session_id = "replay", session_id
        self.paused = self.ended = False
        self.connected = False
        timeline_path = api_path("replay", "timeline", session_id)
        track_path = api_path("replay", "track", session_id)
        positions_path = api_path("replay", "positions", session_id)
        assert timeline_path and track_path and positions_path
        timeline, track, positions = await asyncio.gather(
            self._optional_get(timeline_path),
            self._optional_get(track_path),
            self._optional_get(positions_path, {"at_ms": 0}),
        )
        self.timeline = timeline if isinstance(timeline, dict) else {}
        self.track = track if isinstance(track, dict) else None
        self.positions = positions if isinstance(positions, dict) else None
        self.at_ms = int(self.timeline.get("start_ms") or 0)
        self._start_stream()

    def _start_stream(self) -> None:
        if self.stream_worker is not None:
            self.stream_worker.cancel()
        self.stream_worker = self.run_worker(self._stream(), exclusive=True, group="stream")

    async def _stream(self) -> None:
        attempt = 0
        while True:
            path = api_path(self.mode, "stream", self.session_id)
            if path is None:
                return
            params = (
                {"lang": self.lang, "level": "pro", "tick_s": 2}
                if self.mode == "live"
                else {
                    "lang": self.lang,
                    "level": "pro",
                    "speed": self.speed,
                    "from_ms": self.at_ms,
                    "tick_ms": 5000,
                }
            )
            try:
                saw_end = False
                async with self.client.stream(
                    "GET", path, params=params, timeout=httpx.Timeout(10, read=None),
                ) as response:
                    response.raise_for_status()
                    self.connected = True
                    attempt = 0
                    self._update_status()
                    event_name, data_lines = "message", []
                    async for line in response.aiter_lines():
                        if line.startswith("event:"):
                            event_name = line[6:].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())
                        elif not line:
                            if event_name == "end":
                                saw_end = True
                                self.ended = True
                                break
                            if data_lines:
                                try:
                                    frame = json.loads("\n".join(data_lines))
                                except json.JSONDecodeError:
                                    frame = None
                                if isinstance(frame, dict) and frame:
                                    await self._display_frame(frame)
                            event_name, data_lines = "message", []
                if saw_end:
                    if self.mode == "live":
                        status_path = api_path("live", "status")
                        assert status_path
                        status = await self._optional_get(status_path)
                        if isinstance(status, dict):
                            self.live_status = status
                    self._update_status()
                    return
                raise httpx.ReadError("SSE stream closed")
            except (httpx.HTTPError, ValueError):
                self.connected = False
                if self.mode == "live":
                    status_path = api_path("live", "status")
                    assert status_path
                    status = await self._optional_get(status_path)
                    if isinstance(status, dict):
                        self.live_status = status
                    if self.live_status and self.live_status.get("status") in {
                        "finishing", "replay_ready", "failed",
                    }:
                        self.ended = True
                        self._update_status()
                        return
                self._update_status()
                await asyncio.sleep(reconnect_delay(attempt))
                attempt += 1

    async def _display_frame(self, state: dict[str, Any]) -> None:
        self.at_ms = int(state.get("at_ms") or 0)
        if self.mode == "live":
            status_path = api_path("live", "status")
            feed_path = api_path("live", "feed")
            assert status_path and feed_path
            status, feed = await asyncio.gather(
                self._optional_get(status_path),
                self._optional_get(feed_path, {"lang": self.lang, "limit": 8}),
            )
            if isinstance(status, dict):
                self.live_status = status
            battles = state.get("battles") or []
        else:
            battles_path = api_path("replay", "battles", self.session_id)
            feed_path = api_path("replay", "feed", self.session_id)
            assert battles_path and feed_path
            battle_result, feed = await asyncio.gather(
                self._optional_get(battles_path, {"at_ms": self.at_ms}),
                self._optional_get(
                    feed_path, {"until_ms": self.at_ms, "lang": self.lang, "limit": 8},
                ),
            )
            battles = battle_result.get("battles", []) if isinstance(battle_result, dict) else []
        self._update_status(state)
        self._update_timing(state)
        self._update_track(state)
        self._update_lists(battles, state.get("commentary") or [], feed or [])
        self._update_timeline(state)

    def _update_status(self, state: dict[str, Any] | None = None) -> None:
        if self.mode == "live":
            value = status_text(
                self.live_status, connected=self.connected, ended=self.ended, lang=self.lang,
            )
        else:
            if self.lang == "ru":
                run = "ПАУЗА" if self.paused else "ЗАВЕРШЁН" if self.ended else f"{self.speed}x"
                value = f"ПОВТОР · {run} · {self.session_id or ''}"
            else:
                run = "PAUSED" if self.paused else "ENDED" if self.ended else f"{self.speed}x"
                value = f"REPLAY · {run} · {self.session_id or ''}"
        if state:
            value += f" · LAP {state.get('lap', '—')}/{state.get('total_laps') or '—'}"
        self.query_one("#status", Static).update(value)

    def _update_timing(self, state: dict[str, Any]) -> None:
        table = self.query_one("#timing", DataTable)
        table.clear()
        drivers = state.get("drivers") if isinstance(state.get("drivers"), dict) else {}
        order = state.get("classification") if isinstance(state.get("classification"), list) else []
        for position, driver_id in enumerate(order, 1):
            driver = drivers.get(driver_id, {})
            interval = driver.get("interval_s") if isinstance(driver, dict) else None
            tyre = driver.get("tyre_compound") if isinstance(driver, dict) else None
            laps = driver.get("laps_completed") if isinstance(driver, dict) else None
            table.add_row(
                str(position),
                str(driver_id),
                "LEAD" if position == 1 else "—" if interval is None else f"+{interval:.3f}",
                str(tyre or "—")[:6],
                str(laps if laps is not None else "—"),
            )

    def _update_track(self, state: dict[str, Any]) -> None:
        widget = self.query_one("#track", Static)
        drivers = state.get("drivers") if isinstance(state.get("drivers"), dict) else {}
        if self.mode == "replay" and self.positions:
            tick = max(1, int(self.positions.get("tick_ms") or 500))
            index = round((self.at_ms - int(self.positions.get("start_ms") or 0)) / tick)
            frames = self.positions.get("drivers") or {}
            api_drivers = {}
            for driver_id, values in frames.items():
                point = values[index] if isinstance(values, list) and 0 <= index < len(values) else None
                api_drivers[driver_id] = {
                    "x": point[0] if isinstance(point, list) and len(point) >= 2 else None,
                    "y": point[1] if isinstance(point, list) and len(point) >= 2 else None,
                }
            if any(driver["x"] is not None for driver in api_drivers.values()):
                drivers = api_drivers
        drawing = render_track(
            self.track,
            drivers,
            width=max(1, widget.content_region.width),
            height=max(1, widget.content_region.height - 1),
            ascii_only=self.ascii_only,
            live=self.mode == "live",
            lang=self.lang,
        )
        widget.update(f"{_TEXT[self.lang]['track']}\n{drawing}")

    def _update_lists(self, battles: Any, commentary: Any, feed: Any) -> None:
        battle_lines = []
        for item in battles if isinstance(battles, list) else []:
            ids = item.get("driver_ids", []) if isinstance(item, dict) else []
            gap = item.get("evidence", {}).get("interval_s") if isinstance(item, dict) else None
            if len(ids) >= 2:
                battle_lines.append(f"{ids[0]} vs {ids[1]}" + (f"  {gap:.2f}s" if isinstance(gap, (int, float)) else ""))
        wtw_lines = [
            str(item.get("text"))
            for item in commentary if isinstance(item, dict) and item.get("text")
        ] if isinstance(commentary, list) else []
        feed_lines = [
            str(item.get("text"))
            for item in feed if isinstance(item, dict) and item.get("text")
        ] if isinstance(feed, list) else []
        self.query_one("#battles", Static).update(
            f"{_TEXT[self.lang]['battles']}\n" + "\n".join(battle_lines[:5] or [_TEXT[self.lang]["no_battles"]])
        )
        self.query_one("#wtw", Static).update(
            f"{_TEXT[self.lang]['wtw']}\n" + "\n".join(wtw_lines[:4] or ["—"])
        )
        self.query_one("#feed", Static).update(
            f"{_TEXT[self.lang]['feed']}\n" + "\n".join(feed_lines[:5] or [_TEXT[self.lang]["no_feed"]])
        )

    def _update_timeline(self, state: dict[str, Any]) -> None:
        clock = max(0, self.at_ms) // 1000
        if self.mode == "live":
            self.query_one("#timeline", Static).update(
                f"LIVE {clock // 60:02d}:{clock % 60:02d}" if self.lang == "en"
                else f"ЭФИР {clock // 60:02d}:{clock % 60:02d}"
            )
            return
        start = int(self.timeline.get("start_ms") or 0)
        end = int(self.timeline.get("end_ms") or max(start + 1, self.at_ms))
        ratio = min(1, max(0, (self.at_ms - start) / max(1, end - start)))
        filled = round(ratio * 36)
        bar = "█" * filled + "─" * (36 - filled)
        controls = "SPACE play/pause  ←/→ 10s  1/5/0 speed"
        if self.lang == "ru":
            controls = "SPACE игра/пауза  ←/→ 10с  1/5/0 скорость"
        self.query_one("#timeline", Static).update(
            f"{clock // 60:02d}:{clock % 60:02d} [{bar}] {controls}"
        )

    async def action_open_live(self) -> None:
        if self.live_status and (
            self.live_status.get("is_running") or self.live_status.get("status") == "live"
        ):
            await self._open_live()

    def action_focus_catalog(self) -> None:
        self.query_one("#catalog", ListView).focus()

    def action_toggle_pause(self) -> None:
        if self.mode != "replay":
            return
        end = int(self.timeline.get("end_ms") or self.at_ms)
        if self.ended and self.at_ms < end:
            self.ended = self.paused = False
            self._start_stream()
            self._update_status()
            return
        self.paused = not self.paused
        if self.paused:
            if self.stream_worker is not None:
                self.stream_worker.cancel()
            self._update_status()
        else:
            self._start_stream()

    def action_set_speed(self, speed: int) -> None:
        if self.mode == "replay" and speed in {1, 5, 10}:
            self.speed = speed
            if not self.paused:
                self._start_stream()
            self._update_status()

    def action_seek(self, delta_ms: int) -> None:
        if self.mode != "replay":
            return
        start = int(self.timeline.get("start_ms") or 0)
        end = int(self.timeline.get("end_ms") or max(start, self.at_ms))
        self.at_ms = min(end, max(start, self.at_ms + delta_ms))
        if self.at_ms < end:
            self.ended = False
        self.run_worker(self._seek_frame(), exclusive=True, group="stream")

    async def _seek_frame(self) -> None:
        state_path = api_path("replay", "state", self.session_id)
        positions_path = api_path("replay", "positions", self.session_id)
        assert state_path and positions_path
        state, positions = await asyncio.gather(
            self._optional_get(state_path, {"at_ms": self.at_ms}),
            self._optional_get(positions_path, {"at_ms": max(0, self.at_ms)}),
        )
        if isinstance(positions, dict):
            self.positions = positions
        if isinstance(state, dict):
            await self._display_frame(state)
        if not self.paused and not self.ended:
            self._start_stream()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Read-only Race Lens terminal viewer")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--lang", choices=("en", "ru"), default="en")
    args = parser.parse_args(argv)
    url = httpx.URL(args.api_url)
    if url.scheme not in {"http", "https"} or not url.host:
        parser.error("--api-url must be an http(s) URL")
    RaceLensTUI(str(url), args.lang).run()


if __name__ == "__main__":
    main()
