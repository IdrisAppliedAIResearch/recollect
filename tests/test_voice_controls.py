"""Muted audio and interrupted native speech cannot leak into a later turn."""

from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

from recollect.engine.voice import VoiceListener, VoiceService, _speech_pieces
from recollect.voice_api import _speech_stream
from tests.test_voice import (
    Recognizer,
    Recognizers,
    SpeechDetector,
    active_listener,
    audio,
    configuration,
    frames,
    transcripts,
)
from tests.test_voice_api import voice_app as voice_app


def test_pause_discards_inflight_words_and_unpause_needs_no_wake():
    stale = Recognizer([(False, {"partial": "discard these words"})])
    fresh = Recognizer(final_text="only the follow up")
    listener, _, vad = active_listener(stale, fresh)
    frames(listener, 8)
    listener.pause()
    listener.pause()
    count = len(vad.audio)
    assert frames(listener, 100) == []
    assert len(vad.audio) == count
    assert listener.unpause()["state"] == "listening"
    assert listener.unpause()["state"] == "listening"
    frames(listener, 8)
    assert transcripts(frames(listener, 44, value=0)) == ["only the follow up"]
    assert len(stale.audio) == 2


def test_pause_before_activation_restores_waiting():
    listener = VoiceListener(configuration(), Recognizers(), SpeechDetector())
    listener.pause()
    listener.pause()
    assert listener.unpause()["state"] == "waiting"
    assert frames(listener, 50) == []


def test_unpause_keeps_playback_interruption_threshold():
    listener, _, _ = active_listener()
    listener.set_playback(True)
    listener.pause()
    assert listener.unpause()["state"] == "listening"
    assert listener.playback is True
    assert frames(listener, 30, value=700) == []
    assert {"type": "speech_start"} in frames(listener, 8, value=950)


def test_limit_keeps_all_segments_and_discards_remaining_packet_audio():
    command = Recognizer([(True, {"text": "remember the first part"})],
                         final_text="and the last part")
    listener, _, vad = active_listener(
        command, voice_wait_s=1, voice_max_utterance_s=2,
    )
    frames(listener, 60)
    events = listener.feed(audio(1))
    assert [event for event in events if event["type"] == "limit"] == [{
        "type": "limit", "text": "remember the first part and the last part",
        "limit_s": 2,
    }]
    assert transcripts(events) == []
    before = len(vad.audio)
    assert listener.feed(audio(1)) == []
    assert len(vad.audio) == before
    assert listener.unpause()["state"] == "listening"


def test_natural_endpoint_at_limit_still_submits_normally():
    listener, _, _ = active_listener(
        Recognizer(final_text="a complete request"), voice_wait_s=1,
        voice_max_utterance_s=2.048, voice_end_s=1.536,
    )
    frames(listener, 16)
    events = frames(listener, 48, value=0)
    assert transcripts(events) == ["a complete request"]
    assert not any(event["type"] == "limit" for event in events)
    assert listener.state == "listening"


def test_websocket_acknowledges_controls_without_reactivation(voice_app):
    client, state = voice_app
    listener, _, _ = active_listener()
    state.voice.new_listener = lambda: listener
    with client.websocket_connect("/api/voice/listen") as socket:
        assert socket.receive_json()["state"] == "listening"
        for control, control_id, expected in (
            ("pause", 1, "paused"), ("pause", 2, "paused"),
            ("unpause", 3, "listening"),
        ):
            socket.send_json({"type": control, "control_id": control_id})
            assert socket.receive_json() == {
                "type": "state", "state": expected, "wake_phrase": "hey idris",
                "control_id": control_id,
            }
        socket.send_json({"type": "pause"})
        assert "control_id" not in socket.receive_json()
        socket.send_json({"type": "unpause"})
        assert socket.receive_json()["state"] == "listening"
    assert not state.voice.listener_claim.locked()


@pytest.mark.parametrize("control_id", [None, False, 0, -1, 1.5, "1", 2**53])
def test_invalid_control_id_is_rejected_before_listener_mutation(
    voice_app, control_id,
):
    client, state = voice_app
    with client.websocket_connect("/api/voice/listen") as socket:
        socket.receive_json()
        socket.send_json({"type": "pause", "control_id": control_id})
        assert socket.receive_json()["type"] == "error"
        assert socket.receive()["type"] == "websocket.close"
    assert state.voice.listeners[0].state == "waiting"
    assert not state.voice.listener_claim.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("successful_chunks", [0, 1])
async def test_native_failure_finishes_stream_and_allows_replay(successful_chunks):
    class NativeError(Exception):
        pass

    calls = 0

    def create(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == successful_chunks + 1:
            raise NativeError("native diagnostic should not escape to speech")
        return np.zeros(240, dtype=np.float32), 24000

    service = VoiceService(configuration())
    service._model = object()
    service._kokoro = SimpleNamespace(create=create)
    text = "A sentence with enough words for multiple chunks. " * 8
    records = [json.loads(record) async for record in _speech_stream(service, text)]
    assert [record["type"] for record in records] == [
        *(["audio"] * successful_chunks), "error",
    ]
    assert records[-1]["message"] == "Kokoro could not speak this reply. Try replaying."
    assert not service._speech_lock.locked()
    replay = [json.loads(record) async for record in _speech_stream(service, "Again.")]
    assert [record["type"] for record in replay] == ["audio", "done"]


@pytest.mark.asyncio
async def test_native_warmup_failure_returns_actionable_stream_error():
    service = VoiceService(configuration())

    def broken_warmup():
        raise OSError("native initialization failed")

    service.warm_up = broken_warmup
    records = [json.loads(record) async for record in _speech_stream(service, "Hello.")]
    assert records == [{
        "type": "error", "message": "Kokoro could not prepare audio. Try replaying.",
    }]
    assert not service._speech_lock.locked()


def test_first_audio_chunk_can_be_a_short_complete_sentence():
    text = "Yes. " + "Here is the explanation with more details and useful context " * 5
    pieces = _speech_pieces(text)
    assert pieces[0] == "Yes."
    assert " ".join(pieces) == text.strip()
    assert max(map(len, pieces[1:])) <= 240
