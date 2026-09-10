"""Local speech, kept outside the memory mechanism and its model slot.

The wake phrase opens a conversation until voice is stopped. Audio stays
ephemeral; the ordinary chat endpoint owns all submitted turns.
"""

from __future__ import annotations

import importlib.util
import io
import json
import threading
import wave
from collections.abc import Callable, Iterator

import numpy as np

from recollect.config import RecollectConfig

from .voice_text import speech_sentences
from .voice_text import spoken_text as spoken_text
from .voice_vad import VAD_BYTES, SpeechDetector

SAMPLE_RATE = 16_000
MAX_FRAME_BYTES = SAMPLE_RATE * 2
_PREROLL_BYTES = SAMPLE_RATE * 2 * 4
SENTENCE_PAUSE_S = 0.5


class VoiceUnavailable(RuntimeError):
    pass


class VoiceCancelled(RuntimeError):
    pass


def wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    audio = np.asarray(samples, dtype=np.float32).reshape(-1)
    if not len(audio) or not np.isfinite(audio).all():
        raise VoiceUnavailable("Kokoro returned empty or invalid audio.")
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())
    return output.getvalue()


class VoiceService:
    """Warm local models per server; native calls run in worker threads."""

    def __init__(self, config: RecollectConfig) -> None:
        self.config = config
        self._model = None
        self._kokoro = None
        self._vad_session = None
        self._asr = None
        self._provider: str | None = None
        self._load_lock = threading.Lock()
        self._speech_lock = threading.Lock()
        self.listener_claim = threading.Lock()

    def status(self) -> dict:
        root = self.config.voice_model_dir
        required = [
            root / "vosk-model-small-en-us-0.15" / "am" / "final.mdl",
            root / "kokoro-v1.0.onnx",
            root / "voices-v1.0.bin",
            root / "silero-vad.onnx",
        ]
        error = None
        if any(importlib.util.find_spec(name) is None for name in (
            "vosk", "kokoro_onnx", "onnxruntime",
        )):
            error = "Install local speech with uv sync --extra voice --inexact."
        elif any(not path.is_file() for path in required):
            error = "Speech models are missing. Run recollect voice-setup."
        if error is None and self.config.voice_asr_backend == "whisper":
            if importlib.util.find_spec("faster_whisper") is None:
                error = "Install the Whisper speech transcription extra."
            elif not (self.config.voice_asr_model_dir / "model.bin").is_file():
                error = "Whisper model files are missing. Run recollect voice-setup."
        return {
            "available": error is None,
            "wake_phrase": self.config.voice_wake_phrase,
            "sample_rate": SAMPLE_RATE,
            "device": self.config.voice_device,
            "provider": self._provider,
            "asr_backend": self.config.voice_asr_backend,
            "asr_model": (str(self.config.voice_asr_model_dir)
                          if self.config.voice_asr_backend == "whisper"
                          else "vosk-model-small-en-us-0.15"),
            "asr_device": (self.config.voice_asr_device
                           if self.config.voice_asr_backend == "whisper" else "cpu"),
            "asr_compute_type": (self.config.voice_asr_compute_type
                                 if self.config.voice_asr_backend == "whisper"
                                 else None),
            "asr_ready": (bool(self._asr is not None and self._asr.ready)
                          if self.config.voice_asr_backend == "whisper"
                          else self._model is not None),
            "error": error,
        }

    def _speech_session(self, ort):
        device = self.config.voice_device
        cuda = (device != "cpu"
                and "CUDAExecutionProvider" in ort.get_available_providers())
        if device == "cuda" and not cuda:
            raise VoiceUnavailable(
                "CUDA speech requires uv sync --extra voice-gpu --inexact. "
                "Remove the CPU onnxruntime package before switching runtimes."
            )
        if cuda:
            directory = self.config.voice_cuda_dll_dir
            if directory is not None and not directory.is_dir():
                raise VoiceUnavailable("The configured CUDA DLL directory is missing.")
            ort.preload_dlls(directory=str(directory) if directory else None)
        options = ort.SessionOptions()
        options.intra_op_num_threads = self.config.voice_threads
        options.inter_op_num_threads = 1
        provider = "CUDAExecutionProvider" if cuda else "CPUExecutionProvider"
        session = ort.InferenceSession(
            str(self.config.voice_model_dir / "kokoro-v1.0.onnx"),
            sess_options=options,
            providers=[(provider, {
                # Exhaustive convolution tuning repeats for different utterance
                # shapes. Heuristics keep conversational first-chunk latency low.
                "cudnn_conv_algo_search": "HEURISTIC",
                "arena_extend_strategy": "kSameAsRequested",
            })] if cuda else [provider],
        )
        if cuda and provider not in session.get_providers():
            raise VoiceUnavailable(
                "Kokoro could not load CUDA. Check CUDA 13 and cuDNN 9 DLLs "
                "or RECOLLECT_VOICE_CUDA_DLL_DIR; CPU fallback was refused."
            )
        # ORT's Python wrapper otherwise retries a failing run on CPU silently.
        session.disable_fallback()
        self._provider = provider
        return session

    def warm_up(self) -> None:
        with self._load_lock:
            if self._model is not None and self._kokoro is not None:
                return
            status = self.status()
            if not status["available"]:
                raise VoiceUnavailable(status["error"])
            import onnxruntime as ort
            import vosk
            from kokoro_onnx import Kokoro

            root = self.config.voice_model_dir
            vosk.SetLogLevel(-1)
            model = vosk.Model(str(root / "vosk-model-small-en-us-0.15"))
            words = self.config.voice_wake_phrase.lower().split()
            missing = [word for word in words if model.vosk_model_find_word(word) < 0]
            if missing:
                raise VoiceUnavailable(
                    "Wake phrase has words outside the speech vocabulary: "
                    + ", ".join(missing)
                )
            session = self._speech_session(ort)
            kokoro = Kokoro.from_session(session, str(root / "voices-v1.0.bin"))
            if self.config.voice_name not in kokoro.get_voices():
                raise VoiceUnavailable(
                    f"Unknown Kokoro voice: {self.config.voice_name}"
                )
            # Exercise phonemization as well as loading, before opening the mic.
            kokoro.create("Ready.", voice=self.config.voice_name, lang="en-us")
            vad_options = ort.SessionOptions()
            vad_options.intra_op_num_threads = 1
            vad_options.inter_op_num_threads = 1
            self._vad_session = ort.InferenceSession(
                str(root / "silero-vad.onnx"), sess_options=vad_options,
                providers=["CPUExecutionProvider"],
            )
            SpeechDetector(self._vad_session)(bytes(VAD_BYTES))
            if self.config.voice_asr_backend == "whisper":
                from .voice_asr import WhisperTranscriber

                self._asr = WhisperTranscriber(self.config)
                self._asr.warm_up()
            self._model, self._kokoro = model, kokoro

    def new_listener(self) -> VoiceListener:
        self.warm_up()
        import vosk

        def recognizer(wake: bool):
            args = [self._model, SAMPLE_RATE]
            if wake:
                args.append(json.dumps([
                    self.config.voice_wake_phrase.lower(), "[unk]",
                ]))
            result = vosk.KaldiRecognizer(*args)
            result.SetWords(True)
            result.SetPartialWords(True)
            return result

        detector = SpeechDetector(self._vad_session)
        if self.config.voice_asr_backend == "whisper":
            from .whisper_listener import WhisperListener

            return WhisperListener(self.config, recognizer, detector, self._asr)
        return VoiceListener(self.config, recognizer, detector)

    def synthesize(
        self, text: str, *, cancelled: threading.Event | None = None
    ) -> bytes:
        chunks = list(self._synthesize_pieces(text, cancelled=cancelled))
        return wav_bytes(
            np.concatenate([samples for samples, _ in chunks]), chunks[0][1],
        )

    def synthesize_chunks(
        self, text: str, *, cancelled: threading.Event | None = None
    ) -> Iterator[bytes]:
        for samples, rate in self._synthesize_pieces(text, cancelled=cancelled):
            yield wav_bytes(samples, rate)

    def _synthesize_pieces(
        self, text: str, *, cancelled: threading.Event | None = None
    ) -> Iterator[tuple[np.ndarray, int]]:
        try:
            self.warm_up()
        except VoiceUnavailable:
            raise
        except Exception as error:
            raise VoiceUnavailable(
                "Kokoro could not prepare audio. Try replaying."
            ) from error
        prose = spoken_text(text)
        if not prose:
            raise VoiceUnavailable("This reply has no speakable text.")
        cancelled = cancelled or threading.Event()

        def check_cancelled() -> None:
            if cancelled.is_set():
                raise VoiceCancelled("Speech was interrupted.")

        check_cancelled()
        # Native calls cannot be killed safely. Bound their text and check
        # cancellation between chunks, including while another call owns TTS.
        sentences = speech_sentences(prose)
        pieces = []
        for index, sentence in enumerate(sentences):
            bounded = _speech_pieces(sentence)
            pieces.extend(
                (piece, index < len(sentences) - 1 and part == len(bounded) - 1)
                for part, piece in enumerate(bounded)
            )
        for piece, pause_after in pieces:
            check_cancelled()
            while not self._speech_lock.acquire(timeout=0.1):
                check_cancelled()
            try:
                check_cancelled()
                try:
                    samples, rate = self._kokoro.create(
                        piece, voice=self.config.voice_name, lang="en-us",
                    )
                except Exception as error:
                    # Native ORT errors do not inherit RuntimeError. Normalize
                    # them here so transport can finish with an error record.
                    raise VoiceUnavailable(
                        "Kokoro could not speak this reply. Try replaying."
                    ) from error
                check_cancelled()
            finally:
                self._speech_lock.release()
            samples = np.asarray(samples, dtype=np.float32).reshape(-1)
            if not len(samples) or not np.isfinite(samples).all():
                raise VoiceUnavailable("Kokoro returned empty or invalid audio.")
            if pause_after:
                # Kokoro trims each native result. Punctuation alone cannot
                # guarantee a pause when separately rendered sentences join.
                samples = np.concatenate((
                    samples, np.zeros(round(rate * SENTENCE_PAUSE_S), np.float32),
                ))
            # Never hold a native lock while transport waits for its consumer.
            yield samples, rate


def _speech_pieces(prose: str) -> list[str]:
    pieces = []
    limit = 120
    while len(prose) > limit:
        boundary = max(prose.rfind(mark, 0, limit) for mark in (". ", "! ", "? ", "; "))
        if boundary < 0:
            boundary = prose.rfind(" ", 0, limit)
        else:
            boundary += 1
        if boundary <= 0:
            boundary = limit
        pieces.append(prose[:boundary].strip())
        prose = prose[boundary:].strip()
        limit = 240
    if prose:
        pieces.append(prose)
    return pieces


class VoiceListener:
    """Wake once, then endpoint on sustained silence and allow interruptions.

    The recognizer's early endpoints are segments, not complete user turns.
    Word timestamps preserve same-breath activation; a second pre-roll keeps
    the start of speech while the interruption detector gains confidence.
    """

    def __init__(self, config: RecollectConfig, recognizer: Callable,
                 vad: Callable[[bytes], float]) -> None:
        self.config = config
        self._recognizer = recognizer
        self._vad = vad
        self.resume()

    def event(self) -> dict:
        return {"type": "state", "state": self.state,
                "wake_phrase": self.config.voice_wake_phrase}

    def _reset_attempt(self) -> None:
        self._dictation = None
        self._onset_bytes = 0
        self._utterance_bytes = 0
        self._silence_bytes = 0
        self._command_preroll = bytearray()
        self._segments: list[str] = []
        self._partial = ""

    def resume(self) -> dict:
        self._wake = self._recognizer(True)
        self._preroll = bytearray()
        self._received = 0
        self._pending = bytearray()
        self.playback = False
        self._reset_attempt()
        if hasattr(self._vad, "reset"):
            self._vad.reset()
        self.state = "waiting"
        return self.event()

    def pause(self) -> dict:
        if self.state != "paused":
            self._paused_from = self.state
        self.state = "paused"
        self._preroll.clear()
        self._pending.clear()
        self._reset_attempt()
        return self.event()

    def unpause(self) -> dict:
        if self.state != "paused":
            return self.event()
        previous, playback = self._paused_from, self.playback
        self.resume()
        self.state = previous
        # A reply may still be playing while the microphone is muted.
        self.playback = playback
        return self.event()

    def set_playback(self, active: bool) -> None:
        if not isinstance(active, bool):
            raise ValueError("Playback state must be a boolean.")
        if active != self.playback:
            self._onset_bytes = 0
        self.playback = active

    def feed(self, pcm: bytes) -> list[dict]:
        if not pcm or len(pcm) % 2 or len(pcm) > MAX_FRAME_BYTES:
            raise ValueError("Audio must be PCM16 mono, 16 kHz, in frames <= 1 second.")
        if self.state == "paused":
            return []
        if self.state == "listening":
            return self._listen(pcm)

        self._received += len(pcm)
        self._preroll.extend(pcm)
        del self._preroll[:-_PREROLL_BYTES]
        final = self._wake.AcceptWaveform(pcm)
        result = json.loads(
            self._wake.Result() if final else self._wake.PartialResult()
        )
        word_times = result.get("result" if final else "partial_result", [])
        words = [word["word"] for word in word_times]
        phrase = self.config.voice_wake_phrase.lower().split()
        for index in range(len(words) - len(phrase) + 1):
            if words[index:index + len(phrase)] != phrase:
                continue
            end = word_times[index + len(phrase) - 1]["end"]
            end_byte = round(end * SAMPLE_RATE) * 2
            offset = max(0, end_byte - (self._received - len(self._preroll)))
            tail = bytes(self._preroll[offset:])
            self._preroll.clear()
            self.state = "listening"
            return [self.event(), *self._listen(tail)]
        if final or self._received >= SAMPLE_RATE * 2 * 60:
            self.resume()
        return []

    def _listen(self, pcm: bytes) -> list[dict]:
        self._pending.extend(pcm)
        events = []
        while len(self._pending) >= VAD_BYTES:
            frame = bytes(self._pending[:VAD_BYTES])
            del self._pending[:VAD_BYTES]
            events.extend(self._frame(frame))
        return events

    def _frame(self, frame: bytes) -> list[dict]:
        probability = self._vad(frame)
        events = []
        if self._dictation is None:
            self._command_preroll.extend(frame)
            keep = round(max(0.4, self.config.voice_speech_start_s + 0.064)
                         * SAMPLE_RATE) * 2
            del self._command_preroll[:-keep]
            threshold = (self.config.voice_interrupt_threshold if self.playback
                         else self.config.voice_speech_threshold)
            self._onset_bytes = (self._onset_bytes + len(frame)
                                 if probability >= threshold else 0)
            if self._onset_bytes < self.config.voice_speech_start_s * SAMPLE_RATE * 2:
                return []
            self._dictation = self._start_dictation()
            self._utterance_bytes = len(self._command_preroll)
            events.append({"type": "speech_start"})
            events.extend(self._decode(bytes(self._command_preroll)))
            self._command_preroll.clear()
            return events

        self._utterance_bytes += len(frame)
        # Hysteresis retains quiet endings once a real utterance has started.
        if probability >= max(0.01, self.config.voice_speech_threshold - 0.15):
            self._silence_bytes = 0
        else:
            self._silence_bytes += len(frame)
        events.extend(self._decode(frame))
        elapsed = self._utterance_bytes / (SAMPLE_RATE * 2)
        silence_ended = self._silence_bytes >= self.config.voice_end_s * SAMPLE_RATE * 2
        at_limit = elapsed >= self.config.voice_max_utterance_s
        if (
            silence_ended
            or at_limit
            or self._empty_utterance_expired(elapsed)
        ):
            events.extend(self._finish_utterance(at_limit and not silence_ended))
        return events

    def _start_dictation(self):
        return self._recognizer(False)

    def _empty_utterance_expired(self, elapsed: float) -> bool:
        return not self._partial and elapsed >= self.config.voice_wait_s

    def _finish_utterance(self, at_limit: bool) -> list[dict]:
        final = json.loads(self._dictation.FinalResult()).get("text", "").strip()
        text = " ".join([*self._segments, final]).strip()
        self._reset_attempt()
        if at_limit and text:
            # Never start generating from a cut-off sentence while the
            # user is still talking. Let them explicitly send or discard it.
            return [self.pause(), {"type": "limit", "text": text,
                                   "limit_s": self.config.voice_max_utterance_s}]
        return [self.event(), {"type": "transcript", "text": text} if text
                else {"type": "partial", "text": ""}]

    def _decode(self, pcm: bytes) -> list[dict]:
        final = self._dictation.AcceptWaveform(pcm)
        result = json.loads(
            self._dictation.Result() if final else self._dictation.PartialResult()
        )
        text = result.get("text" if final else "partial", "").strip()
        if final and text:
            self._segments.append(text)
        combined = " ".join([*self._segments, "" if final else text]).strip()
        if combined != self._partial:
            self._partial = combined
            return [{"type": "partial", "text": combined}]
        return []
