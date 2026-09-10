"""The listening kit preserves baseline audio and records auditable comparisons."""

import io
import json
import wave

import numpy as np
import pytest

from recollect.engine.voice import _speech_pieces, wav_bytes
from recollect.engine.voice_text import spoken_text
from recollect.voice_listening import (
    SAMPLES,
    Profile,
    comparisons,
    generate,
    profiles,
    render,
    sentence_pieces,
    sha256,
)


class FakeKokoro:
    def __init__(self):
        self.calls = []

    def create(self, text, *, voice, lang, speed=1.0):
        self.calls.append((text, voice, lang, speed))
        return np.full(len(text) * 10, 0.2, dtype=np.float32), 24000

    def get_voice_style(self, voice):
        return np.full((2, 3), 1 if voice == "af_heart" else 3, dtype=np.float32)


@pytest.mark.parametrize("sample", SAMPLES.values())
def test_historical_baseline_retains_original_chunking_without_added_pauses(sample):
    pieces = _speech_pieces(spoken_text(sample["text"]))
    expected = wav_bytes(
        np.full(sum(map(len, pieces)) * 10, 0.2, dtype=np.float32), 24000,
    )
    fake = FakeKokoro()
    actual, metrics = render(fake, sample["text"], profiles("af_heart")[0])
    assert actual == expected
    assert fake.calls == [(p, "af_heart", "en-us", 1.0) for p in pieces]
    assert metrics["pieces"] == pieces


@pytest.mark.parametrize("text", [
    *[sample["text"] for sample in SAMPLES.values()],
    "One sentence without punctuation " * 40,
    "A" * 1000,
    "A short question? Yes! A longer answer follows.",
])
def test_candidate_segmentation_preserves_text_and_bounds_native_calls(text):
    prose = spoken_text(text)
    pieces = sentence_pieces(prose)
    # Long unbroken strings may gain spaces at forced boundaries, as in the
    # production fallback, but no character may disappear or be duplicated.
    assert "".join("".join(pieces).split()) == "".join(prose.split())
    assert all(0 < len(piece) <= 420 for piece in pieces)


def test_sentence_grouping_reduces_unnecessary_splits():
    prose = spoken_text(SAMPLES["conversation"]["text"])
    assert len(sentence_pieces(prose)) < len(_speech_pieces(prose))
    assert all(piece.endswith((".", "?", "!")) for piece in sentence_pieces(prose))


def test_blend_passes_weighted_styles_and_speed():
    fake = FakeKokoro()
    profile = Profile("mix", "Mix", "af_heart", speed=0.95,
                      blend_voice="af_bella", blend_weight=0.25)
    render(fake, "Hello.", profile)
    assert np.all(fake.calls[0][1] == 1.5)
    assert fake.calls[0][2:] == ("en-us", 0.95)


def test_pause_is_added_only_between_sentence_groups():
    text = SAMPLES["conversation"]["text"]
    base, before = render(FakeKokoro(), text, Profile("base", "Base", "af_heart"))
    paused, after = render(FakeKokoro(), text, Profile(
        "pause", "Pause", "af_heart", boundary_pause_s=0.18,
    ))
    assert len(paused) - len(base) == (len(before["pieces"]) - 1) * 4320 * 2
    assert after["duration_s"] - before["duration_s"] == pytest.approx(
        (len(before["pieces"]) - 1) * 0.18,
    )


def test_invalid_audio_fails_instead_of_producing_a_listening_clip():
    fake = FakeKokoro()
    fake.create = lambda *args, **kwargs: (np.array([np.nan]), 24000)
    with pytest.raises(ValueError, match="invalid audio"):
        render(fake, "Hello.", profiles("af_heart")[0])


def test_kit_contains_every_pair_and_playable_hashed_audio(tmp_path):
    output = tmp_path / "kit"
    manifest = generate(output, FakeKokoro(), "af_heart", {"fake": True})
    assert json.loads((output / "manifest.json").read_text()) == manifest
    assert len(manifest["comparisons"]) == 11
    for pair in comparisons():
        for profile in pair["profiles"]:
            clip = manifest["clips"][f"{pair['sample']}--{profile}"]
            path = output / clip["file"]
            assert sha256(path) == clip["sha256"]
            with wave.open(io.BytesIO(path.read_bytes())) as audio:
                assert audio.getframerate() == clip["sample_rate"]
                assert audio.getnframes() / audio.getframerate() == clip["duration_s"]
    for name in ("index.html", "app.js", "style.css", "manifest.js"):
        assert (output / name).stat().st_size > 0
    with pytest.raises(FileExistsError):
        generate(output, FakeKokoro(), "af_heart", {})


def test_only_one_variable_changes_in_each_comparison():
    candidates = {p.id: p for p in profiles("af_heart")}
    for pair in comparisons():
        a, b = (candidates[key] for key in pair["profiles"])
        differences = sum(getattr(a, field) != getattr(b, field) for field in (
            "voice", "grouping", "speed", "boundary_pause_s",
        ))
        blend_changes = (a.blend_voice != b.blend_voice
                         or a.blend_weight != b.blend_weight)
        assert differences + bool(blend_changes) == 1
