"""The trial must preserve raw results and reject unusable structured replies."""

import json

import httpx
import numpy as np
import pytest
from pydantic import ValidationError

from recollect.voice_trial import EXAMPLE, DualOutput, complete, dual_prompt
from recollect.voice_trial_listening import build_kit


def test_prompt_includes_schema_example_and_memory_obligations():
    prompt = dual_prompt("Preserve the real memory instructions.")
    assert prompt.startswith("Preserve the real memory instructions.")
    assert '"additionalProperties": false' in prompt
    assert json.dumps(EXAMPLE, ensure_ascii=False, indent=2) in prompt
    assert "Preserve facts, names, quantities, negations" in prompt
    assert DualOutput.model_validate(EXAMPLE).speech.text == EXAMPLE["speech"]["text"]


@pytest.mark.parametrize("value", [
    {"text": "x"},
    {"text": "x", "speech": {"text": " "}},
    {"text": "\n", "speech": {"text": "x"}},
    {"text": 12, "speech": {"text": "x"}},
    {"text": "x", "speech": {"text": "x", "speed": 2}},
    {"text": "x", "speech": {"text": "x"}, "extra": True},
])
def test_rejects_missing_blank_or_unrecognized_fields(value):
    with pytest.raises(ValidationError):
        DualOutput.model_validate(value)


@pytest.mark.asyncio
async def test_stream_preserves_json_and_records_truncation_without_repair():
    events = [
        {"choices": [{"delta": {"reasoning_content": "private planning"}}]},
        {"choices": [{"delta": {"content": '{"text": "Hi"'}}]},
        {"choices": [{"delta": {}, "finish_reason": "length"}],
         "usage": {"completion_tokens": 5}},
    ]
    data = "\n\n".join("data: " + json.dumps(event) for event in events)
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, text=data + "\n\ndata: [DONE]\n\n"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        result = await complete(client, {})
    assert result["raw"] == '{"text": "Hi"'
    assert result["finish_reason"] == "length"
    assert result["usage"]["completion_tokens"] == 5
    assert result["ttft_ms"] is not None
    with pytest.raises(ValidationError):
        DualOutput.model_validate_json(result["raw"])


def test_audio_uses_validated_raw_speech_not_display_or_derived_fields(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "metadata.json").write_text(json.dumps({
        "prompt_sha256": {}, "cases": [{"id": "case", "title": "Case",
                                        "question": "Hello", "memory": ""}],
    }))
    for name in ("dual-prompt.txt", "baseline-prompt.txt", "schema.json"):
        (source / name).write_text("{}")
    rows = []
    for arm, repeat in (("baseline", 0), ("dual", 0), ("dual", 1)):
        raw = "Baseline" if arm == "baseline" else json.dumps({
            "text": "Displayed words", "speech": {"text": "Spoken words"},
        })
        rows.append({"case": "case", "arm": arm, "repeat": repeat,
                     "raw": raw, "speech": "Stale derived words", "valid": True,
                     "finish_reason": "stop", "total_ms": 1, "ttft_ms": 0.5,
                     "display_checks": {}, "speech_checks": {}, "usage": {}})
    (source / "responses.jsonl").write_text("\n".join(map(json.dumps, rows)))

    class FakeKokoro:
        def __init__(self):
            self.spoken = []

        def get_voice_style(self, name):
            return np.zeros((1, 1))

        def create(self, text, **kwargs):
            self.spoken.append(text)
            return np.zeros(240, dtype=np.float32), 24000

    kokoro = FakeKokoro()
    manifest = build_kit(source, tmp_path / "kit", kokoro, {})
    assert kokoro.spoken == ["Baseline", "Spoken words", "Spoken words"]
    assert manifest["cases"][0]["clips"][1]["display"] == "Displayed words"
    report = json.loads((tmp_path / "kit" / "benchmark.json").read_text())
    assert report["dual"]["responses"] == 2
    rows[1]["raw"] = '{"text":"truncated"'
    (source / "responses.jsonl").write_text("\n".join(map(json.dumps, rows)))
    with pytest.raises(ValidationError):
        build_kit(source, tmp_path / "invalid-kit", kokoro, {})
