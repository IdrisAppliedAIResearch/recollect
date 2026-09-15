"""Host provider relay: a sandboxed subagent's only route to its test calendar.

The relay listens beside the model ingress and accepts one bearer token that
grants no Google credential. It exposes Calendar-API-shaped event routes on a
stable alias, maps them to the private calendar through ``ProviderBroker`` and
forwards with the currently bound worker capability. Workers read the frozen
action identity from ``/recollect/action`` so repeated dispatches reuse one
provider event ID. The host, not the worker, supplies the invocation identity
recorded with each operation. Refusals are journaled as violations. There is
no request timeout: slow provider work is observed, never cut off.
"""

import asyncio
import contextlib
import json
import re
import secrets
import socket

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from ..limits import ResourceLimitsMiddleware
from .journal import IntegrityError
from .provider_broker import MAX_BODY_BYTES, Capability, ProviderBroker

EVENT_ID = r"[A-Za-z0-9_-]{1,1024}"


class _RelayServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):
        # The trial process owns signals and ordered shutdown.
        yield


class ProviderRelay:
    def __init__(self, broker, *, alias, invocation_for=None):
        if type(broker) is not ProviderBroker:
            raise IntegrityError("The relay forwards only through a provider broker")
        if (type(alias) is not str
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", alias)):
            raise ValueError("Freeze a stable public calendar alias")
        self.broker, self.alias = broker, alias
        # invocation_for(task_id) returns the host-observed running tool invocation.
        self._invocation_for = invocation_for or (lambda task_id: None)
        self.token = secrets.token_urlsafe(32)
        self.base_url = ""
        self._capability = None
        self._phase, self._invocation_override = "original", None
        self._server = self._worker = self._listener = None
        self.app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        # Bodies are bounded by size only; amendment 02 forbids an upload timer.
        self.app.add_middleware(ResourceLimitsMiddleware,
                                max_body_bytes=MAX_BODY_BYTES, max_requests=16,
                                upload_timeout=None)
        self._routes()

    # -- host lifecycle ----------------------------------------------------

    def bind(self, capability, *, phase="original", invocation_id=None):
        """Serve one worker action; the broker still enforces its gate.

        A harness-driven replay binds ``phase="replay"`` with the host-assigned
        invocation identity of the call it re-executes.
        """
        if type(capability) is not Capability or capability.principal != "worker":
            raise IntegrityError("The relay serves only a worker capability")
        if phase not in {"original", "replay"} or (
                invocation_id is not None and phase != "replay"):
            raise IntegrityError("Only a replay binding fixes its invocation")
        self._capability, self._phase = capability, phase
        self._invocation_override = invocation_id
        self.broker._record("relay_bound", {
            "action_id": capability.action_id, "task_id": capability.task_id,
            "routing_epoch": capability.routing_epoch,
            "serving_digest": capability.serving_digest, "phase": phase,
            "invocation_id": invocation_id})

    def unbind(self, reason):
        previous, self._capability = self._capability, None
        self.broker._record("relay_unbound", {
            "reason": reason,
            "action_id": previous.action_id if previous is not None else None})

    # -- worker routes -----------------------------------------------------

    def _authorize(self, request):
        if not secrets.compare_digest(request.headers.get("authorization", ""),
                                      "Bearer " + self.token):
            raise HTTPException(401, "Provider relay requires its worker token.")

    async def _refuse(self, status, reason, request, path):
        await asyncio.to_thread(self.broker._record, "relay_refused", {
            "status": status, "reason": reason, "method": request.method,
            "path": path, "action_id": (self._capability.action_id
                                        if self._capability is not None else None)})
        return JSONResponse({"error": {"code": status, "message": reason}},
                            status_code=status)

    def _routes(self):
        prefix = "/calendar/v3/calendars/{alias}/events"

        @self.app.get("/recollect/action")
        async def action(request: Request):
            self._authorize(request)
            capability = self._capability
            if capability is None:
                return await self._refuse(409, "No calendar action is active.",
                                          request, "/recollect/action")
            return {"action_id": capability.action_id,
                    "event_id": capability.dedup_id, "calendar": self.alias}

        @self.app.api_route(prefix, methods=["GET", "POST"])
        async def events(alias: str, request: Request):
            return await self._forward(request, alias, None)

        @self.app.get(prefix + "/{event_id}")
        async def event(alias: str, event_id: str, request: Request):
            return await self._forward(request, alias, event_id)

    async def _forward(self, request, alias, event_id):
        self._authorize(request)
        shown = request.url.path
        if alias != self.alias:
            return await self._refuse(404, "Unknown calendar alias.", request, shown)
        if event_id is not None and not re.fullmatch(EVENT_ID, event_id):
            return await self._refuse(400, "Invalid event identifier.", request, shown)
        capability = self._capability
        if capability is None:
            return await self._refuse(409, "No calendar action is active.",
                                      request, shown)
        body = None
        if request.method == "POST":
            try:
                body = json.loads(await request.body() or b"null")
            except ValueError:
                return await self._refuse(400, "Expected a JSON event body.",
                                          request, shown)
            if type(body) is not dict:
                return await self._refuse(400, "Expected a JSON event body.",
                                          request, shown)
        path = self.broker.policy.events_path + (
            "/" + event_id if event_id is not None else "")
        try:
            status, payload = await self.broker.request(
                capability, request.method, path, params=dict(request.query_params),
                body=body, phase=self._phase,
                invocation_id=(self._invocation_override
                               or self._invocation_for(capability.task_id)))
        except IntegrityError as error:
            return await self._refuse(403, str(error), request, shown)
        except httpx.HTTPError as error:
            # The broker journaled the uncertain operation; the worker must
            # reconcile with a read before any further create.
            return JSONResponse({"error": {"code": 502, "message": (
                "Provider response was not received: " + type(error).__name__)}},
                status_code=502)
        if payload is None:
            return Response(status_code=status)
        return JSONResponse(payload, status_code=status)

    # -- server --------------------------------------------------------------

    async def start(self):
        if self._worker is not None:
            raise RuntimeError("Provider relay already started")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Containers reach it through host.docker.internal, like the model ingress.
        listener.bind(("0.0.0.0", 0))
        listener.listen(16)
        self._listener = listener
        self.base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        self._server = _RelayServer(uvicorn.Config(
            self.app, access_log=False, log_level="error", lifespan="off"))
        self._worker = asyncio.create_task(self._server.serve(sockets=[listener]))
        while not self._server.started:
            if self._worker.done():
                self._worker.result()
                raise RuntimeError("Provider relay stopped during startup")
            await asyncio.sleep(0.01)

    async def close(self):
        if self._server is not None:
            self._server.should_exit = True
        if self._worker is not None:
            await asyncio.gather(self._worker, return_exceptions=True)
        if self._listener is not None:
            self._listener.close()
            self._listener = None
