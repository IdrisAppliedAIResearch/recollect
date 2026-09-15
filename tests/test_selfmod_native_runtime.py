"""Host supervisor channel and native handoff; in-memory pipes, no Docker/models."""

import asyncio
import base64
import json
from dataclasses import asdict, replace

import httpx
import pytest

from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.development import CheckResults, Review, Stage
from recollect.selfmod.evidence_segments import iter_segments
from recollect.selfmod.journal import (
    IntegrityError,
    Journal,
    encode,
    read_archive,
    sha256,
)
from recollect.selfmod.native_admission import NativeCandidate, NativeReceipt
from recollect.selfmod.native_history_reader import HistoryReadError
from recollect.selfmod.native_runtime import (
    NativeIPCTransport,
    SupervisorChannel,
    _history_result,
)
from tests.selfmod_checkpoint_helpers import EVIDENCE, values
from tests.test_selfmod_evidence_segments import tamper
from tests.test_selfmod_native_admission import Resources, admit
from tests.test_selfmod_native_admission import case as case

BINDING = {"run_id": "a" * 32, "runtime_sha256": "b" * 64}


class Pipe:
    """Host-written frames and a supervisor output stream fed by the test."""

    def __init__(self):
        self.sent = asyncio.Queue()
        self.output = asyncio.StreamReader()
        self.block = None

    async def write(self, raw):
        if self.block is not None:
            await self.block.wait()
        self.sent.put_nowait(json.loads(raw))

    def emit(self, frame):
        self.output.feed_data(encode(frame))

    async def next(self):
        return await asyncio.wait_for(self.sent.get(), 3)


@pytest.fixture
async def channel():
    pipe = Pipe()
    value = SupervisorChannel(pipe.write, BINDING)
    value.pipe = pipe
    reader = asyncio.create_task(value.read(pipe.output))
    yield value
    pipe.output.feed_eof()
    await asyncio.wait_for(reader, 3)


async def serve_http(channel, status=200, headers=(), chunks=(b"ok",), *,
                     end=True, identity=0):
    pipe = channel.pipe
    request = await pipe.next()
    assert request["kind"] == "http_request" and request["id"] == identity
    body = bytearray()
    while (frame := await pipe.next())["kind"] == "http_request_chunk":
        body.extend(base64.b64decode(frame["base64"]))
    assert frame == {"kind": "http_request_end", "id": identity,
                     "chunks": frame["chunks"]}
    pipe.emit({"kind": "http_head", "id": identity, "sequence": 0,
               "status": status, "headers": [list(h) for h in headers]})
    assert await pipe.next() == {"kind": "http_ack", "id": identity, "sequence": 0}
    for index, data in enumerate(chunks, 1):
        pipe.emit({"kind": "http_chunk", "id": identity, "sequence": index,
                   "base64": base64.b64encode(data).decode()})
        assert await pipe.next() == {"kind": "http_ack", "id": identity,
                                     "sequence": index}
    if end:
        pipe.emit({"kind": "http_end", "id": identity, "sequence": len(chunks) + 1})
        assert (await pipe.next())["sequence"] == len(chunks) + 1
    return request, bytes(body)


async def test_ipc_transport_streams_native_http_under_credit(channel):
    client = httpx.AsyncClient(base_url="http://native.invalid",
                               transport=NativeIPCTransport(channel))
    body = b"x" * 100_000
    server = asyncio.create_task(serve_http(
        channel, headers=[("Content-Length", "4")], chunks=(b"ab", b"cd")))
    response = await client.post("/session", content=body)
    request, received = await server
    assert response.status_code == 200 and response.content == b"abcd"
    assert request["bytes"] == len(body) and received == body
    assert not channel.http_lock.locked() and channel.failure is None
    # The next request uses the next monotonic identity.
    server = asyncio.create_task(serve_http(channel, identity=1))
    assert (await client.get("/global/health")).content == b"ok"
    await server


@pytest.mark.parametrize("path", ["/admin", "/config?x=1",
                                  "/session/ses_a/summarize"])
async def test_ipc_transport_keeps_route_validation(channel, path):
    # httpx normalizes dot segments before transport; these stay forbidden.
    client = httpx.AsyncClient(base_url="http://native.invalid",
                               transport=NativeIPCTransport(channel))
    with pytest.raises(httpx.UnsupportedProtocol):
        await client.get(path)
    assert channel.pipe.sent.empty() and channel.failure is None


async def test_foreign_origin_never_reaches_supervisor(channel):
    client = httpx.AsyncClient(transport=NativeIPCTransport(channel))
    with pytest.raises(httpx.UnsupportedProtocol):
        await client.get("http://127.0.0.1:4096/global/health")
    assert channel.pipe.sent.empty()


async def test_abandoned_response_fails_run_without_reusing_lane(channel):
    client = httpx.AsyncClient(base_url="http://native.invalid",
                               transport=NativeIPCTransport(channel))
    server = asyncio.create_task(serve_http(channel, chunks=(), end=False))
    async with client.stream("GET", "/global/health") as response:
        assert response.status_code == 200
    await server
    assert isinstance(channel.failure, IntegrityError)
    assert channel.http_lock.locked()
    with pytest.raises(IntegrityError):
        await client.get("/global/health")


@pytest.mark.parametrize("frame", [
    {"kind": "http_chunk", "id": 0, "sequence": 5, "base64": "AA=="},
    {"kind": "http_end", "id": 9, "sequence": 1},
    {"kind": "http_head", "id": 0, "sequence": 1, "status": 200, "headers": []},
])
async def test_stale_or_replayed_http_frames_fail_closed(channel, frame):
    client = httpx.AsyncClient(base_url="http://native.invalid",
                               transport=NativeIPCTransport(channel))
    server = asyncio.create_task(serve_http(channel, chunks=(), end=False))
    async with client.stream("GET", "/global/health") as response:
        await server
        channel.pipe.emit(frame)
        with pytest.raises((IntegrityError, httpx.ReadError)):
            await response.aread()
    assert channel.failure is not None


async def test_supervisor_exceeding_credit_is_protocol_failure(channel):
    for sequence in (1, 2):
        channel.pipe.emit({"kind": "http_chunk", "id": 0, "sequence": sequence,
                           "base64": "AA=="})
    await asyncio.wait_for(channel.failed.wait(), 3)
    assert "credit" in str(channel.failure)


@pytest.mark.parametrize("frame", [
    {"kind": "fence", **BINDING},
    {"kind": "ready", "run_id": "c" * 32, "runtime_sha256": "b" * 64},
    {"kind": "native_started", **BINDING, "native": {"pid": 0, "start": 1}},
    {"kind": "terminal_collection_finished", **BINDING,
     "collector_returncode": "0", "failed": False},
])
async def test_unbound_or_unknown_supervisor_controls_fail(channel, frame):
    channel.pipe.emit(frame)
    await asyncio.wait_for(channel.failed.wait(), 3)


async def test_duplicate_control_fails_and_waiters_do_not_hang(channel):
    waiter = asyncio.create_task(channel.control("native_listening"))
    channel.pipe.emit({"kind": "ready", **BINDING})
    assert await channel.control("ready") == {"kind": "ready", **BINDING}
    channel.pipe.emit({"kind": "ready", **BINDING})
    with pytest.raises(IntegrityError):
        await asyncio.wait_for(waiter, 3)


async def test_terminal_wait_returns_on_control_stream_loss(channel):
    waiter = asyncio.create_task(channel.terminal())
    channel.pipe.output.feed_eof()
    assert await asyncio.wait_for(waiter, 3) is None


async def test_blocked_host_write_is_released_by_terminal_collection(channel):
    channel.pipe.block = asyncio.Event()
    stuck = asyncio.create_task(channel.send({"kind": "http_ack", "id": 0,
                                              "sequence": 0}))
    await asyncio.sleep(0)
    channel.pipe.emit({"kind": "terminal_collection_finished", **BINDING,
                       "collector_returncode": 0, "failed": False})
    frame = await asyncio.wait_for(channel.terminal(), 3)
    assert frame["collector_returncode"] == 0
    # PID 1 no longer reads stdin after terminal; the sender must not hang.
    with pytest.raises(IntegrityError):
        await asyncio.wait_for(stuck, 3)
    assert channel.failure is not None and channel._writes
    channel.pipe.block.set()
    await asyncio.wait_for(channel.settle_writes(), 3)
    assert not channel._writes


async def test_waits_wake_when_supervisor_collects_without_lane_output(channel):
    client = httpx.AsyncClient(base_url="http://native.invalid",
                               transport=NativeIPCTransport(channel))
    request = asyncio.create_task(client.get("/global/health"))
    listening = asyncio.create_task(channel.control("native_listening"))
    history = asyncio.create_task(channel.history({"session_id": "ses_a",
                                                   "after": -1}))
    assert (await channel.pipe.next())["kind"] in {"http_request",
                                                   "history_request"}
    # Native exited: PID 1 failed, settled and collected without more frames.
    channel.pipe.emit({"kind": "terminal_collection_finished", **BINDING,
                       "collector_returncode": 0, "failed": True})
    for task in (request, listening, history):
        with pytest.raises(IntegrityError):
            await asyncio.wait_for(task, 3)
    with pytest.raises(IntegrityError, match="collection"):
        await channel.http("GET", "/global/health", b"")


async def test_terminal_owned_waits_still_receive_late_fenced_frame(channel):
    channel.pipe.emit({"kind": "terminal_collection_finished", **BINDING,
                       "collector_returncode": 0, "failed": False})
    await asyncio.wait_for(channel.terminal(), 3)
    fenced = asyncio.create_task(channel.control("fenced", terminal=False))
    await asyncio.sleep(0.05)
    assert not fenced.done()
    channel.pipe.emit({"kind": "fenced", **BINDING})
    assert await asyncio.wait_for(fenced, 3) == {"kind": "fenced", **BINDING}


async def test_history_reads_are_serialized_and_errors_are_typed(channel):
    page = b'{"rows":[]}' * 4000
    reader = asyncio.create_task(channel.history({"session_id": "ses_a",
                                                  "after": -1}))
    request = await channel.pipe.next()
    assert request == {"kind": "history_request", "id": 0,
                       "request": {"session_id": "ses_a", "after": -1}}
    parts = [page[i:i + 32768] for i in range(0, len(page), 32768)]
    for index, part in enumerate(parts):
        channel.pipe.emit({"kind": "history_chunk", "id": 0, "sequence": index,
                           "base64": base64.b64encode(part).decode()})
        assert (await channel.pipe.next())["sequence"] == index
    channel.pipe.emit({"kind": "history_end", "id": 0, "sequence": len(parts),
                       "returncode": 0, "stderr": ""})
    assert (await channel.pipe.next())["kind"] == "history_ack"
    assert await reader == page
    error = json.dumps({"error": "rollback", "raw_prefix": base64.b64encode(
        b"prefix").decode(), "raw_prefix_truncated": False}).encode()
    with pytest.raises(HistoryReadError, match="rollback") as failure:
        _history_result(1, error)
    assert failure.value.raw_prefix == b"prefix"
    with pytest.raises(HistoryReadError):
        _history_result(1, b"not json")


async def test_model_request_assembly_is_exact(channel):
    identity = "d" * 32
    body = b'{"model":"m"}'
    channel.pipe.emit({"kind": "model_request", "id": identity, "bytes": len(body)})
    channel.pipe.emit({"kind": "model_request_chunk", "id": identity, "sequence": 0,
                       "base64": base64.b64encode(body).decode()})
    channel.pipe.emit({"kind": "model_request_end", "id": identity, "chunks": 1})
    assert await asyncio.wait_for(channel.model_request(), 3) == (identity, body)
    sender = asyncio.create_task(channel.model_data({
        "kind": "model_head", "id": identity, "sequence": 0, "status": 200,
        "content_type": "text/event-stream"}))
    assert (await channel.pipe.next())["kind"] == "model_head"
    channel.pipe.emit({"kind": "model_ack", "id": identity, "sequence": 0})
    await asyncio.wait_for(sender, 3)
    channel.pipe.emit({"kind": "model_closed", "id": identity,
                       "response_delivered": True})
    await asyncio.wait_for(channel.model_closed(identity), 3)


@pytest.mark.parametrize("fault", ["size", "sequence", "foreign"])
async def test_malformed_model_request_is_rejected(channel, fault):
    identity = "d" * 32
    channel.pipe.emit({"kind": "model_request", "id": identity, "bytes": 3})
    chunk = {"kind": "model_request_chunk", "id": identity, "sequence": 0,
             "base64": base64.b64encode(b"abcd").decode()}
    if fault == "sequence":
        chunk.update(sequence=1, base64=base64.b64encode(b"abc").decode())
    elif fault == "foreign":
        chunk.update(id="e" * 32, base64=base64.b64encode(b"abc").decode())
    channel.pipe.emit(chunk)
    with pytest.raises(IntegrityError):
        await asyncio.wait_for(channel.model_request(), 3)


class Capturing(Resources):
    def __init__(self, admission, snapshot):
        super().__init__(admission)
        self.snapshot, self.finishes = snapshot, 0
        self.upstream_settled = True
        # The base fixture reuses `finish` as a close-blocking event attribute.
        self.finish = self._capture

    async def _capture(self):
        self.finishes += 1
        return NativeCandidate(self.admission.run, self.snapshot, EVIDENCE,
                               self.upstream_settled)


def candidate(case, value=b"value = 2\n"):
    files = {f.path: f.content for f in case.dev._baseline.files}
    files["editable.py"] = value
    return Snapshot(tuple(File(p, d) for p, d in sorted(files.items())))


async def test_handoff_admits_exact_capture_once_then_settles_with_candidate(case):
    admission = admit(case)
    snapshot = candidate(case)
    resource = admission.start(lambda owner: Capturing(owner, snapshot))
    admission.release()
    before = case.dev.binding
    await admission.handoff()
    assert case.dev.stage == Stage.CHECKS and case.dev.binding != before
    assert type(case.dev._receipt) is NativeReceipt
    assert case.dev._receipt.snapshot == snapshot
    assert case.dev._receipt.diagnostic_only is False
    assert case.dev._receipt_source_sha256 == snapshot.sha256
    with pytest.raises(IntegrityError, match="one-use"):
        await admission.handoff()
    assert case.dev._busy
    await admission.close()
    assert resource.finishes == 1 and admission.settled and not case.dev._busy
    assert case.controller._eligible
    states = [v["state"] for v in values(case.controller, "development_execution")
              if v.get("mode") == "native_admission"]
    assert states[-5:] == ["handoff_intent", "capture_verified", "verified",
                           "stop_observed", "settled_with_candidate"]
    verified = [v for v in values(case.controller, "development_execution")
                if v.get("state") == "verified"][-1]
    assert verified["execution_receipt"] and verified["candidate_sha256"] == (
        snapshot.sha256)
    assert not values(case.controller, "development_failure")
    # The next driver action is deterministic checks on this exact candidate.
    checks = case.dev.authorize("checks")
    assert checks.binding == case.dev.binding


async def test_late_model_guard_after_handoff_is_fenced_and_fails_closed(case):
    admission = admit(case)
    admission.start(lambda owner: Capturing(owner, candidate(case)))
    admission.release()
    await admission.handoff()
    # A broker dispatch after the capture fence is stale authority, not work.
    with pytest.raises(IntegrityError):
        admission.guard()
    assert not case.controller._eligible
    await admission.close()


@pytest.mark.parametrize("fault", ["unsettled", "foreign_run", "scope", "revoked",
                                   "no_evidence"])
async def test_rejected_or_revoked_handoff_fails_primary_and_admits_nothing(
    case, fault,
):
    admission = admit(case)
    snapshot = candidate(case)
    if fault == "scope":
        snapshot = Snapshot((*snapshot.files, File("protected-new.py", b"x\n")))
    resource = admission.start(lambda owner: Capturing(owner, snapshot))
    admission.release()
    if fault == "unsettled":
        resource.upstream_settled = False
    elif fault == "revoked":
        case.controller._abort("revoked_before_handoff")
    elif fault in {"foreign_run", "no_evidence"}:
        original = resource.finish

        async def finish():
            value = await original()
            if fault == "foreign_run":
                return replace(value, run=replace(value.run, run_id="f" * 32))
            return replace(value, evidence=Snapshot(()))

        resource.finish = finish
    with pytest.raises((IntegrityError, ValueError)):
        await admission.handoff()
    assert case.dev._receipt is None
    assert not case.controller._eligible
    assert values(case.controller, "development_failure")
    await admission.close()
    states = [v["state"] for v in values(case.controller, "development_execution")
              if v.get("mode") == "native_admission"]
    assert "verified" not in states and states[-1] == "settled_without_candidate"
    with pytest.raises(IntegrityError):
        case.dev.authorize("execute")


class Driven(Capturing):
    def __init__(self, admission, snapshot, prompt_error=None):
        super().__init__(admission, snapshot)
        self.prompts, self.prompt_error = [], prompt_error

    async def started(self):
        assert self.releases == 1

    async def prompt(self, text):
        self.prompts.append(text)
        if self.prompt_error is not None:
            raise self.prompt_error


NATIVE = {"model": "fixture-model", "context_limit": 32768, "output_limit": 4096,
          "base_url": "http://127.0.0.1:8001/v1"}


async def test_execute_native_hands_exact_capture_to_checks(case):
    created = []

    def factory(owner):
        created.append(Driven(owner, candidate(case)))
        return created[0]

    await case.dev.execute_native(case.dev.authorize("execute"), factory,
                                  prompt="Implement the accepted plan", **NATIVE)
    (resource,) = created
    assert resource.prompts == ["Implement the accepted plan"]
    assert resource.finishes == resource.closes == 1
    assert case.dev.stage == Stage.CHECKS and not case.dev._busy
    assert case.controller._eligible
    assert case.dev._receipt.snapshot == candidate(case)


async def test_execute_native_failure_still_settles_without_candidate(case):
    created = []

    def factory(owner):
        created.append(Driven(owner, candidate(case), OSError("native turn lost")))
        return created[0]

    with pytest.raises(OSError, match="native turn lost"):
        await case.dev.execute_native(case.dev.authorize("execute"), factory,
                                      prompt="Implement", **NATIVE)
    assert created[0].finishes == 0 and created[0].closes == 1
    assert case.dev._receipt is None and not case.dev._busy
    assert not case.controller._eligible
    assert values(case.controller, "development_execution")[-1]["state"] == (
        "settled_without_candidate")


def segment(tmp_path, name="history", count=4):
    root = tmp_path / ("native-" + name)
    with Journal.create(root) as journal:
        for index in range(count):
            journal.append("native_history_event", {"seq": index},
                           Snapshot((File("page.json", b'{"rows":[]}\n'),)))
        head = journal.head
    return name, journal.root, head


async def ready_with_segments(case, tmp_path):
    admission = admit(case)
    snapshot = candidate(case)
    resource = admission.start(lambda owner: Capturing(owner, snapshot))
    source = segment(tmp_path)
    resource.stop = replace(resource.stop, segments=(source,))
    admission.release()
    await admission.handoff()
    await admission.close()
    dev = case.dev
    checks = dev.authorize("checks")
    dev.checks(checks, CheckResults(dev.binding, (("unit", True), ("regression", True)),
                                    EVIDENCE.sha256), EVIDENCE)
    review = dev.authorize("review")
    dev.review(review, Review(dev.binding, review.actor_id, True, (), EVIDENCE.sha256),
               EVIDENCE)
    return admission, source


async def test_closed_native_journals_bind_as_exact_sidecars_through_cp2(
    case, tmp_path,
):
    admission, (name, root, head) = await ready_with_segments(case, tmp_path)
    settled = values(case.controller, "development_execution")[-1]
    relative = f"segments/{admission.run.run_id}/{name}"
    assert settled["state"] == "settled_with_candidate"
    assert settled["segments"] == [{"name": name, "path": relative,
                                    "anchor": asdict(head)}]
    copied = case.controller.journal.root.joinpath(*relative.split("/"))
    assert tuple(iter_segments(copied, head)) == read_archive(root, head)
    number, _ = case.dev.submit(case.dev.authorize("submit"))
    assert number == 1 and case.controller._eligible


@pytest.mark.parametrize("fault", ["marker", "record"])
async def test_tampered_sidecar_blocks_cp2_and_is_accounted(case, tmp_path, fault):
    admission, (name, _, head) = await ready_with_segments(case, tmp_path)
    copied = (case.controller.journal.root / "segments" / admission.run.run_id
              / name)
    if fault == "marker":
        (copied / "complete.json").unlink()
    else:
        tamper(copied, [("DELETE FROM files WHERE seq=4", ()),
                        ("DELETE FROM records WHERE seq=4", ())])
    with pytest.raises(IntegrityError):
        case.dev.submit(case.dev.authorize("submit"))
    assert not case.controller._eligible
    case.controller.account()
    branch = values(case.controller, "accounting_branch")[-1]
    assert any("evidence segment" in issue["reason"] for issue in branch["issues"])


async def test_invalid_segment_report_cannot_be_bound(case, tmp_path):
    admission = admit(case)
    resource = admission.start(lambda owner: Capturing(owner, candidate(case)))
    name, root, head = segment(tmp_path)
    resource.stop = replace(resource.stop, segments=(("../escape", root, head),))
    admission.release()
    await admission.handoff()
    with pytest.raises(IntegrityError, match="unconfirmed"):
        await admission.close()
    assert case.dev._busy and not admission.settled


async def test_handoff_requires_capture_capable_owned_adapter(case):
    admission = admit(case)
    admission.start(lambda owner: Resources(owner))
    admission.release()
    with pytest.raises(IntegrityError, match="capture"):
        await admission.handoff()
    await admission.close()
    assert case.dev._receipt is None and not case.controller._eligible


async def test_unconfirmed_stop_after_handoff_retains_lease(case):
    admission = admit(case)
    resource = admission.start(lambda owner: Capturing(owner, candidate(case)))
    admission.release()
    await admission.handoff()
    resource.stop = replace(resource.stop, upstream_stopped=False)
    with pytest.raises(IntegrityError, match="unconfirmed"):
        await admission.close()
    assert case.dev._busy and not admission.settled
    assert not case.controller._eligible
    assert sha256(admission.settings.authority) == resource.stop.authority_sha256
