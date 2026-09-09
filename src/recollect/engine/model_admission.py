"""Share a model at request boundaries without exposing the chat API to workers."""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import socket
import time
from collections import deque
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from ..limits import ResourceLimitsMiddleware
from .context_window import check_context


class ModelAdmission:
    """Fair single-slot access, or one conversation and one worker lane."""

    def __init__(self, slots: int = 1) -> None:
        if type(slots) is not int or slots not in {1, 2}:
            raise ValueError("Model admission supports one or two slots.")
        self.slots = slots
        self._foreground: deque[asyncio.Future] = deque()
        self._background: deque[asyncio.Future] = deque()
        self._busy = False
        self._foreground_busy = False
        self._background_busy = False
        self._foreground_streak = 0
        self._closed = False

    def locked(self) -> bool:
        return self._busy or self._foreground_busy or self._background_busy

    async def acquire(self, *, background: bool = False) -> bool:
        if self._closed:
            raise RuntimeError("Model admission is closing.")
        if len(self._foreground) + len(self._background) >= 32:
            raise RuntimeError("The model request queue is full. Try again shortly.")
        waiter = asyncio.get_running_loop().create_future()
        queue = self._background if background else self._foreground
        queue.append(waiter)
        self._wake()
        try:
            await waiter
        except BaseException:
            if waiter.done() and not waiter.cancelled() and waiter.exception() is None:
                self.release(background=background)
            else:
                with contextlib.suppress(ValueError):
                    queue.remove(waiter)
                self._wake()
            raise
        return True

    def close(self) -> None:
        self._closed = True
        for queue in (self._foreground, self._background):
            while queue:
                waiter = queue.popleft()
                if not waiter.done():
                    waiter.set_exception(RuntimeError("Model admission is closing."))

    def release(self, *, background: bool = False) -> None:
        field = (
            "_background_busy" if background else "_foreground_busy"
        ) if self.slots == 2 else "_busy"
        if not getattr(self, field):
            raise RuntimeError("The model lease is not held.")
        setattr(self, field, False)
        self._wake()

    def _wake(self) -> None:
        for queue in (self._foreground, self._background):
            while queue and queue[0].cancelled():
                queue.popleft()
        if self.slots == 2:
            # Native child fan-out cannot occupy the conversational lane.
            for queue, field in (
                (self._foreground, "_foreground_busy"),
                (self._background, "_background_busy"),
            ):
                if queue and not getattr(self, field):
                    setattr(self, field, True)
                    queue.popleft().set_result(True)
            return
        if self._busy:
            return
        use_background = self._background and (
            not self._foreground or self._foreground_streak >= 2
        )
        queue = self._background if use_background else self._foreground
        if not queue:
            return
        self._foreground_streak = 0 if use_background else self._foreground_streak + 1
        self._busy = True
        queue.popleft().set_result(True)

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *_):
        self.release()

    @asynccontextmanager
    async def background(self):
        started = time.perf_counter()
        await self.acquire(background=True)
        try:
            yield (time.perf_counter() - started) * 1_000
        finally:
            self.release(background=True)


class _IngressServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):
        # The main Recollect server owns process signals and ordered shutdown.
        yield


class _ModelStream(StreamingResponse):
    def __init__(self, content, cleanup) -> None:
        super().__init__(content, media_type="text/event-stream")
        self.cleanup = cleanup

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.cleanup()


class ModelIngress:
    """Only the configured model's completions are reachable with this token."""

    def __init__(self, config, admission: ModelAdmission) -> None:
        self.config = config
        self.admission = admission
        self.token = secrets.token_urlsafe(32)
        self.base_url = ""
        self.client = httpx.AsyncClient(
            base_url=config.generator_base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {config.generator_api_key}"},
            timeout=httpx.Timeout(config.generator_timeout_s, connect=10),
            trust_env=False,
        )
        self.app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self.app.add_middleware(ResourceLimitsMiddleware, max_requests=16)
        self.server: _IngressServer | None = None
        self.worker: asyncio.Task | None = None
        self.measurements: deque[dict] = deque(maxlen=128)
        self._requests: set[asyncio.Task] = set()
        self._closing = False
        self._listener: socket.socket | None = None
        self._routes()

    def _authorize(self, request: Request) -> None:
        if not secrets.compare_digest(
            request.headers.get("authorization", ""), f"Bearer {self.token}"
        ):
            raise HTTPException(401, "Model admission requires its worker token.")

    def _routes(self) -> None:
        @self.app.get("/v1/models")
        async def models(request: Request):
            self._authorize(request)
            return {
                "object": "list",
                "data": [
                    {
                        "id": self.config.generator_model,
                        "object": "model",
                    }
                ],
            }

        @self.app.post("/v1/chat/completions")
        async def completion(request: Request):
            self._authorize(request)
            if self._closing:
                raise HTTPException(503, "Model admission is closing.")
            try:
                payload = await request.json()
            except ValueError:
                raise HTTPException(
                    400, "Expected a JSON completion request."
                ) from None
            if not isinstance(payload, dict) or payload.get("model") != (
                self.config.generator_model
            ):
                raise HTTPException(400, "Only the configured chat model is allowed.")
            if not isinstance(payload.get("messages"), list):
                raise HTTPException(400, "Messages must be a list.")
            # Bound each inference, never the duration of the delegated task.
            limits = [self.config.subagent_inference_tokens]
            for key in ("max_tokens", "max_completion_tokens"):
                if key in payload:
                    value = payload[key]
                    if type(value) is not int or value < 1:
                        raise HTTPException(400, f"{key} must be a positive integer.")
                    limits.append(value)
            payload["max_tokens"] = min(limits)
            payload.pop("max_completion_tokens", None)
            if payload.get("n", 1) != 1:
                raise HTTPException(
                    400, "Only one completion per request is supported."
                )
            if type(payload.get("stream", False)) is not bool:
                raise HTTPException(400, "stream must be a boolean.")
            payload["chat_template_kwargs"] = {"enable_thinking": False}
            owner = asyncio.current_task()
            if owner is not None:
                self._requests.add(owner)
            lease = False
            response: httpx.Response | None = None
            queued = time.perf_counter()
            started = queued
            prompt_tokens = 0

            async def cleanup():
                nonlocal lease
                try:
                    if response is not None:
                        await response.aclose()
                finally:
                    if lease:
                        lease = False
                        self.admission.release(background=True)
                        self.measurements.append({
                            "queue_ms": (started - queued) * 1_000,
                            "inference_ms": (time.perf_counter() - started) * 1_000,
                            "prompt_tokens": prompt_tokens,
                        })
                    if owner is not None:
                        self._requests.discard(owner)

            async def connect():
                nonlocal lease, started, queued, prompt_tokens, response
                prompt_tokens = await check_context(
                    self.client, payload, self.config.generator_context_tokens
                )
                queued = time.perf_counter()
                await self.admission.acquire(background=True)
                lease = True
                started = time.perf_counter()
                response = await self.client.send(
                    self.client.build_request(
                        "POST", "/chat/completions", json=payload
                    ),
                    stream=True,
                )

            try:
                await self._while_connected(request, connect())
                assert response is not None
                if response.is_error or not payload.get("stream", False):
                    async def read_body():
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > 2 * 1024 * 1024:
                                raise HTTPException(
                                    502, "Model response exceeded 2 MiB."
                                )
                        return bytes(body)

                    body = await self._while_connected(request, read_body())
                    output = Response(
                        body, status_code=response.status_code,
                        media_type="application/json",
                    )
                    await cleanup()
                    return output

                async def forward():
                    try:
                        async for chunk in response.aiter_bytes():
                            yield chunk
                    except httpx.HTTPError:
                        # Headers already reached the SDK. An explicit stream
                        # error prevents a truncated generation looking complete.
                        error = {"error": {
                            "message": "The model stream was interrupted.",
                            "type": "upstream_error",
                        }}
                        yield f"data: {json.dumps(error)}\n\n".encode()
                        yield b"data: [DONE]\n\n"

                return _ModelStream(forward(), cleanup)
            except BaseException as error:
                await cleanup()
                if isinstance(error, httpx.TimeoutException):
                    raise HTTPException(504, "The model request timed out.") from None
                if isinstance(error, httpx.HTTPError):
                    raise HTTPException(
                        502, "The model server is unavailable."
                    ) from None
                if isinstance(error, ValueError):
                    raise HTTPException(400, str(error)) from None
                if isinstance(error, RuntimeError):
                    raise HTTPException(503, str(error)) from None
                raise

    @staticmethod
    async def _while_connected(request: Request, operation):
        work = asyncio.create_task(operation)
        try:
            while not work.done():
                if await request.is_disconnected():
                    raise HTTPException(499, "The requesting worker disconnected.")
                await asyncio.wait({work}, timeout=0.1)
            return work.result()
        finally:
            if not work.done():
                work.cancel()
            await asyncio.gather(work, return_exceptions=True)

    async def start(self) -> None:
        if self.worker is not None or self._closing:
            raise RuntimeError("Model admission has already been started or closed.")
        root = str(self.client.base_url).rstrip("/").removesuffix("/v1")
        response = await self.client.get(f"{root}/props", timeout=10)
        response.raise_for_status()
        properties = response.json()
        slots = properties.get("total_slots")
        context = (properties.get("default_generation_settings") or {}).get("n_ctx")
        if (
            slots != self.config.generator_parallel_slots
            or self.admission.slots != slots
            or context != self.config.generator_context_tokens
        ):
            raise RuntimeError(
                "Model capacity does not match the configured profile: "
                f"expected {self.config.generator_parallel_slots} slots with "
                f"{self.config.generator_context_tokens} tokens each; "
                f"server reports {slots} slots with {context} tokens."
            )
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener = listener
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Docker must reach this port; the capability token exposes no user API.
        listener.bind(("0.0.0.0", 0))
        listener.listen(32)
        self.base_url = f"http://127.0.0.1:{listener.getsockname()[1]}/v1"
        self.server = _IngressServer(
            uvicorn.Config(
                self.app,
                access_log=False,
                log_level="error",
                lifespan="off",
            )
        )
        self.worker = asyncio.create_task(self.server.serve(sockets=[listener]))
        for _ in range(100):
            if self.worker.done():
                self.worker.result()
                raise RuntimeError("Model admission server stopped during startup.")
            if self.server.started:
                return
            await asyncio.sleep(0.01)
        await self.close()
        raise RuntimeError("Model admission server did not start.")

    async def close(self) -> None:
        self._closing = True
        self.admission.close()
        active = [task for task in self._requests if task is not asyncio.current_task()]
        for task in active:
            task.cancel()
        if active:
            done, pending = await asyncio.wait(active, timeout=5)
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, return_exceptions=True)
        if self.server:
            self.server.should_exit = True
        if self.worker:
            try:
                await asyncio.wait_for(asyncio.shield(self.worker), 5)
            except TimeoutError:
                self.worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.worker
        await self.client.aclose()
        if self._listener is not None:
            self._listener.close()
            self._listener = None
