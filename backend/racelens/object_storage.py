"""Private S3 storage for durable preparation jobs and verified replay archives."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, ClassVar, Iterator

from racelens.events.models import WEATHER_BOUNDS
from racelens.preparations import QueueFullError, SESSION_ID

SCHEMA_VERSION = 1
REPLAY_ID = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ARCHIVE_FILES = {
    "events.jsonl": ".jsonl",
    "track.json": ".track.json",
    "positions.json": ".positions.json",
}
MAX_OBJECT_BYTES = 64 * 1024 * 1024
MAX_SESSION_BYTES = 128 * 1024 * 1024
MAX_RECORD_BYTES = 16 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_LIVE_SNAPSHOT_BYTES = 256 * 1024
LIVE_SNAPSHOT_TTL_S = 20
_STATUS_TTL_S = 3
_BACKOFF = (timedelta(minutes=5), timedelta(minutes=15))


class StorageError(RuntimeError):
    """A storage operation failed without exposing provider details."""


class ManifestError(ValueError):
    """A remote archive manifest is unsafe or inconsistent."""


class LiveRecordError(ValueError):
    """A public Live pointer or snapshot is unsafe or inconsistent."""


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _parsed_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _safe_key(key: str) -> str:
    if (
        not isinstance(key, str)
        or not key
        or len(key.encode()) > 1024
        or key.startswith("/")
        or any(part in {"", ".", ".."} for part in key.split("/"))
    ):
        raise ValueError("unsafe object key")
    return key


class StorageConfig:
    """Validated S3-compatible connection settings from the process environment."""

    _FIELDS: ClassVar[dict[str, str]] = {
        "endpoint": "RACELENS_S3_ENDPOINT",
        "region": "RACELENS_S3_REGION",
        "bucket": "RACELENS_S3_BUCKET",
        "access_key_id": "RACELENS_S3_ACCESS_KEY_ID",
        "secret_access_key": "RACELENS_S3_SECRET_ACCESS_KEY",
    }

    def __init__(
        self,
        *,
        endpoint: str,
        region: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        session_token: str | None = None,
    ) -> None:
        if not endpoint.startswith("https://") or endpoint.endswith("/"):
            raise ValueError("RACELENS_S3_ENDPOINT must be an HTTPS origin without a trailing slash")
        if not all((region, bucket, access_key_id, secret_access_key)):
            raise ValueError("object storage settings cannot be empty")
        self.endpoint = endpoint
        self.region = region
        self.bucket = bucket
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.session_token = session_token

    @classmethod
    def from_env(cls) -> StorageConfig | None:
        values = {field: os.environ.get(name) for field, name in cls._FIELDS.items()}
        if not any(values.values()):
            return None
        missing = [name for field, name in cls._FIELDS.items() if not values[field]]
        if missing:
            raise ValueError(f"incomplete object storage settings: {', '.join(missing)}")
        return cls(
            **values,  # type: ignore[arg-type]
            session_token=os.environ.get("RACELENS_S3_SESSION_TOKEN") or None,
        )


class S3Store:
    """Small bounded wrapper around the installed S3 SDK."""

    def __init__(self, config: StorageConfig) -> None:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover - deployment packaging guard
            raise StorageError("object storage support is not installed") from exc
        self.bucket = config.bucket
        try:
            self.client = boto3.client(
                "s3",
                endpoint_url=config.endpoint,
                region_name=config.region,
                aws_access_key_id=config.access_key_id,
                aws_secret_access_key=config.secret_access_key,
                aws_session_token=config.session_token,
                config=Config(
                    connect_timeout=5,
                    read_timeout=30,
                    retries={"mode": "standard", "max_attempts": 3},
                    max_pool_connections=4,
                ),
            )
        except Exception as exc:
            raise StorageError("object storage client failed to initialise") from exc

    @staticmethod
    def _missing(exc: Exception) -> bool:
        response = getattr(exc, "response", {})
        code = str(response.get("Error", {}).get("Code", ""))
        return code in {"404", "NoSuchKey", "NotFound"}

    def list_keys(self, prefix: str, *, limit: int = 1000) -> list[str]:
        _safe_key(prefix.rstrip("/"))
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            keys = []
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                keys.extend(str(item["Key"]) for item in page.get("Contents", ()))
                if len(keys) > limit:
                    raise StorageError("object listing exceeds the configured bound")
            return sorted(keys)
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("object storage listing failed") from exc

    def get_bytes(self, key: str, *, limit: int) -> bytes | None:
        _safe_key(key)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if self._missing(exc):
                return None
            raise StorageError("object storage read failed") from exc
        body = response["Body"]
        try:
            size = int(response.get("ContentLength", -1))
            if size < 0 or size > limit:
                raise StorageError("object exceeds the configured size limit")
            data = body.read(limit + 1)
            if len(data) != size or len(data) > limit:
                raise StorageError("object changed during download")
            return data
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("object storage read failed") from exc
        finally:
            body.close()

    def get_json(self, key: str, *, limit: int = MAX_RECORD_BYTES) -> dict | None:
        raw = self.get_bytes(key, limit=limit)
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StorageError("stored JSON is invalid") from exc
        if not isinstance(value, dict):
            raise StorageError("stored JSON is not an object")
        return value

    def put_json(self, key: str, value: dict) -> None:
        payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"
        limit = MAX_LIVE_SNAPSHOT_BYTES if key.startswith("live/") else MAX_MANIFEST_BYTES
        if len(payload) > limit:
            raise ValueError("JSON object is too large")
        self.put_bytes(key, payload, content_type="application/json")

    def put_bytes(self, key: str, value: bytes, *, content_type: str) -> None:
        _safe_key(key)
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=value,
                ContentLength=len(value),
                ContentType=content_type,
            )
        except Exception as exc:
            raise StorageError("object storage write failed") from exc

    def upload_file(self, key: str, path: Path, *, sha256: str) -> None:
        _safe_key(key)
        size = path.stat().st_size
        if not 0 < size <= MAX_OBJECT_BYTES or not SHA256.fullmatch(sha256):
            raise ValueError("archive object is outside the configured bounds")
        try:
            self.client.upload_file(
                str(path),
                self.bucket,
                key,
                ExtraArgs={"Metadata": {"sha256": sha256}},
            )
        except Exception as exc:
            raise StorageError("object storage upload failed") from exc

    def copy(self, source: str, destination: str) -> None:
        _safe_key(source)
        _safe_key(destination)
        try:
            self.client.copy_object(
                Bucket=self.bucket,
                Key=destination,
                CopySource={"Bucket": self.bucket, "Key": source},
                MetadataDirective="COPY",
            )
        except Exception as exc:
            raise StorageError("object storage copy failed") from exc

    def delete(self, key: str) -> None:
        _safe_key(key)
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise StorageError("temporary object cleanup failed") from exc

    def verify(self, key: str, *, size: int, sha256: str) -> None:
        _safe_key(key)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise StorageError("object verification failed") from exc
        body = response["Body"]
        digest = hashlib.sha256()
        read = 0
        try:
            if int(response.get("ContentLength", -1)) != size:
                raise StorageError("stored object size does not match")
            for chunk in iter(lambda: body.read(1024 * 1024), b""):
                read += len(chunk)
                if read > size:
                    raise StorageError("stored object grew during verification")
                digest.update(chunk)
            if read != size or digest.hexdigest() != sha256:
                raise StorageError("stored object checksum does not match")
        finally:
            body.close()

    def matches(self, key: str, *, size: int, sha256: str) -> bool:
        _safe_key(key)
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if self._missing(exc):
                return False
            raise StorageError("object storage metadata read failed") from exc
        return (
            int(response.get("ContentLength", -1)) == size
            and response.get("Metadata", {}).get("sha256") == sha256
        )

    def download_verified(
        self,
        key: str,
        destination: Path,
        *,
        size: int,
        sha256: str,
    ) -> None:
        _safe_key(key)
        if not 0 < size <= MAX_OBJECT_BYTES or not SHA256.fullmatch(sha256):
            raise ManifestError("manifest object bounds are invalid")
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise StorageError("archive download failed") from exc
        body = response["Body"]
        temporary: str | None = None
        digest = hashlib.sha256()
        read = 0
        try:
            if int(response.get("ContentLength", -1)) != size:
                raise ManifestError("archive object size does not match manifest")
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile(
                "wb", dir=destination.parent, prefix=".download-", delete=False,
            ) as handle:
                temporary = handle.name
                for chunk in iter(lambda: body.read(1024 * 1024), b""):
                    read += len(chunk)
                    if read > size:
                        raise ManifestError("archive object exceeds declared size")
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if read != size or digest.hexdigest() != sha256:
                raise ManifestError("archive object checksum does not match manifest")
            os.replace(temporary, destination)
            temporary = None
        finally:
            body.close()
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)


def _file_digest(path: Path) -> tuple[int, str]:
    size = path.stat().st_size
    if not 0 < size <= MAX_OBJECT_BYTES:
        raise ManifestError(f"archive file size is not allowed: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return size, digest.hexdigest()


def validate_manifest(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "canonical_session_id",
        "replay_session_id",
        "source",
        "created_at",
        "event_count",
        "terminal_status",
        "files",
    }:
        raise ManifestError("manifest fields are invalid")
    canonical = value["canonical_session_id"]
    replay = value["replay_session_id"]
    if not isinstance(canonical, str) or not SESSION_ID.fullmatch(canonical):
        raise ManifestError("manifest canonical session ID is invalid")
    if not isinstance(replay, str) or not REPLAY_ID.fullmatch(replay):
        raise ManifestError("manifest replay session ID is invalid")
    if value["schema_version"] != SCHEMA_VERSION or value["terminal_status"] != "ready":
        raise ManifestError("manifest schema or terminal status is invalid")
    if (
        not isinstance(value["source"], str)
        or not 0 < len(value["source"]) <= 32
        or not isinstance(value["event_count"], int)
        or isinstance(value["event_count"], bool)
        or value["event_count"] < 1
    ):
        raise ManifestError("manifest metadata is invalid")
    try:
        _parsed_time(value["created_at"])
    except ValueError as exc:
        raise ManifestError("manifest timestamp is invalid") from exc
    files = value["files"]
    if not isinstance(files, dict) or set(files) != set(ARCHIVE_FILES):
        raise ManifestError("manifest archive file set is invalid")
    total = 0
    for name, row in files.items():
        expected_key = f"sessions/{replay}/{name}"
        if (
            not isinstance(row, dict)
            or set(row) != {"key", "size", "sha256"}
            or row["key"] != expected_key
            or not isinstance(row["size"], int)
            or isinstance(row["size"], bool)
            or not 0 < row["size"] <= MAX_OBJECT_BYTES
            or not isinstance(row["sha256"], str)
            or not SHA256.fullmatch(row["sha256"])
        ):
            raise ManifestError(f"manifest entry is invalid: {name}")
        total += row["size"]
    if total > MAX_SESSION_BYTES:
        raise ManifestError("manifest session exceeds the configured size limit")
    return value


def manifest_key(replay_session_id: str) -> str:
    if not REPLAY_ID.fullmatch(replay_session_id):
        raise ValueError("invalid replay session ID")
    return f"sessions/{replay_session_id}/manifest.json"


def load_manifest(
    store: Any,
    replay_session_id: str,
    *,
    canonical_session_id: str | None = None,
) -> dict:
    value = store.get_json(manifest_key(replay_session_id), limit=MAX_MANIFEST_BYTES)
    if value is None:
        raise ManifestError("archive manifest is missing")
    manifest = validate_manifest(value)
    if manifest["replay_session_id"] != replay_session_id:
        raise ManifestError("archive manifest replay ID differs")
    if (
        canonical_session_id is not None
        and manifest["canonical_session_id"] != canonical_session_id
    ):
        raise ManifestError("archive manifest canonical ID differs")
    return manifest


def manifest_objects_match(store: Any, manifest: dict) -> bool:
    return all(
        store.matches(row["key"], size=row["size"], sha256=row["sha256"])
        for row in manifest["files"].values()
    )


_LIVE_STATUSES = {"live", "finishing", "replay_ready", "failed"}
_LIVE_FAILURES = {"Archive preparation failed"}
_LIVE_TRANSITIONS = {
    "live": {"finishing", "failed"},
    "finishing": {"replay_ready", "failed"},
    "failed": {"replay_ready"},
    "replay_ready": set(),
}
_LIVE_POINTER_FIELDS = {
    "schema_version", "canonical_session_id", "replay_session_id", "status",
    "snapshot_key", "created_at", "updated_at", "failure",
}
_LIVE_POINTER_OPTIONAL_FIELDS = {"track_replay_session_id"}
_LIVE_SNAPSHOT_FIELDS = {
    "schema_version", "canonical_session_id", "replay_session_id", "sequence",
    "generated_at", "expires_at", "race_state", "battles", "active_insights",
    "recent_passes", "feed", "commentary", "radio", "capture_freshness",
    "data_quality",
}
_LIVE_SNAPSHOT_OPTIONAL_FIELDS = {"stints"}
_RACE_STATE_FIELDS = {
    "session_id", "at_ms", "lap", "session_status", "session_name",
    "status_since_ms", "total_laps", "classification", "drivers",
    "data_quality", "frame_source", "viewbox",
}
_RACE_STATE_OPTIONAL_FIELDS = {"restart_at_ms", "weather"}
_WEATHER_FIELDS = set(WEATHER_BOUNDS) | {"rainfall"}
_DRIVER_FIELDS = {
    "position", "rank", "grid_position", "laps_completed", "last_lap_ms",
    "best_lap_ms", "gap_s", "interval_s", "tyre_compound", "tyre_age_laps",
    "pit_count", "in_pit", "recent_laps_ms", "retired", "x", "y", "progress",
}
_DRIVER_OPTIONAL_FIELDS = {"stopped", "retirement_inferred"}
_INSIGHT_FIELDS = {
    "insight_id", "type", "severity", "confidence", "created_at_ms", "lap",
    "driver_ids", "evidence",
}
_INSIGHT_TYPES = {
    "TRAFFIC_RISK_MEDIUM", "TRAFFIC_RISK_HIGH", "DRS_TRAIN_ACTIVE",
    "PIT_WINDOW_OPEN", "SC_PIT_WINDOW", "UNDERCUT_RISK_MEDIUM",
    "UNDERCUT_RISK_HIGH", "DEGRADATION_TREND_DETECTED",
    "CLEAN_AIR_PACE_LEADER", "BATTLE_DETECTED",
}
_FEED_KINDS = {
    "SessionStarted", "SessionStatusChanged", "LapCompleted", "PitIn", "PitOut",
    "RaceControlMessage", "RetirementDetected", "DriverStoppedChanged",
    "DriverTroubleDetected", "ON_TRACK", "UNDERCUT",
}
_EVIDENCE_FIELDS = {
    "interval_s", "pace_delta_ms", "behind_last_lap_ms", "ahead_last_lap_ms",
    "cars", "head", "intervals_s", "pit_loss_s", "margin_s",
    "gap_to_next_behind_s", "tyre_age_laps", "position",
    "attacker_tyre_age_laps", "defender_tyre_age_laps", "laps_ms", "drift_ms",
    "avg_lap_ms", "vs_race_leader_ms", "drivers_in_clean_air", "positions",
}
_DRIVER_ID = re.compile(r"^[A-Z0-9]{1,4}$")
_UNSAFE_PUBLIC_TEXT = (
    "://", "aws_", "racelens_s3_", "/home/", "/var/", "/tmp/", "/opt/",
    "/etc/", "\\users\\", "secret_access", "access_key",
    "amazonaws.com", "botocore", "boto3",
)


def _choice(value: object, allowed: set[str]) -> bool:
    return isinstance(value, str) and value in allowed


def _integer(value: object, minimum: int = 0, maximum: int = 2**63 - 1) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum


def _number(value: object, *, nullable: bool = False) -> bool:
    if value is None:
        return nullable
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return -(2**63) <= value <= 2**63 - 1
    return isinstance(value, float) and math.isfinite(value)


def _public_text(
    value: object,
    *,
    maximum: int = 500,
    empty: bool = False,
    multiline: bool = False,
) -> bool:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value):
        return False
    lowered = value.lower()
    lines = value.splitlines() if multiline else [value]
    return (
        not any(
            ord(char) < 32 and char not in ("\t\n" if multiline else "\t")
            for char in value
        )
        and "\r" not in value
        and (multiline or "\n" not in value)
        and all(
            not line.startswith(("/", "\\"))
            and not re.match(r"^[a-zA-Z]:[\\/]", line)
            for line in lines
        )
        and not any(token in lowered for token in _UNSAFE_PUBLIC_TEXT)
    )


def _driver_id(value: object) -> bool:
    return isinstance(value, str) and bool(_DRIVER_ID.fullmatch(value))


def live_snapshot_key(canonical_session_id: str) -> str:
    if not isinstance(canonical_session_id, str) or not SESSION_ID.fullmatch(canonical_session_id):
        raise ValueError("invalid canonical session ID")
    return f"live/{canonical_session_id}/snapshot.json"


def validate_live_pointer(value: object) -> dict:
    if (
        not isinstance(value, dict)
        or not _LIVE_POINTER_FIELDS <= set(value)
        or not set(value) <= _LIVE_POINTER_FIELDS | _LIVE_POINTER_OPTIONAL_FIELDS
    ):
        raise LiveRecordError("live pointer fields are invalid")
    canonical = value["canonical_session_id"]
    replay = value["replay_session_id"]
    status = value["status"]
    failure = value["failure"]
    track_replay = value.get("track_replay_session_id")
    if (
        value["schema_version"] != SCHEMA_VERSION
        or not isinstance(canonical, str)
        or not SESSION_ID.fullmatch(canonical)
        or not isinstance(replay, str)
        or not REPLAY_ID.fullmatch(replay)
        or not _choice(status, _LIVE_STATUSES)
        or value["snapshot_key"] != live_snapshot_key(canonical)
        or (failure is not None and not _choice(failure, _LIVE_FAILURES))
        or (track_replay is not None and (
            not isinstance(track_replay, str) or not REPLAY_ID.fullmatch(track_replay)
        ))
        or (status == "failed") != (failure is not None)
    ):
        raise LiveRecordError("live pointer metadata is invalid")
    try:
        created = _parsed_time(value["created_at"])
        updated = _parsed_time(value["updated_at"])
    except (TypeError, ValueError) as exc:
        raise LiveRecordError("live pointer timestamps are invalid") from exc
    if updated < created:
        raise LiveRecordError("live pointer timestamps are invalid")
    return value


def _bounded_rows(value: object, name: str, limit: int) -> list:
    if not isinstance(value, list) or len(value) > limit or any(not isinstance(row, dict) for row in value):
        raise LiveRecordError(f"live snapshot {name} is invalid")
    return value


def _validate_race_state(value: object, replay_session_id: str) -> None:
    if (
        not isinstance(value, dict)
        or not _RACE_STATE_FIELDS <= set(value)
        or not set(value) <= _RACE_STATE_FIELDS | _RACE_STATE_OPTIONAL_FIELDS
    ):
        raise LiveRecordError("live snapshot race state is invalid")
    if value["session_id"] != replay_session_id:
        raise LiveRecordError("live snapshot inner identity differs")
    weather = value.get("weather")
    if (
        not _integer(value["at_ms"])
        or not _integer(value["lap"], maximum=500)
        or not _choice(
            value["session_status"],
            {"unknown", "formation", "started", "red_flag", "safety_car", "vsc", "finished"},
        )
        or (
            value["session_name"] is not None
            and not _public_text(value["session_name"], maximum=120)
        )
        or not _integer(value["status_since_ms"])
        or (
            value.get("restart_at_ms") is not None
            and not _integer(value["restart_at_ms"])
        )
        or (
            value["total_laps"] is not None
            and not _integer(value["total_laps"], maximum=500)
        )
        or value["frame_source"] != "live"
        or value["viewbox"] is not None
        or (
            weather is not None
            and (
                not isinstance(weather, dict)
                or not weather
                or not set(weather) <= _WEATHER_FIELDS
                or any(
                    key != "rainfall" and (
                        not _number(item)
                        or not WEATHER_BOUNDS[key][0] <= item <= WEATHER_BOUNDS[key][1]
                    )
                    for key, item in weather.items()
                )
                or (
                    "rainfall" in weather
                    and not isinstance(weather["rainfall"], bool)
                )
            )
        )
    ):
        raise LiveRecordError("live snapshot race state is invalid")
    drivers = value["drivers"]
    if not isinstance(drivers, dict) or len(drivers) > 30 or not all(
        _driver_id(driver) for driver in drivers
    ):
        raise LiveRecordError("live snapshot race state is invalid")
    for driver in drivers.values():
        if (
            not isinstance(driver, dict)
            or not _DRIVER_FIELDS <= set(driver)
            or not set(driver) <= _DRIVER_FIELDS | _DRIVER_OPTIONAL_FIELDS
        ):
            raise LiveRecordError("live snapshot driver state is invalid")
        if (
            any(
                driver[field] is not None
                and not _integer(driver[field], minimum=1, maximum=30)
                for field in ("position", "rank", "grid_position")
            )
            or not _integer(driver["laps_completed"], maximum=500)
            or any(
                driver[field] is not None and not _integer(driver[field])
                for field in ("last_lap_ms", "best_lap_ms")
            )
            or not _number(driver["gap_s"], nullable=True)
            or not _number(driver["interval_s"], nullable=True)
            or (
                driver["tyre_compound"] is not None
                and not _choice(
                    driver["tyre_compound"],
                    {
                        "Soft", "Medium", "Hard", "Intermediate", "Wet", "Unknown",
                        "S", "M", "H", "I", "W",
                    },
                )
            )
            or (
                driver["tyre_age_laps"] is not None
                and not _integer(driver["tyre_age_laps"], maximum=500)
            )
            or not _integer(driver["pit_count"], maximum=100)
            or not isinstance(driver["in_pit"], bool)
            or not isinstance(driver["retired"], bool)
            or not isinstance(driver.get("retirement_inferred", False), bool)
            or not isinstance(driver.get("stopped", False), bool)
            or not isinstance(driver["recent_laps_ms"], list)
            or len(driver["recent_laps_ms"]) > 3
            or not all(_integer(lap_ms) for lap_ms in driver["recent_laps_ms"])
            or any(driver[field] is not None for field in ("x", "y", "progress"))
        ):
            raise LiveRecordError("live snapshot driver state is invalid")
    classification = value["classification"]
    if (
        not isinstance(classification, list)
        or len(classification) > 30
        or not all(_driver_id(driver) and driver in drivers for driver in classification)
        or len(classification) != len(set(classification))
    ):
        raise LiveRecordError("live snapshot race state is invalid")
    quality = value["data_quality"]
    if (
        not isinstance(quality, dict)
        or set(quality) != {
            "status", "last_event_ms", "events_applied", "duplicates_dropped",
        }
        or not _choice(quality["status"], {"unknown", "good", "stale"})
        or (quality["last_event_ms"] is not None and not _integer(quality["last_event_ms"]))
        or not _integer(quality["events_applied"])
        or not _integer(quality["duplicates_dropped"])
    ):
        raise LiveRecordError("live snapshot race state is invalid")


def _validate_insights(value: object, name: str, limit: int) -> None:
    for insight in _bounded_rows(value, name, limit):
        if set(insight) != _INSIGHT_FIELDS:
            raise LiveRecordError(f"live snapshot {name} is invalid")
        driver_ids = insight["driver_ids"]
        evidence = insight["evidence"]
        if (
            not _public_text(insight["insight_id"], maximum=160)
            or not _choice(insight["type"], _INSIGHT_TYPES)
            or not _choice(insight["severity"], {"info", "low", "medium", "high"})
            or not _choice(insight["confidence"], {"low", "medium", "high"})
            or not _integer(insight["created_at_ms"])
            or not _integer(insight["lap"], maximum=500)
            or not isinstance(driver_ids, list)
            or len(driver_ids) > 20
            or not all(_driver_id(driver) for driver in driver_ids)
            or not isinstance(evidence, dict)
            or not set(evidence) <= _EVIDENCE_FIELDS
        ):
            raise LiveRecordError(f"live snapshot {name} is invalid")
        for key, item in evidence.items():
            if key == "head":
                valid = _driver_id(item)
            elif isinstance(item, list):
                valid = len(item) <= 30 and all(_number(number) for number in item)
            else:
                valid = _number(
                    item,
                    nullable=key in {
                        "vs_race_leader_ms", "defender_tyre_age_laps",
                    },
                )
            if not valid:
                raise LiveRecordError(f"live snapshot {name} is invalid")


def _validate_passes(value: object) -> None:
    for row in _bounded_rows(value, "passes", 100):
        if (
            set(row) != {"ahead", "behind", "kind", "at_ms"}
            or not _driver_id(row["ahead"])
            or not _driver_id(row["behind"])
            or not _choice(row["kind"], {"ON_TRACK", "UNDERCUT"})
            or not _integer(row["at_ms"])
        ):
            raise LiveRecordError("live snapshot passes are invalid")


def _validate_feed(value: object) -> None:
    for row in _bounded_rows(value, "feed", 100):
        required = {"id", "at_ms", "lap", "kind", "tag", "text", "driver_id"}
        optional = {"audio_url", "transcript"}
        if not required <= set(row) or not set(row) <= required | optional:
            raise LiveRecordError("live snapshot feed is invalid")
        if (
            not _public_text(row["id"], maximum=200)
            or not _integer(row["at_ms"])
            or (row["lap"] is not None and not _integer(row["lap"], maximum=500))
            or not _choice(row["kind"], _FEED_KINDS)
            or not _choice(
                row["tag"],
                {"PIT", "FLAG", "FINISH", "FASTEST", "INFO", "PASS", "STEWARDS"},
            )
            or not _public_text(row["text"], maximum=1000)
            or (row["driver_id"] is not None and not _driver_id(row["driver_id"]))
            or (
                "audio_url" in row
                and (
                    not isinstance(row["audio_url"], str)
                    or not row["audio_url"].startswith(
                        "https://livetiming.formula1.com/static/"
                    )
                    or len(row["audio_url"]) > 2048
                )
            )
            or (
                "transcript" in row
                and not _public_text(
                    row["transcript"], maximum=2000, multiline=True,
                )
            )
        ):
            raise LiveRecordError("live snapshot feed is invalid")


def _validate_commentary(value: object) -> None:
    for row in _bounded_rows(value, "commentary", 100):
        if (
            set(row) != {"insight_id", "severity", "lap", "text"}
            or not _public_text(row["insight_id"], maximum=160)
            or not _choice(row["severity"], {"info", "low", "medium", "high"})
            or not _integer(row["lap"], maximum=500)
            or not _public_text(row["text"], maximum=2000)
        ):
            raise LiveRecordError("live snapshot commentary is invalid")


def validate_live_snapshot(
    value: object,
    *,
    pointer: dict,
    now: datetime | None = None,
) -> dict:
    pointer = validate_live_pointer(pointer)
    if (
        not isinstance(value, dict)
        or not _LIVE_SNAPSHOT_FIELDS <= set(value)
        or not set(value) <= _LIVE_SNAPSHOT_FIELDS | _LIVE_SNAPSHOT_OPTIONAL_FIELDS
    ):
        raise LiveRecordError("live snapshot fields are invalid")
    try:
        encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"
    except (TypeError, ValueError) as exc:
        raise LiveRecordError("live snapshot JSON is invalid") from exc
    if len(encoded) > MAX_LIVE_SNAPSHOT_BYTES:
        raise LiveRecordError("live snapshot exceeds the size limit")
    if (
        value["schema_version"] != SCHEMA_VERSION
        or value["canonical_session_id"] != pointer["canonical_session_id"]
        or value["replay_session_id"] != pointer["replay_session_id"]
    ):
        raise LiveRecordError("live pointer and snapshot identity differ")
    sequence = value["sequence"]
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or not 1 <= sequence <= 2**63 - 1
    ):
        raise LiveRecordError("live snapshot sequence is invalid")
    try:
        generated = _parsed_time(value["generated_at"])
        expires = _parsed_time(value["expires_at"])
    except (TypeError, ValueError) as exc:
        raise LiveRecordError("live snapshot timestamps are invalid") from exc
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if generated > current + timedelta(seconds=5) or expires <= generated:
        raise LiveRecordError("live snapshot timestamps are invalid")
    if expires - generated > timedelta(seconds=LIVE_SNAPSHOT_TTL_S) or current > expires:
        raise LiveRecordError("live snapshot is stale")

    _validate_race_state(value["race_state"], pointer["replay_session_id"])
    _validate_insights(value["battles"], "battles", 50)
    _validate_insights(value["active_insights"], "insights", 100)
    _validate_passes(value["recent_passes"])
    feed = value["feed"]
    if not isinstance(feed, dict) or set(feed) != {"en", "ru"}:
        raise LiveRecordError("live snapshot feed is invalid")
    for language in ("en", "ru"):
        _validate_feed(feed[language])
    commentary = value["commentary"]
    if not isinstance(commentary, dict) or set(commentary) != {"en", "ru"}:
        raise LiveRecordError("live snapshot commentary is invalid")
    for language in ("en", "ru"):
        levels = commentary[language]
        if not isinstance(levels, dict) or set(levels) != {"beginner", "pro"}:
            raise LiveRecordError("live snapshot commentary is invalid")
        for level in ("beginner", "pro"):
            _validate_commentary(levels[level])
    radio = _bounded_rows(value["radio"], "radio", 20)
    for row in radio:
        if (
            set(row) != {"audio_url", "transcript", "driver_id", "at_ms"}
            or not isinstance(row["audio_url"], str)
            or not row["audio_url"].startswith(
                "https://livetiming.formula1.com/static/"
            )
            or len(row["audio_url"]) > 2048
            or (
                row["transcript"] is not None
                and not _public_text(
                    row["transcript"], maximum=2000, multiline=True,
                )
            )
            or (row["driver_id"] is not None and not _driver_id(row["driver_id"]))
            or not _integer(row["at_ms"])
        ):
            raise LiveRecordError("live snapshot radio is invalid")
    stints = value.get("stints")
    if stints is not None:
        if (
            not isinstance(stints, dict)
            or set(stints) != {"session_id", "total_laps", "stints"}
            or stints["session_id"] != pointer["replay_session_id"]
            or not _integer(stints["total_laps"], maximum=500)
            or not isinstance(stints["stints"], dict)
            or len(stints["stints"]) > 30
        ):
            raise LiveRecordError("live snapshot stints are invalid")
        for driver_id, rows in stints["stints"].items():
            if not _driver_id(driver_id) or not isinstance(rows, list) or len(rows) > 20:
                raise LiveRecordError("live snapshot stints are invalid")
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or set(row) != {"compound", "start_lap", "end_lap", "laps"}
                    or not _public_text(row["compound"], maximum=20)
                    or not all(
                        _integer(row[field], minimum=1, maximum=500)
                        for field in ("start_lap", "end_lap", "laps")
                    )
                    or row["end_lap"] < row["start_lap"]
                    or row["laps"] != row["end_lap"] - row["start_lap"] + 1
                ):
                    raise LiveRecordError("live snapshot stints are invalid")
    freshness = value["capture_freshness"]
    if (
        not isinstance(freshness, dict)
        or set(freshness) != {
            "raw_size", "raw_updated_at", "seconds_since_growth", "transport_growing",
        }
        or not isinstance(freshness["raw_size"], int)
        or isinstance(freshness["raw_size"], bool)
        or not 0 <= freshness["raw_size"] <= MAX_SESSION_BYTES
        or not _number(freshness["seconds_since_growth"])
        or freshness["seconds_since_growth"] < 0
        or not isinstance(freshness["transport_growing"], bool)
        or not _choice(value["data_quality"], {"good", "degraded", "stalled"})
    ):
        raise LiveRecordError("live snapshot freshness is invalid")
    try:
        raw_updated = _parsed_time(freshness["raw_updated_at"])
    except (TypeError, ValueError) as exc:
        raise LiveRecordError("live snapshot freshness is invalid") from exc
    if raw_updated > current + timedelta(seconds=5):
        raise LiveRecordError("live snapshot freshness is invalid")
    return value


def write_live_snapshot(
    store: Any,
    pointer: dict,
    snapshot: dict,
    *,
    now: datetime | None = None,
) -> None:
    pointer = validate_live_pointer(pointer)
    if pointer["status"] != "live":
        raise LiveRecordError("live snapshot pointer must have live status")
    snapshot = validate_live_snapshot(snapshot, pointer=pointer, now=now)
    current = store.get_json("live/current.json", limit=MAX_RECORD_BYTES)
    if (
        isinstance(current, dict)
        and current.get("canonical_session_id") == pointer["canonical_session_id"]
        and current.get("replay_session_id") == pointer["replay_session_id"]
    ):
        existing = validate_live_pointer(current)
        if existing["status"] != "live":
            raise LiveRecordError("live snapshot lifecycle is no longer active")
        pointer = validate_live_pointer({**pointer, "created_at": existing["created_at"]})
    store.put_json(pointer["snapshot_key"], snapshot)
    store.put_json("live/current.json", pointer)


def load_live(store: Any, *, now: datetime | None = None) -> dict | None:
    pointer_value = store.get_json("live/current.json", limit=MAX_RECORD_BYTES)
    if pointer_value is None:
        return None
    pointer = validate_live_pointer(pointer_value)
    snapshot_value = store.get_json(
        pointer["snapshot_key"], limit=MAX_LIVE_SNAPSHOT_BYTES,
    )
    if snapshot_value is None:
        raise LiveRecordError("live snapshot is missing")
    try:
        snapshot = validate_live_snapshot(snapshot_value, pointer=pointer, now=now)
    except LiveRecordError as exc:
        # A stale snapshot is never returned, whatever the lifecycle status:
        # the pointer stays discoverable so /api/live/status can report an
        # honest "live but stalled" state instead of a 502.
        if str(exc) == "live snapshot is stale":
            snapshot = None
        else:
            raise
    return {"pointer": pointer, "snapshot": snapshot}


def write_live_status(
    store: Any,
    canonical_session_id: str,
    replay_session_id: str,
    status: str,
    *,
    failure: str | None = None,
    now: datetime | None = None,
) -> dict:
    current = store.get_json("live/current.json", limit=MAX_RECORD_BYTES)
    if current is None:
        raise LiveRecordError("live pointer is missing")
    pointer = validate_live_pointer(current)
    if (
        pointer["canonical_session_id"] != canonical_session_id
        or pointer["replay_session_id"] != replay_session_id
    ):
        raise LiveRecordError("live pointer identity differs")
    if not _choice(status, _LIVE_STATUSES):
        raise LiveRecordError("live status is invalid")
    current_status = pointer["status"]
    if status != current_status and status not in _LIVE_TRANSITIONS[current_status]:
        raise LiveRecordError(f"live status transition {current_status} -> {status} is invalid")
    if status == "replay_ready":
        try:
            manifest = load_manifest(
                store, replay_session_id, canonical_session_id=canonical_session_id,
            )
            if not manifest_objects_match(store, manifest):
                raise ManifestError("archive objects differ from manifest")
        except (ManifestError, StorageError) as exc:
            raise LiveRecordError("verified archive manifest is not ready") from exc
    stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z")
    updated = {
        **pointer,
        "status": status,
        "updated_at": stamp,
        "failure": failure,
    }
    validate_live_pointer(updated)
    store.put_json("live/current.json", updated)
    return updated


def publish_session(
    store: Any,
    canonical_session_id: str,
    replay_session_id: str,
    fixture_path: Path,
    track_path: Path,
    positions_path: Path,
    *,
    event_count: int,
    source: str = "fastf1",
) -> dict:
    """Upload, read-verify, copy, then publish the final manifest last."""
    if not SESSION_ID.fullmatch(canonical_session_id):
        raise ValueError("invalid canonical session ID")
    if not REPLAY_ID.fullmatch(replay_session_id):
        raise ValueError("invalid replay session ID")
    paths = {
        "events.jsonl": Path(fixture_path),
        "track.json": Path(track_path),
        "positions.json": Path(positions_path),
    }
    files = {}
    for name, path in paths.items():
        size, digest = _file_digest(path)
        files[name] = {
            "key": f"sessions/{replay_session_id}/{name}",
            "size": size,
            "sha256": digest,
        }
    manifest = validate_manifest({
        "schema_version": SCHEMA_VERSION,
        "canonical_session_id": canonical_session_id,
        "replay_session_id": replay_session_id,
        "source": source,
        "created_at": _now(),
        "event_count": event_count,
        "terminal_status": "ready",
        "files": files,
    })
    try:
        current = load_manifest(
            store, replay_session_id, canonical_session_id=canonical_session_id,
        )
    except ManifestError:
        current = None
    if current is not None:
        if current["files"] != manifest["files"] or current["event_count"] != event_count:
            raise ManifestError("a different ready archive already uses this replay ID")
        return current

    temporary = {
        name: f"tmp/{canonical_session_id}/{name}"
        for name in ARCHIVE_FILES
    }
    uploaded: list[str] = []
    try:
        for name, path in paths.items():
            row = files[name]
            store.upload_file(temporary[name], path, sha256=row["sha256"])
            uploaded.append(temporary[name])
        for name, row in files.items():
            store.verify(temporary[name], size=row["size"], sha256=row["sha256"])
            store.copy(temporary[name], row["key"])
            store.verify(row["key"], size=row["size"], sha256=row["sha256"])
        manifest_path = manifest_key(replay_session_id)
        store.put_json(manifest_path, manifest)
        stored = store.get_json(manifest_path, limit=MAX_MANIFEST_BYTES)
        if stored != manifest:
            raise StorageError("published manifest read-back mismatch")
        return manifest
    finally:
        for key in uploaded:
            try:
                store.delete(key)
            except StorageError:
                pass


class ObjectPreparationQueue:
    """Durable, idempotent queue represented by fixed S3 object keys."""

    _claim_lock = Lock()

    def __init__(
        self,
        store: Any,
        *,
        max_jobs: int = 8,
        daily_max: int = 4,
        max_attempts: int = 3,
        lease_seconds: int = 75 * 60,
    ) -> None:
        self.store = store
        self.max_jobs = max(1, min(max_jobs, 100))
        self.daily_max = max(1, min(daily_max, 100))
        self.max_attempts = max(1, min(max_attempts, 10))
        self.lease_seconds = max(60, lease_seconds)
        self._cache: tuple[float, dict[str, dict], dict[str, dict]] | None = None

    @staticmethod
    def _request_key(session_id: str) -> str:
        if not SESSION_ID.fullmatch(session_id):
            raise ValueError("invalid canonical session ID")
        return f"requests/{session_id}.json"

    @staticmethod
    def _status_key(session_id: str) -> str:
        if not SESSION_ID.fullmatch(session_id):
            raise ValueError("invalid canonical session ID")
        return f"status/{session_id}.json"

    @staticmethod
    def _validate_request(value: dict) -> dict:
        if (
            set(value) != {
                "schema_version", "job_id", "session_id", "fixture_stem",
                "generation", "created_at", "updated_at",
            }
            or value["schema_version"] != SCHEMA_VERSION
            or value["job_id"] != value["session_id"]
            or not isinstance(value["session_id"], str)
            or not SESSION_ID.fullmatch(value["session_id"])
            or not isinstance(value["fixture_stem"], str)
            or not REPLAY_ID.fullmatch(value["fixture_stem"])
            or not isinstance(value["generation"], int)
            or isinstance(value["generation"], bool)
            or value["generation"] < 1
        ):
            raise StorageError("stored preparation request is invalid")
        try:
            _parsed_time(value["created_at"])
            _parsed_time(value["updated_at"])
        except ValueError as exc:
            raise StorageError("stored preparation request is invalid") from exc
        return value

    @staticmethod
    def _validate_status(value: dict) -> dict:
        required = {
            "schema_version", "job_id", "session_id", "fixture_stem", "generation",
            "status", "created_at", "updated_at", "replay_session_id", "error",
            "attempts", "retry_at", "lease_expires_at",
        }
        if (
            set(value) != required
            or value["schema_version"] != SCHEMA_VERSION
            or value["job_id"] != value["session_id"]
            or not isinstance(value["session_id"], str)
            or not SESSION_ID.fullmatch(value["session_id"])
            or not isinstance(value["fixture_stem"], str)
            or not REPLAY_ID.fullmatch(value["fixture_stem"])
            or not isinstance(value["generation"], int)
            or isinstance(value["generation"], bool)
            or value["generation"] < 1
            or value["status"] not in {"queued", "processing", "ready", "failed"}
            or not isinstance(value["attempts"], int)
            or isinstance(value["attempts"], bool)
            or value["attempts"] < 0
            or (value["error"] is not None and not isinstance(value["error"], str))
        ):
            raise StorageError("stored preparation status is invalid")
        try:
            _parsed_time(value["created_at"])
            _parsed_time(value["updated_at"])
            if value["retry_at"] is not None:
                _parsed_time(value["retry_at"])
            if value["lease_expires_at"] is not None:
                _parsed_time(value["lease_expires_at"])
        except ValueError as exc:
            raise StorageError("stored preparation status is invalid") from exc
        replay = value["replay_session_id"]
        if replay is not None and (not isinstance(replay, str) or not REPLAY_ID.fullmatch(replay)):
            raise StorageError("stored preparation status is invalid")
        if value["status"] == "ready" and replay is None:
            raise StorageError("stored preparation status is invalid")
        return value

    def _objects(self, *, force: bool = False) -> tuple[dict[str, dict], dict[str, dict]]:
        if not force and self._cache is not None and time.monotonic() - self._cache[0] < _STATUS_TTL_S:
            return self._cache[1], self._cache[2]
        requests = {}
        statuses = {}
        for key in self.store.list_keys("requests/"):
            if not key.endswith(".json"):
                continue
            value = self.store.get_json(key, limit=MAX_RECORD_BYTES)
            if value is not None:
                request = self._validate_request(value)
                requests[request["session_id"]] = request
        for key in self.store.list_keys("status/"):
            if not key.endswith(".json"):
                continue
            value = self.store.get_json(key, limit=MAX_RECORD_BYTES)
            if value is not None:
                status = self._validate_status(value)
                statuses[status["session_id"]] = status
        self._cache = (time.monotonic(), requests, statuses)
        return requests, statuses

    def _record(self, request: dict | None, status: dict | None) -> dict | None:
        if request is None and status is None:
            return None
        if request is not None and (
            status is None or status["generation"] < request["generation"]
        ):
            return {
                **request,
                "status": "queued",
                "replay_session_id": None,
                "error": None,
                "attempts": 0,
                "retry_at": None,
                "lease_expires_at": None,
            }
        if status is None:
            return None
        record = dict(status)
        if record["status"] == "ready":
            try:
                manifest = load_manifest(
                    self.store,
                    record["replay_session_id"],
                    canonical_session_id=record["session_id"],
                )
                if not manifest_objects_match(self.store, manifest):
                    raise ManifestError("archive objects differ from manifest")
            except (ManifestError, StorageError):
                record.update(
                    status="failed",
                    replay_session_id=None,
                    error="Prepared archive failed integrity checks",
                )
        return record

    def records(self, *, force: bool = False) -> list[dict]:
        requests, statuses = self._objects(force=force)
        return [
            record
            for session_id in sorted(set(requests) | set(statuses))
            if (record := self._record(requests.get(session_id), statuses.get(session_id)))
        ]

    def get(self, session_id: str) -> dict | None:
        self._request_key(session_id)
        requests, statuses = self._objects()
        return self._record(requests.get(session_id), statuses.get(session_id))

    def enqueue(self, session_id: str, fixture_stem: str) -> tuple[dict, bool]:
        if not REPLAY_ID.fullmatch(fixture_stem):
            raise ValueError("invalid fixture stem")
        now = _now()
        # ponytail: one Render process serializes global caps; use a coordinator
        # if the public API is ever scaled to multiple writers.
        with self._claim_lock:
            requests, statuses = self._objects(force=True)
            existing = self._record(requests.get(session_id), statuses.get(session_id))
            if existing is not None and existing["status"] != "failed":
                return existing, False
            if existing is not None:
                request = requests.get(session_id) or {
                    "schema_version": SCHEMA_VERSION,
                    "job_id": session_id,
                    "session_id": session_id,
                    "fixture_stem": fixture_stem,
                    "generation": 0,
                    "created_at": existing["created_at"],
                    "updated_at": now,
                }
                request.update(
                    fixture_stem=fixture_stem,
                    generation=request["generation"] + 1,
                    updated_at=now,
                )
            else:
                records = [
                    record
                    for item in set(requests) | set(statuses)
                    if (record := self._record(requests.get(item), statuses.get(item)))
                ]
                if sum(record["status"] in {"queued", "processing"} for record in records) >= self.max_jobs:
                    raise QueueFullError("preparation queue is full")
                today = datetime.now(UTC).date()
                if sum(
                    _parsed_time(request["created_at"]).date() == today
                    for request in requests.values()
                ) >= self.daily_max:
                    raise QueueFullError("daily preparation limit reached")
                request = {
                    "schema_version": SCHEMA_VERSION,
                    "job_id": session_id,
                    "session_id": session_id,
                    "fixture_stem": fixture_stem,
                    "generation": 1,
                    "created_at": now,
                    "updated_at": now,
                }
            self.store.put_json(self._request_key(session_id), request)
            self._cache = None
            return self._record(request, None), True  # type: ignore[return-value]

    def claim_next(self, now: datetime | None = None) -> dict | None:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        # ponytail: one worker is the claim. Add conditional status writes only
        # when a second consumer is actually deployed.
        with self._claim_lock:
            requests, statuses = self._objects(force=True)
            ordered = sorted(requests.values(), key=lambda item: (item["created_at"], item["session_id"]))
            for request in ordered:
                status = statuses.get(request["session_id"])
                if status is not None and status["generation"] > request["generation"]:
                    continue
                if status is not None and status["generation"] == request["generation"]:
                    if status["status"] in {"ready", "failed"}:
                        continue
                    if status["retry_at"] and _parsed_time(status["retry_at"]) > current:
                        continue
                    if (
                        status["status"] == "processing"
                        and status["lease_expires_at"]
                        and _parsed_time(status["lease_expires_at"]) > current
                    ):
                        continue
                    attempts = status["attempts"]
                    if attempts >= self.max_attempts:
                        status.update(
                            status="failed",
                            updated_at=_now(),
                            replay_session_id=None,
                            error="Archive preparation stopped after repeated worker failures",
                            retry_at=None,
                            lease_expires_at=None,
                        )
                        self.store.put_json(self._status_key(request["session_id"]), status)
                        continue
                else:
                    attempts = 0
                stamp = _now()
                claimed = {
                    "schema_version": SCHEMA_VERSION,
                    "job_id": request["session_id"],
                    "session_id": request["session_id"],
                    "fixture_stem": request["fixture_stem"],
                    "generation": request["generation"],
                    "status": "processing",
                    "created_at": request["created_at"],
                    "updated_at": stamp,
                    "replay_session_id": None,
                    "error": None,
                    "attempts": attempts + 1,
                    "retry_at": None,
                    "lease_expires_at": (
                        current + timedelta(seconds=self.lease_seconds)
                    ).isoformat().replace("+00:00", "Z"),
                }
                self.store.put_json(self._status_key(request["session_id"]), claimed)
                self._cache = None
                return claimed
        return None

    def finish(
        self,
        session_id: str,
        *,
        replay_session_id: str | None = None,
        error: str | None = None,
    ) -> dict:
        with self._claim_lock:
            requests, statuses = self._objects(force=True)
            request = requests.get(session_id)
            status = statuses.get(session_id)
            if request is None and status is None:
                if replay_session_id is None:
                    raise KeyError(session_id)
                now = _now()
                request = {
                    "session_id": session_id,
                    "fixture_stem": replay_session_id,
                    "generation": 1,
                    "created_at": now,
                }
            generation = request["generation"] if request else status["generation"]
            attempts = status["attempts"] if status and status["generation"] == generation else 1
            stamp = _now()
            if error and attempts < self.max_attempts:
                retry_at = datetime.now(UTC) + _BACKOFF[min(attempts - 1, len(_BACKOFF) - 1)]
                state = "queued"
                safe_error = "Temporary archive preparation failure; retrying"
            elif error:
                retry_at = None
                state = "failed"
                safe_error = error[:300]
            else:
                if replay_session_id is None:
                    raise ValueError("ready status requires a replay session ID")
                load_manifest(
                    self.store, replay_session_id, canonical_session_id=session_id,
                )
                retry_at = None
                state = "ready"
                safe_error = None
            record = {
                "schema_version": SCHEMA_VERSION,
                "job_id": session_id,
                "session_id": session_id,
                "fixture_stem": request["fixture_stem"] if request else status["fixture_stem"],
                "generation": generation,
                "status": state,
                "created_at": request["created_at"] if request else status["created_at"],
                "updated_at": stamp,
                "replay_session_id": replay_session_id if state == "ready" else None,
                "error": safe_error,
                "attempts": attempts,
                "retry_at": retry_at.isoformat().replace("+00:00", "Z") if retry_at else None,
                "lease_expires_at": None,
            }
            self.store.put_json(self._status_key(session_id), record)
            self._cache = None
            return record


class RemoteSessionCache:
    """Lazy verified on-disk cache; object storage remains the source of truth."""

    _lock = Lock()

    def __init__(
        self,
        store: Any,
        directory: Path,
        *,
        max_bytes: int = 160 * 1024 * 1024,
    ) -> None:
        self.store = store
        self.directory = Path(directory)
        self.max_bytes = max(MAX_SESSION_BYTES, max_bytes)
        self._active: dict[str, int] = {}
        self._stats = {
            "materializations": 0, "hits": 0, "misses": 0, "evictions": 0, "bytes": 0,
        }

    @staticmethod
    def _size(path: Path) -> int:
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())

    def _evict(self) -> None:
        entries = [
            path for path in self.directory.iterdir()
            if path.is_dir() and REPLAY_ID.fullmatch(path.name)
        ]
        total = sum(self._size(path) for path in entries)
        for path in sorted(entries, key=lambda item: item.stat().st_mtime):
            if total <= self.max_bytes:
                break
            if self._active.get(path.name, 0):
                continue
            size = self._size(path)
            shutil.rmtree(path)
            total -= size
            self._stats["evictions"] += 1

    def _materialize(self, replay_session_id: str) -> Path:
        if not REPLAY_ID.fullmatch(replay_session_id):
            raise ValueError("invalid replay session ID")
        manifest = load_manifest(self.store, replay_session_id)
        marker = hashlib.sha256(
            json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.directory / replay_session_id
        ready = target / ".manifest-sha256"
        complete = all(
            (path := target / f"{replay_session_id}{ARCHIVE_FILES[name]}").is_file()
            and path.stat().st_size == row["size"]
            for name, row in manifest["files"].items()
        )
        if (
            complete
            and ready.is_file()
            and ready.read_text(encoding="ascii").strip() == marker
        ):
            os.utime(target)
            self._stats["hits"] += 1
            return target
        self._stats["misses"] += 1
        temporary = Path(tempfile.mkdtemp(prefix=".session-", dir=self.directory))
        try:
            for name, suffix in ARCHIVE_FILES.items():
                row = manifest["files"][name]
                self.store.download_verified(
                    row["key"],
                    temporary / f"{replay_session_id}{suffix}",
                    size=row["size"],
                    sha256=row["sha256"],
                )
            (temporary / ".manifest-sha256").write_text(marker + "\n", encoding="ascii")
            if target.exists():
                shutil.rmtree(target)
            os.replace(temporary, target)
            self._stats["materializations"] += 1
            self._stats["bytes"] += sum(row["size"] for row in manifest["files"].values())
            return target
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    @contextmanager
    def lease(self, replay_session_id: str) -> Iterator[Path]:
        """Keep a materialized session alive until its caller finishes reading."""
        with self._lock:
            target = self._materialize(replay_session_id)
            self._active[replay_session_id] = self._active.get(replay_session_id, 0) + 1
            self._evict()
        try:
            yield target
        finally:
            with self._lock:
                remaining = self._active[replay_session_id] - 1
                if remaining:
                    self._active[replay_session_id] = remaining
                else:
                    del self._active[replay_session_id]
                # ponytail: retry globally; per-session eviction queues only matter at scale.
                self._evict()

    def stats(self) -> dict[str, int]:
        with self._lock:
            disk_bytes = self._size(self.directory) if self.directory.is_dir() else 0
            return {
                **self._stats,
                "leases": sum(self._active.values()),
                "disk_bytes": disk_bytes,
                "max_bytes": self.max_bytes,
            }
