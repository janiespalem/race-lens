"""Radio transcription plumbing (model itself is not exercised — too heavy)."""
import json
from pathlib import Path

import pytest

from racelens.commentary.feed import render_feed
from racelens.events.models import dump_jsonl, event, load_jsonl, make_event_id
from racelens.radio import transcribe as rt
from racelens.radio.evaluate import (
    CURRENT_PROFILE,
    EvaluationMetrics,
    evaluate_manifest,
    gate_profile,
    load_manifest,
    word_error_rate,
)


def test_feed_carries_transcript() -> None:
    e = event(
        "s", "RaceControlMessage", 1000, "HAM",
        category="Radio", message="RADIO: HAM",
        audio_url="https://x/clip.mp3", transcript="box box",
    )
    items = render_feed([e], until_ms=2000)
    radio = [i for i in items if i.get("audio_url")]
    assert radio and radio[0]["transcript"] == "box box"


def test_transcribe_rejects_oversized_radio_clip(monkeypatch) -> None:
    class Model:
        called = False

        def transcribe(self, *_args, **_kwargs):
            self.called = True
            return [], None

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        @staticmethod
        def read(limit):
            assert limit == 5
            return b"12345"

    model = Model()
    monkeypatch.setattr(rt, "MAX_AUDIO_BYTES", 4)
    monkeypatch.setattr(rt, "_model", lambda: model)
    monkeypatch.setattr(rt.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())

    assert rt.transcribe("https://provider.test/radio.mp3") is None
    assert not model.called


def test_enrich_fixture_fills_and_skips(tmp_path, monkeypatch) -> None:
    fx = tmp_path / "r.jsonl"
    rows = [
        {"event_id": "a", "session_id": "s", "type": "RaceControlMessage",
         "session_time_ms": 1, "driver_id": "HAM", "source": "fixture",
         "confidence": "high",
         "payload": {"category": "Radio", "message": "RADIO: HAM",
                     "audio_url": "https://x/1.mp3"}},
        {"event_id": "b", "session_id": "s", "type": "RaceControlMessage",
         "session_time_ms": 2, "driver_id": "VER", "source": "fixture",
         "confidence": "high",
         "payload": {"category": "Radio", "message": "RADIO: VER",
                     "audio_url": "https://x/2.mp3", "transcript": "old"}},
        {"event_id": "c", "session_id": "s", "type": "PitIn",
         "session_time_ms": 3, "driver_id": "HAM", "source": "fixture",
         "confidence": "high", "payload": {}},
    ]
    fx.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    monkeypatch.setattr(rt, "transcribe", lambda url: f"text for {url}")
    assert rt.enrich_fixture(fx) == 1  # only the transcript-less radio event

    got = [json.loads(ln) for ln in fx.read_text().splitlines()]
    assert got[0]["payload"]["transcript"] == "text for https://x/1.mp3"
    assert got[0]["payload"]["transcript_metadata"] == {
        "model": "medium",
        "profile": "current-medium-int8",
        "version": 1,
    }
    assert got[1]["payload"]["transcript"] == "old"  # untouched
    assert "transcript" not in got[2]["payload"]
    assert got[0]["event_id"] == make_event_id(
        "s", "RaceControlMessage", 1, "HAM", got[0]["payload"]
    )


def test_enrich_fixture_restores_order_after_event_id_changes(tmp_path, monkeypatch) -> None:
    fx = tmp_path / "r.jsonl"
    radio = event(
        "s", "RaceControlMessage", 1, "HAM", category="Radio",
        message="RADIO: HAM", audio_url="https://x/1.mp3",
    )
    other = event("s", "RaceControlMessage", 1, category="Other", message="5")
    fx.write_text(dump_jsonl(sorted([radio, other], key=lambda item: item.event_id)))
    monkeypatch.setattr(rt, "transcribe", lambda url: f"text for {url}")

    rt.enrich_fixture(fx)

    got = load_jsonl(fx.read_text())
    assert got == sorted(got, key=lambda item: (item.session_time_ms, item.event_id))


def test_live_worker_close_waits_and_releases_model(monkeypatch) -> None:
    completed: list[str] = []
    cleared: list[bool] = []
    monkeypatch.setattr(rt, "transcribe", lambda url: completed.append(url) or "box")
    monkeypatch.setattr(rt._model, "cache_clear", lambda: cleared.append(True))
    worker = rt.TranscriptWorker()
    worker.get("https://provider.test/radio.mp3")

    worker.close()

    assert completed == ["https://provider.test/radio.mp3"]
    assert cleared == [True]


def test_live_worker_close_can_cancel_queue_without_waiting(monkeypatch) -> None:
    shutdown = []
    cleared = []
    worker = rt.TranscriptWorker()
    monkeypatch.setattr(
        worker._pool,
        "shutdown",
        lambda **kwargs: shutdown.append(kwargs),
    )
    monkeypatch.setattr(rt._model, "cache_clear", lambda: cleared.append(True))

    worker.close(wait=False)

    assert shutdown == [{"wait": False, "cancel_futures": True}]
    assert cleared == [True]


def _evaluation_manifest(tmp_path: Path, count: int = 50) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "private-radio-reference.jsonl"
    rows = []
    for index in range(count):
        audio = tmp_path / f"clip-{index}.mp3"
        audio.write_bytes(b"audio")
        rows.append({
            "audio_path": audio.name,
            "transcript": "box box now verstappen",
            "keywords": ["box", "Verstappen"],
        })
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def test_radio_eval_requires_exactly_fifty_bounded_records(tmp_path) -> None:
    assert len(load_manifest(_evaluation_manifest(tmp_path))) == 50
    for count in (49, 51):
        with pytest.raises(ValueError, match="exactly 50"):
            load_manifest(_evaluation_manifest(tmp_path / str(count), count))


def test_radio_eval_metrics_and_profile_options(tmp_path) -> None:
    manifest = _evaluation_manifest(tmp_path)
    calls = []

    class Segment:
        text = "box box now verstappen"

    class Model:
        def __init__(self, name):
            self.name = name

        def transcribe(self, audio_path, **options):
            calls.append((self.name, Path(audio_path).name, options))
            return [Segment()], None

    report = evaluate_manifest(
        manifest,
        model_factory=lambda model, device, compute_type: Model(model),
    )
    assert report["profiles"][CURRENT_PROFILE.name]["wer"] == 0
    assert report["profiles"][CURRENT_PROFILE.name]["keyword_accuracy"] == 1
    assert len(calls) == 150
    prompted = [call for call in calls if call[0] == "medium" and "initial_prompt" in call[2]]
    assert len(prompted) == 50
    assert prompted[0][2]["condition_on_previous_text"] is False
    assert prompted[0][2]["vad_filter"] is True
    assert any(call[0] == "distil-large-v3" for call in calls)


def test_radio_eval_wer_and_default_gate_are_explicit() -> None:
    assert word_error_rate("Box, box!", "box now") == 0.5
    current = EvaluationMetrics(wer=0.5, keyword_accuracy=0.6, latency_s=10)
    passing = gate_profile(current, EvaluationMetrics(wer=0.4, keyword_accuracy=0.7, latency_s=12.5))
    failing = gate_profile(current, EvaluationMetrics(wer=0.41, keyword_accuracy=0.69, latency_s=12.6))
    assert passing == {
        "passed": True,
        "relative_wer_improvement": pytest.approx(0.2),
        "keyword_point_improvement": pytest.approx(0.1),
        "latency_ratio": pytest.approx(1.25),
    }
    assert failing["passed"] is False
