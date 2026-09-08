"""Opt-in, paced WebSocket conversation checks; no model-free pytest collection.

Run ``uv run --no-sync python -m tests.voice_whisper_flow --backend whisper
--report docs/voice-whisper-flow-YYYY-MM-DD.json``. Select ``--backend vosk`` for
the same ordinary PCM flow; held-Whisper-result probes are then explicitly skipped.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from websockets.asyncio.client import connect

from recollect.config import RecollectConfig
from recollect.engine.voice_text import spoken_text

from .voice_conversation_cases import conversations
from .voice_live_evaluation import (
    SpeechConnection,
    chat,
    input_audio,
    isolated_voice_app,
    save,
    speech,
    word_error,
)

BYTES_PER_SECOND = 32000
FRAME_BYTES = 1600
FRAME_SECONDS = FRAME_BYTES / BYTES_PER_SECOND


class AudioPump:
    """One bounded utterance, otherwise silence, at microphone wall-clock pace."""

    def __init__(self, socket):
        self.socket = socket
        self.enabled = True
        self.frames_sent = 0
        self.late_frames = []
        self.current = None
        self.task = asyncio.create_task(self.run())

    async def play(self, pcm, *, markers=None):
        if not self.enabled or self.current is not None:
            raise RuntimeError(
                "The evaluation microphone is muted or already speaking."
            )
        done = asyncio.get_running_loop().create_future()
        self.current = {
            "pcm": pcm,
            "offset": 0,
            "began": None,
            "done": done,
            "markers": markers or {},
            "times": {},
        }
        return await done

    def mute(self):
        self.enabled = False
        if self.current is not None:
            self.current["done"].cancel()
            self.current = None

    async def run(self):
        due = time.perf_counter()
        try:
            while True:
                await asyncio.sleep(max(0, due - time.perf_counter()))
                now = time.perf_counter()
                if now - due > FRAME_SECONDS:
                    self.late_frames.append(now - due)
                    due = now + FRAME_SECONDS
                else:
                    due += FRAME_SECONDS
                if not self.enabled:
                    continue
                current = self.current
                packet = bytes(FRAME_BYTES)
                if current is not None:
                    if current["began"] is None:
                        current["began"] = now
                    offset = current["offset"]
                    packet = current["pcm"][offset : offset + FRAME_BYTES]
                    for name, boundary in current["markers"].items():
                        if name not in current[
                            "times"
                        ] and offset <= boundary <= offset + len(packet):
                            current["times"][name] = (
                                now + (boundary - offset) / BYTES_PER_SECOND
                            )
                    current["offset"] += len(packet)
                await self.socket.send(packet.ljust(FRAME_BYTES, b"\0"))
                self.frames_sent += 1
                if (
                    current is not None
                    and self.current is current
                    and current["offset"] == len(current["pcm"])
                ):
                    self.current = None
                    if not current["done"].done():
                        current["done"].set_result(
                            {
                                "began": current["began"],
                                "ended": now + len(packet) / BYTES_PER_SECOND,
                                **current["times"],
                            }
                        )
        finally:
            self.mute()

    async def close(self):
        self.mute()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


class DecodeGate:
    """Hold one real ASR result in this evaluator, never in production config."""

    def __init__(self, voice):
        self.voice = voice
        self.original = voice._asr.transcribe
        self.original_listener = voice.new_listener
        self.listener = None
        self.target = None
        self.entered = threading.Event()
        self.released = threading.Event()
        self.released.set()
        self.entered_at = None
        self.released_at = None
        self.expired = False
        voice._asr.transcribe = self.transcribe
        voice.new_listener = self.new_listener

    def new_listener(self):
        self.listener = self.original_listener()
        return self.listener

    def arm(self, target="any"):
        self.entered.clear()
        self.released.clear()
        self.entered_at = self.released_at = None
        self.expired = False
        self.target = target

    def transcribe(self, pcm, *, cancelled):
        job = getattr(self.listener, "_running", None)
        should_hold = self.target == "any" or (
            self.target == "final" and job is not None and job.final
        )
        result = self.original(pcm, cancelled=cancelled)
        if should_hold and self.target is not None:
            self.target = None
            self.entered_at = time.perf_counter()
            self.entered.set()
            self.expired = not self.released.wait(12)
            self.released_at = time.perf_counter()
        return result

    async def wait(self):
        if not await asyncio.to_thread(self.entered.wait, 10):
            raise TimeoutError("No real ASR result reached the evaluator hold point.")

    def release(self):
        self.target = None
        self.released.set()

    def close(self):
        self.release()
        self.voice._asr.transcribe = self.original
        self.voice.new_listener = self.original_listener


class Flow:
    def __init__(self, socket, voice, config):
        self.socket = socket
        self.voice = voice
        self.config = config
        self.connection = SpeechConnection(socket)
        self.pump = AudioPump(socket)

    async def audio(self, segments):
        pieces = []
        for segment in segments:
            if isinstance(segment, (int, float)):
                pieces.append(bytes(round(segment * BYTES_PER_SECOND)))
            else:
                pieces.append(
                    await asyncio.to_thread(
                        input_audio,
                        self.voice,
                        segment,
                        "af_heart",
                        1.0,
                    )
                )
        return b"".join(pieces)

    async def control(self, kind, timeout=2):
        if kind == "pause":
            self.pump.mute()
        self.connection.control_id += 1
        control_id = self.connection.control_id
        start = len(self.connection.events)
        began = time.perf_counter()
        await self.socket.send(json.dumps({"type": kind, "control_id": control_id}))
        event = await self.connection.until(
            lambda e: e.get("control_id") == control_id,
            start,
            timeout,
        )
        if kind == "unpause" and event["state"] != "paused":
            self.pump.enabled = True
        return {"control": kind, "ack_s": event["at"] - began, "event": event}

    async def playback(self, active):
        await self.socket.send(json.dumps({"type": "playback", "active": active}))

    async def activate(self):
        await self.connection.until(lambda e: e["type"] == "state")
        start = len(self.connection.events)
        pcm = await self.audio([0.25, self.config.voice_wake_phrase, 2.0])
        await self.pump.play(pcm)
        await self.connection.until(
            lambda e: e["type"] == "state" and e["state"] == "listening",
            start,
        )
        events = self.connection.events[start:]
        return {
            "events": events,
            "unexpected_transcripts": [
                e["text"] for e in events if e["type"] == "transcript"
            ],
        }

    async def capture(self, segments):
        pcm = await self.audio(segments)
        start = len(self.connection.events)
        playback = await self.pump.play(
            bytes(8000)
            + pcm
            + bytes(round((self.config.voice_end_s + 0.8) * BYTES_PER_SECOND)),
            markers={"speech_start": 8000, "speech_end": 8000 + len(pcm)},
        )
        await self.connection.until(
            lambda e: e["type"] in {"transcript", "limit", "error"},
            start,
        )
        events = self.connection.events[start:]
        return capture_result(
            events, playback["speech_start"], len(pcm), input_end=playback["speech_end"]
        )

    async def close(self):
        await self.pump.close()
        await self.connection.close()


def capture_result(events, began, pcm_bytes, *, input_end=None):
    finals = [e for e in events if e["type"] == "transcript"]
    partials = [e for e in events if e["type"] == "partial" and e["text"].strip()]
    first_final = finals[0]["at"] if finals else None
    starts = [e["at"] for e in events if e["type"] == "speech_start"]
    input_end = (
        input_end if input_end is not None else began + pcm_bytes / BYTES_PER_SECOND
    )
    return {
        "transcripts": [e["text"] for e in finals],
        "events": events,
        "input_audio_s": pcm_bytes / BYTES_PER_SECOND,
        "first_partial_s": partials[0]["at"] - began if partials else None,
        "partial_before_final": bool(
            partials and first_final is not None and partials[0]["at"] < first_final
        ),
        "partial_while_speaking": bool(partials and partials[0]["at"] < input_end),
        "final_after_input_s": first_final - input_end
        if first_final is not None
        else None,
        "speech_start_s": [at - began for at in starts],
        "errors": [e for e in events if e["type"] == "error"],
        "waiting_events": [
            e for e in events if e["type"] == "state" and e["state"] == "waiting"
        ],
    }


async def complete_turn(client, session_id, text, flow):
    before = flow.pump.frames_sent
    result = await chat(client, session_id, text)
    result["pcm_frames_during_chat"] = flow.pump.frames_sent - before
    before = flow.pump.frames_sent
    audio = None
    if result.get("committed"):
        await flow.playback(True)
        try:
            audio = await speech(client, result["turn_id"])
        finally:
            await flow.playback(False)
    result["pcm_frames_during_synthesis"] = flow.pump.frames_sent - before
    return {
        "chat": result,
        "speech": audio,
        "spoken_text": spoken_text(result.get("reply", "")),
    }


def turn_issues(row):
    generation = row.get("chat", {})
    audio = row.get("speech") or {}
    verification = generation.get("verification", {})
    issues = []
    if not generation.get("committed") or generation.get("generation", {}).get("error"):
        issues.append("The chat did not complete as a committed successful turn.")
    if not all(
        verification.get(key)
        for key in ("payload_identical", "report_fields_identical")
    ):
        issues.append("The turn did not report both exact verification checks.")
    if not audio.get("completed") or audio.get("errors") or not audio.get("audio_s"):
        issues.append("The speech stream did not complete with nonempty audio.")
    return issues


def conversation_cases():
    groups = conversations()
    source = {case["id"]: case for cases in groups.values() for case in cases}
    selected = [
        "two_values",
        "clarify",
        "confirm",
        "time_explicit",
        "time_correction",
        "time_corrected_recall",
        "money_precision",
        "quote_full_contrast",
        "short_decision_prompt",
        "short_decision",
    ]
    cases = [{"id": key, "segments": [source[key]["text"]]} for key in selected]
    cases.insert(
        3,
        {
            "id": "ongoing_beyond_eight_seconds",
            "segments": [
                "For the dinner party please remember that the blue folder "
                "contains the shopping list and the green notebook has the "
                "guest names and the red envelope contains the receipts. "
                "Repeat only the three colors."
            ],
            "minimum_audio_s": 8,
            "requires_partial": True,
        },
    )
    cases.insert(
        4,
        {
            "id": "pause_inside_request",
            "segments": [
                "Remember the blue folder",
                0.8,
                "and put it on the top shelf.",
            ],
            "requires_partial": True,
        },
    )
    return cases


def nonspeech_pcm(kind, seconds=3):
    count = round(seconds * 16000)
    rng = np.random.default_rng(20261000)
    samples = np.zeros(count)
    if kind == "hiss":
        samples = rng.normal(0, 0.03, count)
    elif kind == "clicks":
        for offset in range(0, count, round(0.23 * 16000)):
            length = min(32, count - offset)
            samples[offset : offset + length] = (
                0.7 * np.hanning(length) * rng.choice([-1, 1])
            )
    elif kind != "silence":
        raise ValueError(f"Unknown nonspeech fixture: {kind}")
    return (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()


async def nonspeech_probe(flow, *, kind, playback):
    row = {
        "id": f"nonspeech_{kind}_{playback}",
        "kind": kind,
        "playback_active": playback,
        "input_audio_s": 3,
        "issues": [],
    }
    start = len(flow.connection.events)
    await flow.playback(playback)
    try:
        await flow.pump.play(
            nonspeech_pcm(kind)
            + bytes(round((flow.config.voice_end_s + 0.8) * BYTES_PER_SECOND))
        )
        events = flow.connection.events[start:]
        row["events"] = events
        if any(
            e["type"] in {"speech_start", "partial", "transcript", "limit"}
            for e in events
        ):
            row["issues"].append("Nonspeech reached the speech event pipeline.")
        if any(e["type"] == "error" for e in events):
            row["issues"].append("The listener reported an error during nonspeech.")
    finally:
        await flow.playback(False)
    row["status"] = "fail" if row["issues"] else "pass"
    return row


async def control_probe(flow, gate, *, playback):
    row = {
        "id": "mute_held_asr_with_playback" if playback else "mute_held_asr",
        "artificial_result_hold": True,
        "issues": [],
    }
    if gate is None:
        return {**row, "status": "not_applicable", "reason": "Whisper-only probe."}
    old = await flow.audio([0.25, "Discard the orange lantern and the purple basket."])
    new = await flow.audio([0.25, "Remember the blue folder.", 2.2])
    start = len(flow.connection.events)
    await flow.playback(playback)
    gate.arm()
    new_audio = None
    try:
        await flow.pump.play(old)
        await gate.wait()
        row["pause"] = await flow.control("pause")
        row["unpause"] = await flow.control("unpause")
        after_unpause = len(flow.connection.events)
        began = time.perf_counter()
        new_audio = asyncio.create_task(flow.pump.play(new))
        onset = await flow.connection.until(
            lambda e: e["type"] == "speech_start",
            after_unpause,
            2,
        )
        row["speech_start_while_held_s"] = onset["at"] - began
        row["control_finished_before_release"] = not gate.released.is_set()
        if not row["control_finished_before_release"]:
            row["issues"].append("Controls waited for the held ASR result.")
        gate.release()
        await new_audio
        final = await flow.connection.until(
            lambda e: e["type"] == "transcript",
            after_unpause,
        )
        row["transcript"] = final["text"]
        row["held_from"] = gate.entered_at
        row["held_until"] = gate.released_at
        if max(row["pause"]["ack_s"], row["unpause"]["ack_s"]) > 1:
            row["issues"].append("A microphone acknowledgment exceeded one second.")
        after_pause = [
            e
            for e in flow.connection.events[start:]
            if e["at"] > row["pause"]["event"]["at"]
        ]
        if any(
            "orange" in e.get("text", "").lower()
            or "purple" in e.get("text", "").lower()
            for e in after_pause
        ):
            row["issues"].append(
                "Discarded speech appeared after the mute acknowledgment."
            )
        if len([e for e in after_pause if e["type"] == "transcript"]) != 1:
            row["issues"].append(
                "Expected only the fresh utterance's final transcript."
            )
        if "blue" not in final["text"].lower() or gate.expired:
            row["issues"].append(
                "The held-result recovery did not preserve fresh speech."
            )
    finally:
        gate.release()
        if new_audio is not None and not new_audio.done():
            flow.pump.mute()
            await asyncio.gather(new_audio, return_exceptions=True)
        await flow.playback(False)
        row["events"] = flow.connection.events[start:]
    row["status"] = "fail" if row["issues"] else "pass"
    return row


async def continued_final_probe(flow, gate, client, session_id):
    row = {
        "id": "speech_resumes_before_final",
        "artificial_result_hold": True,
        "issues": [],
    }
    if gate is None:
        return {**row, "status": "not_applicable", "reason": "Whisper-only probe."}
    first = await flow.audio([0.25, "Remember the silver key.", 2.2])
    second = await flow.audio([0.25, "And keep it beside the blue folder.", 2.2])
    start = len(flow.connection.events)
    pending = None
    gate.arm("final")
    try:
        await flow.pump.play(first)
        await gate.wait()
        if any(e["type"] == "transcript" for e in flow.connection.events[start:]):
            row["issues"].append("The first final escaped before continued speech.")
        new_start = len(flow.connection.events)
        began = time.perf_counter()
        pending = asyncio.create_task(flow.pump.play(second))
        event = await flow.connection.until(
            lambda e: e["type"] == "speech_start",
            new_start,
            2,
        )
        row["speech_start_while_final_held_s"] = event["at"] - began
        gate.release()
        await pending
        final = await flow.connection.until(
            lambda e: e["type"] == "transcript",
            new_start,
        )
        finals = [
            e for e in flow.connection.events[start:] if e["type"] == "transcript"
        ]
        if len(finals) != 1 or not all(
            term in final["text"].lower() for term in ("silver", "blue")
        ):
            row["issues"].append(
                "Continued speech was split, lost, or emitted stale text."
            )
        row["transcript"] = final["text"]
        row.update(await complete_turn(client, session_id, final["text"], flow))
        row["issues"].extend(turn_issues(row))
    finally:
        gate.release()
        if pending is not None and not pending.done():
            flow.pump.mute()
            await asyncio.gather(pending, return_exceptions=True)
        row["events"] = flow.connection.events[start:]
    row["status"] = "fail" if row["issues"] else "pass"
    return row


async def limit_probe(flow, client, session_id, *, send):
    row = {"id": "limit_send" if send else "limit_discard", "issues": []}
    piece = await flow.audio(["The silver key belongs beside the blue folder.", 0.15])
    repeats = (
        int((flow.config.voice_max_utterance_s + 3) * BYTES_PER_SECOND / len(piece)) + 1
    )
    start = len(flow.connection.events)
    before = len((await client.get(f"/api/sessions/{session_id}/turns")).json())
    pending = asyncio.create_task(flow.pump.play(bytes(8000) + piece * repeats))
    try:
        await flow.connection.until(
            lambda e: e["type"] == "state" and e["state"] == "paused",
            start,
            flow.config.voice_max_utterance_s + 20,
        )
        flow.pump.mute()
        event = await flow.connection.until(lambda e: e["type"] == "limit", start)
        row["captured_text"] = event["text"]
        row["limit_s"] = event["limit_s"]
        if any(e["type"] == "transcript" for e in flow.connection.events[start:]):
            row["issues"].append(
                "The hard limit automatically produced a chat transcript."
            )
        while_muted = len(
            (await client.get(f"/api/sessions/{session_id}/turns")).json()
        )
        if while_muted != before:
            row["issues"].append(
                "The captured request was stored before an explicit action."
            )
        row["unpause"] = await flow.control("unpause")
        if send and event["text"].strip():
            row.update(await complete_turn(client, session_id, event["text"], flow))
            row["issues"].extend(turn_issues(row))
        after = len((await client.get(f"/api/sessions/{session_id}/turns")).json())
        row["stored_before"], row["stored_after"] = before, after
        if after - before != int(send):
            row["issues"].append(
                "Explicit send/discard did not match stored-turn accounting."
            )
    finally:
        if not pending.done():
            flow.pump.mute()
        await asyncio.gather(pending, return_exceptions=True)
        row["events"] = flow.connection.events[start:]
    row["status"] = "fail" if row["issues"] else "pass"
    return row


async def record_probe(report, name, action, flow):
    row = {"id": name, "status": "running"}
    report["probes"].append(row)
    start = len(flow.connection.events)
    try:
        row.update(await action)
    except Exception as error:
        row.update(
            status="error",
            error=f"{type(error).__name__}: {error}",
            events=flow.connection.events[start:],
        )
        raise


async def evaluate(args, config):
    config = replace(
        config,
        voice_asr_backend=args.backend,
        voice_max_utterance_s=args.max_utterance_s,
    )
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "method": "Real-time paced synthetic PCM over HTTP/WebSocket with an "
        "independent event collector and continuous silence during chat/synthesis. "
        "Real configured ASR, verified retrieval, local chat, and Kokoro. "
        "Marked probes hold one real ASR result in the evaluator. Playback controls "
        "simulate browser state; no speaker, microphone, AEC, or perception score.",
        "settings": {
            "asr_backend": args.backend,
            "model": config.generator_model,
            "max_utterance_s": config.voice_max_utterance_s,
            "end_s": config.voice_end_s,
            "wait_s": config.voice_wait_s,
            "frame_s": FRAME_SECONDS,
        },
        "planned_conversation_turns": len(conversation_cases()),
        "turns": [],
        "probes": [],
    }
    try:
        async with isolated_voice_app(config, report) as (voice, client, port):
            gate = DecodeGate(voice) if args.backend == "whisper" else None
            session_id = (
                await client.post(
                    "/api/sessions",
                    json={"title": f"Paced voice flow: {args.backend}"},
                )
            ).json()["session_id"]
            async with connect(f"ws://127.0.0.1:{port}/api/voice/listen") as ws:
                flow = Flow(ws, voice, config)
                try:
                    report["activation"] = await flow.activate()
                    for case in conversation_cases():
                        row = {**case, "issues": []}
                        report["turns"].append(row)
                        row["asr"] = await flow.capture(case["segments"])
                        texts = row["asr"]["transcripts"]
                        if len(texts) != 1:
                            raise RuntimeError(
                                "Expected one final transcript per utterance."
                            )
                        if (
                            case.get("requires_partial")
                            and not row["asr"]["partial_while_speaking"]
                        ):
                            row["issues"].append(
                                "No live partial appeared while input speech continued."
                            )
                        if row["asr"]["final_after_input_s"] < 0:
                            row["issues"].append(
                                "A final transcript arrived before the input ended."
                            )
                        if row["asr"]["waiting_events"]:
                            row["issues"].append(
                                "A follow-up unexpectedly required another wake."
                            )
                        if row["asr"]["input_audio_s"] <= case.get(
                            "minimum_audio_s", 0
                        ):
                            row["issues"].append(
                                "The generated fixture missed its duration requirement."
                            )
                        intended = " ".join(
                            s for s in case["segments"] if isinstance(s, str)
                        )
                        row["asr"]["word_error_rate"] = word_error(intended, texts[0])
                        row.update(
                            await complete_turn(client, session_id, texts[0], flow)
                        )
                        row["issues"].extend(turn_issues(row))
                        row["status"] = "fail" if row["issues"] else "pass"
                        print(
                            json.dumps(
                                {
                                    "id": row["id"],
                                    "status": row["status"],
                                    "heard": texts,
                                }
                            ),
                            flush=True,
                        )
                        save(report, args.report)
                    for playback in (False, True):
                        for kind in ("silence", "hiss", "clicks"):
                            await record_probe(
                                report,
                                f"nonspeech_{kind}_{playback}",
                                nonspeech_probe(flow, kind=kind, playback=playback),
                                flow,
                            )
                            save(report, args.report)
                    for playback in (False, True):
                        await record_probe(
                            report,
                            f"mute_held_{playback}",
                            control_probe(flow, gate, playback=playback),
                            flow,
                        )
                        save(report, args.report)
                    await record_probe(
                        report,
                        "speech_resumes_before_final",
                        continued_final_probe(flow, gate, client, session_id),
                        flow,
                    )
                    save(report, args.report)
                    if not args.skip_limits:
                        for send in (False, True):
                            await record_probe(
                                report,
                                f"limit_send_{send}",
                                limit_probe(flow, client, session_id, send=send),
                                flow,
                            )
                            save(report, args.report)
                    report["stored_turn_count"] = len(
                        (await client.get(f"/api/sessions/{session_id}/turns")).json()
                    )
                    report["pcm_frames_sent"] = flow.pump.frames_sent
                    report["late_frame_delays_s"] = flow.pump.late_frames
                finally:
                    if gate:
                        gate.close()
                    await flow.close()
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        save(report, args.report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--backend", choices=("vosk", "whisper"), default="whisper")
    parser.add_argument("--max-utterance-s", type=float, default=20)
    parser.add_argument("--skip-limits", action="store_true")
    args = parser.parse_args()
    if args.report.exists():
        parser.error("Choose a new report path to preserve earlier results.")
    asyncio.run(evaluate(args, RecollectConfig.from_env()))
