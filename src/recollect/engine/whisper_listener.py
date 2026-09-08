"""Keep Vosk wake/Silero capture responsive while Whisper decodes snapshots."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field

from .voice import SAMPLE_RATE, VoiceListener
from .voice_asr import TranscriptionCancelled

_DRAFT_BYTES = SAMPLE_RATE * 2


@dataclass
class _Decode:
    generation: int
    pcm: bytes
    final: bool
    at_limit: bool = False
    cancelled: threading.Event = field(default_factory=threading.Event)


class WhisperListener(VoiceListener):
    """At most one native decode and one replaceable pending snapshot.

    The captured utterance is bounded by the configured 120-second maximum.
    Silence requests a final decode; text availability never ends speech.
    """

    def __init__(self, config, recognizer, vad, transcriber) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._transcriber = transcriber
        self._closed = False
        self._generation = 0
        self._job: _Decode | None = None
        self._running: _Decode | None = None
        self._pcm = bytearray()
        self._draft_at = 0
        self._final_pending = False
        super().__init__(config, recognizer, vad)

    def _invalidate(self, *, preserve_audio: bool = False) -> None:
        self._generation += 1
        for job in (self._job, self._running):
            if job is not None:
                job.cancelled.set()
        self._job = None
        self._final_pending = False
        if not preserve_audio:
            self._pcm.clear()
            self._draft_at = 0

    def resume(self) -> dict:
        with self._condition:
            self._invalidate()
            return super().resume()

    def pause(self) -> dict:
        with self._condition:
            self._invalidate()
            return super().pause()

    def unpause(self) -> dict:
        with self._condition:
            return super().unpause()

    def set_playback(self, active: bool) -> None:
        with self._condition:
            super().set_playback(active)

    def feed(self, pcm: bytes) -> list[dict]:
        with self._condition:
            return [] if self._closed else super().feed(pcm)

    def _start_dictation(self):
        # Speech resumed before a final was delivered: extend the unsent
        # request, rather than emitting its old ending after the new onset.
        self._invalidate(preserve_audio=self._final_pending)
        return True

    def _empty_utterance_expired(self, elapsed: float) -> bool:
        return False

    def _decode(self, pcm: bytes) -> list[dict]:
        maximum = round(self.config.voice_max_utterance_s * SAMPLE_RATE) * 2
        self._pcm.extend(pcm[:max(0, maximum - len(self._pcm))])
        self._utterance_bytes = len(self._pcm)
        if len(self._pcm) - self._draft_at >= _DRAFT_BYTES:
            self._draft_at = len(self._pcm)
            self._schedule(final=False)
        return []

    def _schedule(self, *, final: bool, at_limit: bool = False) -> None:
        if self._job is not None:
            self._job.cancelled.set()
        if final and self._running is not None:
            self._running.cancelled.set()
        self._job = _Decode(
            self._generation, bytes(self._pcm), final=final, at_limit=at_limit,
        )
        self._condition.notify_all()

    def _finish_utterance(self, at_limit: bool) -> list[dict]:
        self._schedule(final=True, at_limit=at_limit)
        self._final_pending = True
        self._reset_attempt()
        if at_limit:
            # Stop accepting PCM immediately; send the paused/review events
            # together once the complete captured text is available.
            self._paused_from = "listening"
            self.state = "paused"
            self._pending.clear()
        return []

    def _take_job(self) -> _Decode | None:
        with self._condition:
            self._condition.wait_for(lambda: self._closed or self._job is not None)
            if self._closed:
                return None
            self._running, self._job = self._job, None
            return self._running

    def _complete(self, job: _Decode, text: str) -> list[dict]:
        with self._condition:
            self._running = None
            if (self._closed or job.cancelled.is_set()
                    or job.generation != self._generation):
                return []
            text = text.strip()
            if not job.final:
                if text == self._partial:
                    return []
                self._partial = text
                return [{"type": "partial", "text": text}]
            self._final_pending = False
            self._pcm.clear()
            self._draft_at = 0
            if job.at_limit and text:
                return [self.event(), {"type": "limit", "text": text,
                                       "limit_s": self.config.voice_max_utterance_s}]
            self.state = "listening"
            return [self.event(), {"type": "transcript", "text": text} if text
                    else {"type": "partial", "text": ""}]

    async def run(self, send, *, lock: asyncio.Lock | None = None) -> None:
        lock = lock or asyncio.Lock()
        try:
            while (job := await asyncio.to_thread(self._take_job)) is not None:
                try:
                    text = await asyncio.to_thread(
                        self._transcriber.transcribe, job.pcm,
                        cancelled=job.cancelled,
                    )
                except TranscriptionCancelled:
                    continue
                except Exception:
                    async with lock:
                        if job.cancelled.is_set() or self._closed:
                            continue
                        await send({"type": "error", "message":
                                    "Whisper could not transcribe. "
                                    "Enable voice to retry."})
                        return
                async with lock:
                    for event in self._complete(job, text):
                        await send(event)
        finally:
            self.close()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._invalidate()
            self._pending.clear()
            self._preroll.clear()
            self._reset_attempt()
            self._condition.notify_all()
