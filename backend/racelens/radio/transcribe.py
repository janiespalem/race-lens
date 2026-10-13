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

from racelens.radio.models import (
    CURRENT_RADIO_POLICY,
    CURRENT_RADIO_PROFILE,
    FinalTranscriptDecision,
    RadioSummary,
)

MODEL_NAME = "medium"  # spike winner; large-v3-int8 was slower AND noisier
MAX_AUDIO_BYTES = 10 * 1024 * 1024
TRANSCRIPT_METADATA = {
    "model": MODEL_NAME,
    "profile": "final-medium-int8-word-gated",
    "version": 3,
}
MIN_SEGMENT_AVG_LOGPROB = -0.75
MIN_WORD_PROBABILITY = 0.20
MIN_LOW_CONFIDENCE_WORDS = 2
MAX_LOW_CONFIDENCE_WORD_RATIO = 0.07


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


def _quality_metadata(
    segments: list[Any], *, inspect_words: bool,
) -> tuple[dict[str, Any], str | None]:
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
    if inspect_words:
        probabilities = [
            float(word.probability)
            for segment in segments
            for word in (segment.words or [])
        ]
        low_confidence_words = sum(
            probability < MIN_WORD_PROBABILITY for probability in probabilities
        )
        low_confidence_word_ratio = (
            low_confidence_words / len(probabilities) if probabilities else 1.0
        )
        quality.update({
            "words": len(probabilities),
            "low_confidence_words": low_confidence_words,
            "min_required_word_probability": MIN_WORD_PROBABILITY,
            "max_low_confidence_word_ratio": MAX_LOW_CONFIDENCE_WORD_RATIO,
        })
        if not probabilities:
            return quality, "missing_word_confidence"
        # Count plus density tolerates one weak proper noun in an otherwise clear clip.
        if (
            low_confidence_words >= MIN_LOW_CONFIDENCE_WORDS
            and low_confidence_word_ratio >= MAX_LOW_CONFIDENCE_WORD_RATIO
        ):
            return quality, "low_word_confidence"
    return quality, None


def _transcribe_result(
    url: str, *, inspect_words: bool,
) -> TranscriptionResult | None:
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
            options = {"language": "en", "vad_filter": True}
            if inspect_words:
                options["word_timestamps"] = True
            generated, _ = model.transcribe(tmp.name, **options)
            segments = [segment for segment in generated if segment.text.strip()]
            metadata = dict(TRANSCRIPT_METADATA)
            if not segments:
                metadata.update({"status": "rejected", "reason": "no_speech"})
                return TranscriptionResult(None, metadata)
            quality, reason = _quality_metadata(segments, inspect_words=inspect_words)
            metadata["quality"] = quality
            if reason is not None:
                metadata.update({"status": "rejected", "reason": reason})
                return TranscriptionResult(None, metadata)
            metadata["status"] = "accepted"
            text = "\n".join(segment.text.strip() for segment in segments).strip()
        return TranscriptionResult(text, metadata)
    except Exception as exc:  # network/codec hiccups must not kill the caller
        if not inspect_words:
            print(f"radio transcribe failed for {url}: {exc}", file=sys.stderr)
        return None


def transcribe_result(url: str) -> TranscriptionResult | None:
    """Legacy word-confidence result; durable callers use transcribe_final()."""
    return _transcribe_result(url, inspect_words=True)


def transcribe_final(url: str) -> FinalTranscriptDecision:
    """Return a terminal quality decision or a retryable processing failure."""
    metadata = {
        **TRANSCRIPT_METADATA,
        "profile": CURRENT_RADIO_PROFILE,
        "policy": CURRENT_RADIO_POLICY,
    }
    try:
        result = _transcribe_result(url, inspect_words=True)
    except Exception:
        result = None
    if result is None:
        metadata.update({"status": "deferred", "reason": "processing_failed"})
        return FinalTranscriptDecision("deferred", None, "processing_failed", metadata)
    metadata.update(result.metadata)
    metadata["profile"] = CURRENT_RADIO_PROFILE
    if result.text is None:
        reason = metadata["reason"]
        return FinalTranscriptDecision("rejected", None, reason, metadata)
    return FinalTranscriptDecision("accepted", result.text, None, metadata)


def transcribe(url: str) -> str | None:
    """Return a fast, segment-confidence-gated transcript for live mode."""
    result = _transcribe_result(url, inspect_words=False)
    return result.text if result is not None else None


class IncompleteTranscriptionError(RuntimeError):
    """A fixture still contains radio clips requiring retry."""


def _radio_state(payload: dict[str, Any]) -> str:
    text = payload.get("transcript")
    if text is not None and not isinstance(text, str):
        raise ValueError("invalid radio transcript")
    metadata = payload.get("transcript_metadata")
    if metadata is None:
        return "manual" if text else "unprocessed"
    if not isinstance(metadata, dict):
        raise ValueError("invalid radio transcript metadata")
    profile = metadata.get("profile")
    policy = metadata.get("policy")
    if (
        profile == CURRENT_RADIO_PROFILE
        and "model" in metadata
        and metadata["model"] != MODEL_NAME
    ):
        raise ValueError("radio model contradicts transcript profile")
    legacy_versions = {
        "current-medium-int8": 1,
        "current-medium-int8-quality-gated": 2,
        "final-medium-int8-word-gated": 3,
    }
    legacy = (
        isinstance(profile, str)
        and profile in legacy_versions
        and metadata.get("model") == MODEL_NAME
        and type(metadata.get("version")) is int
        and metadata["version"] == legacy_versions[profile]
        and policy is None
    )
    if not legacy and not (
        isinstance(profile, str) and profile
        and isinstance(policy, str) and policy
    ):
        raise ValueError("invalid radio transcript provenance")
    if "quality" in metadata and not isinstance(metadata["quality"], dict):
        raise ValueError("invalid radio transcript quality")
    status = metadata.get("status")
    if legacy and metadata["version"] == 1 and status is None and text:
        return "stale"
    if status == "accepted":
        if not text or metadata.get("reason") is not None:
            raise ValueError("invalid accepted radio transcript")
    elif status == "rejected":
        reason = metadata.get("reason")
        if text or not isinstance(reason, str) or not reason:
            raise ValueError("invalid rejected radio transcript")
    else:
        raise ValueError("invalid radio transcript status")
    if profile == CURRENT_RADIO_PROFILE and policy == CURRENT_RADIO_POLICY:
        return status
    return "stale"


def summarize_fixture(path: Path) -> RadioSummary:
    """Summarize only complete fixtures with current machine provenance."""
    from racelens.events.models import load_jsonl

    counts = {"accepted": 0, "rejected": 0, "manual": 0}
    for item in load_jsonl(path.read_text(encoding="utf-8")):
        if item.payload.get("category") != "Radio":
            continue
        state = _radio_state(item.payload)
        if state not in counts:
            raise ValueError("radio transcription is incomplete or stale")
        counts[state] += 1
    return RadioSummary(
        CURRENT_RADIO_PROFILE, CURRENT_RADIO_POLICY,
        sum(counts.values()), counts["accepted"], counts["rejected"], counts["manual"],
    )


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
    Returns the number of transcripts written. Deferred clips raise
    IncompleteTranscriptionError after terminal decisions are checkpointed.
    """
    import json

    from racelens.events.models import dump_jsonl, load_jsonl, make_event_id
    from racelens.recorder.postprocess import atomic_write_text

    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    events = load_jsonl("\n".join(lines))
    states = [
        _radio_state(item.payload) if item.payload.get("category") == "Radio" else None
        for item in events
    ]
    done = 0
    deferred = 0
    changed = False
    for i, ln in enumerate(lines):
        e = json.loads(ln)
        p = e.get("payload", {})
        url = p.get("audio_url")
        if states[i] in {"unprocessed", "stale"}:
            if not url:
                deferred += 1
                continue
            result = transcribe_final(url)
            if result.status == "deferred":
                deferred += 1
            else:
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
                changed = True
                # Write after every clip: a killed run keeps its progress and a
                # re-run resumes from the first missing transcript.
                atomic_write_text(path, "\n".join(lines) + "\n")
                print(f"  radio transcription: {result.status}", file=sys.stderr)
    if changed or not deferred:
        events = load_jsonl("\n".join(lines))
        events.sort(key=lambda event_: (event_.session_time_ms, event_.event_id))
        atomic_write_text(path, dump_jsonl(events))
    if deferred:
        raise IncompleteTranscriptionError(f"{deferred} radio clips deferred; retry required")
    return done
