"""Team-radio transcription: faster-whisper medium-int8 on CPU.

Model choice is the 2026-07-07 spike verdict on 20 real Silverstone clips:
medium-int8 ≈ 12 s/clip on 8 cores and read cleaner than large-v3-int8
(which hallucinated on noisy clips). ~20 clips per race — the pace works
for live too (a clip's text lands in the feed within a minute).

Two consumers:
  * CLI `radio-transcribe <fixture>` — enrich replay fixtures in place;
  * TranscriptWorker — background thread for live mode (never blocks polls).

faster-whisper is an optional dependency ([whisper]); without it transcribe()
returns None and the worker stays idle instead of crashing the API.
"""
from __future__ import annotations

import gc
import sys
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

MODEL_NAME = "medium"  # spike winner; large-v3-int8 was slower AND noisier
MAX_AUDIO_BYTES = 10 * 1024 * 1024
TRANSCRIPT_METADATA = {
    "model": MODEL_NAME,
    "profile": "current-medium-int8-quality-gated",
    "version": 2,
}
# Conservative screen: rejected 2/5 visibly corrupted Singapore samples and 0/4 controls.
MIN_SEGMENT_AVG_LOGPROB = -0.75


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    text: str | None
    metadata: dict[str, Any]


@lru_cache(maxsize=1)
def _model():
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    return WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")


def _quality_metadata(segments: list[Any]) -> tuple[dict[str, Any], str | None]:
    min_avg_logprob = min(segment.avg_logprob for segment in segments)
    quality = {
        "segments": len(segments),
        "min_avg_logprob": round(min_avg_logprob, 4),
        "min_required_avg_logprob": MIN_SEGMENT_AVG_LOGPROB,
        "max_no_speech_prob": round(max(segment.no_speech_prob for segment in segments), 4),
        "max_compression_ratio": round(max(segment.compression_ratio for segment in segments), 4),
    }
    if min_avg_logprob < MIN_SEGMENT_AVG_LOGPROB:
        return quality, "low_log_probability"
    return quality, None


def transcribe_result(url: str) -> TranscriptionResult | None:
    """Download and inspect one radio clip. None only on processing failure."""
    model = _model()
    if model is None:
        return None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3") as tmp:
            with urllib.request.urlopen(url, timeout=30) as resp:
                audio = resp.read(MAX_AUDIO_BYTES + 1)
                if len(audio) > MAX_AUDIO_BYTES:
                    raise ValueError("radio clip exceeds byte limit")
                tmp.write(audio)
            tmp.flush()
            generated, _ = model.transcribe(tmp.name, language="en", vad_filter=True)
            segments = [segment for segment in generated if segment.text.strip()]
            metadata = dict(TRANSCRIPT_METADATA)
            if not segments:
                metadata.update({"status": "rejected", "reason": "no_speech"})
                return TranscriptionResult(None, metadata)
            quality, reason = _quality_metadata(segments)
            metadata["quality"] = quality
            if reason is not None:
                metadata.update({"status": "rejected", "reason": reason})
                return TranscriptionResult(None, metadata)
            metadata["status"] = "accepted"
            text = "\n".join(segment.text.strip() for segment in segments).strip()
        return TranscriptionResult(text, metadata)
    except Exception as exc:  # network/codec hiccups must not kill the caller
        print(f"radio transcribe failed for {url}: {exc}", file=sys.stderr)
        return None


def transcribe(url: str) -> str | None:
    """Compatibility wrapper returning only quality-gated text."""
    result = transcribe_result(url)
    return result.text if result is not None else None


class TranscriptWorker:
    """Live-mode transcript cache: one background thread, in-memory results.

    get() never blocks — unknown urls are queued and return None until the
    worker finishes them. The cache is intentionally in-memory; post-race
    fixture enrichment through the CLI is the durable path.
    """

    def __init__(self) -> None:
        self._cache: dict[str, str | None] = {}  # url -> text (None = queued/failed)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="radio-whisper")

    def get(self, url: str) -> str | None:
        if url not in self._cache:
            self._cache[url] = None
            self._pool.submit(self._work, url)
        return self._cache[url]

    def _work(self, url: str) -> None:
        text = transcribe(url)
        if text:
            self._cache[url] = text

    def close(self, *, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait, cancel_futures=not wait)
        self._cache.clear()
        _model.cache_clear()
        gc.collect()


def enrich_fixture(path: Path) -> int:
    """Add payload["transcript"] to radio events of a fixture jsonl, in place.

    Preserves manual transcripts and rechecks machine transcripts from older profiles.
    Returns the number of transcripts written.
    """
    import json

    from racelens.events.models import dump_jsonl, load_jsonl, make_event_id
    from racelens.recorder.postprocess import atomic_write_text

    lines = path.read_text(encoding="utf-8").splitlines()
    done = 0
    for i, ln in enumerate(lines):
        e = json.loads(ln)
        p = e.get("payload", {})
        url = p.get("audio_url")
        metadata = p.get("transcript_metadata")
        current_machine_result = (
            isinstance(metadata, dict)
            and metadata.get("version") == TRANSCRIPT_METADATA["version"]
            and metadata.get("profile") == TRANSCRIPT_METADATA["profile"]
        )
        manual_transcript = bool(p.get("transcript")) and metadata is None
        if (
            p.get("category") == "Radio"
            and url
            and not manual_transcript
            and not current_machine_result
        ):
            result = transcribe_result(url)
            if result is not None:
                p.pop("transcript", None)
                p["transcript_metadata"] = result.metadata
                if result.text:
                    p["transcript"] = result.text
                    done += 1
                e["event_id"] = make_event_id(
                    e["session_id"], e["type"], e["session_time_ms"],
                    e.get("driver_id"), p,
                )
                lines[i] = json.dumps(e, ensure_ascii=False, separators=(",", ":"))
                # Write after every clip: a killed run keeps its progress and a
                # re-run resumes from the first missing transcript.
                atomic_write_text(path, "\n".join(lines) + "\n")
                summary = result.text[:70] if result.text else result.metadata["reason"]
                print(f"  {e.get('driver_id')}: {summary}", file=sys.stderr)
    events = load_jsonl("\n".join(lines))
    events.sort(key=lambda event_: (event_.session_time_ms, event_.event_id))
    atomic_write_text(path, dump_jsonl(events))
    return done
