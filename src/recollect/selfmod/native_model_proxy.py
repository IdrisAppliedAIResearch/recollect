"""Stdlib streaming model bridge embedded in the trusted namespace PID 1.

Only the host owns the framed input/output pipes. The worker can reach the
loopback HTTP handler, never those pipes. This module grants neither model
access nor settlement authority: the host broker must validate every request.
"""

import base64
import contextlib
import io
import re
import selectors
import socket
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

CHUNK_BYTES = 32768
MAX_REQUEST_BYTES = 16 * 1024 * 1024


class BridgeError(ValueError):
    pass


class _BridgeClosed(BridgeError):
    """New work was fenced; unlike an admitted I/O error, this is not a failure."""


class Exchange:
    def __init__(self, identity):
        self.identity = identity
        self._pending = None
        self._unacknowledged = None
        self._received = None
        self.closed = threading.Event()
        self.sequence = 0
        self.ended = False
        self.lock = threading.Condition()

    def close(self):
        with self.lock:
            self.closed.set()
            self._pending = None
            self.lock.notify_all()

    def deliver(self, frame):
        with self.lock:
            if (type(frame) is not dict or self.ended or self.closed.is_set()
                    or frame.get("id") != self.identity
                    or type(frame.get("sequence")) is not int
                    or frame["sequence"] != self.sequence):
                raise BridgeError("Stale or out-of-order model response")
            if self._unacknowledged is not None:
                raise BridgeError("Model response arrived before acknowledgement")
            kind = frame.get("kind")
            keys = {"kind", "id", "sequence"}
            if kind == "model_head" and self.sequence == 0:
                keys |= {"status", "content_type"}
                if (type(frame.get("status")) is not int
                        or not 200 <= frame["status"] <= 599
                        or frame["status"] in {204, 304}
                        or type(frame.get("content_type")) is not str
                        or not re.fullmatch(r"[\x20-\x7e]{1,256}",
                                            frame["content_type"])):
                    raise BridgeError("Invalid model response headers")
            elif kind == "model_chunk" and self.sequence > 0:
                keys.add("base64")
                if (type(frame.get("base64")) is not str
                        or len(frame["base64"]) > 4 * ((CHUNK_BYTES + 2) // 3)):
                    raise BridgeError("Invalid model response bytes")
                try:
                    data = base64.b64decode(frame["base64"], validate=True)
                except ValueError as exc:
                    raise BridgeError("Invalid model response bytes") from exc
                if not 0 < len(data) <= CHUNK_BYTES:
                    raise BridgeError("Model response chunk exceeds bound")
            elif kind == "model_end" and self.sequence > 0:
                pass
            else:
                raise BridgeError("Invalid model response phase")
            if set(frame) != keys:
                raise BridgeError("Unknown model response field")
            self._pending = dict(frame)
            self._unacknowledged = self.sequence
            self.sequence += 1
            self.ended = kind == "model_end"
            self.lock.notify()

    def receive(self):
        with self.lock:
            while self._pending is None and not self.closed.is_set():
                self.lock.wait()
            if self.closed.is_set():
                raise BridgeError("Model exchange was closed")
            frame, self._pending = self._pending, None
            self._received = frame["sequence"]
            return frame

    def acknowledge(self, frame):
        with self.lock:
            if (self.closed.is_set() or self._received != frame["sequence"]
                    or self._unacknowledged != frame["sequence"]):
                raise BridgeError("Invalid model acknowledgement")
            self._unacknowledged = self._received = None


class ModelBridge:
    """One conversation, one admitted HTTP connection, permanent terminal state.

    failed is a sticky supervisor-observed Event, including interrupted admitted
    work. Idle close and close after explicit successful completion stay clean.
    close() synchronously shuts down admitted/listening sockets and fences new
    emission; it never joins an HTTP thread or waits for the supplied writer.
    A writer call already entered belongs to the supervisor's pipe lifecycle.
    """

    def __init__(self, emit):
        self._emit = emit
        self._state = threading.Lock()
        self._emission = threading.Lock()
        self._active = None
        self._completed = True
        self._closed = False
        self._sockets = set()
        self._server = None
        self.failed = threading.Event()

    def _check(self):
        with self._state:
            if self._closed:
                raise _BridgeClosed("Model bridge closed")

    def emit(self, value):
        # Never hold the state/response-credit locks across a pipe write. The
        # control reader must still be able to reject frames and fence sockets.
        with self._emission:
            self._check()
            try:
                self._emit(value)
            except BaseException:
                self.fail()
                raise

    def deliver(self, frame):
        try:
            with self._state:
                active = self._active
                if self._closed:
                    raise _BridgeClosed("No owned model exchange")
                if active is None:
                    raise BridgeError("No owned model exchange")
            active.deliver(frame)
        except _BridgeClosed:
            raise
        except BridgeError:
            self.fail()
            raise

    def fail(self):
        with self._state:
            # Previously admitted I/O may report its error after a fence won the
            # race. A terminal close must never erase that later failure report.
            self.failed.set()
            self._close_locked()

    def close(self):
        with self._state:
            if not self._completed:
                self.failed.set()
            self._close_locked()

    def _complete(self, exchange):
        with self._state:
            if self._closed:
                raise _BridgeClosed("Model bridge closed")
            if self._active is not exchange:
                raise BridgeError("No owned model exchange to complete")
            # The terminator was written and end ACK emitted. Publish completion
            # before model_closed, whose reader may immediately fence this bridge.
            self._completed = True

    def _request_finished(self):
        with self._state:
            if not self._completed:
                self.failed.set()
                self._close_locked()

    def _close_locked(self):
        self._closed = True
        if self._active is not None:
            self._active.close()
        for connection in self._sockets:
            _close_socket(connection)
        if self._server is not None:
            self._server.fence()

    def _admit(self, connection):
        with self._state:
            if self._closed or self._sockets:
                _close_socket(connection)
                raise BridgeError("Model HTTP connection admission is closed or busy")
            self._sockets.add(connection)
            self._completed = False

    def _release(self, connection):
        with self._state:
            self._sockets.discard(connection)

    def serve(self, handler):
        # Never queue root handler threads behind a conversation-level lock.
        try:
            with self._state:
                if self._closed:
                    raise _BridgeClosed("Model bridge closed")
                if self._active is not None:
                    raise BridgeError("Overlapping model exchange")
                exchange = Exchange(uuid.uuid4().hex)
                self._active = exchange
                self._completed = False
        except _BridgeClosed:
            raise
        except BridgeError:
            self.fail()
            raise
        try:
            self._request(handler, exchange)
            frame = exchange.receive()
            self._check()
            handler.send_response(frame["status"])
            handler.send_header("Content-Type", frame["content_type"])
            handler.send_header("Transfer-Encoding", "chunked")
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.flush()
            self._ack(exchange, frame)
            while True:
                frame = exchange.receive()
                self._check()
                if frame["kind"] == "model_end":
                    # A closed socket is truncation. Only authenticated model_end
                    # permits the HTTP chunk terminator that denotes completion.
                    handler.wfile.write(b"0\r\n\r\n")
                    handler.wfile.flush()
                    self._ack(exchange, frame)
                    self._complete(exchange)
                    self.emit({"kind": "model_closed", "id": exchange.identity,
                               "response_delivered": True})
                    return
                data = base64.b64decode(frame["base64"], validate=True)
                handler.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                handler.wfile.flush()
                self._ack(exchange, frame)
        except _BridgeClosed:
            raise
        except BaseException:
            self.fail()
            raise
        finally:
            exchange.close()
            with self._state:
                if self._active is exchange:
                    self._active = None

    def _ack(self, exchange, frame):
        self._check()
        # Grant credit after the HTTP write and before publishing its ACK, so a
        # host that immediately answers the ACK cannot race a delayed unlock.
        exchange.acknowledge(frame)
        self.emit({"kind": "model_ack", "id": exchange.identity,
                   "sequence": frame["sequence"]})

    def _request(self, handler, exchange):
        lengths = handler.headers.get_all("Content-Length") or []
        if (handler.path != "/v1/chat/completions"
                or handler.headers.get_all("Transfer-Encoding")
                or len(lengths) != 1
                or not re.fullmatch(r"[0-9]{1,9}", lengths[0])):
            raise BridgeError("Only bounded chat-completions requests are allowed")
        remaining = int(lengths[0])
        if not 0 < remaining <= MAX_REQUEST_BYTES:
            raise BridgeError("Native model request exceeds bound")
        self.emit({"kind": "model_request", "id": exchange.identity,
                   "bytes": remaining})
        sequence = 0
        while remaining:
            data = handler.rfile.read(min(remaining, CHUNK_BYTES))
            if not data:
                raise BridgeError("Truncated native model request")
            self.emit({"kind": "model_request_chunk", "id": exchange.identity,
                       "sequence": sequence,
                       "base64": base64.b64encode(data).decode("ascii")})
            remaining -= len(data)
            sequence += 1
        self.emit({"kind": "model_request_end", "id": exchange.identity,
                   "chunks": sequence})


class ModelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        self.connection = self.request
        self.connection.setblocking(False)
        self.rfile = io.BufferedReader(
            _SocketInput(self.connection, self.server.bridge),
        )
        self.wfile = _SocketOutput(self.connection, self.server.bridge)

    def log_message(self, *args):
        pass

    def handle(self):
        try:
            self.handle_one_request()
        except _BridgeClosed:
            pass
        except BaseException:
            self.server.bridge.fail()
            raise
        finally:
            self.server.bridge._request_finished()

    def send_error(self, *args, **kwargs):
        self.server.bridge.fail()
        raise BridgeError("Invalid native model HTTP request")

    def handle_expect_100(self):
        self.send_error()

    def do_POST(self):
        self.close_connection = True
        try:
            self.server.bridge.serve(self)
        except (BridgeError, OSError):
            # Partial HTTP output is not replaced by a fabricated completed reply.
            self.close_connection = True


def _close_socket(connection):
    with contextlib.suppress(OSError):
        connection.shutdown(socket.SHUT_RDWR)
    connection.close()


def _ready(connection, bridge, event):
    # Windows buffered blocking recv/send may survive shutdown in another thread.
    # Nonblocking syscalls plus terminal polling never impose a work timeout.
    with selectors.DefaultSelector() as selector:
        bridge._check()
        selector.register(connection, event)
        while True:
            bridge._check()
            if selector.select(0.1):
                return


class _SocketInput(io.RawIOBase):
    def __init__(self, connection, bridge):
        self.connection, self.bridge = connection, bridge

    def readable(self):
        return True

    def readinto(self, buffer):
        while True:
            self.bridge._check()
            try:
                return self.connection.recv_into(buffer)
            except BlockingIOError:
                _ready(self.connection, self.bridge, selectors.EVENT_READ)


class _SocketOutput(io.RawIOBase):
    def __init__(self, connection, bridge):
        self.connection, self.bridge = connection, bridge

    def writable(self):
        return True

    def write(self, data):
        remaining = memoryview(data)
        while remaining:
            self.bridge._check()
            try:
                sent = self.connection.send(remaining)
            except BlockingIOError:
                _ready(self.connection, self.bridge, selectors.EVENT_WRITE)
                continue
            if not sent:
                raise BridgeError("Native model socket disconnected during write")
            remaining = remaining[sent:]
        return len(data)


class ModelHTTPServer(HTTPServer):
    """One handler thread total; a bounded kernel backlog replaces thread queues."""

    request_queue_size = 1

    def __init__(self, bridge, port):
        self.bridge = bridge
        self._stopped = threading.Event()
        super().__init__(("127.0.0.1", port), ModelHandler)
        with bridge._state:
            if bridge._closed or bridge._server is not None:
                super().server_close()
                raise BridgeError("Model bridge already has a listener or is closed")
            bridge._server = self

    def get_request(self):
        connection, address = super().get_request()
        try:
            self.bridge._admit(connection)
        except BridgeError as exc:
            raise OSError("Model bridge admission is closed") from exc
        return connection, address

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            self.bridge._release(request)

    def handle_error(self, request, client_address):
        self.bridge.fail()

    def serve_forever(self, poll_interval=0.1):
        # This poll observes an explicit fence only; it imposes no work deadline.
        # No shutdown()/join wait is needed to interrupt an incomplete request.
        try:
            with selectors.DefaultSelector() as selector:
                if self._stopped.is_set():
                    return
                selector.register(self, selectors.EVENT_READ)
                while not self._stopped.is_set():
                    ready = selector.select(poll_interval)
                    if self._stopped.is_set():
                        return
                    if ready:
                        self._handle_request_noblock()
        except (OSError, ValueError):
            if not self._stopped.is_set():
                self.bridge.fail()
                raise

    def fence(self):
        self._stopped.set()
        _close_socket(self.socket)

    def shutdown(self):
        self.bridge.close()

    def server_close(self):
        self.bridge.close()
        super().server_close()


def listener(bridge, *, port=4097):
    return ModelHTTPServer(bridge, port)
