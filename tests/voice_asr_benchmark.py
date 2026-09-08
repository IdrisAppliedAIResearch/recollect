"""Opt-in paired dictation evaluation, separate from wake and microphone tests.

Run ``uv run --no-sync python -m tests.voice_asr_benchmark --report PATH``.
The report must not exist. Models run sequentially on identical in-memory PCM;
ordinary pytest only imports the pure metrics and never loads these models.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import platform
import re
import shutil
import statistics
import subprocess
import time
import unicodedata
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from .voice_asr_cases import asr_cases

SAMPLE_RATE = 16_000
LIMITATIONS = [
    "Synthetic Kokoro voices and seeded noise; not human accents or room acoustics.",
    "Dictation only: wake matching, Silero gating, partials, interruption, microphone "
    "transport, and real-time scheduling are outside this benchmark.",
    "Lexical WER ignores case/punctuation and normalizes known abbreviations, but "
    "does not equate spelled numbers with digits or rewrite homophones.",
    "Critical checks accept only declared forms within an aligned span. They are "
    "a lexical proxy for meaning; inspect expected/observed spans and full text.",
    "Held-out wording is newly written evaluation text, not a claim about model "
    "training data. Two repeats measure this run, not independent human trials.",
    "Warm latency includes complete offline decoding and excludes model loading, "
    "audio generation, warm-up inference, and conversational endpoint waiting.",
]


def normalize_tokens(text: str) -> list[str]:
    """Ignore typography while retaining numeric values and currency identity."""
    text = unicodedata.normalize("NFKC", text).lower().replace("’", "'")
    for short in ("am", "pm", "eta"):
        letters = r"[.\s]*".join(short)
        text = re.sub(rf"\b{letters}\.?(?!\w)", short, text)
    text = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", text)
    text = text.replace("'", "")
    tokens = re.findall(r"[$€£]?\d+(?:[.:]\d+)*(?:st|nd|rd|th)?|[^\W\d_]+", text)
    return [token.replace(":", ".") for token in tokens]


def _alignment(reference: list[str], actual: list[str]) -> list[tuple]:
    costs = [[0] * (len(actual) + 1) for _ in range(len(reference) + 1)]
    for i in range(len(reference) + 1):
        costs[i][0] = i
    for j in range(len(actual) + 1):
        costs[0][j] = j
    for i, left in enumerate(reference, 1):
        for j, right in enumerate(actual, 1):
            costs[i][j] = min(
                costs[i - 1][j - 1] + (left != right),
                costs[i - 1][j] + 1,
                costs[i][j - 1] + 1,
            )
    i, j = len(reference), len(actual)
    steps = []
    while i or j:
        if (
            i
            and j
            and costs[i][j]
            == (costs[i - 1][j - 1] + (reference[i - 1] != actual[j - 1]))
        ):
            steps.append(
                (
                    "equal" if reference[i - 1] == actual[j - 1] else "substitution",
                    i - 1,
                    j - 1,
                )
            )
            i -= 1
            j -= 1
        elif i and costs[i][j] == costs[i - 1][j] + 1:
            steps.append(("deletion", i - 1, None))
            i -= 1
        else:
            steps.append(("insertion", None, j - 1))
            j -= 1
    return list(reversed(steps))


def _span(tokens: list[str], phrase: list[str]) -> tuple[int, int] | None:
    for index in range(len(tokens) - len(phrase) + 1):
        if tokens[index : index + len(phrase)] == phrase:
            return index, index + len(phrase)
    return None


def transcript_metrics(reference: str, actual: str, critical: list[dict]) -> dict:
    expected_tokens, actual_tokens = (
        normalize_tokens(reference),
        normalize_tokens(actual),
    )
    aligned = _alignment(expected_tokens, actual_tokens)
    counts = {
        kind: sum(step[0] == kind for step in aligned)
        for kind in ("substitution", "deletion", "insertion")
    }
    errors = sum(counts.values())
    checks = []
    for term in critical:
        bounds = _span(expected_tokens, normalize_tokens(term["reference"]))
        if bounds is None:
            raise ValueError(f"Critical reference is absent: {term['reference']!r}")
        start, end = bounds
        relevant = [
            i
            for i, (_, ref_index, _) in enumerate(aligned)
            if ref_index is not None and start <= ref_index < end
        ]
        # Include insertions inside the aligned phrase, but not another phrase
        # elsewhere in the utterance that happens to contain the expected value.
        observed_indices = [
            actual_index
            for _, _, actual_index in aligned[relevant[0] : relevant[-1] + 1]
            if actual_index is not None
        ]
        observed = [actual_tokens[i] for i in observed_indices]
        forms = term["accepted_forms"]
        checks.append(
            {
                "expected": term["expected"],
                "reference": term["reference"],
                "observed_normalized": " ".join(observed),
                "accepted_forms": forms,
                "matches_expected_form": any(
                    observed == normalize_tokens(form) for form in forms
                ),
            }
        )
    return {
        "normalized_reference": " ".join(expected_tokens),
        "normalized_actual": " ".join(actual_tokens),
        "reference_words": len(expected_tokens),
        "actual_words": len(actual_tokens),
        **counts,
        "word_errors": errors,
        "lexical_wer": errors / len(expected_tokens) if expected_tokens else None,
        "normalized_exact": expected_tokens == actual_tokens,
        "nonspeech_hallucination": not expected_tokens and bool(actual_tokens),
        "critical": checks,
        "all_critical_forms_match": all(row["matches_expected_form"] for row in checks)
        if checks
        else None,
    }


def _pcm(voice, case: dict, cache: dict) -> bytes:
    import numpy as np

    rng = np.random.default_rng(case["seed"])
    if not case["reference"]:
        count = round(case["seconds"] * SAMPLE_RATE)
        audio = np.zeros(count, dtype=np.float32)
        if case["condition"] == "hiss":
            audio = rng.normal(0, case["amplitude"], count)
        elif case["condition"] == "clicks":
            for index in range(SAMPLE_RATE // 3, count, SAMPLE_RATE // 2):
                audio[index : index + 4] = case["amplitude"]
    else:
        text = case["reference"]
        segments = [text]
        if case["pause_s"]:
            words = text.split()
            midpoint = len(words) // 2
            segments = [" ".join(words[:midpoint]), " ".join(words[midpoint:])]
        chunks = []
        for segment in segments:
            key = (segment, case["voice"], case["lang"], case["speed"])
            if key not in cache:
                # The input reference bypasses reply-only speech formatting.
                with voice._speech_lock:
                    samples, rate = voice._kokoro.create(
                        segment,
                        voice=case["voice"],
                        lang=case["lang"],
                        speed=case["speed"],
                    )
                samples = np.asarray(samples).reshape(-1)
                if not len(samples) or not np.isfinite(samples).all():
                    raise ValueError("Kokoro returned invalid benchmark audio")
                count = round(len(samples) * SAMPLE_RATE / rate)
                cache[key] = np.interp(
                    np.arange(count) * rate / SAMPLE_RATE,
                    np.arange(len(samples)),
                    samples,
                )
            if chunks:
                chunks.append(np.zeros(round(case["pause_s"] * SAMPLE_RATE)))
            chunks.append(cache[key])
        audio = np.concatenate(chunks) * case["gain"]
        audio = np.concatenate(
            (np.zeros(SAMPLE_RATE // 4), audio, np.zeros(SAMPLE_RATE // 2))
        )
        if case["noise_std"]:
            audio = audio + rng.normal(0, case["noise_std"], len(audio))
    return (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()


def _vosk_decode(model, pcm: bytes) -> str:
    import vosk

    recognizer = vosk.KaldiRecognizer(model, SAMPLE_RATE)
    parts = []
    for offset in range(0, len(pcm), 1024):
        if recognizer.AcceptWaveform(pcm[offset : offset + 1024]):
            parts.append(json.loads(recognizer.Result()).get("text", ""))
    parts.append(json.loads(recognizer.FinalResult()).get("text", ""))
    return " ".join(part.strip() for part in parts if part.strip())


def _gpu_info() -> dict:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return {"available": False}
    outputs = {}
    for key, query in (
        (
            "gpu",
            "--query-gpu=index,name,uuid,memory.total,memory.used,memory.free,driver_version",
        ),
        ("processes", "--query-compute-apps=pid,process_name,used_gpu_memory"),
    ):
        try:
            result = subprocess.run(
                [executable, query, "--format=csv"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            outputs[key] = {
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
                "returncode": result.returncode,
            }
        except (OSError, subprocess.TimeoutExpired) as exc:
            outputs[key] = {"error": str(exc)}
    return {"available": True, "raw": outputs}


def _packages() -> dict:
    result = {}
    for name in (
        "vosk",
        "kokoro-onnx",
        "onnxruntime",
        "onnxruntime-gpu",
        "faster-whisper",
        "ctranslate2",
        "numpy",
    ):
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = None
    return result


def _file_identity(path: Path) -> dict:
    result = {"path": str(path.resolve()), "exists": path.is_file()}
    if path.is_file():
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        result.update(bytes=path.stat().st_size, sha256=digest.hexdigest())
    return result


def summarize(cases: list[dict]) -> dict:
    result = {}
    for backend in ("vosk", "whisper"):
        rows = [
            decode
            for case in cases
            for decode in case.get("decodes", [])
            if decode["backend"] == backend
        ]
        good = [row for row in rows if "metrics" in row]
        metrics = [row["metrics"] for row in good]
        speech = [row for row in metrics if row["reference_words"]]
        checks = [check for row in metrics for check in row["critical"]]
        latencies = sorted(row["decode_s"] for row in good)
        reference_words = sum(row["reference_words"] for row in speech)
        result[backend] = {
            "decodes": len(rows),
            "decode_errors": len(rows) - len(good),
            "normalized_exact": sum(row["normalized_exact"] for row in metrics),
            "speech_decodes": len(speech),
            "speech_normalized_exact": sum(row["normalized_exact"] for row in speech),
            "lexical_wer_weighted": (
                sum(row["word_errors"] for row in speech) / reference_words
            )
            if reference_words
            else None,
            "critical_form_checks": len(checks),
            "critical_forms_matched": sum(
                row["matches_expected_form"] for row in checks
            ),
            "nonspeech_decodes": len(metrics) - len(speech),
            "nonspeech_hallucinations": sum(
                row["nonspeech_hallucination"] for row in metrics
            ),
            "warm_decode_s_median": statistics.median(latencies) if latencies else None,
            "warm_decode_s_p95_nearest_rank": latencies[
                math.ceil(0.95 * len(latencies)) - 1
            ]
            if latencies
            else None,
            "warm_rtf_median": statistics.median(
                row["real_time_factor"] for row in good
            )
            if good
            else None,
        }
    return result


def run_benchmark(
    config, *, report: Path, repeats: int = 2, case_ids: list[str] | None = None
) -> dict:
    """Create a durable report exclusively; only this call loads real models."""
    if repeats < 1:
        raise ValueError("repeats must be positive")
    fixtures = asr_cases()
    if case_ids:
        missing = set(case_ids) - {case["id"] for case in fixtures}
        if missing:
            raise ValueError(f"Unknown case IDs: {sorted(missing)}")
        fixtures = [case for case in fixtures if case["id"] in case_ids]
    # Exclusive creation happens before imports, model loading, or GPU queries.
    with report.open("x", encoding="utf-8") as handle:
        result = {
            "schema_version": 1,
            "started_utc": datetime.now(UTC).isoformat(),
            "status": "running",
            "purpose": "paired offline dictation",
            "limitations": LIMITATIONS,
            "repeats": repeats,
            "case_count": len(fixtures),
            "sample_rate": SAMPLE_RATE,
            "pcm_format": "mono signed little-endian int16",
            "resampling": "numpy linear interpolation to 16000 Hz",
            "speech_padding_s": {"leading": 0.25, "trailing": 0.5},
            "decode_order": "alternate first backend by case index plus repeat index",
            "runtime": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "packages": _packages(),
            },
            "cases": [],
        }

        def save():
            handle.seek(0)
            json.dump(result, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
            handle.truncate()
            handle.flush()

        save()
        try:
            from recollect.engine.voice import VoiceService
            from recollect.engine.voice_asr import WhisperTranscriber

            result["gpu_before_loading"] = _gpu_info()
            result["models"] = {
                "vosk": _file_identity(
                    config.voice_model_dir
                    / "vosk-model-small-en-us-0.15"
                    / "am"
                    / "final.mdl"
                ),
                "whisper": _file_identity(config.voice_asr_model_dir / "model.bin"),
                "kokoro": _file_identity(config.voice_model_dir / "kokoro-v1.0.onnx"),
            }
            voice = VoiceService(replace(config, voice_asr_backend="vosk"))
            transcriber = WhisperTranscriber(config)
            started = time.perf_counter()
            voice.warm_up()
            result["voice_load_s"] = time.perf_counter() - started
            started = time.perf_counter()
            transcriber.warm_up()
            result["whisper_load_s"] = time.perf_counter() - started
            result["configuration"] = {
                "kokoro_provider": voice.status()["provider"],
                "kokoro_cuda_dll_dir": str(config.voice_cuda_dll_dir)
                if config.voice_cuda_dll_dir
                else None,
                "voice_threads": config.voice_threads,
                "vosk": {"grammar": None, "device": "cpu", "frame_samples": 512},
                "whisper": {
                    "device": config.voice_asr_device,
                    "compute_type": config.voice_asr_compute_type,
                    "cuda_dll_dir": str(config.voice_asr_cuda_dll_dir)
                    if config.voice_asr_cuda_dll_dir
                    else None,
                    "language": "en",
                    "task": "transcribe",
                    "beam_size": 1,
                    "temperature": 0,
                    "condition_on_previous_text": False,
                    "vad_filter": False,
                    "local_files_only": True,
                },
            }
            decoders = {
                "vosk": lambda pcm: _vosk_decode(voice._model, pcm),
                "whisper": transcriber.transcribe,
            }
            cache = {}
            warmup_case = {
                "reference": "This is the warm up sentence before evaluation.",
                "voice": "af_heart",
                "lang": "en-us",
                "speed": 1.0,
                "gain": 1.0,
                "noise_std": 0.0,
                "pause_s": 0.0,
                "seed": 1,
            }
            warmup_pcm = _pcm(voice, warmup_case, cache)
            result["warmup"] = {
                "reference": warmup_case["reference"],
                "audio_s": len(warmup_pcm) / (2 * SAMPLE_RATE),
                "pcm_sha256": hashlib.sha256(warmup_pcm).hexdigest(),
                "excluded_from_summary": True,
                "decodes": [],
            }
            for backend, decoder in decoders.items():
                started = time.perf_counter()
                transcript = decoder(warmup_pcm)
                result["warmup"]["decodes"].append(
                    {
                        "backend": backend,
                        "transcript": transcript,
                        "decode_s": time.perf_counter() - started,
                    }
                )
                save()
            result["gpu_after_warmup"] = _gpu_info()
            for index, case in enumerate(fixtures):
                started = time.perf_counter()
                pcm = _pcm(voice, case, cache)
                audio_s = len(pcm) / (2 * SAMPLE_RATE)
                row = {
                    **case,
                    "audio_s": audio_s,
                    "audio_prepare_s": time.perf_counter() - started,
                    "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
                    "decodes": [],
                }
                result["cases"].append(row)
                for repeat in range(repeats):
                    order = (
                        ("vosk", "whisper")
                        if (index + repeat) % 2 == 0
                        else ("whisper", "vosk")
                    )
                    for backend in order:
                        started = time.perf_counter()
                        decode = {"backend": backend, "repeat": repeat + 1}
                        try:
                            transcript = decoders[backend](pcm)
                            elapsed = time.perf_counter() - started
                            decode.update(
                                transcript=transcript,
                                decode_s=elapsed,
                                real_time_factor=elapsed / audio_s,
                                metrics=transcript_metrics(
                                    case["reference"], transcript, case["critical"]
                                ),
                            )
                        except Exception as exc:
                            decode.update(
                                error=f"{type(exc).__name__}: {exc}",
                                decode_s=time.perf_counter() - started,
                            )
                        row["decodes"].append(decode)
                        save()
                print(f"{index + 1}/{len(fixtures)} {case['id']}", flush=True)
            result["summary"] = summarize(result["cases"])
            result["summary_by_split"] = {
                split: summarize(
                    [case for case in result["cases"] if case["split"] == split]
                )
                for split in sorted({case["split"] for case in result["cases"]})
            }
            result["gpu_after_decodes"] = _gpu_info()
            result["status"] = (
                "completed_with_errors"
                if any(row["decode_errors"] for row in result["summary"].values())
                else "complete"
            )
        except BaseException as exc:
            result.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            result["finished_utc"] = datetime.now(UTC).isoformat()
            save()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--case", dest="case_ids", action="append")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--whisper-model-dir", type=Path)
    args = parser.parse_args(argv)
    if args.report.exists():
        parser.error("Report already exists; choose a new path.")
    from recollect.config import RecollectConfig

    config = RecollectConfig.from_env()
    if args.whisper_model_dir:
        config = replace(config, voice_asr_model_dir=args.whisper_model_dir)
    result = run_benchmark(
        config, report=args.report, repeats=args.repeats, case_ids=args.case_ids
    )
    print(json.dumps(result.get("summary", {}), indent=2), flush=True)
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
