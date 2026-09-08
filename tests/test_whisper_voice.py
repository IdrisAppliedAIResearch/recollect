"""Whisper lifecycle contracts with local fakes, never model files or CUDA."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import FastAPI

from recollect.config import RecollectConfig
from recollect.engine import voice_asr
from recollect.engine.voice import VoiceService
from recollect.engine.voice_asr import TranscriptionCancelled, WhisperTranscriber
from recollect.engine.whisper_listener import WhisperListener
from recollect.voice_api import install_voice_routes


def config(**kwargs):
    return RecollectConfig(embedding_model_path=Path("unused.gguf"), **kwargs)


def pcm(frames=1, value=900):
    return np.full(frames * 512, value, dtype="<i2").tobytes()


class Wake:
    def AcceptWaveform(self, audio):
        return False

    def PartialResult(self):
        return json.dumps({"partial_result": [
            {"word": "hey", "end": 0.016}, {"word": "idris", "end": 0.032},
        ]})


class Decoder:
    def __init__(self, *, block=False, text="recognized request", error=False):
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = block
        self.text = text
        self.error = error

    def transcribe(self, audio, *, cancelled):
        self.calls.append((audio, cancelled))
        if self.block and len(self.calls) == 1:
            self.started.set()
            assert self.release.wait(3), "Test did not release its fake native call"
        if self.error:
            raise RuntimeError("native failure")
        # Deliberately return even after cancellation, as native work may do.
        return self.text


def listener(decoder, **kwargs):
    wakes = []

    def recognizer(wake):
        assert wake, "Whisper mode must not create a Vosk dictation recognizer"
        wakes.append(Wake())
        return wakes[-1]

    result = WhisperListener(
        config(voice_asr_backend="whisper", **kwargs), recognizer,
        lambda data: float(np.frombuffer(data, dtype="<i2").mean()) / 1000,
        decoder,
    )
    assert result.feed(pcm(value=0)) == [result.event()]
    return result, wakes


def feed(result, count, value=900):
    return [event for _ in range(count) for event in result.feed(pcm(value=value))]


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


@asynccontextmanager
async def running(result, decoder):
    events = []

    async def send(event):
        events.append(event)

    task = asyncio.create_task(result.run(send))
    try:
        yield events
    finally:
        result.close()
        decoder.release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_whisper_loader_is_local_lazy_and_warms_inference_once(tmp_path, monkeypatch):
    (tmp_path / "model.bin").write_bytes(b"fake model")
    loads, decodes = [], []

    def model(*args, **kwargs):
        loads.append((args, kwargs))

        def transcribe(audio, **options):
            decodes.append((audio.copy(), options))
            return iter([
                SimpleNamespace(text=" first "), SimpleNamespace(text="word"),
            ]), None

        return SimpleNamespace(transcribe=transcribe)

    monkeypatch.setitem(
        sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=model),
    )
    transcriber = WhisperTranscriber(config(
        voice_asr_model_dir=tmp_path, voice_asr_device="cpu",
    ))
    assert loads == [] and decodes == [] and not transcriber.ready
    transcriber.warm_up()
    transcriber.warm_up()
    assert transcriber.ready and len(loads) == 1 and len(decodes) == 1
    assert len(decodes[0][0]) == 16_000 and not decodes[0][0].any()
    assert loads[0] == ((str(tmp_path),), {
        "device": "cpu", "compute_type": "float16", "cpu_threads": 2,
        "num_workers": 1, "local_files_only": True,
    })
    audio = np.array([-32768, 0, 16384, 32767], dtype="<i2")
    assert transcriber.transcribe(audio.tobytes()) == "first word"
    np.testing.assert_array_equal(decodes[1][0], audio.astype(np.float32) / 32768)
    assert decodes[1][1] == {
        "language": "en", "task": "transcribe", "beam_size": 1, "temperature": 0,
        "condition_on_previous_text": False, "vad_filter": False,
    }


def test_missing_whisper_model_never_attempts_a_runtime_download(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(
        WhisperModel=lambda *args, **kwargs: pytest.fail("Must not load by model name"),
    ))
    transcriber = WhisperTranscriber(config(voice_asr_model_dir=tmp_path))
    with pytest.raises(RuntimeError, match="missing"):
        transcriber.warm_up()
    assert not transcriber.ready


def test_windows_whisper_loads_separate_cuda_directories_and_retains_handles(
    tmp_path, monkeypatch,
):
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    packages = {}
    for name in ("nvidia.cublas", "nvidia.cudnn"):
        root = tmp_path / name
        (root / "bin").mkdir(parents=True)
        packages[name] = SimpleNamespace(submodule_search_locations=[str(root)])
    added, handles = [], []

    def add(path):
        added.append(path)
        handle = object()
        handles.append(handle)
        return handle

    monkeypatch.setattr(voice_asr, "os", SimpleNamespace(
        name="nt", add_dll_directory=add,
    ))
    monkeypatch.setattr(voice_asr.importlib.util, "find_spec", packages.get)
    transcriber = WhisperTranscriber(config(voice_asr_cuda_dll_dir=explicit))
    transcriber._prepare_dlls()
    assert added == [
        str(explicit), *(str(tmp_path / name / "bin") for name in packages),
    ]
    assert transcriber._dll_handles == handles


def test_whisper_status_distinguishes_configured_backend_from_warmed_model(
    tmp_path, monkeypatch,
):
    for filename in (
        "vosk-model-small-en-us-0.15/am/final.mdl", "kokoro-v1.0.onnx",
        "voices-v1.0.bin", "silero-vad.onnx", "whisper/model.bin",
    ):
        target = tmp_path / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake")
    monkeypatch.setattr(voice_asr.importlib.util, "find_spec", lambda name: object())
    service = VoiceService(config(
        voice_model_dir=tmp_path, voice_asr_backend="whisper",
        voice_asr_model_dir=tmp_path / "whisper",
    ))
    status = service.status()
    assert status["available"] and not status["asr_ready"]
    assert status["asr_backend"] == "whisper"
    assert status["asr_device"] == "cuda" and status["asr_compute_type"] == "float16"
    service._asr = SimpleNamespace(ready=True)
    assert service.status()["asr_ready"]


def test_whisper_cancellation_closes_segments_and_releases_native_lock():
    cancelled, closed = threading.Event(), threading.Event()

    def segments():
        try:
            yield SimpleNamespace(text="first")
            cancelled.set()
            yield SimpleNamespace(text="must not escape")
        finally:
            closed.set()

    transcriber = WhisperTranscriber(config())
    transcriber._model = SimpleNamespace(transcribe=lambda *args, **kwargs: (
        segments(), None,
    ))
    with pytest.raises(TranscriptionCancelled):
        transcriber.transcribe(pcm(), cancelled=cancelled)
    assert closed.is_set() and not transcriber._lock.locked()
    with pytest.raises(TranscriptionCancelled):
        transcriber.transcribe(pcm(), cancelled=cancelled)


async def test_cancelled_whisper_waiter_does_not_wait_for_an_old_native_call():
    transcriber = WhisperTranscriber(config())
    transcriber._lock.acquire()
    cancelled = threading.Event()
    pending = asyncio.create_task(asyncio.to_thread(
        transcriber.transcribe, pcm(), cancelled=cancelled,
    ))
    try:
        cancelled.set()
        with pytest.raises(TranscriptionCancelled):
            await asyncio.wait_for(pending, 0.5)
        assert transcriber._lock.locked()
    finally:
        transcriber._lock.release()


@pytest.mark.parametrize("audio", [
    pytest.param(b"", id="empty"),
    pytest.param(b"x", id="odd"),
    pytest.param(b"x" * (120 * 32_000 + 2), id="oversized"),
])
def test_whisper_rejects_invalid_or_unbounded_pcm_before_loading(audio):
    transcriber = WhisperTranscriber(config())
    with pytest.raises(ValueError, match="PCM16"):
        transcriber.transcribe(audio)
    assert transcriber._model is None


@pytest.mark.parametrize("changes", [
    {"voice_asr_backend": "remote"}, {"voice_asr_device": "auto"},
    {"voice_asr_compute_type": "unsupported"},
    {"voice_asr_cuda_dll_dir": Path("relative")},
])
def test_invalid_asr_settings_are_refused(changes):
    with pytest.raises(ValueError):
        config(**changes)


def test_whisper_settings_are_opt_in_and_round_trip_from_environment(
    monkeypatch, tmp_path,
):
    assert config().voice_asr_backend == "vosk"
    for name, value in {
        "EMBEDDING_MODEL_PATH": "unused.gguf", "VOICE_ASR_BACKEND": "whisper",
        "VOICE_ASR_MODEL_DIR": str(tmp_path), "VOICE_ASR_DEVICE": "cuda",
        "VOICE_ASR_COMPUTE_TYPE": "int8_float16",
        "VOICE_ASR_CUDA_DLL_DIR": str(tmp_path),
    }.items():
        monkeypatch.setenv("RECOLLECT_" + name, value)
    settings = RecollectConfig.from_env(env_file=None)
    assert settings.voice_asr_backend == "whisper"
    assert settings.voice_asr_model_dir == tmp_path
    assert settings.voice_asr_device == "cuda"
    assert settings.voice_asr_compute_type == "int8_float16"
    assert settings.voice_asr_cuda_dll_dir == tmp_path


async def test_whisper_live_draft_final_and_followup_need_only_one_wake():
    decoder = Decoder()
    capture, wakes = listener(decoder)
    async with running(capture, decoder) as events:
        for index in range(2):
            assert feed(capture, 34).count({"type": "speech_start"}) == 1
            await until(lambda index=index: len([
                e for e in events if e["type"] == "partial"
            ]) > index)
            assert not any(e["type"] == "transcript" for e in events[index * 3:])
            feed(capture, 25, value=0)  # A short pause is still inside the request.
            assert not capture._final_pending
            feed(capture, 4)
            feed(capture, 44, value=0)
            await until(lambda index=index: len([
                e for e in events if e["type"] == "transcript"
            ]) > index)
        assert len(wakes) == 1
        assert [e["text"] for e in events if e["type"] == "transcript"] == [
            "recognized request", "recognized request",
        ]
        assert capture.state == "listening"


async def test_whisper_short_utterance_finalizes_without_a_live_draft():
    decoder = Decoder(text="Books")
    capture, _ = listener(decoder, voice_end_s=0.3)
    async with running(capture, decoder) as events:
        assert feed(capture, 8).count({"type": "speech_start"}) == 1
        feed(capture, 10, value=0)
        await until(lambda: any(e["type"] == "transcript" for e in events))
        assert events == [capture.event(), {"type": "transcript", "text": "Books"}]
        assert len(decoder.calls) == 1


async def test_slow_whisper_has_no_eight_second_cutoff_and_only_one_queued_draft():
    decoder = Decoder(block=True)
    capture, _ = listener(decoder)
    async with running(capture, decoder):
        feed(capture, 34)
        assert await asyncio.to_thread(decoder.started.wait, 2)
        feed(capture, 300)
        assert capture._dictation is not None and not capture._final_pending
        assert len(decoder.calls) == 1
        assert capture._job is not None and not capture._job.final
        assert len(capture._job.pcm) > 8 * 32_000
        feed(capture, 44, value=0)
        assert capture._job.final and capture._final_pending
        assert decoder.calls[0][1].is_set()


async def test_speech_resuming_during_final_decode_extends_unsent_audio():
    decoder = Decoder(block=True)
    capture, _ = listener(decoder, voice_end_s=0.3)
    async with running(capture, decoder) as events:
        feed(capture, 8)
        feed(capture, 10, value=0)
        assert await asyncio.to_thread(decoder.started.wait, 2)
        first_audio = decoder.calls[0][0]
        assert capture._final_pending
        assert feed(capture, 8).count({"type": "speech_start"}) == 1
        assert decoder.calls[0][1].is_set()
        assert bytes(capture._pcm).startswith(first_audio)
        feed(capture, 10, value=0)
        decoder.release.set()
        await until(lambda: any(e["type"] == "transcript" for e in events))
        assert len([e for e in events if e["type"] == "transcript"]) == 1
        assert len(decoder.calls[-1][0]) > len(first_audio)


@pytest.mark.parametrize("control", ["pause", "resume", "close"])
async def test_control_invalidates_inflight_final_even_if_native_returns_text(control):
    decoder = Decoder(block=True)
    capture, _ = listener(decoder, voice_end_s=0.3)
    async with running(capture, decoder) as events:
        feed(capture, 8)
        feed(capture, 10, value=0)
        assert await asyncio.to_thread(decoder.started.wait, 2)
        getattr(capture, control)()
        assert decoder.calls[0][1].is_set()
        assert capture._pcm == b"" and capture._job is None
        decoder.release.set()
        await until(lambda: capture._running is None or capture._closed)
        assert events == []


async def test_hard_limit_bounds_pcm_and_returns_complete_captured_review():
    decoder = Decoder(block=True, text="complete captured request")
    capture, _ = listener(decoder, voice_max_utterance_s=2, voice_wait_s=1)
    async with running(capture, decoder) as events:
        feed(capture, 34)
        assert await asyncio.to_thread(decoder.started.wait, 2)
        feed(capture, 100)
        assert capture.state == "paused"
        assert len(capture._pcm) == 2 * 32_000
        assert capture._job.final and capture._job.at_limit
        assert capture.feed(pcm(20)) == []
        assert events == []
        decoder.release.set()
        await until(lambda: any(e["type"] == "limit" for e in events))
        assert events == [capture.event(), {
            "type": "limit", "text": "complete captured request", "limit_s": 2,
        }]
        assert capture._pcm == b""
        assert capture.unpause()["state"] == "listening"


async def test_empty_decode_clears_attempt_and_keeps_followups_available():
    decoder = Decoder(text="")
    capture, _ = listener(decoder, voice_end_s=0.3)
    async with running(capture, decoder) as events:
        feed(capture, 8)
        feed(capture, 10, value=0)
        await until(lambda: bool(events))
        assert events == [capture.event(), {"type": "partial", "text": ""}]
        assert capture.state == "listening"
        assert feed(capture, 8).count({"type": "speech_start"}) == 1


def test_whisper_same_breath_wake_keeps_command_audio_and_vad_onset():
    decoder = Decoder()
    capture = WhisperListener(
        config(voice_asr_backend="whisper"), lambda wake: Wake(),
        lambda data: float(any(data)), decoder,
    )
    events = capture.feed(pcm(value=0) + pcm(8))
    assert events == [capture.event(), {"type": "speech_start"}]
    assert bytes(capture._pcm) == pcm(8)
    capture.close()


@asynccontextmanager
async def websocket(capture):
    app = FastAPI()
    voice = SimpleNamespace(
        listener_claim=threading.Lock(), new_listener=lambda: capture,
    )
    install_voice_routes(app, lambda: SimpleNamespace(voice=voice))
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    await incoming.put({"type": "websocket.connect"})
    task = asyncio.create_task(app({
        "type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "scheme": "ws", "path": "/api/voice/listen", "raw_path": b"/api/voice/listen",
        "root_path": "", "query_string": b"", "headers": [],
        "server": ("testserver", 80), "client": ("127.0.0.1", 1),
        "subprotocols": [],
    }, incoming.get, outgoing.put))
    try:
        assert (await asyncio.wait_for(outgoing.get(), 2))["type"] == "websocket.accept"
        yield incoming, outgoing, voice
    finally:
        await incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, 2)


async def event(outgoing):
    return json.loads((await asyncio.wait_for(outgoing.get(), 2))["text"])


async def test_socket_mute_ack_and_unmute_do_not_wait_for_gpu_or_replay_stale_text():
    decoder = Decoder(block=True)
    capture, _ = listener(decoder)
    try:
        async with websocket(capture) as (incoming, outgoing, voice):
            assert (await event(outgoing))["state"] == "listening"
            for _ in range(34):
                await incoming.put({"type": "websocket.receive", "bytes": pcm()})
            assert await event(outgoing) == {"type": "speech_start"}
            assert await asyncio.to_thread(decoder.started.wait, 2)
            await incoming.put({"type": "websocket.receive", "text": json.dumps({
                "type": "pause", "control_id": 1,
            })})
            assert await event(outgoing) == {
                "type": "state", "state": "paused", "wake_phrase": "hey idris",
                "control_id": 1,
            }
            assert not decoder.release.is_set() and decoder.calls[0][1].is_set()
            decoder.release.set()
            await incoming.put({"type": "websocket.receive", "text": json.dumps({
                "type": "unpause", "control_id": 2,
            })})
            assert (await event(outgoing))["control_id"] == 2
            for _ in range(8):
                await incoming.put({"type": "websocket.receive", "bytes": pcm()})
            assert await event(outgoing) == {"type": "speech_start"}
            assert voice.listener_claim.locked()
        assert not voice.listener_claim.locked()
    finally:
        decoder.release.set()


async def test_socket_closes_on_decoder_failure_and_releases_listener_claim():
    decoder = Decoder(error=True)
    capture, _ = listener(decoder)
    async with websocket(capture) as (incoming, outgoing, voice):
        await event(outgoing)
        for _ in range(34):
            await incoming.put({"type": "websocket.receive", "bytes": pcm()})
        assert (await event(outgoing))["type"] == "speech_start"
        assert (await event(outgoing))["type"] == "error"
        closed = await asyncio.wait_for(outgoing.get(), 2)
        assert closed == {"type": "websocket.close", "code": 1011, "reason": ""}
    assert not voice.listener_claim.locked()


async def test_socket_disconnect_releases_capture_during_native_decode():
    decoder = Decoder(block=True)
    capture, _ = listener(decoder)
    try:
        async with websocket(capture) as (incoming, outgoing, voice):
            await event(outgoing)
            for _ in range(34):
                await incoming.put({"type": "websocket.receive", "bytes": pcm()})
            assert await event(outgoing) == {"type": "speech_start"}
            assert await asyncio.to_thread(decoder.started.wait, 2)
        assert not voice.listener_claim.locked()
        assert not decoder.release.is_set()
        assert decoder.calls[0][1].is_set()
        assert capture._closed and not capture._pcm and capture._job is None
    finally:
        decoder.release.set()


def test_voice_service_selects_whisper_without_a_second_dictation_model(monkeypatch):
    service = VoiceService(config(voice_asr_backend="whisper"))
    service._asr = Decoder()
    service._vad_session = SimpleNamespace(run=lambda *args: (np.zeros((1, 1)), None))
    monkeypatch.setattr(service, "warm_up", lambda: None)

    class FakeWake(Wake):
        def SetWords(self, enabled):
            pass

        def SetPartialWords(self, enabled):
            pass

    monkeypatch.setitem(sys.modules, "vosk", SimpleNamespace(
        KaldiRecognizer=lambda *args: FakeWake(),
    ))
    result = service.new_listener()
    assert isinstance(result, WhisperListener)
    assert result._transcriber is service._asr
    result.close()
