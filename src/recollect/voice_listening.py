"""Generate an isolated Kokoro audition kit; never open a conversation store."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import inspect
import json
import platform
import random
import re
import shutil
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from recollect.config import RecollectConfig
from recollect.engine.voice import VoiceService, _speech_pieces, wav_bytes
from recollect.engine.voice_text import spoken_text

ASSETS = Path(__file__).with_name("listening")
SAMPLES = {
    "short": {
        "title": "A short reply",
        "text": ("Yes, that works. We can start with the simpler option "
                 "and adjust later."),
    },
    "conversation": {
        "title": "A conversational explanation",
        "text": (
            "There are a couple of ways we could approach this, and I think it helps "
            "to separate what we know from what we still need to find out. "
            "The first option is quicker to try, but it gives us less room to change "
            "direction once we have started. "
            "The second takes a little more preparation. "
            "In return, we can test one part at a time and keep the pieces that work. "
            "Which tradeoff matters more to you: getting an answer today, or having "
            "something we can comfortably build on next week?"
        ),
    },
    "pronunciation": {
        "title": "Names, numbers, and technical words",
        "text": (
            "Doctor Nguyen and Aisha will review the API on September 12, 2026. "
            "The estimate is $1,250.50, including a 15% contingency. "
            "We measured 0.95 seconds for the first result and 240 milliseconds for "
            "the next one. Kokoro handles speech synthesis; Whisper handles "
            "transcription. Please check the JSON file before restarting Recollect."
        ),
    },
    "long": {
        "title": "A longer listen",
        "text": (
            "Imagine that we are planning a small garden together. We have a patch "
            "of ground, a few ideas, and a free weekend. Before we buy anything, "
            "I would like to understand how you want to use the space. Would you "
            "rather have somewhere quiet to sit, a place to grow vegetables, or "
            "a mixture of both? There is no single right answer. The best plan "
            "is one that you will enjoy looking after. "
            "Let us begin by watching where the sunlight falls. A spot that looks "
            "bright in the morning may be shaded for most of the afternoon. "
            "We could make a few notes over several days, then choose plants "
            "that suit the conditions we actually have. That is usually easier "
            "than trying to change the whole garden to suit a plant we saw in a shop. "
            "Next, we should think about the paths. They do not need to be "
            "elaborate, but they should let us reach the beds without stepping "
            "on the soil. If we leave enough room for a chair and a watering can, "
            "the space will be more comfortable to use. Small practical details "
            "often matter more than they seem to on a drawing. "
            "We can keep the first planting simple. A few herbs near the kitchen "
            "door might be a good start. We could add flowers that bloom at "
            "different times, so there is something to notice as the seasons "
            "change. There is no need to fill every empty corner immediately. "
            "Leaving some room gives us a chance to learn what grows well. "
            "After a month, we can look back at what happened. Which plants "
            "needed more water? Which part of the garden did you use most? "
            "Were there tasks that felt enjoyable, and others that became a chore? "
            "Those observations can guide the next step. We do not have to get "
            "everything right on the first weekend. We just need a beginning "
            "that makes it pleasant to return."
        ),
    },
}


@dataclass(frozen=True)
class Profile:
    id: str
    label: str
    voice: str
    grouping: str = "sentences"
    speed: float = 1.0
    boundary_pause_s: float = 0.0
    blend_voice: str | None = None
    blend_weight: float = 0.0


def profiles(baseline_voice: str) -> list[Profile]:
    return [
        Profile("baseline", "Original synthesis (before issue 12)", baseline_voice,
                "current"),
        Profile("grouped", "Sentence grouping", baseline_voice),
        Profile("bella", "Bella", "af_bella"),
        Profile("michael", "Michael", "am_michael"),
        Profile("puck", "Puck", "am_puck"),
        Profile("slower", "Sentence grouping at 0.95×", baseline_voice, speed=0.95),
        Profile("faster", "Sentence grouping at 1.05×", baseline_voice, speed=1.05),
        Profile("pauses", "Sentence grouping with pauses", baseline_voice,
                boundary_pause_s=0.18),
        Profile("blend", "75% baseline + 25% Bella", baseline_voice,
                blend_voice="af_bella", blend_weight=0.25),
    ]


def comparisons() -> list[dict]:
    rows = [
        ("Phrasing", "short", "baseline", "grouped"),
        ("Phrasing", "conversation", "baseline", "grouped"),
        ("Phrasing", "pronunciation", "baseline", "grouped"),
        ("Voice", "conversation", "grouped", "bella"),
        ("Voice", "conversation", "grouped", "michael"),
        ("Voice", "conversation", "grouped", "puck"),
        ("Pace", "conversation", "grouped", "slower"),
        ("Pace", "conversation", "grouped", "faster"),
        ("Pauses", "conversation", "grouped", "pauses"),
        ("Blend", "conversation", "grouped", "blend"),
        ("Long listen", "long", "baseline", "grouped"),
    ]
    return [
        {"id": f"pair-{index + 1:02}", "stage": stage, "sample": sample,
         "profiles": [left, right]}
        for index, (stage, sample, left, right) in enumerate(rows)
    ]


def sentence_pieces(prose: str, limit: int = 420) -> list[str]:
    """Group the fixed audition prose, retaining punctuation and every word."""
    pieces: list[str] = []
    pending = ""
    for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z])", prose):
        if len(sentence) > limit:
            if pending:
                pieces.append(pending)
                pending = ""
            pieces.extend(_speech_pieces(sentence))
        elif pending and len(pending) + len(sentence) + 1 > limit:
            pieces.append(pending)
            pending = sentence
        else:
            pending = f"{pending} {sentence}".strip()
    if pending:
        pieces.append(pending)
    return pieces


def render(kokoro, text: str, profile: Profile) -> tuple[bytes, dict]:
    prose = spoken_text(text)
    pieces = (_speech_pieces(prose) if profile.grouping == "current"
              else sentence_pieces(prose))
    voice = profile.voice
    if profile.blend_voice:
        weight = profile.blend_weight
        voice = (kokoro.get_voice_style(profile.voice) * (1 - weight)
                 + kokoro.get_voice_style(profile.blend_voice) * weight)
    chunks, timings, rates = [], [], []
    for index, piece in enumerate(pieces):
        started = time.perf_counter()
        # Keep the original no-added-pause baseline reproducible even after
        # VoiceService adopts sentence splitting and explicit pauses.
        audio, rate = kokoro.create(
            piece, voice=voice, speed=profile.speed, lang="en-us",
        )
        timings.append(time.perf_counter() - started)
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        if rate <= 0 or not len(audio) or not np.isfinite(audio).all():
            raise ValueError("Synthesis returned invalid audio or sample rate")
        rates.append(rate)
        chunks.append(audio)
        if (index < len(pieces) - 1 and profile.boundary_pause_s
                and piece.endswith((".", "!", "?"))):
            chunks.append(np.zeros(round(rate * profile.boundary_pause_s), np.float32))
    if not chunks or len(set(rates)) != 1:
        raise ValueError("Synthesis returned no audio or inconsistent sample rates")
    joined = np.concatenate(chunks)
    duration = len(joined) / rates[0]
    return wav_bytes(joined, rates[0]), {
        "spoken_text": prose, "pieces": pieces, "sample_rate": rates[0],
        "duration_s": duration, "chunk_synthesis_s": timings,
        "first_chunk_synthesis_s": timings[0], "synthesis_s": sum(timings),
        "real_time_factor": sum(timings) / duration,
        "peak_amplitude": float(np.max(np.abs(joined))),
        "clipped_samples": int(np.count_nonzero(np.abs(joined) > 1)),
    }


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def generate(output: Path, kokoro, baseline_voice: str, metadata: dict) -> dict:
    # A fresh directory prevents a new run from silently replacing audio that
    # a listener has already rated. Old kits and their session IDs stay valid.
    output.mkdir(parents=True, exist_ok=False)
    (output / "audio").mkdir()
    candidates = {profile.id: profile for profile in profiles(baseline_voice)}
    pairs = comparisons()
    required = sorted({(pair["sample"], profile)
                       for pair in pairs for profile in pair["profiles"]})
    # Shuffle generation order as well, so warm/shape-cache order is recorded
    # rather than always favoring a particular experimental profile.
    generation_seed = 12
    random.Random(generation_seed).shuffle(required)
    manifest = {
        "schema": 1, "id": uuid.uuid4().hex,
        "created_at": datetime.now(UTC).isoformat(),
        "metadata": metadata, "generation_seed": generation_seed,
        "profiles": {key: asdict(value) for key, value in candidates.items()},
        "samples": SAMPLES, "comparisons": pairs, "clips": {},
    }
    for index, (sample, profile_id) in enumerate(required):
        key = f"{sample}--{profile_id}"
        print(f"[{index + 1}/{len(required)}] {key}", flush=True)
        payload, metrics = render(
            kokoro, SAMPLES[sample]["text"], candidates[profile_id],
        )
        filename = f"audio/clip-{index + 1:03}.wav"
        (output / filename).write_bytes(payload)
        manifest["clips"][key] = {
            "file": filename, "sha256": hashlib.sha256(payload).hexdigest(),
            **metrics,
        }
    for name in ("index.html", "app.js", "style.css"):
        shutil.copyfile(ASSETS / name, output / name)
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8",
    )
    # A script payload lets the complete kit also work via file://, without
    # requiring fetch permissions or a running application.
    encoded = json.dumps(manifest).replace("<", "\\u003c")
    (output / "manifest.js").write_text(
        f"window.LISTENING_KIT = {encoded};\n", encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--round", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output directory; existing listening kits are kept.")
    config = RecollectConfig.from_env()
    model = config.voice_model_dir / "kokoro-v1.0.onnx"
    voices = config.voice_model_dir / "voices-v1.0.bin"
    if not model.is_file() or not voices.is_file():
        parser.error("Installed Kokoro model and voice files are required.")
    import kokoro_onnx
    import onnxruntime as ort

    # Reuse the production CUDA selection and no-fallback rules, but load only
    # TTS: no embedder, ASR, microphone, model server, or conversation store.
    service = VoiceService(config)
    session = service._speech_session(ort)
    kokoro = kokoro_onnx.Kokoro.from_session(session, str(voices))
    needed = {p.voice for p in profiles(config.voice_name)} | {"af_bella"}
    missing = needed - set(kokoro.get_voices())
    if missing:
        parser.error(f"Installed voice bundle is missing: {sorted(missing)}")
    kokoro.create("Ready for the listening comparison.",
                  voice=config.voice_name, lang="en-us")
    metadata = {
        "model": model.name, "model_sha256": sha256(model),
        "voices_sha256": sha256(voices), "baseline_voice": config.voice_name,
        "kokoro_onnx_version": importlib.metadata.version("kokoro-onnx"),
        "kokoro_source_sha256": sha256(Path(kokoro_onnx.__file__)),
        "generator_sha256": sha256(Path(__file__)),
        "production_voice_sha256": sha256(
            Path(__file__).parent / "engine" / "voice.py",
        ),
        "onnxruntime_version": ort.__version__, "python": platform.python_version(),
        "providers": session.get_providers(), "requested_device": config.voice_device,
        "model_outputs": [item.name for item in session.get_outputs()],
        "synthesis_defaults": {
            name: parameter.default
            for name, parameter in inspect.signature(kokoro.create).parameters.items()
            if parameter.default is not inspect.Parameter.empty
        },
        "timing_scope": "Warm offline synthesis; excludes chat and browser latency.",
        "playback_scope": (
            "Chunks concatenated without transport gaps; no normalization."
        ),
    }
    if args.round == 2:
        from recollect.voice_listening_round2 import generate_round2

        manifest = generate_round2(args.output, kokoro, config.voice_name, metadata)
    else:
        manifest = generate(args.output, kokoro, config.voice_name, metadata)
    print(json.dumps({"output": str(args.output.resolve()),
                      "clips": len(manifest["clips"]),
                      "comparisons": len(manifest["comparisons"])}), flush=True)


if __name__ == "__main__":
    main()
