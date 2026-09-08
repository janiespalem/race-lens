"""Focused dependency-light check for the terminal viewer."""
from __future__ import annotations

import asyncio
import importlib.util

import httpx
from textual.containers import Horizontal
from textual.widgets import DataTable, Static


assert importlib.util.find_spec("racelens.tui") is not None, "racelens.tui is missing"

from racelens.tui import (  # noqa: E402
    api_path,
    RaceLensTUI,
    reconnect_delay,
    render_track,
    resize_message,
    status_text,
)


async def check_resize_ui() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/catalog":
            return httpx.Response(200, json={
                "season": 2026,
                "seasons": [2026],
                "catalog_available": True,
                "preparation_enabled": False,
                "events": [],
            })
        return httpx.Response(404, json={"detail": "No live session active"})

    app = RaceLensTUI("https://api.example", "en")
    await app.client.aclose()
    app.client = httpx.AsyncClient(
        base_url="https://api.example/", transport=httpx.MockTransport(handler),
    )
    async with app.run_test(size=(99, 28)) as pilot:
        await pilot.pause()
        assert str(app.query_one("#resize", Static).styles.display) == "block"
        assert str(app.query_one("#workspace", Horizontal).styles.display) == "none"
        await pilot.resize_terminal(100, 28)
        await pilot.pause()
        assert str(app.query_one("#resize", Static).styles.display) == "none"
        assert str(app.query_one("#workspace", Horizontal).styles.display) == "block"

        app.mode = "replay"
        app.session_id = "demo"
        app.track = {
            "viewbox": [10, 10],
            "points": [[0, 0], [2.5, 2.5], [5, 5], [7.5, 7.5], [10, 10]],
        }
        app._update_track({"drivers": {"VER": {"x": 5, "y": 5}}})
        track_widget = app.query_one("#track", Static)
        track_lines = track_widget.content.splitlines()
        assert len(track_lines) <= track_widget.content_region.height
        assert max(map(len, track_lines)) <= track_widget.content_region.width

        app.timeline = {"start_ms": 0, "end_ms": 100_000}
        app.at_ms = 50_000
        app.paused = True
        timing = app.query_one("#timing", DataTable)
        timing.cursor_type = "cell"
        timing.focus()
        await pilot.pause()
        assert app.focused is timing
        await pilot.press("left")
        await pilot.pause()
        assert app.at_ms == 40_000

        app.at_ms = 100_000
        app.ended = True
        app.paused = False
        app.action_seek(-10_000)
        assert app.at_ms == 90_000
        assert app.ended is False and app.paused is False
        await pilot.pause()

        app.ended = True
        app.action_toggle_pause()
        assert app.ended is False and app.paused is False
        await pilot.pause()


async def check_session_switch_and_timeouts() -> None:
    metadata_started, release_metadata = asyncio.Event(), asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stream"):
            assert request.extensions["timeout"]["read"] is None
            return httpx.Response(200, text="event: end\ndata: {}\n\n")
        read_timeout = request.extensions["timeout"]["read"]
        assert read_timeout is not None and read_timeout > 0, "GET reads must be bounded"
        if "/new_session/" in request.url.path:
            metadata_started.set()
            await release_metadata.wait()
            return httpx.Response(200, json={})
        raise httpx.ReadTimeout("side data stalled", request=request)

    app = RaceLensTUI("https://api.example", "en")
    timeout = app.client.timeout
    await app.client.aclose()
    app.client = httpx.AsyncClient(
        base_url="https://api.example/", timeout=timeout, transport=httpx.MockTransport(handler),
    )
    async with app.run_test(size=(100, 28)) as pilot:
        await pilot.pause()
        assert await app._optional_get("/stalled") is None
        app.mode, app.session_id = "replay", "old_session"
        app.stream_worker = app.run_worker(
            asyncio.Event().wait(), exclusive=True, group="stream",
        )
        old_stream = app.stream_worker
        pending_seek = app.run_worker(asyncio.Event().wait(), group="stream")
        opening = asyncio.create_task(app._open_replay("new_session"))
        try:
            await asyncio.wait_for(metadata_started.wait(), timeout=1)
            assert old_stream.is_cancelled, "cancel the old session before loading new metadata"
            assert pending_seek.is_cancelled, "cancel pending seeks for the old session too"
        finally:
            release_metadata.set()
            await opening
        await pilot.pause()
        assert app.session_id == "new_session" and app.ended


def main() -> None:
    assert resize_message(99, 28, "en") == "Resize terminal to at least 100x28 (now 99x28)."
    assert resize_message(100, 28, "en") is None
    assert resize_message(100, 27, "ru") == "Увеличьте терминал до 100x28 (сейчас 100x27)."

    assert api_path("catalog", "catalog") == "/api/catalog"
    assert api_path("catalog", "sessions") == "/api/sessions"
    assert api_path("live", "stream") == "/api/live/stream"
    assert api_path("live", "track") is None
    assert api_path("replay", "stream", "monaco_2024_race") == (
        "/api/sessions/monaco_2024_race/stream"
    )
    assert api_path("replay", "positions", "monaco_2024_race") == (
        "/api/sessions/monaco_2024_race/positions"
    )

    track = {
        "viewbox": [10, 10],
        "points": [[0, 0], [2.5, 2.5], [5, 5], [7.5, 7.5], [10, 10]],
    }
    drivers = {"VER": {"x": 5, "y": 5}}
    braille = render_track(track, drivers, width=10, height=4, ascii_only=False)
    ascii_track = render_track(track, drivers, width=10, height=4, ascii_only=True)
    assert "V" in braille and any("\u2800" <= char <= "\u28ff" for char in braille)
    assert "V" in ascii_track and "#" in ascii_track
    assert not any("\u2800" <= char <= "\u28ff" for char in ascii_track)
    assert render_track(None, {"VER": {"x": None, "y": None}}, live=True) == (
        "Live track telemetry unavailable."
    )

    assert [reconnect_delay(attempt) for attempt in range(6)] == [0.5, 1, 2, 4, 8, 8]
    assert reconnect_delay(10_000) == 8
    assert status_text(
        {"is_running": True, "data_quality": "stalled", "status": "live"},
        connected=False,
        ended=False,
        lang="en",
    ) == "STALE · RECONNECTING"
    assert status_text(
        {"is_running": False, "data_quality": "good", "status": "replay_ready"},
        connected=True,
        ended=False,
        lang="en",
    ) == "ENDED · REPLAY READY"
    assert status_text(
        {
            "is_running": True,
            "data_quality": "good",
            "status": "live",
            "expires_at": "2000-01-01T00:00:00Z",
        },
        connected=False,
        ended=False,
        lang="en",
    ) == "STALE · RECONNECTING"
    degraded_ru = {"is_running": True, "data_quality": "degraded", "status": "live"}
    assert status_text(
        degraded_ru, connected=True, ended=False, lang="ru",
    ) == "ДАННЫЕ ЗАДЕРЖИВАЮТСЯ"
    assert status_text(
        degraded_ru, connected=False, ended=False, lang="ru",
    ) == "ДАННЫЕ ЗАДЕРЖИВАЮТСЯ · ПЕРЕПОДКЛЮЧЕНИЕ"

    asyncio.run(check_resize_ui())
    asyncio.run(check_session_switch_and_timeouts())
    print("TUI check passed")


if __name__ == "__main__":
    main()
