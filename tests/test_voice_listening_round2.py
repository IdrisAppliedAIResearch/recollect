import hashlib

import numpy as np
import pytest

from recollect.voice_listening_round2 import (
    UNITS,
    adaptive_speeds,
    decode,
    generate_round2,
    pause_frames,
)


class FakeKokoro:
    def create(self, text, *, voice, lang, speed=1.0):
        # A nonzero varying waveform makes missing, duplicated, or shifted
        # speech detectable independently of inserted zeros.
        count = round(len(text) * 1000 / speed)
        return np.linspace(0.1, 0.4, count, dtype=np.float32), 24000

    def get_voice_style(self, voice):
        return np.ones((2, 3), dtype=np.float32)


def test_variance_is_repeatable_bounded_and_preserves_each_pause_budget():
    units = UNITS["long"]
    durations = [len(text.split()) / 2.5 for text, _ in units]
    fixed = pause_frames(units, durations, 24000)
    varied = pause_frames(units, durations, 24000, True)
    assert varied == pause_frames(units, durations, 24000, True)
    assert varied != fixed
    assert fixed[-1] == varied[-1] == 0
    for kind in ("clause", "sentence", "paragraph"):
        indexes = [i for i, unit in enumerate(units) if unit[1] == kind]
        assert sum(fixed[i] for i in indexes) == sum(varied[i] for i in indexes)
        assert all(0.5 * fixed[i] <= varied[i] <= 1.5 * fixed[i] for i in indexes)


def test_adaptive_pace_is_small_and_centered_on_control():
    units = UNITS["conversation"]
    rates = adaptive_speeds(units)
    weights = [len(text.split()) for text, _ in units]
    assert min(rates) >= 1.01
    assert max(rates) <= 1.04
    assert max(rates) > min(rates)
    assert np.average(rates, weights=weights) == pytest.approx(1.025)


def test_round2_comparisons_preserve_identical_speech_and_exact_silence(tmp_path):
    output = tmp_path / "round2"
    kit = generate_round2(output, FakeKokoro(), "af_heart", {})
    assert len(kit["comparisons"]) == 8
    assert len(kit["clips"]) == 11
    speech_by_clip = {}
    for key, clip in kit["clips"].items():
        payload = (output / clip["file"]).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == clip["sha256"]
        pcm, rate = decode(payload)
        assert len(pcm) / 2 / rate == clip["duration_s"]
        if "boundaries" not in clip:
            continue
        speech = []
        offset = 0
        for boundary in clip["boundaries"]:
            assert boundary["speech_start_frame"] == offset
            start = offset * 2
            end = start + boundary["speech_frames"] * 2
            stem = pcm[start:end]
            assert hashlib.sha256(stem).hexdigest() == boundary["pcm_sha256"]
            pause_end = end + boundary["added_pause_frames"] * 2
            assert pcm[end:pause_end] == b"\0" * (pause_end - end)
            speech.append(stem)
            offset = pause_end // 2
        assert offset * 2 == len(pcm)
        speech_by_clip[key] = b"".join(speech)
    for sample in UNITS:
        fixed = kit["clips"][f"{sample}--fixed"]
        varied = kit["clips"][f"{sample}--varied"]
        assert fixed["duration_s"] == varied["duration_s"]
        assert speech_by_clip[f"{sample}--fixed"] == speech_by_clip[f"{sample}--varied"]
        assert fixed["sha256"] != varied["sha256"]
    for profile in ("fast", "mid", "adaptive"):
        assert [b["added_pause_frames"] for b in
                kit["clips"][f"conversation--{profile}"]["boundaries"]] == [
                    b["added_pause_frames"] for b in
                    kit["clips"]["conversation--varied"]["boundaries"]]
    with pytest.raises(FileExistsError):
        generate_round2(output, FakeKokoro(), "af_heart", {})
