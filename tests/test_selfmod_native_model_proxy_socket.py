"""Loopback-only proxy fixtures; no Docker, external services or model calls."""

import contextlib
import http.client
import queue
import socket
import threading
from types import SimpleNamespace

import pytest

from recollect.selfmod import native_model_proxy as proxy
from tests.test_selfmod_native_model_proxy import chunk, head


@pytest.fixture
def live(monkeypatch):
    emitted = queue.Queue()
    accepted = threading.Event()
    bridge = proxy.ModelBridge(emitted.put)
    server = proxy.listener(bridge, port=0)
    admit = bridge._admit

    def observe(connection):
        admit(connection)
        accepted.set()

    monkeypatch.setattr(bridge, "_admit", observe)
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="native-model-proxy-test")
    thread.start()
    fixture = SimpleNamespace(bridge=bridge, server=server, thread=thread,
                              emitted=emitted, accepted=accepted,
                              address=server.server_address)
    try:
        yield fixture
    finally:
        bridge.close()
        thread.join(3)
        server.server_close()
        assert not thread.is_alive(), "Owned HTTP handler failed to stop"


def request_id(live):
    start = live.emitted.get(timeout=3)
    assert start["kind"] == "model_request"
    identity = start["id"]
    assert live.emitted.get(timeout=3)["kind"] == "model_request_chunk"
    assert live.emitted.get(timeout=3)["kind"] == "model_request_end"
    return identity


def deliver(live, frame):
    live.bridge.deliver(frame)
    assert live.emitted.get(timeout=3) == {
        "kind": "model_ack", "id": frame["id"], "sequence": frame["sequence"],
    }


def assert_disconnected(client):
    # Closing with unread request bytes may send RST rather than FIN on Windows.
    with contextlib.suppress(ConnectionResetError):
        assert client.recv(1024) == b""


@pytest.mark.parametrize("complete", [False, True])
def test_real_http_client_distinguishes_truncation_from_model_end(live, complete):
    connection = http.client.HTTPConnection(*live.address, timeout=3)
    try:
        connection.request("POST", "/v1/chat/completions", body=b"{}")
        identity = request_id(live)
        deliver(live, head(identity))
        response = connection.getresponse()
        assert response.version == 11 and response.chunked
        assert response.getheader("Content-Length") is None
        # Even a provider's SSE sentinel cannot substitute for host model_end.
        payload = b"data: [DONE]\n\n"
        deliver(live, chunk(identity, data=payload))
        if complete:
            deliver(live, {"kind": "model_end", "id": identity, "sequence": 2})
            assert response.read() == payload
            assert live.emitted.get(timeout=3)["response_delivered"] is True
            assert not live.bridge.failed.is_set()
        else:
            with pytest.raises(proxy.BridgeError):
                live.bridge.deliver(chunk(identity, sequence=99))
            assert live.bridge.failed.is_set()
            with pytest.raises(http.client.IncompleteRead) as failure:
                response.read()
            assert failure.value.partial == payload
            assert live.emitted.empty()
    finally:
        connection.close()


@pytest.mark.parametrize("phase", ["headers", "body", "response"])
def test_terminal_fence_interrupts_owned_socket_in_every_read_phase(live, phase):
    with socket.create_connection(live.address, timeout=3) as client:
        if phase == "headers":
            client.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nContent-Len")
        elif phase == "body":
            client.sendall(b"POST /v1/chat/completions HTTP/1.1\r\n"
                           b"Content-Length: 100\r\n\r\n{")
            assert live.emitted.get(timeout=3)["kind"] == "model_request"
        else:
            client.sendall(b"POST /v1/chat/completions HTTP/1.1\r\n"
                           b"Content-Length: 2\r\n\r\n{}")
            request_id(live)
        assert live.accepted.wait(3)
        live.bridge.close()
        live.thread.join(3)
        assert not live.thread.is_alive()
        assert live.bridge.failed.is_set()
        assert_disconnected(client)
        assert live.emitted.empty()
        assert not live.bridge._sockets


def test_terminal_fence_interrupts_blocked_socket_write(live, monkeypatch):
    admit = live.bridge._admit

    def small_buffer(connection):
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        admit(connection)

    monkeypatch.setattr(live.bridge, "_admit", small_buffer)
    with socket.socket() as client:
        client.settimeout(3)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        client.connect(live.address)
        client.sendall(b"POST /v1/chat/completions HTTP/1.1\r\n"
                       b"Content-Length: 2\r\n\r\n{}")
        identity = request_id(live)
        deliver(live, head(identity))
        for sequence in range(1, 257):
            live.bridge.deliver(chunk(identity, sequence, b"x" * proxy.CHUNK_BYTES))
            try:
                ack = live.emitted.get(timeout=0.1)
            except queue.Empty:
                break
            assert ack["kind"] == "model_ack" and ack["sequence"] == sequence
        else:
            pytest.fail("Fixture failed to saturate the deliberately small socket")
        assert live.bridge._active._unacknowledged == sequence
        live.bridge.close()
        live.thread.join(3)
        assert not live.thread.is_alive()
        assert live.emitted.empty()
        assert live.bridge.failed.is_set()
        assert not live.bridge._sockets


def test_stalled_connection_does_not_create_root_thread_queue(live, monkeypatch):
    handled = []
    original = proxy.ModelHandler.handle

    def observe(handler):
        handled.append(threading.get_ident())
        return original(handler)

    monkeypatch.setattr(proxy.ModelHandler, "handle", observe)
    with socket.create_connection(live.address, timeout=3) as first:
        first.sendall(b"POST /v1/chat/completions HTTP/1.1\r\n")
        assert live.accepted.wait(3)
        with socket.create_connection(live.address, timeout=3) as second:
            second.sendall(b"POST /v1/chat/completions HTTP/1.1\r\n"
                           b"Content-Length: 2\r\n\r\n{}")
            assert len(live.bridge._sockets) == 1
            live.bridge.close()
            live.thread.join(3)
        assert handled == [live.thread.ident]
        assert live.emitted.empty()
        assert live.bridge.failed.is_set()
        assert live.server.request_queue_size == 1


def test_client_truncated_body_sets_terminal_failure_before_control_eof(live):
    with socket.create_connection(live.address, timeout=3) as client:
        client.sendall(b"POST /v1/chat/completions HTTP/1.1\r\n"
                       b"Content-Length: 100\r\n\r\n{")
        assert live.emitted.get(timeout=3)["kind"] == "model_request"
        client.shutdown(socket.SHUT_WR)
        assert live.bridge.failed.wait(3)
        live.thread.join(3)
        assert not live.thread.is_alive()
        assert_disconnected(client)
        before = live.emitted.qsize()
        with pytest.raises(proxy.BridgeError, match="closed"):
            live.bridge.emit({"kind": "must_not_emit"})
        assert live.emitted.qsize() == before


def test_close_from_http_emitter_does_not_join_itself(live, monkeypatch):
    output = live.bridge._emit

    def close_on_request(value):
        output(value)
        live.bridge.close()

    monkeypatch.setattr(live.bridge, "_emit", close_on_request)
    with socket.create_connection(live.address, timeout=3) as client:
        client.sendall(b"POST /v1/chat/completions HTTP/1.1\r\n"
                       b"Content-Length: 2\r\n\r\n{}")
        assert live.emitted.get(timeout=3)["kind"] == "model_request"
        live.thread.join(3)
        assert not live.thread.is_alive()
        assert_disconnected(client)
        assert live.bridge.failed.is_set()


def test_sequential_connections_reuse_conversation_bridge(live):
    identities = []
    for _ in range(3):
        connection = http.client.HTTPConnection(*live.address, timeout=3)
        try:
            connection.request("POST", "/v1/chat/completions", body=b"{}")
            identity = request_id(live)
            identities.append(identity)
            deliver(live, head(identity))
            deliver(live, {"kind": "model_end", "id": identity, "sequence": 1})
            assert connection.getresponse().read() == b""
            assert live.emitted.get(timeout=3)["response_delivered"] is True
        finally:
            connection.close()
    assert len(set(identities)) == 3
    assert not live.bridge.failed.is_set()


def test_idle_listener_close_is_clean(live):
    live.bridge.close()
    live.thread.join(3)
    assert not live.thread.is_alive()
    assert not live.bridge.failed.is_set()
    assert live.emitted.empty()


def test_immediate_fence_on_model_closed_is_clean_with_owned_socket(live, monkeypatch):
    output = live.bridge._emit

    def fence_on_completion(frame):
        if frame["kind"] == "model_closed":
            assert live.bridge._active is not None and live.bridge._completed
            live.bridge.close()
            assert not live.bridge.failed.is_set()
        output(frame)

    monkeypatch.setattr(live.bridge, "_emit", fence_on_completion)
    connection = http.client.HTTPConnection(*live.address, timeout=3)
    try:
        connection.request("POST", "/v1/chat/completions", body=b"{}")
        identity = request_id(live)
        deliver(live, head(identity))
        response = connection.getresponse()
        deliver(live, {"kind": "model_end", "id": identity, "sequence": 1})
        assert live.emitted.get(timeout=3)["response_delivered"] is True
        assert response.read() == b""
        live.thread.join(3)
        assert not live.thread.is_alive()
        assert not live.bridge.failed.is_set()
    finally:
        connection.close()
