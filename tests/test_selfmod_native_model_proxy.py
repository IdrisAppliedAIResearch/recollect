import base64
import io
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from types import SimpleNamespace

import pytest

from recollect.selfmod import native_model_proxy as proxy


def head(identity="owned", sequence=0):
    return {"kind": "model_head", "id": identity, "sequence": sequence,
            "status": 200, "content_type": "text/event-stream"}


def chunk(identity="owned", sequence=1, data=b"reply"):
    return {"kind": "model_chunk", "id": identity, "sequence": sequence,
            "base64": base64.b64encode(data).decode()}


def handler(body=b'{"model":"fixture"}'):
    headers = Message()
    headers["Content-Length"] = str(len(body))
    status, response_headers = [], []
    return SimpleNamespace(
        path="/v1/chat/completions", headers=headers, rfile=io.BytesIO(body),
        wfile=io.BytesIO(), send_response=status.append,
        send_header=lambda k, v: response_headers.append((k, v)),
        end_headers=lambda: None, status=status, response_headers=response_headers,
    )


def test_streamed_exchange_acknowledges_each_delivered_chunk():
    emitted = queue.Queue()
    bridge = proxy.ModelBridge(emitted.put)
    request = handler()
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(bridge.serve, request)
        start = emitted.get(timeout=5)
        identity = start["id"]
        body = emitted.get(timeout=5)
        assert base64.b64decode(body["base64"]) == b'{"model":"fixture"}'
        assert emitted.get(timeout=5) == {
            "kind": "model_request_end", "id": identity, "chunks": 1,
        }
        frames = [head(identity), *(chunk(identity, n, b"x" * proxy.CHUNK_BYTES)
                                   for n in range(1, 6)),
                  {"kind": "model_end", "id": identity, "sequence": 6}]
        for frame in frames:
            bridge.deliver(frame)
            assert emitted.get(timeout=5) == {
                "kind": "model_ack", "id": identity, "sequence": frame["sequence"],
            }
        running.result(timeout=5)
    expected = (f"{proxy.CHUNK_BYTES:x}\r\n".encode()
                + b"x" * proxy.CHUNK_BYTES + b"\r\n") * 5 + b"0\r\n\r\n"
    assert request.wfile.getvalue() == expected
    assert request.status == [200]
    assert ("Transfer-Encoding", "chunked") in request.response_headers
    assert emitted.get(timeout=5) == {
        "kind": "model_closed", "id": identity, "response_delivered": True,
    }
    with pytest.raises(proxy.BridgeError, match="No owned"):
        bridge.deliver(head(identity))


def test_response_queue_rejects_unacknowledged_sender_without_blocking():
    exchange = proxy.Exchange("owned")
    exchange.deliver(head())
    with pytest.raises(proxy.BridgeError, match="before acknowledgement"):
        exchange.deliver(chunk())
    assert exchange.receive() == head()
    # Removing the buffered frame does not grant credit while HTTP is writing it.
    with pytest.raises(proxy.BridgeError, match="before acknowledgement"):
        exchange.deliver(chunk())
    exchange.acknowledge(head())
    exchange.deliver(chunk())
    assert exchange.receive() == chunk()
    exchange.close()
    with pytest.raises(proxy.BridgeError, match="closed"):
        exchange.receive()


@pytest.mark.parametrize("frame", [
    None, {}, {**head(), "id": "foreign"}, {**head(), "sequence": True},
    {**head(), "sequence": 1}, {**head(), "status": True},
    {**head(), "status": 101}, {**head(), "content_type": "a\r\nb: c"},
    {**head(), "status": 204}, {**head(), "status": 304},
    {**head(), "extra": 1}, chunk(),
    {"kind": "model_end", "id": "owned", "sequence": 0},
])
def test_invalid_first_response_frame_rejected(frame):
    with pytest.raises(proxy.BridgeError):
        proxy.Exchange("owned").deliver(frame)


@pytest.mark.parametrize("frame", [
    head(sequence=1), chunk(sequence=2), {**chunk(), "base64": "!"},
    chunk(data=b""), chunk(data=b"x" * (proxy.CHUNK_BYTES + 1)),
    {**chunk(), "extra": 1}, {**chunk(), "sequence": 1.0},
])
def test_invalid_followup_response_rejected(frame):
    exchange = proxy.Exchange("owned")
    exchange.deliver(head())
    assert exchange.receive() == head()
    exchange.acknowledge(head())
    with pytest.raises(proxy.BridgeError):
        exchange.deliver(frame)


@pytest.mark.parametrize("fault", ["path", "missing", "duplicate", "negative",
                                  "transfer", "large", "truncated"])
def test_invalid_http_request_never_reports_completion(fault):
    request = handler()
    if fault == "path":
        request.path = "http://host.docker.internal/v1/chat/completions"
    elif fault == "missing":
        del request.headers["Content-Length"]
    elif fault == "duplicate":
        request.headers["Content-Length"] = "1"
    elif fault == "transfer":
        request.headers["Transfer-Encoding"] = "chunked"
    elif fault == "truncated":
        request.rfile = io.BytesIO(b"")
    else:
        request.headers.replace_header("Content-Length", "-1" if fault == "negative"
                                       else str(proxy.MAX_REQUEST_BYTES + 1))
    emitted = []
    bridge = proxy.ModelBridge(emitted.append)
    with pytest.raises(proxy.BridgeError):
        bridge.serve(request)
    assert bridge.failed.is_set()
    assert not any(f["kind"] == "model_closed" for f in emitted)
    before = list(emitted)
    with pytest.raises(proxy.BridgeError, match="closed"):
        bridge.serve(handler())
    assert emitted == before


def test_closed_bridge_cannot_restart_or_accept_responses():
    bridge = proxy.ModelBridge(lambda _: None)
    bridge.close()
    assert not bridge.failed.is_set()
    with pytest.raises(proxy.BridgeError, match="closed"):
        bridge.serve(handler())
    with pytest.raises(proxy.BridgeError, match="No owned"):
        bridge.deliver(head())
    assert not bridge.failed.is_set()


def test_disconnect_does_not_report_success():
    emitted = queue.Queue()
    bridge = proxy.ModelBridge(emitted.put)
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(bridge.serve, handler())
        for _ in range(3):
            emitted.get(timeout=5)
        bridge.close()
        with pytest.raises(proxy.BridgeError, match="closed"):
            running.result(timeout=5)
    assert emitted.empty()
    assert bridge.failed.is_set()


def test_control_delivery_never_waits_for_blocked_host_writer():
    entered, release = threading.Event(), threading.Event()
    emitted = []

    def blocked_emit(frame):
        emitted.append(frame)
        entered.set()
        assert release.wait(5)

    bridge = proxy.ModelBridge(blocked_emit)
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(bridge.serve, handler())
        try:
            assert entered.wait(5)
            identity = emitted[0]["id"]
            bridge.deliver(head(identity))
            with pytest.raises(proxy.BridgeError, match="before acknowledgement"):
                bridge.deliver(chunk(identity))
            assert bridge.failed.is_set()
            bridge.close()
        finally:
            release.set()
        with pytest.raises(proxy.BridgeError, match="closed"):
            running.result(timeout=5)
    assert len(emitted) == 1


def test_overlap_fails_immediately_and_permanently_fences_conversation():
    emitted = queue.Queue()
    bridge = proxy.ModelBridge(emitted.put)
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(bridge.serve, handler())
        for _ in range(3):
            emitted.get(timeout=5)
        with pytest.raises(proxy.BridgeError, match="Overlapping"):
            bridge.serve(handler())
        assert bridge.failed.is_set()
        with pytest.raises(proxy.BridgeError, match="closed"):
            running.result(timeout=5)
        with pytest.raises(proxy.BridgeError, match="closed"):
            bridge.serve(handler())
    assert emitted.empty()


def test_failed_host_writer_latches_failure_and_cannot_emit_again():
    calls = []

    def broken_emit(frame):
        calls.append(frame)
        raise OSError("closed host pipe")

    bridge = proxy.ModelBridge(broken_emit)
    with pytest.raises(OSError, match="closed host pipe"):
        bridge.serve(handler())
    assert bridge.failed.is_set()
    with pytest.raises(proxy.BridgeError, match="closed"):
        bridge.serve(handler())
    bridge.close()
    assert bridge.failed.is_set() and len(calls) == 1


def test_deliberate_close_does_not_wait_for_blocked_emitter_or_allow_more_emission():
    entered, release = threading.Event(), threading.Event()
    emitted = []

    def blocked_emit(frame):
        emitted.append(frame)
        entered.set()
        assert release.wait(5)

    bridge = proxy.ModelBridge(blocked_emit)
    with ThreadPoolExecutor(2) as pool:
        running = pool.submit(bridge.serve, handler())
        try:
            assert entered.wait(5)
            # This must finish while the admitted emitter call is still blocked.
            closing = pool.submit(bridge.close)
            closing.result(timeout=1)
            assert not release.is_set() and bridge.failed.is_set()
            bridge.close()
        finally:
            release.set()
        with pytest.raises(proxy.BridgeError, match="closed"):
            running.result(timeout=5)
    assert len(emitted) == 1
    with pytest.raises(proxy.BridgeError, match="closed"):
        bridge.emit({"kind": "must_not_emit"})


def test_delayed_actual_io_failure_survives_close_winning_the_state_race(monkeypatch):
    failing, release = threading.Event(), threading.Event()
    bridge = proxy.ModelBridge(lambda _: None)
    original = bridge.fail

    def delayed_failure():
        failing.set()
        assert release.wait(5)
        original()

    def read_error(size):
        raise OSError("admitted read failed")

    monkeypatch.setattr(bridge, "fail", delayed_failure)
    request = handler()
    request.rfile = SimpleNamespace(read=read_error)
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(bridge.serve, request)
        try:
            assert failing.wait(5)
            bridge.close()
            assert bridge.failed.is_set()
        finally:
            release.set()
        with pytest.raises(OSError, match="admitted read failed"):
            running.result(timeout=5)
    bridge.close()
    assert bridge.failed.is_set()


@pytest.mark.parametrize("late_error", [False, True])
def test_close_inside_model_closed_publication_uses_explicit_completion(late_error):
    emitted = queue.Queue()
    publishing, release = threading.Event(), threading.Event()

    def emit(frame):
        if frame["kind"] == "model_closed":
            # Exercise the window before serve() clears _active in its finally.
            assert bridge._active is not None and bridge._completed
            bridge.close()
            assert not bridge.failed.is_set()
            publishing.set()
            assert release.wait(5)
            if late_error:
                raise OSError("admitted publication failed after close")
        emitted.put(frame)

    bridge = proxy.ModelBridge(emit)
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(bridge.serve, handler())
        identity = emitted.get(timeout=5)["id"]
        for _ in range(2):
            emitted.get(timeout=5)
        for frame in (head(identity),
                      {"kind": "model_end", "id": identity, "sequence": 1}):
            bridge.deliver(frame)
            assert emitted.get(timeout=5)["kind"] == "model_ack"
        try:
            assert publishing.wait(5)
            assert not bridge.failed.is_set()
        finally:
            release.set()
        if late_error:
            with pytest.raises(OSError, match="publication failed after close"):
                running.result(timeout=5)
            assert bridge.failed.is_set()
            assert emitted.empty()
        else:
            running.result(timeout=5)
            assert emitted.get(timeout=5)["response_delivered"] is True
            assert not bridge.failed.is_set()
    bridge.close()
    assert bridge.failed.is_set() is late_error


def test_accepted_model_end_is_not_success_until_http_delivery_finishes():
    writing_end, release = threading.Event(), threading.Event()
    emitted = queue.Queue()
    bridge = proxy.ModelBridge(emitted.put)
    request = handler()

    class PendingEnd(io.BytesIO):
        def write(self, data):
            assert data == b"0\r\n\r\n"
            writing_end.set()
            assert release.wait(5)
            bridge._check()
            return super().write(data)

    request.wfile = PendingEnd()
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(bridge.serve, request)
        identity = emitted.get(timeout=5)["id"]
        for _ in range(2):
            emitted.get(timeout=5)
        bridge.deliver(head(identity))
        assert emitted.get(timeout=5)["kind"] == "model_ack"
        bridge.deliver({"kind": "model_end", "id": identity, "sequence": 1})
        try:
            assert writing_end.wait(5)
            assert bridge._active.ended and not bridge._completed
            bridge.close()
            assert bridge.failed.is_set()
        finally:
            release.set()
        with pytest.raises(proxy.BridgeError, match="closed"):
            running.result(timeout=5)
    assert emitted.empty() and not request.wfile.getvalue()
