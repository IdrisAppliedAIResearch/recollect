"""Model-free checks for the opt-in flow recorder's timing and control logic."""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from .voice_live_evaluation import SpeechConnection, isolated_voice_app
from .voice_whisper_flow import (
    AudioPump,
    DecodeGate,
    capture_result,
    nonspeech_pcm,
    turn_issues,
)


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self.incoming.get()
        if event is None:
            raise StopAsyncIteration
        return json.dumps(event)

    async def send(self, packet):
        self.sent.append(packet)


def test_nonspeech_fixtures_are_bounded_repeatable_pcm():
    assert nonspeech_pcm("silence") == bytes(96000)
    for kind in ("hiss", "clicks"):
        pcm = nonspeech_pcm(kind)
        assert len(pcm) == 96000
        assert pcm != bytes(96000)
        assert pcm == nonspeech_pcm(kind)
    with pytest.raises(ValueError, match="Unknown"):
        nonspeech_pcm("unrecognized")


def test_collector_waits_for_async_final_without_resetting_the_listener():
    async def scenario():
        socket = Socket()
        connection = SpeechConnection(socket)
        pending = asyncio.create_task(connection.utterance(bytes(1600)))
        while not socket.sent:
            await asyncio.sleep(0)
        await socket.incoming.put({"type": "partial", "text": "four"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not pending.done()
        await socket.incoming.put({"type": "state", "state": "listening"})
        await asyncio.sleep(0)
        assert not pending.done()
        await socket.incoming.put({"type": "transcript", "text": "four thirty"})
        result = await asyncio.wait_for(pending, 1)
        assert result["transcripts"] == ["four thirty"]
        assert result["partial_count"] == 1
        assert all(isinstance(packet, bytes) for packet in socket.sent)
        await connection.close()

    asyncio.run(scenario())


def test_closed_collector_wakes_waiter_without_waiting_for_decode_timeout():
    async def scenario():
        socket = Socket()
        connection = SpeechConnection(socket)
        pending = asyncio.create_task(connection.until(lambda e: False))
        await socket.incoming.put(None)
        with pytest.raises(RuntimeError, match="closed"):
            await asyncio.wait_for(pending, 1)
        await connection.close()

    asyncio.run(scenario())


def test_partial_after_speech_is_not_reported_as_live_transcription():
    events = [
        {"type": "speech_start", "at": 1.3},
        {"type": "partial", "text": "late words", "at": 11.2},
        {"type": "transcript", "text": "late words", "at": 12.5},
    ]
    result = capture_result(events, 1, 320000, input_end=11)
    assert result["partial_before_final"] is True
    assert result["partial_while_speaking"] is False
    assert result["final_after_input_s"] == 1.5
    events[1]["at"] = 5
    assert capture_result(events, 1, 320000)["partial_while_speaking"] is True


def test_audio_pump_keeps_silence_flowing_and_discards_muted_utterance_tail():
    async def scenario():
        socket = Socket()
        pump = AudioPump(socket)
        first = asyncio.create_task(pump.play(bytes([1, 0]) * 4000))
        while not socket.sent:
            await asyncio.sleep(0)
        pump.mute()
        await asyncio.gather(first, return_exceptions=True)
        count = len(socket.sent)
        await asyncio.sleep(0.07)
        assert len(socket.sent) == count
        pump.enabled = True
        await pump.play(bytes([2, 0]) * 1600)
        await asyncio.sleep(0.07)
        assert all(
            packet in (bytes([2, 0]) * 800, bytes(1600))
            for packet in socket.sent[count:]
        )
        assert socket.sent[-1] == bytes(1600)
        await pump.close()

    asyncio.run(scenario())


def test_real_result_gate_holds_final_only_and_restores_the_decoder():
    async def scenario():
        calls = []

        def transcribe(pcm, *, cancelled):
            calls.append(pcm)
            return "recognized words"

        listener = SimpleNamespace(_running=SimpleNamespace(final=False))
        voice = SimpleNamespace(
            _asr=SimpleNamespace(transcribe=transcribe), new_listener=lambda: listener
        )
        gate = DecodeGate(voice)
        voice.new_listener()
        gate.arm("final")
        assert (
            voice._asr.transcribe(b"draft", cancelled=threading.Event())
            == "recognized words"
        )
        assert not gate.entered.is_set()
        listener._running.final = True
        pending = asyncio.create_task(
            asyncio.to_thread(
                voice._asr.transcribe,
                b"final",
                cancelled=threading.Event(),
            )
        )
        await gate.wait()
        assert not pending.done()
        gate.release()
        assert await pending == "recognized words"
        assert calls == [b"draft", b"final"]
        gate.close()
        assert voice._asr.transcribe is transcribe

    asyncio.run(scenario())


def test_turn_success_requires_commit_exact_verification_and_complete_audio():
    result = {
        "chat": {
            "committed": True,
            "generation": {"error": None},
            "verification": {
                "payload_identical": True,
                "report_fields_identical": True,
            },
        },
        "speech": {"completed": True, "errors": [], "audio_s": 1},
    }
    assert turn_issues(result) == []
    result["chat"]["verification"]["report_fields_identical"] = False
    result["chat"]["committed"] = False
    result["speech"]["completed"] = False
    assert len(turn_issues(result)) == 3


def test_isolation_cleanup_is_reported_even_when_application_setup_fails(
    tmp_path, monkeypatch
):
    from recollect.config import RecollectConfig

    from . import voice_live_evaluation

    paths = []

    def fail(config):
        paths.extend((config.data_dir, config.sandbox_root))
        raise RuntimeError("deliberate startup failure")

    monkeypatch.setattr(voice_live_evaluation, "create_app", fail)
    report = {}

    async def scenario():
        with pytest.raises(RuntimeError, match="deliberate"):
            async with isolated_voice_app(
                RecollectConfig(
                    embedding_model_path=tmp_path / "unused.gguf",
                    sandbox_root=tmp_path / "sandbox",
                ),
                report,
            ):
                raise AssertionError("The failed app must not yield a runtime.")

    asyncio.run(scenario())
    assert report["temporary_data_removed"] is True
    assert report["finished_at"]
    assert all(not path.exists() for path in paths)
