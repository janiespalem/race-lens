import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from racelens.events.models import dump_jsonl, event  # noqa: E402

from tests.test_replay import mini_race  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    (tmp_path / "2024_mini_race.jsonl").write_text(dump_jsonl(mini_race()), encoding="utf-8")
    import racelens.api as api

    monkeypatch.setattr(api, "FIXTURES_DIR", tmp_path)
    return TestClient(api.app)


def test_sessions_and_state(client):
    assert client.get("/api/sessions").json() == [
        {"session_id": "2024_mini_race", "source": "fixture"},
    ]

    s = client.get("/api/sessions/2024_mini_race/state", params={"at_ms": 140_000}).json()
    assert s["classification"] == ["VER", "NOR", "LEC"]
    assert s["drivers"]["LEC"]["pit_count"] == 1
    # rank = 1-based classification index — the single ordering truth.
    assert [s["drivers"][d]["rank"] for d in ["VER", "NOR", "LEC"]] == [1, 2, 3]
    # No positions.json for this fixture → live frame, x/y null (map dead-reckons).
    assert s["frame_source"] == "live"
    assert s["drivers"]["VER"]["x"] is None

    assert client.get("/api/sessions/nope/state", params={"at_ms": 0}).status_code == 404


def test_security_headers(client):
    response = client.get("/api/ping")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_engine_cold_load_is_single_flight(tmp_path, monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor

    import racelens.api as api

    session_id = "cold_race"
    (tmp_path / f"{session_id}.jsonl").write_text(dump_jsonl(mini_race()), encoding="utf-8")
    monkeypatch.setattr(api, "FIXTURES_DIR", tmp_path)
    api._engine_cached.cache_clear()
    original_load = api.load_jsonl
    load_count = 0

    def slow_load(text):
        nonlocal load_count
        load_count += 1
        time.sleep(0.05)
        return original_load(text)

    monkeypatch.setattr(api, "load_jsonl", slow_load)
    with ThreadPoolExecutor(max_workers=4) as pool:
        engines = list(pool.map(lambda _: api._engine(session_id), range(4)))

    assert load_count == 1
    assert len({id(engine) for engine in engines}) == 1


def test_positions_cold_load_is_single_flight(tmp_path, monkeypatch):
    import json
    import time
    from concurrent.futures import ThreadPoolExecutor

    import racelens.api as api

    session_id = "cold_race"
    (tmp_path / f"{session_id}.jsonl").write_text(dump_jsonl(mini_race()), encoding="utf-8")
    (tmp_path / f"{session_id}.positions.json").write_text(
        json.dumps({"drivers": {"VER": [[1, 2]]}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(api, "FIXTURES_DIR", tmp_path)
    api._positions_data_cached.cache_clear()
    original_loads = api.json.loads
    load_count = 0

    def slow_load(text):
        nonlocal load_count
        load_count += 1
        time.sleep(0.05)
        return original_loads(text)

    monkeypatch.setattr(api.json, "loads", slow_load)
    with ThreadPoolExecutor(max_workers=4) as pool:
        positions = list(pool.map(lambda _: api._positions_data(session_id), range(4)))

    assert load_count == 1
    assert len({id(position) for position in positions}) == 1


def test_sessions_with_positions_are_listed_first(tmp_path, monkeypatch):
    import racelens.api as api

    for name in ["aaa_no_map", "zzz_demo"]:
        (tmp_path / f"{name}.jsonl").write_text(dump_jsonl(mini_race()), encoding="utf-8")
    (tmp_path / "zzz_demo.positions_raw.jsonl").write_text("not a replay", encoding="utf-8")
    (tmp_path / "zzz_demo.positions.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(api, "FIXTURES_DIR", tmp_path)

    c = TestClient(api.app)
    assert c.get("/api/sessions").json() == [
        {"session_id": "zzz_demo", "source": "fixture"},
        {"session_id": "aaa_no_map", "source": "fixture"},
    ]


def test_attach_frame_merges_xy_progress_by_tick(tmp_path, monkeypatch):
    import json

    import racelens.api as api

    monkeypatch.setattr(api, "FIXTURES_DIR", tmp_path)
    (tmp_path / "demo.positions.json").write_text(
        json.dumps({
            "tick_ms": 1000, "start_ms": 0, "viewbox": [600, 400],
            "drivers": {"VER": [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]},
            "progress": {"VER": [0.0, 0.5, 1.0]},
        }),
        encoding="utf-8",
    )
    # at_ms=1000 → tick 1 → the middle sample.
    state = {"session_id": "internal_id", "at_ms": 1000, "drivers": {"VER": {}}}
    api._attach_frame(state, "demo")
    assert state["frame_source"] == "replay"
    assert (state["drivers"]["VER"]["x"], state["drivers"]["VER"]["y"]) == (3.0, 4.0)
    assert state["drivers"]["VER"]["progress"] == 0.5
    assert state["viewbox"] == [600, 400]
    # Past the last tick → null, not a crash.
    state2 = {"session_id": "x", "at_ms": 9_999_999, "drivers": {"VER": {}}}
    api._attach_frame(state2, "demo")
    assert state2["drivers"]["VER"]["x"] is None


@pytest.mark.parametrize("path,extra_params", [
    ("forecast", {"laps": 5}),
    ("simulate-pit", {"driver": "VER"}),
    ("what-if", {"scenario": "baseline"}),
    ("overtake", {"ahead": "VER", "behind": "LEC"}),
])
def test_negative_at_ms_clamps_to_zero(client, path, extra_params):
    """Negative at_ms (formation lap, before lights-out) must not 422 — it
    clamps to 0 via _clamp_at_ms, the same semantic already used by
    state/insights/battles/commentary/win-prob (see state() docstring)."""
    url = f"/api/sessions/2024_mini_race/{path}"
    r_neg = client.get(url, params={"at_ms": -5000, **extra_params})
    r_zero = client.get(url, params={"at_ms": 0, **extra_params})
    assert r_neg.status_code == 200, r_neg.text
    assert r_zero.status_code == 200, r_zero.text
    assert r_neg.json() == r_zero.json()


def test_insights_endpoint(client):
    r = client.get("/api/sessions/2024_mini_race/insights", params={"at_ms": 247_000}).json()
    assert r["at_ms"] == 247_000
    assert r["insights"]
    assert all(insight["evidence"] for insight in r["insights"])
    early = client.get("/api/sessions/2024_mini_race/insights", params={"at_ms": 100_000}).json()
    assert early["insights"] == []


def test_stream_simulated_live(client):
    chunks = []
    with client.stream(
        "GET",
        "/api/sessions/2024_mini_race/stream",
        params={"speed": 100, "from_ms": 245_000, "tick_ms": 2_000},
    ) as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                chunks.append(line)
    # 245s, 247s, 249s + clamped final 250s = 4 states, then the end marker's data line
    assert len(chunks) == 5
    import json

    last_state = json.loads(chunks[3].removeprefix("data:"))
    assert last_state["session_status"] == "finished"
    assert last_state["active_insights"] == []
    assert "commentary" in last_state
    assert len(last_state["commentary"]) == len(last_state["active_insights"])


def test_stream_carries_recent_passes_in_window(tmp_path, monkeypatch):
    """A stream frame must carry recent_passes for on-track passes whose at_ms
    falls in (cur-20_000, cur], and drop them once the window has elapsed."""
    import json

    import racelens.api as api
    from racelens.events.models import dump_jsonl, event

    sid = "passrace"
    evs = [
        event(sid, "SessionStarted", 0, total_laps=3),
        event(sid, "PositionChanged", 0, "A", position=2),
        event(sid, "PositionChanged", 0, "B", position=1),
        event(sid, "LapCompleted", 90_000, "A", lap=1, lap_time_ms=90_000),
        # On-track pass: A retakes B at 200s (mirrors test_passes.py's pattern).
        event(sid, "PositionChanged", 200_000, "A", position=1),
        event(sid, "PositionChanged", 200_000, "B", position=2),
        event(sid, "SessionStatusChanged", 260_000, status="finished"),
    ]
    (tmp_path / f"{sid}.jsonl").write_text(dump_jsonl(evs), encoding="utf-8")
    monkeypatch.setattr(api, "FIXTURES_DIR", tmp_path)
    client = TestClient(api.app)

    chunks = []
    with client.stream(
        "GET",
        f"/api/sessions/{sid}/stream",
        params={"speed": 100, "from_ms": 200_000, "tick_ms": 10_000},
    ) as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                chunks.append(json.loads(line.removeprefix("data:")))

    # cur=200_000: the pass just happened — inside the (180_000, 200_000] window.
    assert chunks[0]["recent_passes"] == [
        {"ahead": "A", "behind": "B", "kind": "ON_TRACK", "at_ms": 200_000},
    ]
    # cur=230_000: still within 20s? no — window is (210_000, 230_000], pass is gone.
    at_230 = next(c for c in chunks if c.get("at_ms") == 230_000)
    assert at_230["recent_passes"] == []


def test_stream_speed_zero_returns_422(client):
    r = client.get(
        "/api/sessions/2024_mini_race/stream",
        params={"speed": 0, "tick_ms": 1000},
    )
    assert r.status_code == 422


@pytest.mark.parametrize(
    "path,params",
    [
        ("/api/sessions/2024_mini_race/feed", {"until_ms": 250_000}),
        ("/api/live/feed", {}),
    ],
)
@pytest.mark.parametrize("limit", [-1, 0, 101])
def test_feed_limit_is_bounded(client, path, params, limit):
    assert client.get(path, params={**params, "limit": limit}).status_code == 422


def test_track_endpoint(tmp_path, monkeypatch):
    import json as _json
    import racelens.api as api

    track_data = {"session_id": "2024_mini_race", "viewbox": [600, 400], "points": [[10.0, 20.0], [30.0, 40.0]]}
    (tmp_path / "2024_mini_race.jsonl").write_text(
        dump_jsonl(mini_race()), encoding="utf-8"
    )
    (tmp_path / "2024_mini_race.track.json").write_text(
        _json.dumps(track_data), encoding="utf-8"
    )
    monkeypatch.setattr(api, "FIXTURES_DIR", tmp_path)
    c = TestClient(api.app)

    r = c.get("/api/sessions/2024_mini_race/track")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "2024_mini_race"
    assert body["viewbox"] == [600, 400]
    assert len(body["points"]) == 2

    assert c.get("/api/sessions/nope/track").status_code == 404


def test_positions_endpoint(tmp_path, monkeypatch):
    import json as _json
    import racelens.api as api

    frames = [[float(index), float(index + 1)] for index in range(600)]
    frames[1] = None
    progress = [index / 100 for index in range(600)]
    pos_data = {
        "session_id": "2024_mini_race",
        "start_ms": 0,
        "tick_ms": 500,
        "viewbox": [600, 400],
        "drivers": {"LEC": frames},
        "progress": {"LEC": progress},
    }
    (tmp_path / "2024_mini_race.jsonl").write_text(
        dump_jsonl(mini_race()), encoding="utf-8"
    )
    (tmp_path / "2024_mini_race.positions.json").write_text(
        _json.dumps(pos_data), encoding="utf-8"
    )
    monkeypatch.setattr(api, "FIXTURES_DIR", tmp_path)
    c = TestClient(api.app)

    r = c.get("/api/sessions/2024_mini_race/positions")
    assert r.status_code == 200
    body = r.json()
    assert body["tick_ms"] == 500
    assert "LEC" in body["drivers"]
    assert len(body["drivers"]["LEC"]) == 600
    assert body["drivers"]["LEC"][1] is None

    window = c.get(
        "/api/sessions/2024_mini_race/positions", params={"at_ms": 100_000}
    ).json()
    assert window["start_ms"] == 70_000
    assert len(window["drivers"]["LEC"]) == 301
    assert window["drivers"]["LEC"][0] == frames[140]
    assert window["progress"]["LEC"][-1] == progress[440]

    assert c.get("/api/sessions/nope/positions").status_code == 404


def test_timeline(client):
    t = client.get("/api/sessions/2024_mini_race/timeline").json()
    assert t["start_ms"] == 0
    assert t["end_ms"] == 250_000
    assert t["lap_marks"]["1"] == 80_000  # first lap-1 completion


def test_timeline_includes_short_post_race_radio_not_late_steward_messages():
    import racelens.api as api

    session_id = "2024_mini_race"
    events = mini_race() + [
        event(
            session_id, "RaceControlMessage", 300_000, "VER",
            category="Radio", message="RADIO: VER", audio_url="https://provider.test/ver.mp3",
        ),
        event(
            session_id, "RaceControlMessage", 400_000, "NOR",
            category="Radio", message="RADIO: NOR",
        ),
        event(
            session_id, "RaceControlMessage", 551_000, "LEC",
            category="Radio", message="RADIO: LEC", audio_url="https://provider.test/lec.mp3",
        ),
        event(
            session_id, "RaceControlMessage", 450_000,
            category="Other", message="POST-RACE INVESTIGATION",
        ),
    ]

    assert api._race_end_ms(api.ReplayEngine(events)) == 300_000
