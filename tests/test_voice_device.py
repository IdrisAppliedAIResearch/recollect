"""Speech device selection must never disguise a failed CUDA load as CPU."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from recollect.config import RecollectConfig
from recollect.engine.voice import VoiceService, VoiceUnavailable


class FakeSession:
    def __init__(self, providers):
        self.providers = providers
        self.fallback_disabled = False

    def get_providers(self):
        return self.providers

    def disable_fallback(self):
        self.fallback_disabled = True

    def run(self, outputs, inputs):
        return np.array([[0]], dtype=np.float32), inputs["state"]


class FakeOrt:
    def __init__(self, *, cuda=False, cuda_loads=True):
        self.available = ["CPUExecutionProvider"]
        if cuda:
            self.available.insert(0, "CUDAExecutionProvider")
        self.cuda_loads = cuda_loads
        self.calls = []
        self.sessions = []

    def get_available_providers(self):
        return self.available

    def preload_dlls(self, *, directory):
        self.calls.append(("preload", directory))

    def SessionOptions(self):
        return SimpleNamespace()

    def InferenceSession(self, model, *, sess_options, providers):
        self.calls.append(("session", model, providers, sess_options))
        names = [entry[0] if isinstance(entry, tuple) else entry for entry in providers]
        if not self.cuda_loads and "CUDAExecutionProvider" in names:
            names = ["CPUExecutionProvider"]
        session = FakeSession(names)
        self.sessions.append(session)
        return session


def service(**kwargs):
    return VoiceService(RecollectConfig(embedding_model_path=Path("unused"), **kwargs))


@pytest.mark.parametrize("device,available_cuda", [("auto", False), ("cpu", True)])
def test_cpu_selection_does_not_preload_cuda(device, available_cuda):
    voice = service(voice_device=device)
    ort = FakeOrt(cuda=available_cuda)

    session = voice._speech_session(ort)

    assert session.get_providers() == ["CPUExecutionProvider"]
    assert session.fallback_disabled
    assert [call[0] for call in ort.calls] == ["session"]
    assert voice.status()["provider"] == "CPUExecutionProvider"


def test_required_cuda_without_gpu_runtime_refuses_to_create_a_cpu_session():
    voice = service(voice_device="cuda")
    ort = FakeOrt()

    with pytest.raises(VoiceUnavailable, match="voice-gpu"):
        voice._speech_session(ort)

    assert ort.calls == []
    assert voice.status()["provider"] is None


@pytest.mark.parametrize("device", ["auto", "cuda"])
@pytest.mark.parametrize("configured_directory", [False, True])
def test_cuda_preloads_libraries_before_session_and_disables_runtime_fallback(
    tmp_path, device, configured_directory,
):
    directory = tmp_path if configured_directory else None
    voice = service(voice_device=device, voice_cuda_dll_dir=directory)
    ort = FakeOrt(cuda=True)

    session = voice._speech_session(ort)

    assert ort.calls[0] == ("preload", str(directory) if directory else None)
    assert ort.calls[1][0] == "session"
    assert session.get_providers() == ["CUDAExecutionProvider"]
    assert session.fallback_disabled
    assert voice.status()["provider"] == "CUDAExecutionProvider"


@pytest.mark.parametrize("device", ["auto", "cuda"])
def test_advertised_cuda_provider_that_fails_to_load_is_not_reported_as_gpu(device):
    voice = service(voice_device=device)
    ort = FakeOrt(cuda=True, cuda_loads=False)

    with pytest.raises(VoiceUnavailable, match="CPU fallback was refused"):
        voice._speech_session(ort)

    assert voice.status()["provider"] is None


def test_missing_configured_dll_directory_fails_before_loading_models(tmp_path):
    voice = service(voice_device="cuda", voice_cuda_dll_dir=tmp_path / "missing")
    ort = FakeOrt(cuda=True)

    with pytest.raises(VoiceUnavailable, match="DLL directory is missing"):
        voice._speech_session(ort)

    assert ort.calls == []


def test_gpu_speech_keeps_voice_activity_detection_on_cpu(monkeypatch):
    voice = service(voice_device="cuda")
    ort = FakeOrt(cuda=True)
    kokoro = SimpleNamespace(
        get_voices=lambda: ["af_heart"],
        create=lambda *args, **kwargs: (np.zeros(512), 24_000),
    )
    monkeypatch.setattr(voice, "status", lambda: {"available": True})
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    monkeypatch.setitem(sys.modules, "vosk", SimpleNamespace(
        SetLogLevel=lambda level: None,
        Model=lambda path: SimpleNamespace(vosk_model_find_word=lambda word: 1),
    ))
    monkeypatch.setitem(sys.modules, "kokoro_onnx", SimpleNamespace(
        Kokoro=SimpleNamespace(from_session=lambda *args: kokoro),
    ))

    voice.warm_up()

    assert [session.get_providers() for session in ort.sessions] == [
        ["CUDAExecutionProvider"], ["CPUExecutionProvider"],
    ]
    assert voice._vad_session is ort.sessions[1]
    assert voice._kokoro is kokoro


@pytest.mark.parametrize("kwargs,match", [
    ({"voice_device": "gpu"}, "voice_device"),
    ({"voice_device": ""}, "voice_device"),
    ({"voice_cuda_dll_dir": Path("relative/cuda")}, "absolute path"),
])
def test_invalid_device_configuration_is_rejected(kwargs, match):
    with pytest.raises(ValueError, match=match):
        service(**kwargs)


@pytest.mark.parametrize("configured", [False, True])
def test_device_configuration_reads_environment_without_loading_dotenv(
    monkeypatch, tmp_path, configured,
):
    for name in list(os.environ):
        if name.startswith(("RECOLLECT_", "CDW_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("RECOLLECT_EMBEDDING_MODEL_PATH", "unused.gguf")
    if configured:
        monkeypatch.setenv("RECOLLECT_VOICE_DEVICE", "CUDA")
        monkeypatch.setenv("RECOLLECT_VOICE_CUDA_DLL_DIR", str(tmp_path))

    config = RecollectConfig.from_env(env_file=None)

    assert config.voice_device == ("cuda" if configured else "auto")
    assert config.voice_cuda_dll_dir == (tmp_path if configured else None)
