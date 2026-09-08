"""Local Whisper inference, independent of capture and conversational VAD."""

from __future__ import annotations

import importlib.util
import os
import threading
from pathlib import Path

import numpy as np

from recollect.config import RecollectConfig


class TranscriptionCancelled(RuntimeError):
    pass


class WhisperTranscriber:
    """One warm model, with cancellable waiting between bounded native calls."""

    def __init__(self, config: RecollectConfig) -> None:
        self.config = config
        self._model = None
        self.ready = False
        self._lock = threading.Lock()
        self._dll_handles: list = []

    def _prepare_dlls(self) -> None:
        if os.name != "nt" or self.config.voice_asr_device != "cuda":
            return
        directories = []
        explicit = self.config.voice_asr_cuda_dll_dir
        if explicit is not None:
            if not explicit.is_dir():
                raise RuntimeError("The Whisper CUDA DLL directory is missing.")
            directories.append(explicit)
        for package in ("nvidia.cublas", "nvidia.cudnn"):
            try:
                spec = importlib.util.find_spec(package)
            except ModuleNotFoundError:
                continue
            if spec is not None:
                directories.extend(
                    Path(location) / "bin"
                    for location in spec.submodule_search_locations or ()
                )
        for directory in dict.fromkeys(directories):
            if directory.is_dir():
                self._dll_handles.append(os.add_dll_directory(str(directory)))

    def _load(self) -> None:
        if self._model is not None:
            return
        root = self.config.voice_asr_model_dir
        if not root.is_dir() or not (root / "model.bin").is_file():
            raise RuntimeError("Whisper model files are missing. Run voice-setup.")
        self._prepare_dlls()
        from faster_whisper import WhisperModel

        self._model = WhisperModel(
            str(root), device=self.config.voice_asr_device,
            compute_type=self.config.voice_asr_compute_type,
            cpu_threads=self.config.voice_threads, num_workers=1,
            local_files_only=True,
        )

    def warm_up(self) -> None:
        with self._lock:
            self._load()
            if not self.ready:
                # Weight loading does not initialize the CUDA decode kernels.
                # Exercise a bounded inference before opening the microphone.
                self._infer(np.zeros(16_000, dtype=np.float32), threading.Event())
                self.ready = True

    def _infer(self, audio: np.ndarray, cancelled: threading.Event) -> str:
        segments, _ = self._model.transcribe(
            audio, language="en", task="transcribe", beam_size=1, temperature=0,
            condition_on_previous_text=False, vad_filter=False,
        )
        try:
            parts = []
            iterator = iter(segments)
            while True:
                if cancelled.is_set():
                    raise TranscriptionCancelled("Transcription was interrupted.")
                segment = next(iterator, None)
                if segment is None:
                    break
                parts.append(segment.text.strip())
            if cancelled.is_set():
                raise TranscriptionCancelled("Transcription was interrupted.")
            return " ".join(part for part in parts if part)
        finally:
            if hasattr(segments, "close"):
                segments.close()

    def transcribe(
        self, pcm: bytes, *, cancelled: threading.Event | None = None,
    ) -> str:
        if not pcm or len(pcm) % 2 or len(pcm) > 120 * 16_000 * 2:
            raise ValueError("Whisper requires nonempty PCM16 audio up to 120 seconds.")
        cancelled = cancelled or threading.Event()

        def check() -> None:
            if cancelled.is_set():
                raise TranscriptionCancelled("Transcription was interrupted.")

        check()
        while not self._lock.acquire(timeout=0.05):
            check()
        try:
            check()
            self._load()
            audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
            return self._infer(audio, cancelled)
        finally:
            self._lock.release()
