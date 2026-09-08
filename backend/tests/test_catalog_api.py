from datetime import UTC, datetime
import json

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from racelens.events.models import dump_jsonl  # noqa: E402
from racelens.object_storage import (  # noqa: E402
    ObjectPreparationQueue,
    RemoteSessionCache,
    StorageError,
    publish_session,
)
from racelens.preparations import PreparationQueue, QueueFullError  # noqa: E402
from racelens.recorder.schedule import ScheduledSession  # noqa: E402
from tests.test_object_storage import MemoryStore  # noqa: E402
from tests.test_replay import mini_race  # noqa: E402


def test_preparation_queue_is_atomic_bounded_and_idempotent(tmp_path):
    queue = PreparationQueue(tmp_path / "queue", max_jobs=1)
    first, created = queue.enqueue("2024-08-fp1", "monaco_2024_fp1")
    duplicate, created_again = queue.enqueue("2024-08-fp1", "monaco_2024_fp1")

    assert created is True
    assert created_again is False
    assert duplicate == first
    assert queue.get("2024-08-fp1") == first
    with pytest.raises(QueueFullError):
        queue.enqueue("2024-08-q", "monaco_2024_qualifying")
    assert sorted(path.name for path in (tmp_path / "queue").glob("*.json")) == [
        "2024-08-fp1.json",
    ]
    queue.finish("2024-08-fp1", error="upstream unavailable")
    retried, retried_now = queue.enqueue("2024-08-fp1", "monaco_2024_fp1")
    assert retried_now is True
    assert retried["status"] == "queued"
    assert retried["error"] is None


def test_catalog_matches_legacy_venue_fixture_names(tmp_path, monkeypatch):
    import racelens.catalog as catalog

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    for name in ("spain_2024_race", "silverstone_2024_race"):
        (fixtures / f"{name}.jsonl").write_text("{}\n", encoding="utf-8")
    sessions = [
        ScheduledSession(2024, 10, "Spanish Grand Prix", "R", datetime(2024, 6, 23, 13, tzinfo=UTC)),
        ScheduledSession(2024, 12, "British Grand Prix", "R", datetime(2024, 7, 7, 14, tzinfo=UTC)),
    ]
    monkeypatch.setattr(catalog, "load_fastf1_schedule", lambda year: sessions)

    catalog.build_catalog(
        2024,
        fixtures,
        PreparationQueue(tmp_path / "queue"),
        tmp_path / "cache",
        preparation_enabled=False,
    )
    monkeypatch.setattr(
        catalog,
        "load_fastf1_schedule",
        lambda year: (_ for _ in ()).throw(ModuleNotFoundError()),
    )
    monkeypatch.setattr(
        catalog,
        "load_jolpica_schedule",
        lambda year, cache_dir: (_ for _ in ()).throw(catalog.CatalogUnavailable()),
    )
    body = catalog.build_catalog(
        2024,
        fixtures,
        PreparationQueue(tmp_path / "queue"),
        tmp_path / "cache",
        preparation_enabled=False,
    )
    by_id = {
        item["session_id"]: item
        for event in body["events"]
        for item in event["sessions"]
    }
    assert by_id["2024-10-r"]["replay_session_id"] == "spain_2024_race"
    assert by_id["2024-12-r"]["replay_session_id"] == "silverstone_2024_race"


def test_catalog_exposes_only_completed_sessions(tmp_path, monkeypatch):
    import racelens.catalog as catalog

    start = datetime(2024, 5, 26, 13, tzinfo=UTC)
    session = ScheduledSession(2024, 8, "Monaco Grand Prix", "R", start)
    monkeypatch.setattr(catalog, "load_schedule", lambda year, cache_dir: [session])

    during = catalog.build_catalog(
        2024,
        tmp_path / "fixtures",
        PreparationQueue(tmp_path / "queue"),
        tmp_path / "cache",
        preparation_enabled=True,
        now=start + (session.capture_until - start) / 2,
    )
    after = catalog.build_catalog(
        2024,
        tmp_path / "fixtures",
        PreparationQueue(tmp_path / "queue"),
        tmp_path / "cache",
        preparation_enabled=True,
        now=session.capture_until,
    )
    assert during["events"] == []
    assert after["events"][0]["sessions"][0]["session_id"] == "2024-08-r"


def test_jolpica_fallback_uses_fixed_host_and_disk_cache(tmp_path, monkeypatch):
    import racelens.catalog as catalog

    payload = {
        "MRData": {"RaceTable": {"Races": [{
            "round": "8",
            "raceName": "Monaco Grand Prix",
            "date": "2024-05-26",
            "time": "13:00:00Z",
            "FirstPractice": {"date": "2024-05-24", "time": "11:30:00Z"},
            "SprintQualifying": {"date": "2024-05-24", "time": "15:30:00Z"},
            "Qualifying": {"date": "2024-05-25", "time": "14:00:00Z"},
        }]}}
    }
    called = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size=-1):
            return json.dumps(payload).encode()

    def urlopen(request, timeout):
        called.update(url=request.full_url, timeout=timeout)
        return Response()

    monkeypatch.setattr(catalog.urllib.request, "urlopen", urlopen)
    sessions = catalog.load_jolpica_schedule(2024, tmp_path / "cache")
    assert called == {"url": "https://api.jolpi.ca/ergast/f1/2024.json", "timeout": 5}
    assert [item.kind for item in sessions] == ["FP1", "SQ", "Q", "R"]

    monkeypatch.setattr(
        catalog.urllib.request,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("fresh disk cache should avoid the network"),
    )
    assert catalog.load_jolpica_schedule(2024, tmp_path / "cache") == sessions


def test_catalog_prepare_is_whitelisted_bounded_and_idempotent(tmp_path, monkeypatch):
    import racelens.api as api
    import racelens.catalog as catalog

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "monaco_2024_race.jsonl").write_text(
        dump_jsonl(mini_race()), encoding="utf-8",
    )
    for name in ("spain_2024_race", "silverstone_2024_race"):
        (fixtures / f"{name}.jsonl").write_text(dump_jsonl(mini_race()), encoding="utf-8")
    sessions = [
        ScheduledSession(2024, 8, "Monaco Grand Prix", "FP1", datetime(2024, 5, 24, 11, tzinfo=UTC)),
        ScheduledSession(2024, 8, "Monaco Grand Prix", "Q", datetime(2024, 5, 25, 14, tzinfo=UTC)),
        ScheduledSession(2024, 8, "Monaco Grand Prix", "R", datetime(2024, 5, 26, 13, tzinfo=UTC)),
        ScheduledSession(2024, 10, "Spanish Grand Prix", "R", datetime(2024, 6, 23, 13, tzinfo=UTC)),
        ScheduledSession(2024, 12, "British Grand Prix", "R", datetime(2024, 7, 7, 14, tzinfo=UTC)),
    ]
    monkeypatch.setattr(catalog, "load_schedule", lambda year, cache_dir: sessions)
    monkeypatch.setattr(api, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(api, "CATALOG_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(api, "PREPARATION_QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(api, "PREPARATION_QUEUE_MAX", 1)
    monkeypatch.setattr(api, "READONLY", False)
    client = TestClient(api.app)

    response = client.get("/api/catalog", params={"season": 2024})
    assert response.status_code == 200
    body = response.json()
    assert body["catalog_available"] is True
    assert body["preparation_enabled"] is True
    by_id = {
        item["session_id"]: item
        for event in body["events"]
        for item in event["sessions"]
    }
    assert by_id["2024-08-r"]["status"] == "ready"
    assert by_id["2024-08-r"]["replay_session_id"] == "monaco_2024_race"
    assert by_id["2024-08-fp1"]["status"] == "prepare"
    assert by_id["2024-10-r"]["replay_session_id"] == "spain_2024_race"
    assert by_id["2024-12-r"]["replay_session_id"] == "silverstone_2024_race"

    first = client.post("/api/catalog/2024-08-fp1/prepare")
    second = client.post("/api/catalog/2024-08-fp1/prepare")
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert first.headers["location"] == "/api/preparations/2024-08-fp1"
    assert client.get("/api/preparations/2024-08-fp1").json() == first.json()
    assert len(list((tmp_path / "queue").glob("*.json"))) == 1

    assert client.post("/api/catalog/2024-08-q/prepare").status_code == 429
    assert client.post("/api/catalog/2024-99-r/prepare").status_code == 404
    assert not (tmp_path / "queue" / "2024-99-r.json").exists()


def test_catalog_prepare_is_explicitly_disabled_in_readonly_mode(tmp_path, monkeypatch):
    import racelens.api as api
    import racelens.catalog as catalog

    session = ScheduledSession(
        2024, 8, "Monaco Grand Prix", "FP1", datetime(2024, 5, 24, 11, tzinfo=UTC),
    )
    monkeypatch.setattr(catalog, "load_schedule", lambda year, cache_dir: [session])
    monkeypatch.setattr(api, "FIXTURES_DIR", tmp_path / "fixtures")
    monkeypatch.setattr(api, "PREPARATION_QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(api, "READONLY", True)
    client = TestClient(api.app)

    assert client.get("/api/catalog", params={"season": 2024}).json()["preparation_enabled"] is False
    response = client.post("/api/catalog/2024-08-fp1/prepare")
    assert response.status_code == 403
    assert "read-only" in response.json()["detail"]
    assert client.get("/api/capabilities").json()["preparation_enabled"] is False


def test_preparation_endpoints_report_active_jobs_without_catalog(tmp_path, monkeypatch):
    import racelens.api as api
    import racelens.catalog as catalog

    called = {"catalog": 0}

    class StubQueue:
        def __init__(self, record: dict):
            self._record = record

        def get(self, session_id: str) -> dict | None:
            return self._record if session_id == "2024-08-r" else None

    queue_record = {
        "job_id": "2024-08-r",
        "session_id": "2024-08-r",
        "fixture_stem": "monaco_2024_race",
        "status": "processing",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
        "replay_session_id": None,
        "error": None,
    }

    def blocked_catalog(*_):
        called["catalog"] += 1
        raise catalog.CatalogUnavailable()

    monkeypatch.setattr(api, "FIXTURES_DIR", tmp_path / "fixtures")
    monkeypatch.setattr(api, "READONLY", False)
    monkeypatch.setattr(api, "_catalog", blocked_catalog)
    monkeypatch.setattr(api, "_preparation_queue", lambda: StubQueue(queue_record))

    client = TestClient(api.app)
    status = client.get("/api/preparations/2024-08-r")
    assert status.status_code == 200
    assert status.json()["status"] == "processing"
    assert called["catalog"] == 0

    response = client.post("/api/catalog/2024-08-r/prepare")
    assert response.status_code == 202
    assert response.headers["location"] == "/api/preparations/2024-08-r"
    assert response.headers["retry-after"] == "3"
    assert response.json()["status"] == "processing"
    assert response.json()["session_id"] == "2024-08-r"
    assert called["catalog"] == 0


def test_preparation_failed_record_is_retried_on_post(tmp_path, monkeypatch):
    import racelens.api as api
    import racelens.catalog as catalog

    queue = PreparationQueue(tmp_path / "queue")
    queue.enqueue("2024-08-r", "monaco_2024_race")
    queue.finish("2024-08-r", error="worker failure")
    assert queue.get("2024-08-r")["status"] == "failed"

    def blocked_catalog(*_):
        raise catalog.CatalogUnavailable()

    monkeypatch.setattr(api, "FIXTURES_DIR", tmp_path / "fixtures")
    monkeypatch.setattr(api, "PREPARATION_QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(api, "READONLY", False)
    monkeypatch.setattr(api, "_catalog", blocked_catalog)
    client = TestClient(api.app)

    response = client.post("/api/catalog/2024-08-r/prepare")
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["error"] is None
    assert queue.get("2024-08-r")["status"] == "queued"
    assert client.get("/api/preparations/2024-08-r").json()["status"] == "queued"


def test_preparation_failed_object_record_retry_bumps_generation(tmp_path, monkeypatch):
    import racelens.api as api
    import racelens.catalog as catalog

    store = MemoryStore()
    queue = ObjectPreparationQueue(store, max_attempts=1)
    queue.enqueue("2024-08-r", "monaco_2024_race")
    queue.claim_next()
    queue.finish("2024-08-r", error="worker failure")
    assert queue.get("2024-08-r")["status"] == "failed"
    assert queue.get("2024-08-r")["generation"] == 1

    def blocked_catalog(*_):
        raise catalog.CatalogUnavailable()

    monkeypatch.setattr(api, "FIXTURES_DIR", tmp_path / "fixtures")
    monkeypatch.setattr(api, "STORAGE_CONFIG", object())
    monkeypatch.setattr(api, "_object_queue", lambda: queue)
    monkeypatch.setattr(api, "_catalog", blocked_catalog)
    client = TestClient(api.app)

    response = client.post("/api/catalog/2024-08-r/prepare")
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    record = queue.get("2024-08-r")
    assert record["status"] == "queued"
    assert record["generation"] == 2


@pytest.mark.parametrize("processing", [False, True])
def test_failed_retry_respects_active_queue_capacity(tmp_path, monkeypatch, processing):
    import racelens.api as api

    store = MemoryStore()
    queue = ObjectPreparationQueue(store, max_jobs=1, max_attempts=1)
    queue.enqueue("2024-08-r", "monaco_2024_race")
    queue.claim_next()
    queue.finish("2024-08-r", error="worker failure")
    queue.enqueue("2024-09-r", "canada_2024_race")
    if processing:
        queue.claim_next()

    monkeypatch.setattr(api, "FIXTURES_DIR", tmp_path)
    monkeypatch.setattr(api, "STORAGE_CONFIG", object())
    monkeypatch.setattr(api, "_object_queue", lambda: queue)
    client = TestClient(api.app)

    response = client.post("/api/catalog/2024-08-r/prepare")
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    assert queue.get("2024-08-r")["status"] == "failed"
    assert queue.get("2024-08-r")["generation"] == 1
    # An idempotent request needs no new slot, even when capacity is exhausted.
    assert client.post("/api/catalog/2024-09-r/prepare").status_code == 202

    if not processing:
        queue.claim_next()
    queue.finish("2024-09-r", error="worker failure")
    retried = client.post("/api/catalog/2024-08-r/prepare")
    assert retried.status_code == 202
    assert retried.json()["status"] == "queued"
    assert queue.get("2024-08-r")["generation"] == 2


def test_local_ready_replay_beats_stale_active_queue_record(tmp_path, monkeypatch):
    import racelens.api as api
    import racelens.catalog as catalog

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "monaco_2024_race.jsonl").write_text(
        dump_jsonl(mini_race()), encoding="utf-8",
    )

    class StubQueue:
        def get(self, session_id: str) -> dict:
            return {
                "job_id": "2024-08-r",
                "session_id": "2024-08-r",
                "fixture_stem": "monaco_2024_race",
                "status": "running",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
                "replay_session_id": None,
                "error": None,
            }

    def blocked_catalog(*_):
        raise catalog.CatalogUnavailable()

    monkeypatch.setattr(api, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(api, "READONLY", False)
    monkeypatch.setattr(api, "_catalog", blocked_catalog)
    monkeypatch.setattr(api, "_preparation_queue", lambda: StubQueue())
    client = TestClient(api.app)

    assert client.get("/api/preparations/2024-08-r").json()["status"] == "ready"
    response = client.post("/api/catalog/2024-08-r/prepare")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["replay_session_id"] == "monaco_2024_race"


def test_local_ready_replay_survives_queue_storage_error(tmp_path, monkeypatch):
    import racelens.api as api
    import racelens.catalog as catalog

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "monaco_2024_race.jsonl").write_text(
        dump_jsonl(mini_race()), encoding="utf-8",
    )
    session = ScheduledSession(
        2024, 8, "Monaco Grand Prix", "R", datetime(2024, 5, 26, 13, tzinfo=UTC),
    )

    class BrokenQueue:
        def get(self, session_id: str):
            raise StorageError("SECRET_ACCESS_KEY=do-not-return")

    monkeypatch.setattr(catalog, "load_schedule", lambda year, cache_dir: [session])
    monkeypatch.setattr(api, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(api, "CATALOG_CACHE_DIR", tmp_path / "catalog")
    monkeypatch.setattr(api, "READONLY", False)
    monkeypatch.setattr(api, "_preparation_queue", lambda: BrokenQueue())
    client = TestClient(api.app)

    response = client.post("/api/catalog/2024-08-r/prepare")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["replay_session_id"] == "monaco_2024_race"
    status = client.get("/api/preparations/2024-08-r")
    assert status.status_code == 200
    assert status.json()["status"] == "ready"
    assert "SECRET_ACCESS_KEY" not in status.text


def test_readonly_post_is_rejected_even_with_existing_queue_record(tmp_path, monkeypatch):
    import racelens.api as api

    queue_dir = tmp_path / "queue"
    PreparationQueue(queue_dir).enqueue("2024-08-r", "monaco_2024_race")
    monkeypatch.setattr(api, "FIXTURES_DIR", tmp_path / "fixtures")
    monkeypatch.setattr(api, "PREPARATION_QUEUE_DIR", queue_dir)
    monkeypatch.setattr(api, "READONLY", True)
    client = TestClient(api.app)

    response = client.post("/api/catalog/2024-08-r/prepare")
    assert response.status_code == 403
    assert "read-only" in response.json()["detail"]
    status = client.get("/api/preparations/2024-08-r")
    assert status.status_code == 200
    assert status.json()["status"] == "queued"
    assert client.get("/api/capabilities").json()["preparation_enabled"] is False


def test_preparation_rejects_non_canonical_session_ids():
    import racelens.api as api

    client = TestClient(api.app)
    assert client.post("/api/catalog/not-a-session/prepare").status_code == 404
    assert client.get("/api/preparations/not-a-session").status_code == 404


def test_readonly_api_queues_and_replays_a_verified_remote_session(tmp_path, monkeypatch):
    import racelens.api as api
    import racelens.catalog as catalog

    scheduled = ScheduledSession(
        2024, 8, "Monaco Grand Prix", "R", datetime(2024, 5, 26, 13, tzinfo=UTC),
    )
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    store = MemoryStore()
    queue = ObjectPreparationQueue(store)
    monkeypatch.setattr(catalog, "load_schedule", lambda year, cache_dir: [scheduled])
    monkeypatch.setattr(api, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(api, "CATALOG_CACHE_DIR", tmp_path / "catalog")
    monkeypatch.setattr(api, "READONLY", True)
    monkeypatch.setattr(api, "STORAGE_CONFIG", object())
    monkeypatch.setattr(api, "_object_queue", lambda: queue)
    monkeypatch.setattr(
        api,
        "_remote_cache",
        lambda: RemoteSessionCache(store, tmp_path / "remote-cache"),
    )
    client = TestClient(api.app)

    first = client.post("/api/catalog/2024-08-r/prepare")
    duplicate = client.post("/api/catalog/2024-08-r/prepare")
    assert first.status_code == duplicate.status_code == 202
    assert first.json() == duplicate.json()
    assert client.get("/api/capabilities").json()["preparation_enabled"] is True

    assert queue.claim_next()["status"] == "processing"
    assert client.get("/api/preparations/2024-08-r").json()["status"] == "processing"

    fixture = tmp_path / "monaco_2024_race.jsonl"
    track = tmp_path / "monaco_2024_race.track.json"
    positions = tmp_path / "monaco_2024_race.positions.json"
    fixture.write_text(dump_jsonl(mini_race()), encoding="utf-8")
    track.write_text(
        json.dumps({
            "session_id": "monaco_2024_race",
            "viewbox": [600, 400],
            "points": [[0, 0], [1, 1]],
        }),
        encoding="utf-8",
    )
    positions.write_text(
        json.dumps({
            "session_id": "monaco_2024_race",
            "start_ms": 0,
            "tick_ms": 1000,
            "viewbox": [600, 400],
            "drivers": {},
            "progress": {},
        }),
        encoding="utf-8",
    )
    publish_session(
        store,
        "2024-08-r",
        "monaco_2024_race",
        fixture,
        track,
        positions,
        event_count=len(mini_race()),
    )
    queue.finish("2024-08-r", replay_session_id="monaco_2024_race")

    ready = client.get("/api/preparations/2024-08-r")
    assert ready.json()["status"] == "ready"
    assert client.get(
        "/api/sessions/monaco_2024_race/state", params={"at_ms": 0},
    ).status_code == 200
    assert {"session_id": "monaco_2024_race", "source": "object-storage"} in (
        client.get("/api/sessions").json()
    )


def test_storage_errors_are_publicly_sanitized(tmp_path, monkeypatch):
    import racelens.api as api
    import racelens.catalog as catalog

    session = ScheduledSession(
        2024, 8, "Monaco Grand Prix", "R", datetime(2024, 5, 26, 13, tzinfo=UTC),
    )

    class BrokenQueue:
        def get(self, session_id):
            raise StorageError("SECRET_ACCESS_KEY=do-not-return")

    monkeypatch.setattr(catalog, "load_schedule", lambda year, cache_dir: [session])
    monkeypatch.setattr(api, "FIXTURES_DIR", tmp_path / "fixtures")
    monkeypatch.setattr(api, "CATALOG_CACHE_DIR", tmp_path / "catalog")
    monkeypatch.setattr(api, "_object_queue", lambda: BrokenQueue())
    response = TestClient(api.app).get("/api/catalog", params={"season": 2024})

    # Queue outage is rendered as "prepare" (unknown), not a 503, and never leaks
    # provider error text; the endpoint cannot claim a state it cannot prove.
    assert response.status_code == 200
    assert "SECRET_ACCESS_KEY" not in response.text
    assert response.json()["events"][0]["sessions"][0]["status"] == "prepare"


def test_catalog_tolerates_queue_storage_error_across_multiple_sessions(tmp_path, monkeypatch):
    import racelens.api as api
    import racelens.catalog as catalog

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "monaco_2024_race.jsonl").write_text(
        dump_jsonl(mini_race()), encoding="utf-8",
    )
    sessions = [
        ScheduledSession(2024, 8, "Monaco Grand Prix", "R", datetime(2024, 5, 26, 13, tzinfo=UTC)),
        ScheduledSession(2024, 10, "Spanish Grand Prix", "R", datetime(2024, 6, 23, 13, tzinfo=UTC)),
    ]

    class BrokenQueue:
        def get(self, session_id: str):
            raise StorageError("SECRET_ACCESS_KEY=do-not-return")

    monkeypatch.setattr(catalog, "load_schedule", lambda year, cache_dir: sessions)
    monkeypatch.setattr(api, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(api, "CATALOG_CACHE_DIR", tmp_path / "catalog")
    monkeypatch.setattr(api, "READONLY", False)
    monkeypatch.setattr(api, "_preparation_queue", lambda: BrokenQueue())
    client = TestClient(api.app)

    body = client.get("/api/catalog", params={"season": 2024})
    assert body.status_code == 200
    assert "SECRET_ACCESS_KEY" not in body.text
    by_id = {
        item["session_id"]: item
        for event in body.json()["events"]
        for item in event["sessions"]
    }
    assert by_id["2024-08-r"]["status"] == "ready"
    assert by_id["2024-08-r"]["replay_session_id"] == "monaco_2024_race"
    assert by_id["2024-10-r"]["status"] == "prepare"

    ready = client.post("/api/catalog/2024-08-r/prepare")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert ready.json()["replay_session_id"] == "monaco_2024_race"
