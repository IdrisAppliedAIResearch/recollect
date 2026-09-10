"""Audition the revised voice prompt without opening a conversation store."""

from __future__ import annotations

import argparse
import ast
import asyncio
import importlib.metadata
import json
import shutil
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from recollect.api import _VOICE_INSTRUCTIONS, _turn_system_prompt
from recollect.config import RecollectConfig
from recollect.engine.generator import Generator, GeneratorSettings
from recollect.engine.voice import VoiceService
from recollect.trace import GenerationTrace
from recollect.voice_listening import Profile, render, sha256
from recollect.voice_trial import ASSETS, digest

BASELINE_REF = "c5a3e00"
CASES = [
    {
        "id": "free_hour", "title": "Making a simple plan",
        "memory": "<recent_context>\nUser: I have an hour free this afternoon. "
                  "I'd like a twenty-minute walk and a little time to clear my desk. "
                  "I don't want to turn it into a big productivity exercise.\n"
                  "</recent_context>",
        "question": "Let's keep it simple. How should I spend that free hour?",
    },
    {
        "id": "garden", "title": "Explaining a tradeoff",
        "memory": "<recent_context>\nUser: I'm starting a small garden and I want "
                  "to keep the first season manageable.\nAssistant: We could "
                  "begin with a few herbs near the kitchen door.\n</recent_context>",
        "question": "Why start with a few herbs instead of filling every bed?",
    },
    {
        "id": "first_try", "title": "Thinking something through",
        "memory": "<recent_context>\nUser: I'm learning watercolor for fun. "
                  "I have paper and a small paint set, but I keep putting off "
                  "trying my first little landscape.\n</recent_context>",
        "question": "I keep feeling like I have to get the whole thing right "
                    "before I start. Maybe I'm overthinking it?",
    },
]


def original_instructions(source: str) -> str:
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_VOICE_INSTRUCTIONS"
            for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise ValueError("The baseline source has no literal voice instructions.")


async def generate_text(output: Path, config: RecollectConfig) -> dict:
    root = Path(__file__).resolve().parents[2]
    source = subprocess.run(
        ["git", "show", f"{BASELINE_REF}:src/recollect/api.py"], cwd=root,
        check=True, capture_output=True, encoding="utf-8",
    ).stdout
    original = original_instructions(source)
    revised = _turn_system_prompt(config, datetime.now(UTC), "voice")
    if revised.count(_VOICE_INSTRUCTIONS) != 1:
        raise ValueError("Expected exactly one voice instruction block.")
    prompts = {"original": revised.replace(_VOICE_INSTRUCTIONS, original, 1),
               "revised": revised}
    for arm, prompt in prompts.items():
        (output / f"{arm}-prompt.txt").write_text(prompt, encoding="utf-8")
    settings = GeneratorSettings(
        base_url=config.generator_base_url, model=config.generator_model,
        api_key=config.generator_api_key, timeout_s=config.generator_timeout_s,
        thinking=config.generator_thinking, max_tokens=config.generator_max_tokens,
        temperature=config.generator_temperature,
        context_tokens=config.generator_context_tokens,
    )
    generator = Generator(settings)
    manifest = {
        "version": 1, "mode": "punctuation", "baseline_ref": BASELINE_REF,
        "created_at": datetime.now(UTC).isoformat(),
        "prompt_sha256": {k: digest(v) for k, v in prompts.items()},
        "model": config.generator_model, "thinking": config.generator_thinking,
        "temperature": config.generator_temperature,
        "max_tokens": config.generator_max_tokens, "cases": [],
    }
    try:
        health = await generator.health()
        if not health["reachable"]:
            raise RuntimeError("The configured Qwen server is unavailable.")
        manifest["server_models"] = health["available_models"]
        for index, case in enumerate(CASES):
            entry = {**case, "memory_sha256": digest(case["memory"]), "clips": []}
            arms = (
                ("original", "revised") if index % 2 == 0 else ("revised", "original")
            )
            for arm in arms:
                messages = generator.build_messages(
                    system_prompt=prompts[arm], context_block=case["memory"],
                    user_message=case["question"],
                )
                trace = GenerationTrace(
                    model=settings.model, base_url=settings.base_url,
                    system_prompt_chars=len(prompts[arm]),
                    context_block_chars=len(case["memory"]),
                    total_prompt_chars=(
                        len(prompts[arm]) + len(case["memory"]) + len(case["question"])
                    ),
                )
                async for _ in generator.stream(messages, trace=trace):
                    pass
                record = {"case": case["id"], "arm": arm, "messages": messages,
                          "generation": trace.model_dump(mode="json")}
                with (output / "responses.jsonl").open("a", encoding="utf-8") as log:
                    log.write(json.dumps(record, ensure_ascii=False) + "\n")
                if trace.finish_reason != "stop" or not trace.response_text.strip():
                    raise ValueError("Incomplete response; no audio published.")
                entry["clips"].append({
                    "arm": arm, "repeat": 0, "raw": trace.response_text,
                    "display": trace.response_text, "generation_ms": trace.total_ms,
                })
                print(json.dumps({"case": case["id"], "arm": arm,
                                  "response": trace.response_text}), flush=True)
            entry["clips"].sort(key=lambda c: c["arm"] != "original")
            manifest["cases"].append(entry)
    finally:
        await generator.aclose()
    manifest["source_sha256"] = sha256(output / "responses.jsonl")
    return manifest


def generate_audio(output: Path, config: RecollectConfig, manifest: dict) -> None:
    import kokoro_onnx
    import onnxruntime as ort

    session = VoiceService(config)._speech_session(ort)
    voices = config.voice_model_dir / "voices-v1.0.bin"
    kokoro = kokoro_onnx.Kokoro.from_session(session, str(voices))
    profile = Profile("controlled", "Fixed voice", "af_heart", grouping="current",
                      blend_voice="af_bella", blend_weight=0.25)
    kokoro.create("Ready for the examples.", voice="af_heart", lang="en-us")
    manifest["profile"] = asdict(profile)
    manifest["metadata"] = {
        "providers": session.get_providers(),
        "model_sha256": sha256(config.voice_model_dir / "kokoro-v1.0.onnx"),
        "voices_sha256": sha256(voices),
        "kokoro_onnx": importlib.metadata.version("kokoro-onnx"),
    }
    (output / "audio").mkdir()
    for case in manifest["cases"]:
        for clip in case["clips"]:
            audio, stats = render(kokoro, clip["raw"], profile)
            name = f"audio/{case['id']}-{clip['arm']}.wav"
            (output / name).write_bytes(audio)
            clip.update(file=name, sha256=sha256(output / name), audio=stats,
                        speech=stats["spoken_text"])
    for name in ("app.js", "style.css"):
        shutil.copyfile(ASSETS / name, output / name)
    shutil.copyfile(ASSETS / "punctuation.html", output / "index.html")
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2),
                                         encoding="utf-8")
    encoded = json.dumps(manifest, ensure_ascii=True).replace("<", "\\u003c")
    (output / "manifest.js").write_text(f"window.VOICE_TRIAL = {encoded};\n",
                                       encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new directory; existing recordings are preserved.")
    args.output.mkdir(parents=True)
    config = RecollectConfig.from_env()
    manifest = asyncio.run(generate_text(args.output, config))
    generate_audio(args.output, config, manifest)
    print(json.dumps({"output": str(args.output.resolve()), "clips": 6}), flush=True)


if __name__ == "__main__":
    main()
