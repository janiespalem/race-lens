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
from functools import lru_cache
from pathlib import Path

MODEL_NAME = "medium"  # spike winner; large-v3-int8 was slower AND noisier
MAX_AUDIO_BYTES = 10 * 1024 * 1024
TRANSCRIPT_METADATA = {
    "model": MODEL_NAME,
    "profile": "current-medium-int8",
    "version": 1,
}


@lru_cache(maxsize=1)
def _model():
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    return WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")


def transcribe(url: str) -> str | None:
    """Download one radio clip and transcribe it. None on any failure."""
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
            segments, _ = model.transcribe(tmp.name, language="en", vad_filter=True)
            text = "\n".join(s.text.strip() for s in segments if s.text.strip()).strip()
        return text or None
    except Exception as exc:  # network/codec hiccups must not kill the caller
        print(f"radio transcribe failed for {url}: {exc}", file=sys.stderr)
        return None


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

    Skips events that already carry a transcript, so re-runs only fill gaps.
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
        if p.get("category") == "Radio" and url and not p.get("transcript"):
            text = transcribe(url)
            if text:
                p["transcript"] = text
                p["transcript_metadata"] = dict(TRANSCRIPT_METADATA)
                e["event_id"] = make_event_id(
                    e["session_id"], e["type"], e["session_time_ms"],
                    e.get("driver_id"), p,
                )
                done += 1
                lines[i] = json.dumps(e, ensure_ascii=False, separators=(",", ":"))
                # Write after every clip: a killed run keeps its progress and a
                # re-run resumes from the first missing transcript.
                atomic_write_text(path, "\n".join(lines) + "\n")
                print(f"  {e.get('driver_id')}: {text[:70]}", file=sys.stderr)
    events = load_jsonl("\n".join(lines))
    events.sort(key=lambda event_: (event_.session_time_ms, event_.event_id))
    atomic_write_text(path, dump_jsonl(events))
    return done
