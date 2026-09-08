"""Speech transport never bypasses completed, verified chat replies."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request, WebSocketDisconnect
from fastapi.testclient import TestClient

from recollect.config import RecollectConfig
from recollect.engine.voice import (
    MAX_FRAME_BYTES,
    VoiceCancelled,
    VoiceListener,
    VoiceUnavailable,
)
from recollect.trace import GenerationTrace, VerificationTrace
from recollect.voice_api import (
    _allowed_origin,
    _speech_stream,
    _SpeechResponse,
    install_voice_routes,
)


class SilentRecognizer:
    def AcceptWaveform(self, pcm):
        return False

    def PartialResult(self):
        return '{"partial": ""}'


class FakeVoice:
    def __init__(self):
        self.listener_claim = threading.Lock()
        self.listeners = []
        self.spoken = []
        self.speech_error = None
        self.listener_error = None
        self.worker_threads = []

    def status(self):
        self.worker_threads.append(threading.get_ident())
        return {
            "available": True, "wake_phrase": "hey idris", "sample_rate": 16_000,
        }

    def synthesize(self, text, *, cancelled=None):
        self.worker_threads.append(threading.get_ident())
        self.spoken.append(text)
        if self.speech_error:
            raise self.speech_error
        return b"RIFFcompleted reply"

    def synthesize_chunks(self, text, *, cancelled=None):
        yield self.synthesize(text, cancelled=cancelled)

    def new_listener(self):
        self.worker_threads.append(threading.get_ident())
        if self.listener_error:
            raise self.listener_error
        config = RecollectConfig(embedding_model_path=Path("unused.gguf"))
        listener = VoiceListener(config, lambda wake: SilentRecognizer(), lambda pcm: 0)
        self.listeners.append(listener)
        return listener


class FakeSessions:
    def __init__(self):
        self.traces = {}
        self.lookups = []
        self.worker_threads = []

    def find_trace(self, turn_id):
        self.worker_threads.append(threading.get_ident())
        self.lookups.append(turn_id)
        return self.traces.get(turn_id)


def completed_trace(*, text="A persisted reply.", error=None,
                    payload_identical=True, report_fields_identical=True):
    return SimpleNamespace(
        verification=VerificationTrace(
            payload_identical=payload_identical,
            report_fields_identical=report_fields_identical,
            authority_payload_sha256="authority",
            shadow_payload_sha256="shadow",
            library_version="test",
            shadow_latency_ms=0,
        ),
        generation=GenerationTrace(
            model="local", base_url="http://127.0.0.1:8000/v1",
            system_prompt_chars=0, context_block_chars=0, total_prompt_chars=0,
            response_text=text, reasoning_text="Private reasoning, never spoken.",
            error=error,
        ),
    )


@pytest.fixture
def voice_app():
    app = FastAPI()
    state = SimpleNamespace(voice=FakeVoice(), sessions=FakeSessions())
    install_voice_routes(app, lambda: state)

    @app.get("/api/text-alive")
    async def text_alive():
        return {"available": True, "thread": threading.get_ident()}

    with TestClient(app) as client:
        yield client, state


def test_status_does_not_load_models_or_claim_microphone(voice_app):
    client, state = voice_app

    response = client.get("/api/voice/status")

    assert response.status_code == 200
    assert response.json()["wake_phrase"] == "hey idris"
    assert state.voice.listeners == []
    assert not state.voice.listener_claim.locked()
    loop_thread = client.get("/api/text-alive").json()["thread"]
    assert state.voice.worker_threads[0] != loop_thread


def test_speech_uses_only_the_persisted_completed_response(voice_app):
    client, state = voice_app
    state.sessions.traces["turn-1"] = completed_trace()

    response = client.post("/api/voice/speech", json={
        "turn_id": "turn-1", "text": "Injected speech", "response_text": "Untrusted",
    })

    assert response.status_code == 200
    assert response.content == b"RIFFcompleted reply"
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["cache-control"] == "no-store"
    assert state.sessions.lookups == ["turn-1"]
    assert state.voice.spoken == ["A persisted reply."]
    loop_thread = client.get("/api/text-alive").json()["thread"]
    assert loop_thread not in state.sessions.worker_threads + state.voice.worker_threads


def test_arbitrary_text_cannot_be_submitted_directly_to_speech(voice_app):
    client, state = voice_app

    response = client.post("/api/voice/speech", json={"text": "Say this"})

    assert response.status_code == 422
    assert state.voice.spoken == []


def test_missing_turn_is_not_spoken(voice_app):
    client, state = voice_app

    response = client.post("/api/voice/speech", json={"turn_id": "missing"})

    assert response.status_code == 404
    assert state.voice.spoken == []


@pytest.mark.parametrize("changes", [
    {"payload_identical": False},
    {"report_fields_identical": False},
    {"error": "Generation interrupted"},
    {"text": " \n "},
])
def test_unverified_failed_or_empty_reply_is_not_spoken(voice_app, changes):
    client, state = voice_app
    state.sessions.traces["turn-1"] = completed_trace(**changes)

    response = client.post("/api/voice/speech", json={"turn_id": "turn-1"})

    assert response.status_code == 409
    assert state.voice.spoken == []


def test_incomplete_generation_is_not_spoken(voice_app):
    client, state = voice_app
    trace = completed_trace()
    trace.generation = None
    state.sessions.traces["turn-1"] = trace

    response = client.post("/api/voice/speech", json={"turn_id": "turn-1"})

    assert response.status_code == 409
    assert state.voice.spoken == []


@pytest.mark.parametrize("error", [
    VoiceUnavailable("Models missing"), ValueError("Invalid audio"),
    RuntimeError("Speech runtime failed"),
])
def test_speech_failure_keeps_text_routes_available(voice_app, error):
    client, state = voice_app
    state.sessions.traces["turn-1"] = completed_trace()
    state.voice.speech_error = error

    response = client.post("/api/voice/speech", json={"turn_id": "turn-1"})

    assert response.status_code == 503
    assert response.json()["detail"] == str(error)
    assert client.get("/api/text-alive").json()["available"] is True


def test_speech_disconnect_cancels_worker_and_next_reply_remains_available(
    voice_app, monkeypatch,
):
    client, state = voice_app
    state.sessions.traces["turn-1"] = completed_trace()
    started = threading.Event()
    cancellations = []
    original_synthesis = state.voice.synthesize

    def interrupted_synthesis(text, *, cancelled=None):
        assert isinstance(cancelled, threading.Event)
        cancellations.append(cancelled)
        started.set()
        assert cancelled.wait(2), "HTTP disconnect must reach the speech worker"
        raise VoiceCancelled("Speech was interrupted.")

    async def disconnected(request):
        return started.is_set()

    with monkeypatch.context() as patched:
        patched.setattr(Request, "is_disconnected", disconnected)
        patched.setattr(state.voice, "synthesize", interrupted_synthesis)
        response = client.post("/api/voice/speech", json={"turn_id": "turn-1"})

    assert response.status_code == 499
    assert response.json()["detail"] == "Speech was interrupted."
    assert len(cancellations) == 1
    assert cancellations[0].is_set()
    assert state.voice.synthesize == original_synthesis
    assert client.get("/api/text-alive").json()["available"] is True
    fresh = client.post("/api/voice/speech", json={"turn_id": "turn-1"})
    assert fresh.status_code == 200
    assert fresh.content == b"RIFFcompleted reply"


def test_streamed_speech_uses_only_verified_response_and_explicit_completion(voice_app):
    client, state = voice_app
    state.sessions.traces["turn-1"] = completed_trace()
    response = client.post("/api/voice/speech", json={
        "turn_id": "turn-1", "stream": True, "text": "Never speak injected text.",
    })
    events = [json.loads(line) for line in response.iter_lines()]
    assert response.headers["content-type"] == "application/x-ndjson"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert events == [
        {"type": "audio", "wav": base64.b64encode(b"RIFFcompleted reply").decode()},
        {"type": "done"},
    ]
    assert state.voice.spoken == ["A persisted reply."]
    loop_thread = client.get("/api/text-alive").json()["thread"]
    assert loop_thread not in state.voice.worker_threads


@pytest.mark.parametrize("changes", [
    {"payload_identical": False}, {"report_fields_identical": False},
    {"error": "Generation failed"}, {"text": " "},
])
def test_stream_does_not_bypass_reply_verification(voice_app, changes):
    client, state = voice_app
    state.sessions.traces["turn-1"] = completed_trace(**changes)
    response = client.post("/api/voice/speech", json={
        "turn_id": "turn-1", "stream": True,
    })
    assert response.status_code == 409
    assert state.voice.spoken == []


def test_stream_reports_mid_reply_failure_without_a_false_done(voice_app):
    client, state = voice_app
    state.sessions.traces["turn-1"] = completed_trace()

    def chunks(text, *, cancelled):
        yield b"RIFFfirst part"
        raise VoiceUnavailable("The speech engine stopped.")

    state.voice.synthesize_chunks = chunks
    response = client.post("/api/voice/speech", json={
        "turn_id": "turn-1", "stream": True,
    })
    events = [json.loads(line) for line in response.iter_lines()]
    assert events[0]["type"] == "audio"
    assert events[1] == {"type": "error", "message": "The speech engine stopped."}
    assert len(events) == 2
    assert client.get("/api/text-alive").status_code == 200


async def test_stream_synthesizes_only_when_consumer_requests_another_chunk():
    synthesized = []
    closed = threading.Event()
    cancellations = []
    loop_thread = threading.get_ident()

    def chunks(text, *, cancelled):
        cancellations.append(cancelled)
        try:
            for index in range(3):
                assert threading.get_ident() != loop_thread
                synthesized.append(index)
                yield b"RIFFchunk"
        finally:
            closed.set()

    stream = _speech_stream(SimpleNamespace(synthesize_chunks=chunks), "reply")
    assert json.loads(await anext(stream))["type"] == "audio"
    await asyncio.sleep(0)
    assert synthesized == [0]
    assert json.loads(await anext(stream))["type"] == "audio"
    assert synthesized == [0, 1]
    await stream.aclose()
    assert closed.is_set()
    assert cancellations[0].is_set()


async def test_stream_disconnect_cancels_native_work_then_closes_the_generator():
    started = threading.Event()
    closed = threading.Event()
    cancellations = []

    def chunks(text, *, cancelled):
        cancellations.append(cancelled)
        try:
            started.set()
            assert cancelled.wait(2), "Disconnect must reach native synthesis"
            raise VoiceCancelled("Speech interrupted.")
            yield b"unreachable"
        finally:
            closed.set()

    stream = _speech_stream(SimpleNamespace(synthesize_chunks=chunks), "reply")
    pending = asyncio.create_task(anext(stream))
    assert await asyncio.to_thread(started.wait, 2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert cancellations[0].is_set()
    assert await asyncio.to_thread(closed.wait, 2)
    await stream.aclose()


async def test_failed_response_write_closes_stream_without_prefetching_later_chunks():
    closed = threading.Event()
    synthesized = []
    cancellations = []

    def chunks(text, *, cancelled):
        cancellations.append(cancelled)
        try:
            for index in range(3):
                synthesized.append(index)
                yield b"RIFFchunk"
        finally:
            closed.set()

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("Client disconnected during response write")

    response = _SpeechResponse(
        _speech_stream(SimpleNamespace(synthesize_chunks=chunks), "reply")
    )
    with pytest.raises(OSError, match="Client disconnected"):
        await response.stream_response(send)
    assert synthesized == [0]
    assert cancellations[0].is_set()
    assert closed.is_set()


async def test_oversized_speech_chunk_fails_with_a_bounded_error_record():
    def chunks(text, *, cancelled):
        yield b"x" * (4 * 1024 * 1024 + 1)

    events = [json.loads(line) async for line in _speech_stream(
        SimpleNamespace(synthesize_chunks=chunks), "reply"
    )]
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert "size limit" in events[0]["message"]


@pytest.mark.parametrize(("origin", "host", "allowed"), [
    (None, "127.0.0.1:8080", True),
    ("http://127.0.0.1:8080", "127.0.0.1:8080", True),
    ("https://recollect.example", "recollect.example", True),
    ("http://localhost:5173", "127.0.0.1:8080", True),
    ("http://127.0.0.1:5173", "localhost:8080", True),
    ("http://localhost:5173", "remote.example", False),
    ("http://evil.example", "127.0.0.1:8080", False),
    ("null", "127.0.0.1:8080", False),
    ("file://127.0.0.1:8080", "127.0.0.1:8080", False),
    ("http://[", "127.0.0.1:8080", False),
])
def test_voice_origin_policy(origin, host, allowed):
    assert _allowed_origin(origin, host) is allowed


@pytest.mark.parametrize("origin", ["http://evil.example", "http://["])
def test_cross_origin_speech_is_refused_before_trace_lookup(voice_app, origin):
    client, state = voice_app

    response = client.post(
        "/api/voice/speech", headers={"origin": origin}, json={"turn_id": "turn-1"},
    )

    assert response.status_code == 403
    assert state.sessions.lookups == []
    assert state.voice.spoken == []


@pytest.mark.parametrize("origin", ["http://evil.example", "http://["])
def test_cross_origin_listener_is_refused_before_microphone_claim(voice_app, origin):
    client, state = voice_app

    with (
        pytest.raises(WebSocketDisconnect) as closed,
        client.websocket_connect("/api/voice/listen", headers={"origin": origin}),
    ):
        pytest.fail("Cross-origin voice must not connect")

    assert closed.value.code == 1008
    assert state.voice.listeners == []
    assert not state.voice.listener_claim.locked()


def test_listener_pause_resume_and_disconnect_release_claim(voice_app):
    client, state = voice_app

    with client.websocket_connect("/api/voice/listen") as socket:
        assert socket.receive_json()["state"] == "waiting"
        assert state.voice.listener_claim.locked()
        socket.send_json({"type": "pause"})
        assert socket.receive_json()["state"] == "paused"
        socket.send_bytes(b"\0\0" * 100)
        socket.send_json({"type": "resume"})
        assert socket.receive_json()["state"] == "waiting"

    assert not state.voice.listener_claim.locked()
    with client.websocket_connect("/api/voice/listen") as socket:
        assert socket.receive_json()["state"] == "waiting"
    assert len(state.voice.listeners) == 2


def test_second_tab_cannot_steal_or_release_active_listener(voice_app):
    client, state = voice_app

    with client.websocket_connect("/api/voice/listen") as first:
        assert first.receive_json()["state"] == "waiting"
        with client.websocket_connect("/api/voice/listen") as second:
            error = second.receive_json()
            assert error["type"] == "error"
            assert "another tab" in error["message"]
            with pytest.raises(WebSocketDisconnect) as closed:
                second.receive_json()
            assert closed.value.code == 1013
        assert state.voice.listener_claim.locked()
        first.send_json({"type": "pause"})
        assert first.receive_json()["state"] == "paused"
        assert len(state.voice.listeners) == 1

    assert not state.voice.listener_claim.locked()


def test_playback_controls_keep_capture_open_and_run_off_the_event_loop(voice_app):
    client, state = voice_app
    calls = []
    with client.websocket_connect("/api/voice/listen") as socket:
        socket.receive_json()
        listener = state.voice.listeners[0]
        original = listener.set_playback

        def playback(active):
            calls.append((active, threading.get_ident()))
            return original(active)

        listener.set_playback = playback
        socket.send_json({"type": "playback", "active": True})
        socket.send_bytes(b"\0\0" * 512)
        socket.send_json({"type": "playback", "active": False})
        socket.send_json({"type": "pause"})
        assert socket.receive_json()["state"] == "paused"
        assert [active for active, _ in calls] == [True, False]
        assert state.voice.listener_claim.locked()

    loop_thread = client.get("/api/text-alive").json()["thread"]
    assert all(worker != loop_thread for _, worker in calls)
    assert not state.voice.listener_claim.locked()


def test_socket_streams_live_text_and_followups_without_resume_or_second_wake(
    voice_app,
):
    client, state = voice_app
    utterances = deque(["first request", "second request"])

    class Recognizer(SilentRecognizer):
        def __init__(self, text):
            self.text = text

        def PartialResult(self):
            if self.text is None:
                return json.dumps({"partial_result": [
                    {"word": "hey", "start": 0, "end": 0.016},
                    {"word": "idris", "start": 0.016, "end": 0.032},
                ]})
            return json.dumps({"partial": self.text})

        def FinalResult(self):
            return json.dumps({"text": self.text})

    def new_listener():
        return VoiceListener(
            RecollectConfig(embedding_model_path=Path("unused.gguf")),
            lambda wake: Recognizer(None if wake else utterances.popleft()),
            lambda pcm: float(any(pcm)),
        )

    state.voice.new_listener = new_listener
    with client.websocket_connect("/api/voice/listen") as socket:
        assert socket.receive_json()["state"] == "waiting"
        socket.send_bytes(b"\0\0" * 512)
        assert socket.receive_json()["state"] == "listening"
        for text in ["first request", "second request"]:
            socket.send_json({"type": "playback", "active": text == "second request"})
            for _ in range(8):
                socket.send_bytes(b"\1\0" * 512)
            for _ in range(44):
                socket.send_bytes(b"\0\0" * 512)
        socket.send_json({"type": "pause"})
        events = []
        while True:
            event = socket.receive_json()
            if event.get("state") == "paused":
                break
            events.append(event)

    assert [event["text"] for event in events if event["type"] == "transcript"] == [
        "first request", "second request",
    ]
    assert events.count({"type": "speech_start"}) == 2
    assert {"type": "partial", "text": "first request"} in events
    assert {"type": "partial", "text": "second request"} in events
    assert not any(event.get("state") == "waiting" for event in events)
    assert not state.voice.listener_claim.locked()


@pytest.mark.parametrize("control", [
    "not json", "[]", "{}", '{"type":"unknown"}', "x" * 257,
    '{"type":"playback"}', '{"type":"playback","active":1}',
    '{"type":"playback","active":"true"}', '{"type":"playback","active":null}',
])
def test_bad_control_closes_only_voice_and_releases_claim(voice_app, control):
    client, state = voice_app

    with client.websocket_connect("/api/voice/listen") as socket:
        socket.receive_json()
        socket.send_text(control)
        assert socket.receive_json()["type"] == "error"
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 1011

    assert not state.voice.listener_claim.locked()
    assert client.get("/api/text-alive").json()["available"] is True


@pytest.mark.parametrize("pcm", [
    pytest.param(b"", id="empty"),
    pytest.param(b"\0", id="odd-byte-count"),
    pytest.param(b"\0" * (MAX_FRAME_BYTES + 2), id="oversized"),
])
def test_bad_audio_frame_closes_only_voice_and_releases_claim(voice_app, pcm):
    client, state = voice_app

    with client.websocket_connect("/api/voice/listen") as socket:
        socket.receive_json()
        socket.send_bytes(pcm)
        error = socket.receive_json()
        assert error["type"] == "error"
        assert "PCM16 mono" in error["message"]
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 1011

    assert not state.voice.listener_claim.locked()
    assert client.get("/api/text-alive").json()["available"] is True


@pytest.mark.parametrize("error", [
    VoiceUnavailable("No models"), OSError("Load failed"),
])
def test_listener_load_failure_releases_claim_and_preserves_text(voice_app, error):
    client, state = voice_app
    state.voice.listener_error = error

    with client.websocket_connect("/api/voice/listen") as socket:
        assert socket.receive_json() == {"type": "error", "message": str(error)}
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 1011

    assert not state.voice.listener_claim.locked()
    assert client.get("/api/text-alive").json()["available"] is True
