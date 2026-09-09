"""Speech transport. Recognized commands use the existing /api/chat route."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from collections.abc import AsyncIterator, Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, field_validator

from .engine.voice import VoiceCancelled, VoiceUnavailable
from .limits import validate_identifier


class SpeechRequest(BaseModel):
    turn_id: str
    stream: bool = False

    @field_validator("turn_id")
    @classmethod
    def valid_turn(cls, value: str) -> str:
        return validate_identifier(value)


class NotificationSpeechRequest(BaseModel):
    session_id: str
    notification_id: str
    stream: bool = False

    @field_validator("session_id", "notification_id")
    @classmethod
    def valid_identifier(cls, value: str) -> str:
        return validate_identifier(value)


class _SpeechResponse(StreamingResponse):
    async def stream_response(self, send) -> None:
        try:
            await super().stream_response(send)
        finally:
            # Also close when a disconnected socket fails during a blocked
            # response write, while the audio generator is paused at yield.
            await self.body_iterator.aclose()


async def _speech_worker(gate, function, *args, **kwargs) -> asyncio.Task:
    if gate is not None:
        await gate.acquire()
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))

    def finished(task):
        # Cancellation of the request must not release admission while native
        # inference still owns a worker. Waiters stay on the event loop.
        if gate is not None:
            gate.release()
        if not task.cancelled():
            task.exception()

    worker.add_done_callback(finished)
    return worker


async def _speech_stream(voice, text: str, *, gate=None) -> AsyncIterator[bytes]:
    cancelled = threading.Event()
    chunks = voice.synthesize_chunks(text, cancelled=cancelled)
    worker = None

    def close(completed=None):
        if completed is not None and not completed.cancelled():
            completed.exception()
        chunks.close()

    try:
        while True:
            # Advance only when the response consumer is ready for another
            # chunk; synthesis and queued WAV data cannot grow ahead of it.
            worker = await _speech_worker(gate, next, chunks, None)
            audio = await asyncio.shield(worker)
            if audio is None:
                yield b'{"type":"done"}\n'
                break
            if len(audio) > 4 * 1024 * 1024:
                raise ValueError("A speech chunk exceeded the audio size limit.")
            yield (json.dumps({
                "type": "audio", "wav": base64.b64encode(audio).decode("ascii"),
            }) + "\n").encode()
    except (VoiceCancelled, VoiceUnavailable, ValueError, RuntimeError) as error:
        yield (json.dumps({"type": "error", "message": str(error)}) + "\n").encode()
    finally:
        cancelled.set()
        # A native inference already executing cannot be force-stopped. Close
        # its generator only after that worker exits, releasing the speech lock.
        if worker is not None and not worker.done():
            worker.add_done_callback(close)
        else:
            close(worker)


def _allowed_origin(origin: str | None, host: str | None) -> bool:
    if origin is None:
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.netloc == host:
        return True
    # Vite changes Host when proxying to the backend, but preserves Origin.
    return origin in {"http://127.0.0.1:5173", "http://localhost:5173"} and (
        urlsplit(f"http://{host}").hostname in {"127.0.0.1", "localhost"}
    )


def install_voice_routes(app: FastAPI, state: Callable) -> None:
    speech_gate = asyncio.Semaphore(1)

    async def speak_saved_text(current, text, stream, request):
        if stream:
            return _SpeechResponse(
                _speech_stream(current.voice, text, gate=speech_gate),
                media_type="application/x-ndjson",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )
        cancelled = threading.Event()
        worker = None
        admission = asyncio.create_task(_speech_worker(
            speech_gate, current.voice.synthesize, text, cancelled=cancelled,
        ))

        async def watch_disconnect() -> None:
            while worker is None or not worker.done():
                if await request.is_disconnected():
                    cancelled.set()
                    # A queued full-WAV request has no native worker yet.
                    # Remove its waiter instead of synthesizing abandoned audio.
                    admission.cancel()
                    return
                await asyncio.sleep(0.1)

        watcher = asyncio.create_task(watch_disconnect())
        try:
            try:
                worker = await admission
            except asyncio.CancelledError:
                if cancelled.is_set():
                    raise VoiceCancelled("Speech was interrupted.") from None
                raise
            audio = await asyncio.shield(worker)
        except VoiceCancelled as error:
            raise HTTPException(499, str(error)) from error
        except (VoiceUnavailable, ValueError, RuntimeError) as error:
            raise HTTPException(503, str(error)) from error
        finally:
            cancelled.set()
            admission.cancel()
            watcher.cancel()
            await asyncio.gather(admission, watcher, return_exceptions=True)
        return Response(audio, media_type="audio/wav", headers={
            "Cache-Control": "no-store",
        })

    @app.get("/api/voice/status")
    async def status() -> dict:
        return await asyncio.to_thread(state().voice.status)

    @app.post("/api/voice/speech")
    async def speech(body: SpeechRequest, request: Request) -> Response:
        if not _allowed_origin(request.headers.get("origin"),
                               request.headers.get("host")):
            raise HTTPException(403, "Voice requests must come from Recollect.")
        current = state()
        try:
            trace = await asyncio.to_thread(current.sessions.find_trace, body.turn_id)
        except ValueError as error:
            raise HTTPException(400, "Invalid turn identifier or storage path.") \
                from error
        if trace is None:
            raise HTTPException(404, "No such completed turn.")
        generation = trace.generation
        if not trace.verification.trustworthy or generation is None or generation.error:
            raise HTTPException(409, "Only a verified, completed reply can be spoken.")
        if not generation.response_text.strip():
            raise HTTPException(409, "This turn has no reply to speak.")
        return await speak_saved_text(
            current, generation.response_text, body.stream, request,
        )

    @app.post("/api/voice/notification")
    async def notification_speech(
        body: NotificationSpeechRequest, request: Request,
    ) -> Response:
        if not _allowed_origin(request.headers.get("origin"),
                               request.headers.get("host")):
            raise HTTPException(403, "Voice requests must come from Recollect.")
        current = state()
        try:
            notification = await asyncio.to_thread(
                current.task_store.get_notification,
                body.session_id, body.notification_id,
            )
        except KeyError as error:
            raise HTTPException(
                404, "No such saved notification in this conversation.",
            ) from error
        except ValueError as error:
            raise HTTPException(
                400, "Invalid notification identifier or storage path.",
            ) from error
        if notification is None or notification["session_id"] != body.session_id:
            raise HTTPException(404, "No such saved notification in this conversation.")
        text = notification["text"]
        if not text.strip():
            raise HTTPException(409, "This notification has no text to speak.")
        return await speak_saved_text(current, text, body.stream, request)

    @app.websocket("/api/voice/listen")
    async def listen(socket: WebSocket) -> None:
        if not _allowed_origin(socket.headers.get("origin"),
                               socket.headers.get("host")):
            await socket.close(code=1008)
            return
        await socket.accept()
        voice = state().voice
        if not voice.listener_claim.acquire(blocking=False):
            await socket.send_json({"type": "error", "message":
                                    "Voice is already enabled in another tab."})
            await socket.close(code=1013)
            return
        listener = None
        decoder = None
        receive = None
        try:
            listener = await asyncio.to_thread(voice.new_listener)
            await socket.send_json(listener.event())
            event_lock = asyncio.Lock()
            if hasattr(listener, "run"):
                decoder = asyncio.create_task(listener.run(
                    socket.send_json, lock=event_lock,
                ))
            while True:
                if decoder is None:
                    packet = await socket.receive()
                else:
                    receive = asyncio.create_task(socket.receive())
                    done, _ = await asyncio.wait(
                        [receive, decoder], return_when=asyncio.FIRST_COMPLETED,
                    )
                    if decoder in done:
                        await decoder
                        await socket.close(code=1011)
                        break
                    packet = await receive
                if packet["type"] == "websocket.disconnect":
                    break
                # Serialize event publication with controls, never native ASR.
                # A completed decode cannot appear after its mute acknowledgment.
                async with event_lock:
                    events = await _listen_packet(listener, packet)
                    for event in events:
                        await socket.send_json(event)
        except WebSocketDisconnect:
            pass
        except Exception as error:  # Speech failure must leave text chat available.
            try:
                await socket.send_json({"type": "error", "message": str(error)})
                await socket.close(code=1011)
            except (WebSocketDisconnect, RuntimeError):
                pass
        finally:
            if listener is not None and hasattr(listener, "close"):
                listener.close()
            tasks = [task for task in (receive, decoder) if task is not None]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            voice.listener_claim.release()


async def _listen_packet(listener, packet: dict) -> list[dict]:
    if packet.get("bytes") is not None:
        return await asyncio.to_thread(listener.feed, packet["bytes"])
    if packet.get("text") is None:
        return []
    if len(packet["text"]) > 256:
        raise ValueError("Voice control message is too large.")
    control = json.loads(packet["text"])
    if not isinstance(control, dict):
        raise ValueError("Invalid voice control message.")
    control_id = control.get("control_id")
    if "control_id" in control and (
        type(control_id) is not int or not 0 < control_id < 2**53
    ):
        raise ValueError("Voice control id must be a positive safe integer.")
    if control.get("type") == "resume":
        events = [await asyncio.to_thread(listener.resume)]
    elif control.get("type") == "pause":
        events = [await asyncio.to_thread(listener.pause)]
    elif control.get("type") == "unpause":
        events = [await asyncio.to_thread(listener.unpause)]
    elif control.get("type") == "playback":
        await asyncio.to_thread(listener.set_playback, control.get("active"))
        events = []
    else:
        raise ValueError("Unknown voice control message.")
    if control_id is not None:
        for event in events:
            event["control_id"] = control_id
    return events
