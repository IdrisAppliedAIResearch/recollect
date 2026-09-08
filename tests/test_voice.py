"""Conversational turn boundaries without speech models or a microphone."""

from __future__ import annotations

import io
import json
import threading
import wave
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from recollect.config import RecollectConfig
from recollect.engine.voice import (
    MAX_FRAME_BYTES,
    SAMPLE_RATE,
    VoiceCancelled,
    VoiceListener,
    VoiceService,
    VoiceUnavailable,
    spoken_text,
    wav_bytes,
)
from recollect.engine.voice_vad import SpeechDetector as OnnxSpeechDetector


class Recognizer:
    def __init__(self, results=(), *, final_text=""):
        self.results = deque(results)
        self.final_text = final_text
        self.audio = []
        self.current = {}

    def AcceptWaveform(self, pcm):
        self.audio.append(pcm)
        final, self.current = self.results.popleft() if self.results else (
            False, {"partial": self.current.get("partial", "")},
        )
        return final

    def PartialResult(self):
        return json.dumps(self.current)

    def Result(self):
        return json.dumps(self.current)

    def FinalResult(self):
        return json.dumps({"text": self.final_text})


class Recognizers:
    def __init__(self, *, wakes=(), dictations=()):
        self.wakes = deque(wakes)
        self.dictations = deque(dictations)
        self.created = []

    def __call__(self, wake):
        queue = self.wakes if wake else self.dictations
        result = queue.popleft() if queue else Recognizer()
        self.created.append((wake, result))
        return result


def configuration(**kwargs):
    return RecollectConfig(embedding_model_path=Path("unused.gguf"), **kwargs)


def words(*entries):
    return [{"word": word, "start": start, "end": end}
            for word, start, end in entries]


def wake_result(end=0.032):
    return False, {
        "partial": "hey idris",
        "partial_result": words(("hey", 0.0, end / 2), ("idris", end / 2, end)),
    }


def audio(seconds=0.032, value=900):
    return np.full(round(SAMPLE_RATE * seconds), value, dtype="<i2").tobytes()


class SpeechDetector:
    def __init__(self):
        self.audio = []

    def __call__(self, pcm):
        assert len(pcm) == 1024, "VAD must always receive 512 mono samples"
        self.audio.append(pcm)
        return float(np.abs(np.frombuffer(pcm, dtype="<i2")).mean()) / 1000


def frames(listener, count, *, value=900):
    return [event for _ in range(count) for event in listener.feed(audio(value=value))]


def active_listener(*dictations, **kwargs):
    factory = Recognizers(wakes=[Recognizer([wake_result()])], dictations=dictations)
    detector = SpeechDetector()
    listener = VoiceListener(configuration(**kwargs), factory, detector)
    assert listener.feed(audio(value=0))[0]["state"] == "listening"
    return listener, factory, detector


def transcripts(events):
    return [event["text"] for event in events if event["type"] == "transcript"]


@pytest.mark.parametrize("result", [
    {"partial": "some ordinary conversation"},
    {"partial": "hey idris"},
    {"partial_result": words(("hey", 0, 0.1), ("recollecting", 0.1, 0.4))},
    {"partial_result": words(("hey", 0, 0.1), ("recollect", 0.1, 0.4))},
    {"partial_result": words(("idris", 0, 0.1), ("hey", 0.1, 0.4))},
    {"partial_result": words(("hey", 0, 0.1), ("[unk]", 0.1, 0.4))},
])
def test_ambient_speech_never_leaves_wake_listener(result):
    factory = Recognizers(wakes=[Recognizer([(False, result)])])
    listener = VoiceListener(configuration(), factory, SpeechDetector())

    assert listener.feed(audio()) == []
    assert listener.state == "waiting"
    assert [wake for wake, _ in factory.created] == [True]


@pytest.mark.parametrize("final", [False, True])
def test_delayed_wake_replays_exact_audio_after_phrase(final):
    phrase = wake_result(0.768)[1]
    if final:
        phrase = {"text": phrase["partial"], "result": phrase["partial_result"]}
    wake = Recognizer([(False, {}), (final, phrase)])
    command = Recognizer([(False, {"partial": "tell me the time"})])
    factory = Recognizers(wakes=[wake], dictations=[command])
    listener = VoiceListener(
        configuration(voice_wake_phrase="Hey Idris"), factory, SpeechDetector(),
    )
    first, second = audio(0.512, value=850), audio(0.512, value=900)

    assert listener.feed(first) == []
    events = listener.feed(second)

    assert b"".join(command.audio) == second[len(second) // 2:]
    assert events[0]["state"] == "listening"
    assert {"type": "speech_start"} in events
    assert transcripts(events) == []
    assert listener.state == "listening"


def test_wake_phrase_can_span_frames_without_losing_same_breath_command():
    wake = Recognizer([
        (False, {"partial_result": words(("hey", 0.1, 0.4))}),
        wake_result(0.896),
    ])
    command = Recognizer([
        (False, {"partial": "remember the"}),
        (False, {"partial": "remember the blue door"}),
    ], final_text="remember the blue door")
    listener = VoiceListener(
        configuration(), Recognizers(wakes=[wake], dictations=[command]),
        SpeechDetector(),
    )
    first, second = audio(0.512, value=850), audio(0.512, value=900)
    third = audio(0.256, value=950)

    assert listener.feed(first) == []
    assert transcripts(listener.feed(second)) == []
    assert transcripts(listener.feed(third)) == []
    assert b"".join(command.audio) == second[12_288:] + third
    assert transcripts(frames(listener, 44, value=0)) == ["remember the blue door"]


def test_wake_then_pause_still_accepts_a_later_command():
    command = Recognizer(final_text="what did we decide")
    listener, factory, _ = active_listener(command)

    assert frames(listener, 300, value=0) == []
    assert command.audio == []
    assert listener.state == "listening"
    assert {"type": "speech_start"} in frames(listener, 8)
    assert transcripts(frames(listener, 44, value=0)) == ["what did we decide"]
    assert sum(wake for wake, _ in factory.created) == 1


def test_paused_audio_is_discarded_and_resume_clears_stale_audio():
    old_command = Recognizer(final_text="old command")
    new_command = Recognizer(final_text="new command")
    old_wake = Recognizer([wake_result()])
    new_wake = Recognizer([wake_result()])
    factory = Recognizers(
        wakes=[old_wake, new_wake], dictations=[old_command, new_command],
    )
    listener = VoiceListener(configuration(), factory, SpeechDetector())
    listener.feed(audio(value=0))
    frames(listener, 8, value=850)
    old_audio = list(old_command.audio)

    assert listener.pause()["state"] == "paused"
    assert listener.feed(audio(value=900)) == []
    assert old_command.audio == old_audio
    assert listener.resume()["state"] == "waiting"
    fresh = audio(value=0)
    assert listener.feed(fresh)[0]["state"] == "listening"
    assert new_wake.audio == [fresh]
    frames(listener, 8, value=950)
    assert b"".join(new_command.audio) == audio(0.256, value=950)
    assert transcripts(frames(listener, 44, value=0)) == ["new command"]


def test_phrase_only_never_submits_or_returns_to_wake_detection():
    listener, factory, _ = active_listener()

    assert frames(listener, 1000, value=0) == []
    assert listener.state == "listening"
    assert sum(wake for wake, _ in factory.created) == 1


def test_maximum_utterance_preserves_text_for_review_without_submitting():
    command = Recognizer(final_text="a long command ends here")
    command.results.append((False, {"partial": "a long command"}))
    listener, factory, _ = active_listener(
        command, voice_wait_s=1, voice_max_utterance_s=2,
    )

    assert transcripts(frames(listener, 60)) == []
    events = frames(listener, 15)
    assert transcripts(events) == []
    assert [event for event in events if event["type"] == "limit"] == [{
        "type": "limit", "text": "a long command ends here", "limit_s": 2,
    }]
    assert listener.state == "paused"
    assert frames(listener, 100) == []
    assert sum(wake for wake, _ in factory.created) == 1


def test_recognizer_finals_accumulate_but_only_sustained_silence_submits():
    command = Recognizer([(True, {"text": "remember the blue door"})])
    listener, _, _ = active_listener(command)

    initial = frames(listener, 8)
    assert {"type": "partial", "text": "remember the blue door"} in initial
    assert transcripts(initial) == []
    assert transcripts(frames(listener, 30, value=0)) == []
    command.results.extend([
        (False, {"partial": "and the"}),
        (True, {"text": "and the red key"}),
    ])
    continuation = frames(listener, 8)
    assert {"type": "partial", "text": "remember the blue door and the"} in continuation
    assert {"type": "speech_start"} not in continuation
    assert transcripts(continuation) == []
    assert transcripts(frames(listener, 43, value=0)) == []
    assert transcripts(frames(listener, 1, value=0)) == [
        "remember the blue door and the red key",
    ]


def test_next_utterance_uses_fresh_dictation_without_wake_or_previous_words():
    first = Recognizer(final_text="my first question")
    second = Recognizer(final_text="and my follow up")
    listener, factory, _ = active_listener(first, second)

    assert frames(listener, 8).count({"type": "speech_start"}) == 1
    assert transcripts(frames(listener, 44, value=0)) == ["my first question"]
    assert frames(listener, 300, value=0) == []
    assert frames(listener, 8).count({"type": "speech_start"}) == 1
    assert transcripts(frames(listener, 44, value=0)) == ["and my follow up"]
    assert first is not second
    assert sum(wake for wake, _ in factory.created) == 1


def test_brief_noise_never_interrupts_or_opens_a_dictation():
    listener, factory, _ = active_listener()
    before = [recognizer for wake, recognizer in factory.created if not wake]

    assert frames(listener, 6) == []
    assert frames(listener, 20, value=0) == []
    assert frames(listener, 6) == []
    assert frames(listener, 44, value=0) == []
    assert [recognizer for wake, recognizer in factory.created if not wake] == before


def test_playback_requires_stronger_sustained_speech_then_accepts_quiet_words():
    command = Recognizer(final_text="please stop and listen")
    listener, _, _ = active_listener(command)
    listener.set_playback(True)

    assert frames(listener, 20, value=600) == []
    assert frames(listener, 6, value=900) == []
    assert frames(listener, 1, value=900).count({"type": "speech_start"}) == 1
    assert {"type": "speech_start"} not in frames(listener, 50, value=600)
    assert transcripts(frames(listener, 43, value=0)) == []
    assert transcripts(frames(listener, 1, value=0)) == ["please stop and listen"]
    listener.set_playback(False)
    assert frames(listener, 7, value=600).count({"type": "speech_start"}) == 1


def test_onset_preroll_preserves_first_words_and_bounds_old_background_audio():
    command = Recognizer(final_text="do not drop the beginning")
    listener, _, _ = active_listener(command)
    frames(listener, 100, value=100)
    frames(listener, 3, value=200)
    assert frames(listener, 7, value=900).count({"type": "speech_start"}) == 1

    replayed = b"".join(command.audio)
    assert replayed.endswith(audio(0.096, value=200) + audio(0.224, value=900))
    assert len(replayed) <= SAMPLE_RATE * 2 * 0.45


def test_transport_packet_boundaries_do_not_change_vad_or_recognized_audio():
    command = Recognizer(final_text="a complete command")
    listener, _, detector = active_listener(command)
    pcm = audio(0.320)
    events = []
    for start, end in [(0, 2), (2, 1022), (1022, 2110), (2110, len(pcm))]:
        events.extend(listener.feed(pcm[start:end]))

    assert events.count({"type": "speech_start"}) == 1
    assert b"".join(detector.audio) == pcm
    assert b"".join(command.audio) == pcm
    assert transcripts(frames(listener, 44, value=0)) == ["a complete command"]


def test_empty_speech_timeout_discards_unrecognized_sound_but_keeps_conversation():
    empty, actual = Recognizer(), Recognizer(final_text="actual words")
    listener, factory, _ = active_listener(
        empty, actual, voice_wait_s=1, voice_max_utterance_s=3,
    )

    reset = []
    for _ in range(45):
        reset = listener.feed(audio())
        assert transcripts(reset) == []
        if any(event["type"] == "state" for event in reset):
            break
    assert {"type": "partial", "text": ""} in reset
    assert listener.state == "listening"
    frames(listener, 44, value=0)
    frames(listener, 8)
    assert transcripts(frames(listener, 44, value=0)) == ["actual words"]
    assert sum(wake for wake, _ in factory.created) == 1


@pytest.mark.parametrize("final", [False, True])
def test_idle_recognizer_is_replaced_at_endpoint_or_one_minute(final):
    wake = Recognizer([(True, {"text": "unrelated"})] if final else [])
    factory = Recognizers(wakes=[wake])
    listener = VoiceListener(configuration(), factory, SpeechDetector())

    for _ in range(1 if final else 60):
        assert listener.feed(audio(1)) == []

    assert listener.state == "waiting"
    assert [wake for wake, _ in factory.created] == [True, True]


@pytest.mark.parametrize("pcm", [
    pytest.param(b"", id="empty"),
    pytest.param(b"\0", id="odd-byte-count"),
    pytest.param(b"\0" * (MAX_FRAME_BYTES + 2), id="oversized"),
])
def test_invalid_audio_frames_are_rejected_before_recognition(pcm):
    wake = Recognizer()
    listener = VoiceListener(
        configuration(), Recognizers(wakes=[wake]), SpeechDetector(),
    )

    with pytest.raises(ValueError, match="PCM16 mono"):
        listener.feed(pcm)
    assert wake.audio == []


def test_vad_carries_context_and_recurrent_state_per_listener():
    calls = []

    def run(outputs, inputs):
        assert outputs is None
        calls.append({name: value.copy() for name, value in inputs.items()})
        return np.array([[0.75]], dtype=np.float32), inputs["state"] + 1

    session = SimpleNamespace(run=run)
    first, second = OnnxSpeechDetector(session), OnnxSpeechDetector(session)
    assert first(audio(value=16_384)) == 0.75
    assert first(audio(value=-16_384)) == 0.75
    assert second(audio(value=0)) == 0.75
    first.reset()
    assert first(audio(value=0)) == 0.75

    assert calls[0]["input"].shape == (1, 576)
    assert np.all(calls[0]["input"][:, :64] == 0)
    assert np.all(calls[0]["input"][:, 64:] == 0.5)
    assert np.all(calls[1]["input"][:, :64] == 0.5)
    assert np.all(calls[1]["input"][:, 64:] == -0.5)
    assert calls[0]["state"].shape == (2, 1, 128)
    assert np.all(calls[0]["state"] == 0)
    assert np.all(calls[1]["state"] == 1)
    assert np.all(calls[2]["state"] == 0)
    assert np.all(calls[2]["input"] == 0)
    assert np.all(calls[3]["state"] == 0)
    assert np.all(calls[3]["input"] == 0)
    assert all(call["sr"].item() == 16_000 for call in calls)


@pytest.mark.parametrize("size", [0, 1022, 1026])
def test_vad_rejects_wrong_frame_size_before_running_model(size):
    def run(*args):
        pytest.fail("Invalid frames must not reach ONNX")

    detector = OnnxSpeechDetector(SimpleNamespace(run=run))
    with pytest.raises(ValueError, match="512 PCM16"):
        detector(b"\0" * size)


def test_wav_is_mono_pcm16_and_clips_samples_without_wrapping():
    result = wav_bytes(np.array([-2, -1, 0, 0.5, 1, 2]), 24_000)

    with wave.open(io.BytesIO(result), "rb") as handle:
        assert (handle.getnchannels(), handle.getsampwidth()) == (1, 2)
        assert (handle.getframerate(), handle.getnframes()) == (24_000, 6)
        assert np.frombuffer(handle.readframes(6), dtype="<i2").tolist() == [
            -32767, -32767, 0, 16383, 32767, 32767,
        ]


@pytest.mark.parametrize("samples", [[], [np.nan], [np.inf], [-np.inf]])
def test_invalid_synthesis_audio_is_refused(samples):
    with pytest.raises(VoiceUnavailable, match="empty or invalid"):
        wav_bytes(np.array(samples), 24_000)


def test_speech_keeps_prose_and_link_labels_without_reading_code_or_urls():
    text = """# A **useful** answer
> Read [the guide](https://example.com/guide).
- Keep `this` and _that_.
```python
dangerous_secret = 'never read this'
```
Visit https://example.com/raw
"""
    assert spoken_text(text) == (
        "A useful answer Read the guide. Keep this and that. "
        "Code omitted from speech. Visit"
    )


def test_missing_optional_dependencies_leave_a_helpful_status(monkeypatch, tmp_path):
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    service = VoiceService(configuration(voice_model_dir=tmp_path))

    status = service.status()
    assert status["available"] is False
    assert status["sample_rate"] == SAMPLE_RATE
    assert status["wake_phrase"] == "hey idris"
    assert "uv sync --extra voice --inexact" in status["error"]
    with pytest.raises(VoiceUnavailable, match="Install local speech"):
        service.warm_up()


def test_missing_models_do_not_trigger_downloads(monkeypatch, tmp_path):
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    service = VoiceService(configuration(voice_model_dir=tmp_path))

    assert service.status()["available"] is False
    with pytest.raises(VoiceUnavailable, match="voice-setup"):
        service.new_listener()
    assert list(tmp_path.iterdir()) == []


def test_synthesis_uses_configured_voice_and_prose():
    calls = []

    def create(text, **kwargs):
        calls.append((text, kwargs))
        return np.array([0, 0.5, -0.5]), 24_000

    service = VoiceService(configuration(voice_name="af_bella"))
    service._model = object()
    service._kokoro = SimpleNamespace(create=create)

    result = service.synthesize("**Hello**, [friend](https://example.com).")

    assert result.startswith(b"RIFF")
    assert calls == [("Hello, friend.", {"voice": "af_bella", "lang": "en-us"})]
    with pytest.raises(VoiceUnavailable, match="no speakable text"):
        service.synthesize("https://example.com")
    assert len(calls) == 1


def test_pre_cancelled_speech_never_enters_native_synthesis():
    def create(*args, **kwargs):
        pytest.fail("Cancelled speech must not enter Kokoro")

    service = VoiceService(configuration())
    service._model = object()
    service._kokoro = SimpleNamespace(create=create)
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(VoiceCancelled, match="interrupted"):
        service.synthesize("This reply is obsolete.", cancelled=cancelled)
    assert not service._speech_lock.locked()


def test_cancel_after_one_bounded_piece_stops_remaining_speech_and_releases_lock():
    calls = []
    cancelled = threading.Event()

    def create(text, **kwargs):
        calls.append(text)
        if len(calls) == 1:
            cancelled.set()
        return np.array([0.25, -0.25]), 24_000

    service = VoiceService(configuration())
    service._model = object()
    service._kokoro = SimpleNamespace(create=create)

    with pytest.raises(VoiceCancelled, match="interrupted"):
        service.synthesize("A long reply with more to say. " * 40, cancelled=cancelled)

    assert len(calls) == 1
    assert 0 < len(calls[0]) <= 240
    assert not service._speech_lock.locked()
    assert service.synthesize("A new reply.").startswith(b"RIFF")
    assert calls[1:] == ["A new reply."]


def test_synthesis_pieces_preserve_prose_and_concatenate_all_generated_audio():
    calls = []
    prose = "Keep this whole sentence and every word intact. " * 17

    def create(text, **kwargs):
        calls.append(text)
        return np.array([len(calls) / 10, 0]), 24_000

    service = VoiceService(configuration())
    service._model = object()
    service._kokoro = SimpleNamespace(create=create)
    result = service.synthesize(prose)

    assert len(calls) > 1
    assert all(0 < len(piece) <= 240 for piece in calls)
    assert " ".join(calls) == prose.strip()
    with wave.open(io.BytesIO(result), "rb") as handle:
        assert handle.getnframes() == len(calls) * 2
        assert handle.getframerate() == 24_000


def test_cancelled_waiter_exits_while_previous_native_synthesis_is_still_running():
    calls = []
    started, release, waiting, cancelled = (threading.Event() for _ in range(4))
    native_lock = threading.Lock()

    class ObservedLock:
        def acquire(self, *, timeout):
            if native_lock.locked():
                waiting.set()
            return native_lock.acquire(timeout=timeout)

        def release(self):
            native_lock.release()

    def create(text, **kwargs):
        calls.append(text)
        if text == "First reply.":
            started.set()
            assert release.wait(3), "Test must release the simulated native call"
        return np.array([0.25, -0.25]), 24_000

    service = VoiceService(configuration())
    service._model = object()
    service._kokoro = SimpleNamespace(create=create)
    service._speech_lock = ObservedLock()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.synthesize, "First reply.")
        try:
            assert started.wait(1)
            obsolete = pool.submit(
                service.synthesize, "Obsolete queued reply.", cancelled=cancelled,
            )
            assert waiting.wait(1)
            cancelled.set()
            with pytest.raises(VoiceCancelled, match="interrupted"):
                obsolete.result(timeout=1)
            assert not first.done()
            assert calls == ["First reply."]
        finally:
            release.set()
        assert first.result(timeout=1).startswith(b"RIFF")

    assert not native_lock.locked()
    assert service.synthesize("Newest reply.").startswith(b"RIFF")
    assert calls == ["First reply.", "Newest reply."]
