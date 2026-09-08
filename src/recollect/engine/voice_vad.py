"""Silero's recurrent 16 kHz detector through the existing CPU ONNX runtime."""

from __future__ import annotations

import numpy as np

VAD_SAMPLES = 512
VAD_BYTES = VAD_SAMPLES * 2


class SpeechDetector:
    """One stream's recurrent state; the read-only model session can be shared."""

    def __init__(self, session) -> None:
        self.session = session
        self.reset()
        self._sample_rate = np.array(16_000, dtype=np.int64)

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, 64), dtype=np.float32)

    def __call__(self, pcm: bytes) -> float:
        if len(pcm) != VAD_BYTES:
            raise ValueError("Speech detection requires 512 PCM16 samples.")
        chunk = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
        # The pinned graph carries 64 samples of context in addition to the
        # next 512 samples. This is the publisher's streaming call shape.
        inputs = np.concatenate((self._context, chunk.reshape(1, -1)), axis=1)
        probability, self._state = self.session.run(None, {
            "input": inputs, "state": self._state, "sr": self._sample_rate,
        })
        self._context = inputs[:, -64:]
        return float(probability[0, 0])
