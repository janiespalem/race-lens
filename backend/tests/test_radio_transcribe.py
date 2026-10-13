"""Radio transcription plumbing (model itself is not exercised — too heavy)."""
import json
from pathlib import Path

import pytest

from racelens import cli
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


class _Segment:
    def __init__(
        self, text, avg_logprob, no_speech_prob, compression_ratio, word_probabilities=None,
    ):
        self.text = text
        self.avg_logprob = avg_logprob
        self.no_speech_prob = no_speech_prob
        self.compression_ratio = compression_ratio
        self.words = [
            type("Word", (), {"probability": probability})()
            for probability in (word_probabilities or [0.9])
        ]


def _stub_transcription(monkeypatch, segment, calls=None) -> None:
    class Model:
        @staticmethod
        def transcribe(*_args, **kwargs):
            if calls is not None:
                calls.append(kwargs)
            return [segment], None

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        @staticmethod
        def read(_limit):
            return b"audio"

    monkeypatch.setattr(rt, "_model", lambda: Model())
    monkeypatch.setattr(rt.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())


def test_feed_carries_transcript() -> None:
    e = event(
        "s", "RaceControlMessage", 1000, "HAM",
        category="Radio", message="RADIO: HAM",
        audio_url="https://x/clip.mp3", transcript="box box",
    )
    items = render_feed([e], until_ms=2000)
    radio = [i for i in items if i.get("audio_url")]
    assert radio and radio[0]["transcript"] == "box box"


def test_transcribe_final_returns_accepted_decision(monkeypatch) -> None:
    calls = []
    _stub_transcription(monkeypatch, _Segment("Box this lap.", -0.42, 0.03, 1.2), calls)

    decision = rt.transcribe_final("https://provider.test/radio.mp3")

    assert decision.status == "accepted"
    assert decision.text == "Box this lap."
    assert decision.reason is None
    assert decision.metadata["profile"] == "medium-int8-word-timestamps-v1"
    assert decision.metadata["policy"] == "f1-confidence-gate-v1"
    assert decision.metadata["status"] == "accepted"
    assert decision.metadata["quality"]["words"] == 1
    assert calls == [{"language": "en", "vad_filter": True, "word_timestamps": True}]


def test_transcribe_final_returns_rejected_decision(monkeypatch) -> None:
    _stub_transcription(monkeypatch, _Segment("Uncertain words.", -0.81, 0.12, 1.1))

    decision = rt.transcribe_final("https://provider.test/radio.mp3")

    assert decision.status == "rejected"
    assert decision.text is None
    assert decision.reason == "low_log_probability"
    assert decision.metadata["status"] == "rejected"
    assert decision.metadata["reason"] == "low_log_probability"
    assert decision.metadata["quality"]["min_avg_logprob"] == pytest.approx(-0.81)


@pytest.mark.parametrize("failure_at", ["model", "download", "inference"])
def test_transcribe_final_returns_deferred_without_sensitive_error(
    monkeypatch, capsys, failure_at,
) -> None:
    url = "https://provider.test/radio.mp3?secret=credential"
    sensitive_error = "private provider response"
    _stub_transcription(monkeypatch, _Segment("Box this lap.", -0.42, 0.03, 1.2))

    def fail(*_args, **_kwargs):
        raise RuntimeError(f"{url}: {sensitive_error}")

    if failure_at == "model":
        monkeypatch.setattr(rt, "_model", fail)
    elif failure_at == "download":
        monkeypatch.setattr(rt.urllib.request, "urlopen", fail)
    else:
        model = rt._model()
        model.transcribe = fail
        monkeypatch.setattr(rt, "_model", lambda: model)

    decision = rt.transcribe_final(url)

    assert decision.status == "deferred"
    assert decision.text is None
    assert decision.reason == "processing_failed"
    assert url not in json.dumps(decision.metadata)
    assert sensitive_error not in json.dumps(decision.metadata)
    logs = capsys.readouterr().err
    assert url not in logs
    assert sensitive_error not in logs


def test_transcribe_final_defers_unavailable_model(monkeypatch) -> None:
    monkeypatch.setattr(rt, "_model", lambda: None)

    decision = rt.transcribe_final("https://provider.test/radio.mp3")

    assert decision.status == "deferred"
    assert decision.text is None
    assert decision.reason == "processing_failed"


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


def test_transcribe_result_rejects_low_segment_confidence(monkeypatch) -> None:
    _stub_transcription(monkeypatch, _Segment(
        "I feel softly licking between my feet.", -0.81, 0.12, 1.1,
    ))

    result = rt.transcribe_result("https://provider.test/radio.mp3")

    assert result is not None
    assert result.text is None
    assert result.metadata["status"] == "rejected"
    assert result.metadata["reason"] == "low_log_probability"
    assert result.metadata["quality"]["min_avg_logprob"] == pytest.approx(-0.81)


def test_transcribe_result_keeps_confident_text_and_diagnostics(monkeypatch) -> None:
    _stub_transcription(monkeypatch, _Segment("Box this lap.", -0.42, 0.03, 1.2))

    result = rt.transcribe_result("https://provider.test/radio.mp3")

    assert result is not None
    assert result.text == "Box this lap."
    assert result.metadata == {
        "model": "medium",
        "profile": "final-medium-int8-word-gated",
        "version": 3,
        "status": "accepted",
        "quality": {
            "segments": 1,
            "min_avg_logprob": pytest.approx(-0.42),
            "min_required_avg_logprob": -0.75,
            "max_no_speech_prob": pytest.approx(0.03),
            "max_compression_ratio": pytest.approx(1.2),
            "words": 1,
            "low_confidence_words": 0,
            "max_low_confidence_word_ratio": 0.07,
            "min_required_word_probability": 0.2,
        },
    }


def test_final_transcript_rejects_dense_low_confidence_words(monkeypatch) -> None:
    calls = []
    _stub_transcription(
        monkeypatch,
        _Segment(
            "Mr Chuck Limmons, keep an eye on this.",
            -0.53,
            0.03,
            1.2,
            [0.19, 0.59, 0.18, 0.72, 0.99, 0.99, 0.99, 0.98],
        ),
        calls,
    )

    result = rt.transcribe_result("https://provider.test/radio.mp3")

    assert result is not None
    assert result.text is None
    assert result.metadata["reason"] == "low_word_confidence"
    assert result.metadata["quality"]["low_confidence_words"] == 2
    assert calls == [{"language": "en", "vad_filter": True, "word_timestamps": True}]


def test_final_transcript_tolerates_one_weak_word(monkeypatch) -> None:
    _stub_transcription(
        monkeypatch,
        _Segment(
            "Car is trying to pick up the pace.",
            -0.33,
            0.03,
            1.2,
            [0.09, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9],
        ),
    )

    result = rt.transcribe_result("https://provider.test/radio.mp3")

    assert result is not None
    assert result.text == "Car is trying to pick up the pace."
    assert result.metadata["status"] == "accepted"


def test_final_transcript_tolerates_sparse_weak_words(monkeypatch) -> None:
    _stub_transcription(
        monkeypatch,
        _Segment(
            "A long otherwise clear radio message.",
            -0.33,
            0.03,
            1.2,
            [0.1, 0.1, *([0.9] * 38)],
        ),
    )

    result = rt.transcribe_result("https://provider.test/radio.mp3")

    assert result is not None
    assert result.text == "A long otherwise clear radio message."
    assert result.metadata["status"] == "accepted"


def test_final_transcript_rejects_missing_word_confidence(monkeypatch) -> None:
    segment = _Segment("Box this lap.", -0.42, 0.03, 1.2)
    segment.words = []
    _stub_transcription(monkeypatch, segment)

    result = rt.transcribe_result("https://provider.test/radio.mp3")

    assert result is not None
    assert result.text is None
    assert result.metadata["reason"] == "missing_word_confidence"


def test_live_transcript_keeps_fast_profile(monkeypatch) -> None:
    calls = []
    _stub_transcription(
        monkeypatch,
        _Segment("Box this lap.", -0.42, 0.03, 1.2, [0.1, 0.1, 0.9]),
        calls,
    )

    assert rt.transcribe("https://provider.test/radio.mp3") == "Box this lap."
    assert calls == [{"language": "en", "vad_filter": True}]


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

    monkeypatch.setattr(
        rt,
        "transcribe_final",
        lambda url: rt.FinalTranscriptDecision(
            status="accepted",
            text=f"text for {url}",
            reason=None,
            metadata=_current_metadata("accepted"),
        ),
    )
    assert rt.enrich_fixture(fx) == 1  # only the transcript-less radio event

    got = [json.loads(ln) for ln in fx.read_text().splitlines()]
    assert got[0]["payload"]["transcript"] == "text for https://x/1.mp3"
    assert got[0]["payload"]["transcript_metadata"] == {
        "model": "medium",
        "profile": "medium-int8-word-timestamps-v1",
        "policy": "f1-confidence-gate-v1",
        "version": 3,
        "status": "accepted",
    }
    assert got[1]["payload"]["transcript"] == "old"  # untouched
    assert "transcript" not in got[2]["payload"]
    assert got[0]["event_id"] == make_event_id(
        "s", "RaceControlMessage", 1, "HAM", got[0]["payload"]
    )


def test_enrich_fixture_rechecks_old_machine_text_but_preserves_manual_text(
    tmp_path, monkeypatch,
) -> None:
    fx = tmp_path / "r.jsonl"
    rows = [
        {"event_id": "a", "session_id": "s", "type": "RaceControlMessage",
         "session_time_ms": 1, "driver_id": "ANT", "source": "fixture",
         "confidence": "high",
         "payload": {"category": "Radio", "message": "RADIO: ANT",
                     "audio_url": "https://x/old-machine.mp3",
                     "transcript": "I feel softly licking between my feet.",
                     "transcript_metadata": {"model": "medium",
                                             "profile": "current-medium-int8",
                                             "version": 1}}},
        {"event_id": "b", "session_id": "s", "type": "RaceControlMessage",
         "session_time_ms": 2, "driver_id": "VER", "source": "fixture",
         "confidence": "high",
         "payload": {"category": "Radio", "message": "RADIO: VER",
                     "audio_url": "https://x/manual.mp3", "transcript": "Manual text"}},
    ]
    fx.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    rejected = rt.FinalTranscriptDecision(
        status="rejected",
        text=None,
        reason="low_log_probability",
        metadata=_current_metadata("rejected"),
    )
    calls = []
    monkeypatch.setattr(
        rt,
        "transcribe_final",
        lambda url: calls.append(url) or rejected,
    )

    assert rt.enrich_fixture(fx) == 0

    got = [json.loads(line) for line in fx.read_text().splitlines()]
    assert calls == ["https://x/old-machine.mp3"]
    assert "transcript" not in got[0]["payload"]
    assert got[0]["payload"]["transcript_metadata"]["status"] == "rejected"
    assert got[1]["payload"]["transcript"] == "Manual text"
    assert "transcript_metadata" not in got[1]["payload"]


def test_enrich_fixture_does_not_retry_current_rejection(tmp_path, monkeypatch) -> None:
    fx = tmp_path / "r.jsonl"
    row = {
        "event_id": "a", "session_id": "s", "type": "RaceControlMessage",
        "session_time_ms": 1, "driver_id": "ANT", "source": "fixture",
        "confidence": "high",
        "payload": {
            "category": "Radio", "message": "RADIO: ANT",
            "audio_url": "https://x/rejected.mp3",
            "transcript_metadata": _current_metadata("rejected"),
        },
    }
    fx.write_text(json.dumps(row) + "\n")
    monkeypatch.setattr(
        rt,
        "transcribe_final",
        lambda _url: pytest.fail("current result must not be reprocessed"),
    )

    assert rt.enrich_fixture(fx) == 0
    assert json.loads(fx.read_text())["payload"]["transcript_metadata"]["status"] == "rejected"


def test_enrich_fixture_restores_order_after_event_id_changes(tmp_path, monkeypatch) -> None:
    fx = tmp_path / "r.jsonl"
    radio = event(
        "s", "RaceControlMessage", 1, "HAM", category="Radio",
        message="RADIO: HAM", audio_url="https://x/1.mp3",
    )
    other = event("s", "RaceControlMessage", 1, category="Other", message="5")
    fx.write_text(dump_jsonl(sorted([radio, other], key=lambda item: item.event_id)))
    monkeypatch.setattr(
        rt,
        "transcribe_final",
        lambda url: rt.FinalTranscriptDecision(
            status="accepted",
            text=f"text for {url}",
            reason=None,
            metadata=_current_metadata("accepted"),
        ),
    )

    rt.enrich_fixture(fx)

    got = load_jsonl(fx.read_text())
    assert got == sorted(got, key=lambda item: (item.session_time_ms, item.event_id))


def _current_metadata(status: str) -> dict:
    metadata = {
        "model": "medium",
        "profile": "medium-int8-word-timestamps-v1",
        "policy": "f1-confidence-gate-v1",
        "version": 3,
        "status": status,
    }
    if status == "rejected":
        metadata["reason"] = "low_log_probability"
    return metadata


def _write_radio_fixture(tmp_path, payloads) -> Path:
    path = tmp_path / "radio.jsonl"
    events = [
        event(
            "s", "RaceControlMessage", index, "HAM", category="Radio",
            audio_url=f"https://provider.test/{index}.mp3", **payload,
        )
        for index, payload in enumerate(payloads)
    ]
    path.write_text(dump_jsonl(events), encoding="utf-8")
    return path


def test_enrich_fixture_reuses_current_accepted_and_preserves_manual_text(
    tmp_path, monkeypatch,
) -> None:
    path = _write_radio_fixture(tmp_path, [
        {"transcript": "  Manual text\n\t unchanged.  "},
        {"transcript": "Box this lap.", "transcript_metadata": _current_metadata("accepted")},
        {"transcript_metadata": _current_metadata("rejected")},
    ])
    original = load_jsonl(path.read_text())
    monkeypatch.setattr(
        rt, "transcribe_final", lambda _url: pytest.fail("terminal text must be reused"),
    )

    assert rt.enrich_fixture(path) == 0
    assert load_jsonl(path.read_text()) == original


def test_enrich_fixture_reprocesses_old_policy(tmp_path, monkeypatch) -> None:
    metadata = {**_current_metadata("accepted"), "policy": "older-confidence-policy"}
    path = _write_radio_fixture(tmp_path, [
        {"transcript": "Old machine text.", "transcript_metadata": metadata},
    ])
    _stub_transcription(monkeypatch, _Segment("Box this lap.", -0.42, 0.03, 1.2))

    assert rt.enrich_fixture(path) == 1
    payload = load_jsonl(path.read_text())[0].payload
    assert payload["transcript"] == "Box this lap."
    assert payload["transcript_metadata"]["policy"] == "f1-confidence-gate-v1"


def test_enrich_fixture_deferred_preserves_old_text_and_checkpoints_terminal_clips(
    tmp_path, monkeypatch, capsys,
) -> None:
    old = {
        "transcript": "Old machine text.",
        "transcript_metadata": {"model": "medium", "profile": "current-medium-int8", "version": 1},
    }
    path = _write_radio_fixture(tmp_path, [old, {}])
    original = load_jsonl(path.read_text())
    calls = []

    def transcribe(url):
        calls.append(url)
        if url.endswith("/0.mp3"):
            return rt.FinalTranscriptDecision("deferred", None, "processing_failed", {})
        return rt.FinalTranscriptDecision(
            "accepted", "New private text.", None, _current_metadata("accepted"),
        )

    monkeypatch.setattr(rt, "transcribe_final", transcribe)

    with pytest.raises(rt.IncompleteTranscriptionError, match="1.*deferred"):
        rt.enrich_fixture(path)
    saved = load_jsonl(path.read_text())
    assert saved[0] == original[0]
    assert saved[1].payload["transcript"] == "New private text."
    assert saved[1].event_id != original[1].event_id
    assert saved[1].event_id == make_event_id(
        "s", "RaceControlMessage", 1, "HAM", saved[1].payload,
    )
    with pytest.raises(rt.IncompleteTranscriptionError):
        rt.enrich_fixture(path)
    assert calls == [
        "https://provider.test/0.mp3", "https://provider.test/1.mp3",
        "https://provider.test/0.mp3",
    ]
    logs = capsys.readouterr().err
    assert "New private text." not in logs
    assert "https://provider.test" not in logs


def test_cli_radio_transcribe_exits_nonzero_on_deferred(tmp_path, monkeypatch, capsys) -> None:
    path = _write_radio_fixture(tmp_path, [{}])
    original = path.read_bytes()
    monkeypatch.setattr(
        rt, "transcribe_final",
        lambda _url: rt.FinalTranscriptDecision("deferred", None, "processing_failed", {}),
    )
    monkeypatch.setattr("sys.argv", ["racelens", "radio-transcribe", str(path)])

    with pytest.raises(SystemExit) as exited:
        cli.main()

    assert exited.value.code == 1
    assert path.read_bytes() == original
    assert "deferred" in capsys.readouterr().err


def test_summarize_fixture_counts_terminal_radio_only(tmp_path) -> None:
    path = _write_radio_fixture(tmp_path, [
        {"transcript": "  Manual text\n unchanged.  "},
        {"transcript": "Box.", "transcript_metadata": _current_metadata("accepted")},
        {"transcript_metadata": _current_metadata("rejected")},
        {"transcript": "Pit.", "transcript_metadata": _current_metadata("accepted")},
    ])
    path.write_text(
        path.read_text() + dump_jsonl([event("s", "PitIn", 5, "HAM")]), encoding="utf-8",
    )

    assert rt.summarize_fixture(path).to_manifest() == {
        "profile": "medium-int8-word-timestamps-v1",
        "policy": "f1-confidence-gate-v1",
        "total": 4,
        "accepted": 2,
        "rejected": 1,
        "manual": 1,
    }


@pytest.mark.parametrize("payload", [
    {"transcript_metadata": "not metadata"},
    {"transcript_metadata": {}},
    {"transcript": "Box.", "transcript_metadata": {"status": "accepted"}},
    {"transcript_metadata": _current_metadata("accepted")},
    {"transcript": "Box.", "transcript_metadata": _current_metadata("rejected")},
    {"transcript_metadata": _current_metadata("deferred")},
    {"transcript": 42},
])
def test_summarize_fixture_rejects_malformed_metadata(tmp_path, payload) -> None:
    path = _write_radio_fixture(tmp_path, [payload])

    with pytest.raises(ValueError):
        rt.summarize_fixture(path)


@pytest.mark.parametrize("payload", [
    {},
    {"transcript": "Old text.", "transcript_metadata": {
        "model": "medium", "profile": "current-medium-int8", "version": 1,
    }},
    {"transcript_metadata": {**_current_metadata("rejected"), "policy": "older-policy"}},
])
def test_summarize_fixture_rejects_incomplete_provenance(tmp_path, payload) -> None:
    path = _write_radio_fixture(tmp_path, [payload])

    with pytest.raises(ValueError):
        rt.summarize_fixture(path)


def test_enrich_fixture_rejects_ambiguous_ownership_before_transcribing(
    tmp_path, monkeypatch,
) -> None:
    path = _write_radio_fixture(tmp_path, [{}, {"transcript_metadata": {"status": "accepted"}}])
    original = path.read_bytes()
    monkeypatch.setattr(rt, "transcribe_final", lambda _url: pytest.fail("validate before inference"))

    with pytest.raises(ValueError):
        rt.enrich_fixture(path)

    assert path.read_bytes() == original


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
