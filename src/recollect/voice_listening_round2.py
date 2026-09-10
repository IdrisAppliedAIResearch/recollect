"""Matched PCM experiments for the second, feedback-led Kokoro audition."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import uuid
import wave
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from recollect.voice_listening import ASSETS, Profile, render, sha256

# Boundaries are authored for this corpus, not inferred by a general tokenizer.
# A clause split is itself experimental and receives a separate control pair.
UNITS = {
    "conversation": [
        ("We can keep this simple.", "sentence"),
        ("Before we choose a solution,", "clause"),
        ("let us make sure we understand what is causing the problem.", "sentence"),
        ("The first test is quick,", "clause"),
        ("but the second gives us more useful information about the smaller details.",
         "paragraph"),
        ("There is no rush.", "sentence"),
        ("We can check the results together and decide which changes "
         "are worth keeping.",
         "sentence"),
        ("Does that sound sensible?", "end"),
    ],
    "long": [
        ("Imagine that we are making a quiet place to sit in the garden.", "sentence"),
        ("We do not need much.", "sentence"),
        ("Before buying plants,", "clause"),
        ("we should watch where the sunlight falls at different times of day.",
         "sentence"),
        ("A bright morning can be misleading.", "paragraph"),
        ("Next, we can think about the path.", "sentence"),
        ("It should leave enough space for a chair and a watering can,", "clause"),
        ("without making the rest of the garden feel crowded.", "sentence"),
        ("Small details matter.", "sentence"),
        ("A comfortable seat in the right place may be more useful than an elaborate "
         "design that is difficult to look after.", "paragraph"),
        ("We can start with a few herbs near the kitchen door.", "sentence"),
        ("If they grow well,", "clause"),
        ("we can add flowers that bring some color as the seasons change.", "sentence"),
        ("After a month, we can see which parts of the space we actually use.",
         "sentence"),
        ("The next step can wait until then.", "end"),
    ],
}
BASE_PAUSES = {"clause": 0.10, "sentence": 0.22, "paragraph": 0.38, "end": 0.0}


def pause_frames(units, durations, rate, varied=False):
    """Redistribute each boundary class's silence budget, exactly in samples."""
    result = [round(BASE_PAUSES[kind] * rate) for _, kind in units]
    if not varied:
        return result
    elapsed = 0.0
    lengths = []
    for (_, kind), duration in zip(units, durations, strict=True):
        elapsed += duration
        lengths.append(elapsed)
        if kind != "clause":
            elapsed = 0.0
    for kind in ("clause", "sentence", "paragraph"):
        indexes = [i for i, unit in enumerate(units) if unit[1] == kind]
        if not indexes:
            continue
        # Bounded variation responds to preceding spoken duration. Centering
        # within class keeps fixed/varied total durations exactly comparable.
        weights = np.array([np.clip(lengths[i], 1.0, 10.0) for i in indexes])
        offsets = (weights - weights.mean()) / 9.0 * BASE_PAUSES[kind] * 0.9
        targets = [(BASE_PAUSES[kind] + offset) * rate for offset in offsets]
        allocated = [int(np.floor(value)) for value in targets]
        remainder = sum(result[i] for i in indexes) - sum(allocated)
        order = sorted(range(len(indexes)),
                       key=lambda j: targets[j] - allocated[j], reverse=True)
        for j in order[:remainder]:
            allocated[j] += 1
        for i, count in zip(indexes, allocated, strict=True):
            result[i] = count
    return result


def adaptive_speeds(units):
    """Small native synthesis changes; longer phrases receive the slower rates."""
    words = np.array([len(text.split()) for text, _ in units], dtype=float)
    density = np.clip(words, 3, 20)
    centered = density - np.average(density, weights=words)
    scale = max(float(np.max(np.abs(centered))), 1.0)
    return (1.025 - centered / scale * 0.015).tolist()


def decode(payload):
    with wave.open(io.BytesIO(payload)) as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError("Expected mono 16-bit PCM")
        return handle.readframes(handle.getnframes()), handle.getframerate()


def assemble(stems, pauses):
    rates = {stem[1] for stem in stems}
    if len(rates) != 1 or len(stems) != len(pauses):
        raise ValueError("Inconsistent stems or pause schedule")
    rate = rates.pop()
    data, boundaries, offset = [], [], 0
    for (pcm, _, _metrics), pause in zip(stems, pauses, strict=True):
        data.extend((pcm, b"\0\0" * pause))
        frames = len(pcm) // 2
        boundaries.append({
            "speech_start_frame": offset, "speech_frames": frames,
            "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
            "added_pause_frames": pause, "added_pause_s": pause / rate,
        })
        offset += frames + pause
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(data))
    timings = [t for _, _, m in stems for t in m["chunk_synthesis_s"]]
    return buffer.getvalue(), {
        "sample_rate": rate, "duration_s": offset / rate,
        "pieces": [p for _, _, m in stems for p in m["pieces"]],
        "first_chunk_synthesis_s": timings[0], "chunk_synthesis_s": timings,
        "synthesis_s": sum(timings), "real_time_factor": sum(timings) / (offset / rate),
        "peak_amplitude": max(m["peak_amplitude"] for _, _, m in stems),
        "clipped_samples": sum(m["clipped_samples"] for _, _, m in stems),
        "boundaries": boundaries, "added_silence_frames": sum(pauses),
        "timing_note": "Cached stem synthesis cost; excludes assembly and playback.",
    }


def generate_round2(output: Path, kokoro, baseline_voice: str, metadata: dict):
    output.mkdir(parents=True, exist_ok=False)
    (output / "audio").mkdir()
    definitions = [
        ("baseline", "Current voice and chunking", "current", "none", 1.0),
        ("blend", "75% baseline + 25% Bella, current chunking", "current", "none", 1.0),
        ("units", "Blend, authored boundaries, no extra silence",
         "authored", "none", 1.0),
        ("fixed", "Blend, fixed pauses by boundary type", "authored", "fixed", 1.0),
        ("varied", "Blend, duration-aware pauses", "authored", "varied", 1.0),
        ("fast", "Duration-aware pauses, steady 1.05×", "authored", "varied", 1.05),
        ("mid", "Duration-aware pauses, steady 1.025×", "authored", "varied", 1.025),
        ("adaptive", "Duration-aware pauses, subtle phrase pace", "authored", "varied",
         1.025),
    ]
    candidates = {}
    for key, label, grouping, pauses, speed in definitions:
        p = Profile(key, label, baseline_voice, grouping, speed,
                    blend_voice=None if key == "baseline" else "af_bella",
                    blend_weight=0 if key == "baseline" else 0.25)
        candidates[key] = {**asdict(p), "pause_policy": pauses,
                           "pace_policy": "adaptive" if key == "adaptive" else "steady"}
    rows = [
        ("Blend", "conversation", "baseline", "blend"),
        ("Boundary control", "conversation", "blend", "units"),
        ("Pause amount", "conversation", "units", "fixed"),
        ("Pause variance", "conversation", "fixed", "varied"),
        ("Overall pace", "conversation", "varied", "fast"),
        ("Pace variance", "conversation", "mid", "adaptive"),
        ("Long listen", "long", "fixed", "varied"),
        ("Long listen", "long", "baseline", "varied"),
    ]
    pairs = [{"id": f"round2-{i + 1:02}", "stage": stage, "sample": sample,
              "profiles": [a, b], "combined_candidate": i == 7}
             for i, (stage, sample, a, b) in enumerate(rows)]
    samples = {key: {"title": "A considered reply" if key == "conversation"
                    else "A longer garden conversation",
                    "text": " ".join(text for text, _ in units), "units": units}
               for key, units in UNITS.items()}
    manifest = {
        "schema": 1, "round": 2, "id": uuid.uuid4().hex,
        "created_at": datetime.now(UTC).isoformat(),
        "metadata": {**metadata, "round2_generator_sha256": sha256(Path(__file__)),
                     "variance_method": (
                         "Authored clauses/sentences; cached PCM for pause pairs. "
                         "Fixed and varied silence totals match within each boundary "
                         "class. Speed pairs reuse the 1.0x pause schedule. Adaptive "
                         "rates have word-weighted mean 1.025x, not matched duration. "
                         "No phoneme alignment, random jitter, or pitch processing."
                     )},
        "profiles": candidates, "samples": samples, "comparisons": pairs, "clips": {},
    }
    for sample, units in UNITS.items():
        needed = {p for pair in pairs if pair["sample"] == sample
                  for p in pair["profiles"]}
        cache = {}

        def stems_for(speeds, units=units, cache=cache):
            stems = []
            for (text, _), speed in zip(units, speeds, strict=True):
                key = (text, speed)
                if key not in cache:
                    p = Profile("stem", "Stem", baseline_voice, "current", speed,
                                blend_voice="af_bella", blend_weight=0.25)
                    payload, metrics = render(kokoro, text, p)
                    pcm, rate = decode(payload)
                    cache[key] = (pcm, rate, metrics)
                stems.append(cache[key])
            return stems

        base_stems = stems_for([1.0] * len(units))
        rate = base_stems[0][1]
        durations = [len(pcm) / 2 / rate for pcm, _, _ in base_stems]
        schedules = {
            "none": [0] * len(units),
            "fixed": pause_frames(units, durations, rate),
            "varied": pause_frames(units, durations, rate, varied=True),
        }
        for profile_id in candidates:
            if profile_id not in needed:
                continue
            profile = candidates[profile_id]
            print(f"Round 2: {sample}--{profile_id}", flush=True)
            if profile["grouping"] == "current":
                payload, metrics = render(kokoro, samples[sample]["text"], Profile(
                    **{key: value for key, value in profile.items()
                       if key in Profile.__dataclass_fields__},
                ))
            else:
                speeds = (adaptive_speeds(units) if profile_id == "adaptive"
                          else [profile["speed"]] * len(units))
                payload, metrics = assemble(stems_for(speeds),
                                            schedules[profile["pause_policy"]])
                for boundary, (text, kind), speed in zip(
                    metrics["boundaries"], units, speeds, strict=True,
                ):
                    boundary.update(text=text, kind=kind, speed=speed)
                metrics["spoken_text"] = samples[sample]["text"]
            filename = f"audio/clip-{len(manifest['clips']) + 1:03}.wav"
            (output / filename).write_bytes(payload)
            manifest["clips"][f"{sample}--{profile_id}"] = {
                "file": filename, "sha256": hashlib.sha256(payload).hexdigest(),
                **metrics,
            }
    for name in ("index.html", "app.js", "style.css"):
        shutil.copyfile(ASSETS / name, output / name)
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8",
    )
    encoded = json.dumps(manifest).replace("<", "\\u003c")
    (output / "manifest.js").write_text(
        f"window.LISTENING_KIT = {encoded};\n", encoding="utf-8",
    )
    return manifest
