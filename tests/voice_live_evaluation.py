"""Opt-in real-model evaluation; never collected by the model-free pytest suite.

Run from the repository root with ``uv run --no-sync python -m
tests.voice_live_evaluation --report docs/voice-evaluation-YYYY-MM-DD.json``.
The default runs all five groups (79 turns). Repeat ``--group`` to select groups;
``--group repair`` runs the 12 repair turns. Add ``--acoustics`` for 20 additional
direct-listener cases. These are opt-in model runs, separate from ordinary pytest.
Synthetic audio tests transport and recognition, not a human microphone or room.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import re
import socket
import tempfile
import time
import wave
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np
import uvicorn
from websockets.asyncio.client import connect

from recollect.api import create_app
from recollect.config import RecollectConfig
from recollect.engine.voice_text import spoken_text

from .voice_conversation_cases import conversations


def input_audio(voice, text: str, name: str, speed: float) -> bytes:
    # Bypass reply formatting: the intended words are the input fixture.
    with voice._speech_lock:
        samples, rate = voice._kokoro.create(
            text,
            voice=name,
            speed=speed,
            lang="en-us",
        )
    count = round(len(samples) * 16000 / rate)
    resampled = np.interp(
        np.arange(count) * rate / 16000, np.arange(len(samples)), samples
    )
    return (np.clip(resampled, -1, 1) * 32767).astype("<i2").tobytes()


def wav_duration(data: bytes) -> float:
    with wave.open(io.BytesIO(data), "rb") as audio:
        if audio.getnchannels() != 1 or audio.getframerate() != 24000:
            raise ValueError("Unexpected output audio format")
        return audio.getnframes() / audio.getframerate()


def word_error(reference: str, hypothesis: str) -> float:
    def words(text):
        return re.findall(r"[a-z0-9]+", text.lower())

    expected, actual = words(reference), words(hypothesis)
    previous = list(range(len(actual) + 1))
    for i, left in enumerate(expected, 1):
        current = [i]
        for j, right in enumerate(actual, 1):
            current.append(
                min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (left != right))
            )
        previous = current
    return previous[-1] / max(1, len(expected))


class SpeechConnection:
    def __init__(self, socket):
        self.socket = socket
        self.events: list[dict] = []
        self.changed = asyncio.Event()
        self.control_id = 0
        self.reader = asyncio.create_task(self.read())

    async def read(self):
        try:
            async for packet in self.socket:
                self.events.append({**json.loads(packet), "at": time.perf_counter()})
                self.changed.set()
        finally:
            self.changed.set()

    async def close(self):
        self.reader.cancel()
        await asyncio.gather(self.reader, return_exceptions=True)

    async def until(self, predicate, start=0, timeout=30):
        async def wait():
            while True:
                self.changed.clear()
                for event in self.events[start:]:
                    if predicate(event):
                        return event
                if self.reader.done():
                    self.reader.result()
                    raise RuntimeError("Voice socket closed before expected event")
                await self.changed.wait()

        return await asyncio.wait_for(wait(), timeout)

    async def utterance(self, pcm: bytes, *, realtime=False):
        start = len(self.events)
        began = time.perf_counter()
        # Leading silence and endpoint silence are transmitted as real PCM.
        padded = bytes(6400) + pcm + bytes(57600)
        for index in range(0, len(padded), 2048):
            if realtime:
                await asyncio.sleep(max(0, began + index / 32000 - time.perf_counter()))
            await self.socket.send(padded[index : index + 2048])
        wait_error = None
        try:
            # A control acknowledgment is not an ASR barrier. In particular,
            # unpause can invalidate an asynchronously decoding final result.
            await self.until(
                lambda e: e["type"] in {"transcript", "limit", "error"}, start,
            )
        except TimeoutError:
            wait_error = "No final transcript arrived before the evaluation timeout."
        events = self.events[start:]
        return {
            "transcripts": [e["text"] for e in events if e["type"] == "transcript"],
            "partial_count": sum(e["type"] == "partial" for e in events),
            "speech_start_count": sum(e["type"] == "speech_start" for e in events),
            "listening_state_events": sum(
                e["type"] == "state"
                and e.get("state") == "listening"
                and "control_id" not in e
                for e in events
            ),
            "errors": [e for e in events if e["type"] == "error"],
            "input_audio_s": len(pcm) / 32000,
            "transport_wall_s": time.perf_counter() - began,
            "realtime_input": realtime,
            "transcript_at": next(
                (e["at"] for e in events if e["type"] == "transcript"), None
            ),
            "input_ended_at": began + 0.2 + len(pcm) / 32000 if realtime else None,
            "wait_error": wait_error,
        }


async def chat(client, session_id, text):
    began = time.perf_counter()
    events = []
    event = ""
    async with client.stream(
        "POST",
        "/api/chat",
        json={
            "session_id": session_id,
            "message": text,
            "input_mode": "voice",
        },
    ) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                events.append((event, json.loads(line[6:])))
                if event in {"subagent_start", "subagent_result", "error"}:
                    print(
                        json.dumps({"event": event, "data": events[-1][1]}), flush=True
                    )
    done = next((data for kind, data in reversed(events) if kind == "done"), None)
    if done is None:
        return {"error": "Chat had no completion event", "events": events}
    trace = (await client.get(f"/api/turns/{done['turn_id']}")).json()
    generation = done["generation"]
    result = {
        "turn_id": done["turn_id"],
        "committed": done["committed"],
        "chat_wall_s": time.perf_counter() - began,
        "reply": generation["response_text"],
        "generation": generation,
        "verification": trace["verification"],
        "memory_payload": trace["context_block"]["payload"],
        "store_before": trace["store"],
        "subagent": trace.get("subagent"),
        "research_events": [
            dict(type=kind, data=data)
            for kind, data in events
            if kind.startswith("subagent_") or kind == "error"
        ],
    }
    return result


async def speech(client, turn_id):
    began = time.perf_counter()
    first_audio = None
    durations = []
    errors = []
    completed = False
    async with client.stream(
        "POST",
        "/api/voice/speech",
        json={
            "turn_id": turn_id,
            "stream": True,
        },
    ) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            record = json.loads(line)
            if record["type"] == "audio":
                if first_audio is None:
                    first_audio = time.perf_counter() - began
                durations.append(wav_duration(base64.b64decode(record["wav"])))
            elif record["type"] == "done":
                completed = True
            elif record["type"] == "error":
                errors.append(record["message"])
    return {
        "first_audio_s": first_audio,
        "synthesis_wall_s": time.perf_counter() - began,
        "audio_s": sum(durations),
        "chunk_count": len(durations),
        "chunk_durations_s": durations,
        "completed": completed,
        "errors": errors,
    }


def save(report, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


@asynccontextmanager
async def isolated_voice_app(config, report):
    """Share the real application lifecycle without touching user sessions."""
    data_path = sandbox_path = None
    try:
        with tempfile.TemporaryDirectory(prefix="recollect-voice-eval-") as data:
            data_path = Path(data)
            # Reuse the configured Docker file share, in a disposable child.
            config.sandbox_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix="voice-eval-", dir=config.sandbox_root,
            ) as sandbox:
                sandbox_path = Path(sandbox)
                isolated = replace(
                    config, data_dir=data_path, sandbox_root=sandbox_path,
                )
                app = create_app(isolated)
                sock = socket.socket()
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
                server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
                task = asyncio.create_task(server.serve(sockets=[sock]))
                try:
                    while not server.started:
                        if task.done():
                            await task
                            raise RuntimeError("Evaluation server failed to start")
                        await asyncio.sleep(0.05)
                    print(f"Isolated evaluation server: http://127.0.0.1:{port}",
                          flush=True)
                    voice = app.state.recollect.voice
                    await asyncio.to_thread(voice.warm_up)
                    report["voice_status"] = voice.status()
                    report["embedder"] = app.state.recollect.embedder_health
                    async with httpx.AsyncClient(
                        base_url=f"http://127.0.0.1:{port}", timeout=None,
                    ) as client:
                        yield voice, client, port
                finally:
                    server.should_exit = True
                    try:
                        await task
                    finally:
                        sock.close()
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        report["temporary_data_removed"] = all(
            path is None or not path.exists() for path in (data_path, sandbox_path)
        )


async def evaluate(args, config):
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "method": "Synthetic Kokoro input through local HTTP/WebSocket app; "
        "real in-process embedder and verified retrieval, configured ASR/Silero, "
        "Qwen and Kokoro output. Most input is accelerated; explicitly marked "
        "real-time trials only. No physical microphone, browser speaker playback, "
        "or perceptual listening score.",
        "settings": {
            "model": config.generator_model,
            "temperature": config.generator_temperature,
            "wake": config.voice_wake_phrase,
            "endpoint_s": config.voice_end_s,
            "recent_n": config.episodic.recency_window_n,
            "asr_backend": getattr(config, "voice_asr_backend", "vosk"),
        },
        "conversations": [],
    }
    try:
        async with isolated_voice_app(config, report) as (voice, client, port):
            for group, cases in conversations().items():
                if args.group and group not in args.group:
                    continue
                session = (
                    await client.post(
                        "/api/sessions",
                        json={"title": f"Synthetic evaluation: {group}"},
                    )
                ).json()
                conversation = {"group": group, "turns": []}
                report["conversations"].append(conversation)
                async with connect(
                    f"ws://127.0.0.1:{port}/api/voice/listen"
                ) as ws:
                    connection = SpeechConnection(ws)
                    await connection.until(lambda e: e["type"] == "state")
                    try:
                        for i, case in enumerate(cases[: args.limit or None]):
                            spoken = (
                                (config.voice_wake_phrase + ", ") if i == 0 else ""
                            ) + case["text"]
                            pcm = await asyncio.to_thread(
                                input_audio, voice, spoken,
                                case["voice"], case["speed"],
                            )
                            row = {
                                **case,
                                "asr": await connection.utterance(
                                    pcm, realtime=(group == "clarification" and i == 0),
                                ),
                            }
                            conversation["turns"].append(row)
                            texts = row["asr"]["transcripts"]
                            if len(texts) != 1:
                                row["error"] = (
                                    f"Expected one transcript; received {len(texts)}"
                                )
                            else:
                                row["asr"]["word_error_rate"] = word_error(
                                    case["text"], texts[0],
                                )
                                row["chat"] = await chat(
                                    client, session["session_id"], texts[0],
                                )
                                reply = row["chat"].get("reply", "")
                                row["spoken_text"] = spoken_text(reply)
                                row["reply_word_count"] = len(reply.split())
                                if row["chat"].get("committed"):
                                    row["speech"] = await speech(
                                        client, row["chat"]["turn_id"],
                                    )
                            print(json.dumps({
                                "group": group, "index": i + 1, "id": case["id"],
                                "heard": texts,
                                "reply": row.get("chat", {}).get("reply"),
                                "audio": row.get("speech"), "error": row.get("error"),
                            }), flush=True)
                            save(report, args.report)
                    finally:
                        await connection.close()
                conversation["stored_turn_count"] = len((
                    await client.get(f"/api/sessions/{session['session_id']}/turns")
                ).json())
            if args.acoustics:
                from .voice_acoustic_cases import run_acoustic_cases

                report["acoustics"] = await asyncio.to_thread(run_acoustic_cases, voice)
    finally:
        save(report, args.report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--group", action="append", choices=list(conversations()))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--acoustics", action="store_true")
    arguments = parser.parse_args()
    if arguments.report.exists():
        parser.error("Choose a new report path to preserve earlier results.")
    asyncio.run(evaluate(arguments, RecollectConfig.from_env()))
