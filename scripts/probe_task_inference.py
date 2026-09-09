"""Opt-in synthetic scheduling/cache probe against an already running GPU model.

Run with the installed environment, never during a user's active conversation:
uv run --no-sync python scripts/probe_task_inference.py --profile single
This warms the configured GPU voice models; it opens no mic and writes no episodes.
The main/build/general/compaction labels are synthetic prompt families, not a
claim to have captured native OpenCode requests. Output is a JSON evidence record.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import statistics
import subprocess
import time
import wave

import httpx
import numpy as np

from recollect.config import RecollectConfig
from recollect.engine.model_admission import ModelAdmission
from recollect.engine.voice import VoiceService


def gpu_memory():
    return int(
        subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=memory.used",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=5,
        )
        .strip()
        .splitlines()[0]
    )


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("single", "two"), required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--stress", action="store_true")
    args = parser.parse_args()
    config = RecollectConfig.from_env()
    voice = VoiceService(config)
    start = time.perf_counter()
    await asyncio.to_thread(voice.warm_up)
    status = voice.status()
    assert status["provider"] == "CUDAExecutionProvider", status
    assert status["asr_ready"] and status["asr_device"] == "cuda", status
    assert status["asr_compute_type"] == "float16", status
    print(
        json.dumps({"voice_warm_s": time.perf_counter() - start, "voice": status}),
        flush=True,
    )
    result = {
        "profile": args.profile,
        "synthetic": True,
        "samples": [],
        "voice": [],
        "vram_mib": [],
    }
    stopping = asyncio.Event()

    async def sample_memory():
        while not stopping.is_set():
            result["vram_mib"].append(await asyncio.to_thread(gpu_memory))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), 0.5)

    sampler = asyncio.create_task(sample_memory())
    admission = ModelAdmission()
    async with httpx.AsyncClient(
        base_url=config.generator_base_url,
        timeout=180,
        headers={"Authorization": "Bearer " + config.generator_api_key},
        trust_env=False,
    ) as client:
        root = str(client.base_url).rstrip("/").removesuffix("/v1")
        result["props"] = (await client.get(root + "/props")).json()

        async def generate(
            family, repeat, *, background=False, output=64, admitted=True, long=False
        ):
            prefix = (
                config.system_prompt
                if family == "main"
                else f"You are a {family} research assistant. Record exact evidence."
            )
            # Numbered text avoids overstating cache reuse from repeated tokens.
            context = "\n".join(
                f"Record {i}: the {family} source measures temperature, cost, "
                "durability and test conditions; uncertainties need verification."
                for i in range((1250 if args.stress else 850) if long else 160)
            )
            payload = {
                "model": config.generator_model,
                "stream": True,
                "messages": [
                    {"role": "system", "content": prefix},
                    {
                        "role": "user",
                        "content": context
                        + "\nWrite a detailed numbered analysis of every record. "
                        f"Batch marker {repeat}. Keep going until all are covered.",
                    },
                ],
                "max_tokens": output,
                "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
                "stream_options": {"include_usage": True},
                "cache_prompt": True,
            }
            queued = time.perf_counter()
            if admitted:
                await admission.acquire(background=background)
            started = time.perf_counter()
            row = {
                "family": family,
                "repeat": repeat,
                "output_cap": output,
                "admitted": admitted,
                "long": long,
                "queue_ms": (started - queued) * 1000,
            }
            try:
                async with client.stream(
                    "POST", "/chat/completions", json=payload
                ) as r:
                    r.raise_for_status()
                    async for line in r.aiter_lines():
                        if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                            continue
                        event = json.loads(line[5:])
                        if any(
                            c.get("delta", {}).get("content")
                            for c in event.get("choices", [])
                        ):
                            row.setdefault(
                                "visible_ms", (time.perf_counter() - queued) * 1000
                            )
                        if "timings" in event:
                            row["timings"] = event["timings"]
                        if event.get("usage"):
                            row["usage"] = event["usage"]
                row["total_ms"] = (time.perf_counter() - queued) * 1000
                result["samples"].append(row)
                print(json.dumps(row), flush=True)
            finally:
                if admitted:
                    admission.release()

        async def speech():
            before = time.perf_counter()
            wav = await asyncio.to_thread(
                voice.synthesize,
                "The research is still running. I have saved the first result.",
            )
            tts = time.perf_counter() - before
            with wave.open(io.BytesIO(wav)) as audio:
                values = np.frombuffer(
                    audio.readframes(audio.getnframes()), dtype="<i2"
                )
                rate = audio.getframerate()
            pcm = (
                np.interp(
                    np.arange(0, len(values), rate / 16000),
                    np.arange(len(values)),
                    values,
                )
                .astype("<i2")
                .tobytes()
            )
            before = time.perf_counter()
            text = await asyncio.to_thread(voice._asr.transcribe, pcm)
            result["voice"].append(
                {
                    "tts_ms": tts * 1000,
                    "asr_ms": (time.perf_counter() - before) * 1000,
                    "transcript": text,
                }
            )

        try:
            if args.stress:
                worker = asyncio.create_task(
                    generate(
                        "build",
                        999,
                        background=True,
                        output=2048,
                        long=True,
                        admitted=args.profile == "single",
                    )
                )
                await asyncio.sleep(0.15)
                await asyncio.gather(
                    worker,
                    generate(
                        "main",
                        999,
                        long=True,
                        admitted=args.profile == "single",
                    ),
                    speech(),
                )
                args.repetitions = 0
            for repeat in range(args.repetitions):
                for family in ("main", "build", "general", "compaction", "main"):
                    await generate(family, repeat)
                worker = asyncio.create_task(
                    generate(
                        "build",
                        repeat,
                        background=True,
                        output=512,
                        admitted=args.profile == "single",
                    )
                )
                await asyncio.sleep(0.15)
                await asyncio.gather(
                    worker,
                    generate(
                        "main",
                        repeat,
                        admitted=args.profile == "single",
                    ),
                    speech(),
                )
            if not args.stress:
                await generate("build", 99, background=True, output=64, long=True)
        finally:
            stopping.set()
            await sampler
    result["peak_vram_mib"] = max(result.pop("vram_mib"))
    for field in ("queue_ms", "visible_ms", "total_ms"):
        values = sorted(r[field] for r in result["samples"] if field in r)
        result[field] = {
            "median": statistics.median(values),
            "worst": max(values),
            "p95": values[min(len(values) - 1, int(len(values) * 0.95))],
        }
    print("RESULT " + json.dumps(result), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
