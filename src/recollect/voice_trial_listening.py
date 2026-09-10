"""Render recorded dual-output trials with fixed Kokoro settings."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import shutil
from dataclasses import asdict
from pathlib import Path

from recollect.config import RecollectConfig
from recollect.engine.voice import VoiceService
from recollect.voice_listening import Profile, render, sha256
from recollect.voice_trial import ASSETS, DualOutput, summarize


def build_kit(source: Path, output: Path, kokoro, metadata: dict) -> dict:
    recorded = json.loads((source / "metadata.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in
            (source / "responses.jsonl").read_text(encoding="utf-8").splitlines()]
    profile = Profile("controlled", "Fixed voice", "af_heart", grouping="current",
                      blend_voice="af_bella", blend_weight=0.25)
    manifest = {
        "version": 1, "source_sha256": sha256(source / "responses.jsonl"),
        "prompt_sha256": recorded["prompt_sha256"], "profile": asdict(profile),
        "metadata": metadata, "cases": [],
    }
    (output / "audio").mkdir(parents=True)
    for case in recorded["cases"]:
        if not re.fullmatch(r"[a-z_]+", case["id"]):
            raise ValueError("Case identifiers must be safe filenames.")
        entry = {"id": case["id"], "title": case["title"],
                 "question": case["question"], "memory": case["memory"], "clips": []}
        # Selection is fixed by repeat index, never by which response sounds best.
        for arm, repeat in (("baseline", 0), ("dual", 0), ("dual", 1)):
            row = next(row for row in rows if row["case"] == case["id"]
                       and row["arm"] == arm and row["repeat"] == repeat)
            if not row["valid"] or row["finish_reason"] != "stop":
                raise ValueError("Refusing to synthesize an incomplete response.")
            parsed = (DualOutput.model_validate_json(row["raw"])
                      if arm == "dual" else None)
            speech = parsed.speech.text if parsed else row["raw"]
            display = parsed.text if parsed else row["raw"]
            wav, stats = render(kokoro, speech, profile)
            filename = f"audio/{case['id']}-{arm}-{repeat}.wav"
            (output / filename).write_bytes(wav)
            entry["clips"].append({
                "arm": arm, "repeat": repeat, "display": display, "speech": speech,
                "raw": row["raw"], "file": filename,
                "sha256": sha256(output / filename), "audio": stats,
                "generation_ms": row["total_ms"],
            })
        manifest["cases"].append(entry)
    for name in ("index.html", "app.js", "style.css"):
        shutil.copyfile(ASSETS / name, output / name)
    for name in ("dual-prompt.txt", "baseline-prompt.txt", "schema.json"):
        shutil.copyfile(source / name, output / name)
    (output / "benchmark.json").write_text(
        json.dumps(summarize(rows), indent=2), encoding="utf-8",
    )
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2),
                                         encoding="utf-8")
    encoded = json.dumps(manifest, ensure_ascii=True).replace("<", "\\u003c")
    (output / "manifest.js").write_text(f"window.VOICE_TRIAL = {encoded};\n",
                                       encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new kit directory; previous recordings are kept.")
    import kokoro_onnx
    import onnxruntime as ort

    config = RecollectConfig.from_env()
    service = VoiceService(config)
    session = service._speech_session(ort)
    model = config.voice_model_dir / "kokoro-v1.0.onnx"
    voices = config.voice_model_dir / "voices-v1.0.bin"
    kokoro = kokoro_onnx.Kokoro.from_session(session, str(voices))
    kokoro.create("Ready for a short conversation.", voice="af_heart", lang="en-us")
    manifest = build_kit(args.source, args.output, kokoro, {
        "model_sha256": sha256(model), "voices_sha256": sha256(voices),
        "providers": session.get_providers(),
        "kokoro_onnx": importlib.metadata.version("kokoro-onnx"),
        "requested_device": config.voice_device,
        "scope": "Warm offline synthesis, production chunking, speed 1.0, "
                 "75% Heart / 25% Bella. No added silence or normalization. "
                 "Excludes network/playback gaps and live turn-taking.",
    })
    print(json.dumps({"output": str(args.output.resolve()),
                      "clips": sum(len(c["clips"]) for c in manifest["cases"])}))


if __name__ == "__main__":
    main()
