"""Sentence pauses preserve speech, streaming backpressure and interruptions."""

import io
import threading
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from recollect.config import RecollectConfig
from recollect.engine.voice import VoiceCancelled, VoiceService, VoiceUnavailable
from recollect.engine.voice_text import speech_sentences


@pytest.mark.parametrize("sentences", [
    ["One sentence."],
    ["Ready?", "Yes!", "Let's go."],
    ['She said, “Try it.”', "Then we waited."],
    ["Ask Dr. Smith about 3.14 first.", "Then call J. R. Jones."],
    ["Try e.g. mint, basil, etc. in a pot.", "That works."],
    ["The U.S. team agrees.", "Let's begin."],
    ["I think... maybe we should wait.", "All right…", "Let's wait."],
    ["A comma, colon: semicolon; or dash—stays inside.", "No final punctuation"],
    ["Email a.b@example.com first.", "then wait."],
])
def test_boundaries_keep_wording_and_common_non_sentence_periods(sentences):
    assert speech_sentences(" ".join(sentences)) == sentences


def service_for(create):
    service = VoiceService(RecollectConfig(embedding_model_path=Path("unused")))
    service._model = object()
    service._kokoro = SimpleNamespace(create=create)
    return service


def pcm(wav):
    with wave.open(io.BytesIO(wav), "rb") as handle:
        return np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2")


@pytest.mark.parametrize("rate", [16_000, 24_000, 48_000])
def test_exact_half_second_between_sentences_in_both_rendering_paths(rate):
    samples = np.array([0.25, -0.5, 0.125], dtype=np.float32)
    service = service_for(lambda *args, **kwargs: (samples, rate))
    text = "Try this. Does it work? Yes!"
    speech = (samples * 32767).astype("<i2")
    gap = np.zeros(rate // 2, dtype="<i2")
    expected = np.concatenate([speech, gap, speech, gap, speech])
    np.testing.assert_array_equal(pcm(service.synthesize(text)), expected)
    chunks = list(service.synthesize_chunks(text))
    assert len(chunks) == 3
    np.testing.assert_array_equal(np.concatenate([pcm(c) for c in chunks]), expected)
    np.testing.assert_array_equal(pcm(chunks[-1]), speech)


def test_long_sentence_has_no_pause_at_bounded_word_or_clause_splits():
    calls = []

    def create(text, **kwargs):
        calls.append(text)
        return np.array([0.25]), 24_000

    service = service_for(create)
    text = "A long clause with more to say; " * 20 + "and finally the end."
    result = pcm(service.synthesize(text + " Next sentence."))
    assert len(calls) > 3
    assert all(len(c) <= 240 for c in calls)
    assert " ".join(calls) == text + " Next sentence."
    assert np.count_nonzero(result) == len(calls)
    assert len(result) == len(calls) + 12_000
    assert np.all(result[-12_001:-1] == 0)
    assert np.all(result[:-12_001] != 0)


def test_pause_does_not_prefetch_or_hold_native_lock_and_can_be_interrupted():
    calls = []

    def create(text, **kwargs):
        calls.append(text)
        return np.array([0.25]), 24_000

    service = service_for(create)
    cancelled = threading.Event()
    stream = service.synthesize_chunks("First. Second.", cancelled=cancelled)
    assert len(pcm(next(stream))) == 12_001
    assert calls == ["First."]
    assert not service._speech_lock.locked()
    cancelled.set()
    with pytest.raises(VoiceCancelled):
        next(stream)
    assert calls == ["First."]


@pytest.mark.parametrize("samples", [[], [float("nan")], [float("inf")]])
def test_padding_never_hides_invalid_native_audio(samples):
    service = service_for(lambda *args, **kwargs: (np.array(samples), 24_000))
    with pytest.raises(VoiceUnavailable, match="empty or invalid"):
        next(service.synthesize_chunks("First. Second."))
