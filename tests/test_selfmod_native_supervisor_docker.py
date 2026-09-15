"""Real pinned supervisor startup/stop; no inference or experiment trial."""

import asyncio
import base64
import contextlib
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path

import httpx
import pytest

from recollect.selfmod import native_transport as transport_module
from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.files import materialize
from recollect.selfmod.journal import decode, encode, root_path
from recollect.selfmod.native import VERSION, _settle
from recollect.selfmod.native_capture import NativeCaptureSpec, verify_stop
from recollect.selfmod.native_containment import (
    NativeRuntimeSpec,
    attest,
    create_arguments,
)
from recollect.selfmod.native_transport import (
    NativeHTTPTransport,
    NativeTransportConfig,
)
from tests.test_selfmod_native_capture import spec as capture_spec

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("RECOLLECT_RUN_SELFMOD_DOCKER_TESTS") != "1",
        reason="opt in to real selfmod Docker qualification",
    ),
]

BINARY_SHA = "bb71f45b564f9234a97f54d6252a4a41d2f4388ae4b078918f691824cc3b3e54"


def observed_stream(trace):
    original = transport_module.NativeHTTPStream

    class ObservedStream(original):
        def __init__(self, process):
            wait = process.wait

            async def observed_wait():
                trace("cli_wait")
                code = await wait()
                trace("cli_exit", returncode=code)
                return code

            process.wait = observed_wait
            super().__init__(process)

        async def frame(self):
            trace("frame_wait")
            try:
                value = await super().frame()
            except BaseException as error:
                trace("frame_error", error_type=type(error).__name__)
                raise
            if "status" in value:
                trace("headers", status=value["status"], headers=value.get("headers"))
            elif "chunk" in value:
                trace("body", bytes=len(base64.b64decode(value["chunk"])))
            elif "end" in value:
                trace("end", value=value["end"])
            return value

        async def _read(self, *, eof=False):
            if eof:
                trace("stdout_eof_wait")
            result = await super()._read(eof=eof)
            if eof:
                trace("stdout_eof", trailing_bytes=len(result))
            return result

        async def _stderr(self):
            await super()._stderr()
            trace("stderr_eof", retained_bytes=len(self.stderr_prefix))

    return ObservedStream


async def invoke(argv, env, *, data=None, check=True):
    spawning = asyncio.create_task(asyncio.create_subprocess_exec(
        *argv, env=env, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    ))
    communication = None
    try:
        process = await asyncio.shield(spawning)
        communication = asyncio.create_task(process.communicate(data))
        output, error = await asyncio.shield(communication)
    finally:
        async def finish():
            process = await spawning
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            if communication is not None:
                await communication
            else:
                await process.communicate()

        await _settle(asyncio.create_task(finish()))
    assert len(output) <= 24 * 1024 * 1024 and len(error) <= 65536
    if check:
        assert process.returncode == 0, error.decode(errors="replace")
    return process.returncode, output, error


class RunningSupervisor:
    def __init__(self, argv, env, config, root, evidence):
        self.argv, self.env, self.config = argv, env, config
        self.root, self.evidence = root, evidence
        self.identity = None
        self.attachment = None
        self.native = None
        self.finished_frame = None
        self._create_task = None
        self._create_resolved = False
        self._attachment_spawn = None
        self._reader = self._stderr = None
        self._frames = asyncio.Queue(maxsize=8)
        self._terminal = asyncio.Event()
        self._reader_error = None

    async def docker(self, *args, **kwargs):
        return await invoke([*self.argv, *args], self.env, **kwargs)

    def trace(self, phase, **details):
        with (self.evidence / "transport-phases.jsonl").open("ab") as output:
            output.write(encode({"phase": phase, "monotonic_ns": time.monotonic_ns(),
                                 **details}))

    async def _read_controls(self):
        try:
            while True:
                raw = await self.attachment.stdout.readline()
                assert raw.endswith(b"\n"), "supervisor closed control output"
                frame = decode(raw)
                assert frame.get("run_id") == self.config.run_id
                assert frame.get("runtime_sha256") == self.config.sha256
                assert frame.get("kind") in {
                    "ready", "native_started", "native_listening", "fenced",
                    "terminal_collection_finished",
                }, "Unexpected control event in no-model qualification"
                with (self.evidence / "control.jsonl").open("ab") as output:
                    output.write(raw)
                if frame["kind"] == "native_listening":
                    # Connect-only readiness is evidence here; this diagnostic
                    # still waits on its own probe, so it is not queued.
                    continue
                if frame["kind"] == "terminal_collection_finished":
                    assert set(frame) == {
                        "kind", "run_id", "runtime_sha256", "collector_returncode",
                        "failed",
                    }
                    assert type(frame["failed"]) is bool
                    assert (frame["collector_returncode"] is None
                            or type(frame["collector_returncode"]) is int)
                    assert self.finished_frame is None
                    self.finished_frame = frame
                    self._terminal.set()
                self._frames.put_nowait(frame)
        except BaseException as error:
            self._reader_error = error
            self._terminal.set()
            with contextlib.suppress(asyncio.QueueFull):
                self._frames.put_nowait(None)

    async def _read_stderr(self):
        prefix = bytearray()
        total = 0
        while data := await self.attachment.stderr.read(8192):
            total += len(data)
            prefix.extend(data[:max(0, 65536 - len(prefix))])
        (self.evidence / "attachment.stderr").write_bytes(prefix)
        (self.evidence / "attachment-stderr.json").write_bytes(encode({
            "received_bytes": total, "truncated": total > len(prefix),
        }))

    async def frame(self):
        if self._reader_error is not None:
            raise self._reader_error
        value = await self._frames.get()
        if self._reader_error is not None:
            raise self._reader_error
        assert value is not None
        return value

    async def _create(self):
        _, raw, _ = await self.docker(
            *create_arguments(self.config, self.root / "input"),
        )
        identity = raw.decode().strip()
        assert re.fullmatch(r"[0-9a-f]{64}", identity)
        self.identity = identity
        self._create_resolved = True

    async def start(self):
        self._create_task = asyncio.create_task(self._create())
        await asyncio.shield(self._create_task)
        _, raw, _ = await self.docker("inspect", self.identity)
        actual = json.loads(raw)[0]
        attest(actual, self.config, self.root / "input", self.identity)
        (self.evidence / "inspection.json").write_bytes(raw)
        self._attachment_spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            *self.argv, "start", "--attach", "--interactive", self.identity,
            env=self.env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=4 * 1024 * 1024,
        ))
        self.attachment = await asyncio.shield(self._attachment_spawn)
        self._reader = asyncio.create_task(self._read_controls())
        self._stderr = asyncio.create_task(self._read_stderr())
        binding = {"run_id": self.config.run_id, "runtime_sha256": self.config.sha256}
        assert await self.frame() == {"kind": "ready", **binding}
        self.attachment.stdin.write(self.config.control("release"))
        await self.attachment.stdin.drain()
        started = await self.frame()
        self.native = started["native"]
        assert started == {"kind": "native_started", **binding, "native": self.native}

    async def _during_startup(self, operation):
        work = asyncio.create_task(operation)
        terminal = asyncio.create_task(self._terminal.wait())
        try:
            await asyncio.wait((work, terminal), return_when=asyncio.FIRST_COMPLETED)
            assert not self._terminal.is_set(), "Supervisor terminated during startup"
            return await work
        finally:
            work.cancel()
            terminal.cancel()
            await _settle(asyncio.create_task(self._settle_tasks(work, terminal)))

    @staticmethod
    async def _settle_tasks(*tasks):
        await asyncio.gather(*tasks, return_exceptions=True)

    async def health(self):
        while True:
            self.trace("readiness_probe_start")
            code, raw, _ = await self._during_startup(self.docker(
                "exec", "--user", "65532:65532", self.identity,
                "/usr/local/bin/python", "-I", "-S", "-c",
                "import urllib.request,sys; sys.stdout.buffer.write("
                "urllib.request.urlopen('http://127.0.0.1:4096/global/health',"
                "timeout=5).read())", check=False,
            ))
            self.trace("readiness_probe_end", returncode=code)
            if code == 0:
                assert json.loads(raw) == {"healthy": True, "version": VERSION}
                break
            _, raw, _ = await self.docker("inspect", self.identity)
            assert json.loads(raw)[0]["State"]["Running"] is True
            assert not self._terminal.is_set(), "Supervisor failed during startup"
            await asyncio.sleep(0.1)
        transport = NativeHTTPTransport(NativeTransportConfig.from_argv(
            self.argv, self.identity, env=self.env,
        ))
        async with httpx.AsyncClient(transport=transport, timeout=None) as client:
            # Once independently ready, do not misclassify a transport failure as
            # ongoing native startup and silently retry it forever.
            response = await self._during_startup(
                client.get("http://native.invalid/global/health"),
            )
            response.raise_for_status()
            assert response.json() == {"healthy": True, "version": VERSION}

    async def read(self, name):
        assert self.finished_frame is not None, "Collector may still own process census"
        result = await self.docker("exec", self.identity, "/bin/cat",
                                   "/evidence/" + name, check=False)
        code, raw, error = result
        if code == 0:
            (self.evidence / name).write_bytes(raw)
        return result

    async def stopped(self):
        finished = self.finished_frame or await self.frame()
        assert finished["kind"] == "terminal_collection_finished"
        assert finished["collector_returncode"] == 0
        assert finished["run_id"] == self.config.run_id
        assert finished["runtime_sha256"] == self.config.sha256
        _, raw, _ = await self.read("collector-result.json")
        assert decode(raw) == {"returncode": 0}
        _, raw, _ = await self.read("disconnect-stop.json")
        verify_stop(raw, self.config.capture, self.native)

    async def _cleanup_namespace(self):
        errors = []
        if self._create_task is not None:
            # Do not cancel create on outer cancellation: an empty lookup while
            # the daemon is still creating is not proof that inputs are unused.
            try:
                await _settle(self._create_task)
            except BaseException as error:
                (self.evidence / "create-error.json").write_bytes(encode({
                    "error_type": type(error).__name__,
                }))
        if self._attachment_spawn is not None:
            try:
                self.attachment = await _settle(self._attachment_spawn)
            except BaseException as error:
                errors.append(error)
        # Reconcile even if create/start returned an ambiguous failure. A matching
        # name alone never authorizes removal of someone else's container.
        _, raw, _ = await self.docker("ps", "-a", "--no-trunc", "--filter",
                                      "name=^/" + self.config.name + "$",
                                      "--format", "{{.ID}}")
        ids = raw.decode().splitlines()
        assert len(ids) <= 1
        if ids:
            identity = ids[0]
            _, raw, _ = await self.docker("inspect", identity)
            actual = json.loads(raw)[0]
            assert actual["Id"] == identity
            assert actual["Name"] == "/" + self.config.name
            assert actual["Image"] == self.config.image_id
            assert actual["Config"]["Labels"]["recollect.selfmod"] == self.config.run_id
            assert actual["Config"]["Labels"]["recollect.spec"] == self.config.sha256
            binds = [m for m in actual["Mounts"] if m["Type"] == "bind"]
            assert len(binds) == 1 and binds[0]["RW"] is False
            assert Path(binds[0]["Source"]).resolve() == (self.root / "input").resolve()
            self._create_resolved = True
            if actual["State"]["Running"]:
                self.identity = identity
                try:
                    if self.finished_frame is not None:
                        for name in ("failure.json", "native.json", "native.stdout",
                                     "native.stderr", "disconnect-stop.json",
                                     "disconnect-stop.stderr", "collector-result.json"):
                            await self.read(name)
                    else:
                        # Emergency diagnostic capture is not qualified stop:
                        # pause all tasks and ask the daemon for opaque tar bytes.
                        # No extra root exec may enter an in-progress census.
                        await self.docker("pause", identity)
                        _, raw, _ = await self.docker("inspect", identity)
                        assert json.loads(raw)[0]["State"]["Paused"] is True
                        _, raw, _ = await self.docker(
                            "cp", identity + ":/evidence", "-",
                        )
                        (self.evidence / "emergency-evidence.tar").write_bytes(raw)
                except BaseException as error:
                    errors.append(error)
                _, raw, _ = await self.docker("inspect", identity)
                if json.loads(raw)[0]["State"].get("Paused"):
                    await self.docker("unpause", identity)
                await self.docker("kill", "--signal=KILL", identity)
            _, raw, _ = await self.docker("inspect", identity)
            stopped = json.loads(raw)[0]["State"]
            assert not stopped["Running"] and stopped["Pid"] == 0
            assert stopped["Status"] in {"created", "exited"}
            await self.docker("rm", identity)
        _, raw, _ = await self.docker("ps", "-a", "--filter",
                                      "name=^/" + self.config.name + "$",
                                      "--format", "{{.ID}}")
        assert not raw.strip()
        if self._create_task is not None and not self._create_resolved:
            errors.append(RuntimeError(
                "Ambiguous create remains unresolved; retain inputs",
            ))
        if errors:
            raise BaseExceptionGroup(
                "Namespace cleanup incomplete; inputs retained", errors,
            )

    async def _close_attachment(self):
        if self._attachment_spawn is not None:
            self.attachment = await _settle(self._attachment_spawn)
        if self.attachment is None:
            return
        errors = []
        if self.attachment.returncode is None:
            try:
                self.attachment.kill()
            except ProcessLookupError:
                pass
            except BaseException as error:
                errors.append(error)
        try:
            self.attachment.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except BaseException as error:
            errors.append(error)
        if self._reader is not None:
            results = await asyncio.gather(
                self._reader, self._stderr, self.attachment.wait(),
                return_exceptions=True,
            )
        else:
            results = await asyncio.gather(
                self.attachment.communicate(), self.attachment.wait(),
                return_exceptions=True,
            )
            if not isinstance(results[0], BaseException):
                out, err = results[0]
                for name, raw in (("attachment-tail.stdout", out),
                                  ("attachment.stderr", err)):
                    try:
                        (self.evidence / name).write_bytes(raw)
                    except BaseException as error:
                        errors.append(error)
        errors.extend(value for value in results if isinstance(value, BaseException))
        if errors:
            raise BaseExceptionGroup("Attachment cleanup failed", errors)

    async def cleanup(self):
        errors = []
        for finish in (self._cleanup_namespace, self._close_attachment):
            try:
                await finish()
            except BaseException as error:
                errors.append(error)
        if errors:
            raise BaseExceptionGroup(
                "Qualification cleanup incomplete; inputs retained", errors,
            )
        assert root_path(self.root) == self.root.absolute()
        assert self.root.name == (
            "selfmod-supervisor-qualification-" + self.config.run_id
        )
        shutil.rmtree(self.root)


@pytest.fixture
async def runtime(monkeypatch):
    executable = shutil.which("docker")
    assert executable
    env = {k: v for k, v in os.environ.items()
           if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "PATHEXT"}}
    _, raw, _ = await invoke([executable, "context", "inspect", "--format",
                             "{{.Endpoints.docker.Host}}"], env)
    endpoint = raw.decode().strip()
    identity = uuid.uuid4().hex
    root = (Path(os.environ["LOCALAPPDATA"]) / "recollect" / "sandboxes"
            / ("selfmod-supervisor-qualification-" + identity))
    root.mkdir()
    evidence = Path(".agent") / ("native-supervisor-live-" + identity)
    evidence.mkdir()
    materialize(root / "cli", Snapshot((File("config.json", b'{"auths":{}}\n'),)))
    argv = [executable, "--config", str(root / "cli"), "--host", endpoint]
    value = None
    try:
        # Everything after endpoint discovery uses this isolated CLI configuration.
        _, raw, _ = await invoke([*argv, "image", "inspect",
                                 "recollect-opencode-sandbox:1.18.18"], env)
        image = json.loads(raw)[0]
        original = capture_spec()
        run = replace(original.run, run_id=identity)
        settings = replace(original.settings, authority=encode({
            **decode(original.settings.authority), "run": asdict(run),
        }))
        capture = NativeCaptureSpec(run, settings, original.baseline)
        config = NativeRuntimeSpec(capture, image["Id"], tuple(image["Config"]["Env"]),
                                   BINARY_SHA)
        materialize(root / "input", config.inputs)
        value = RunningSupervisor(argv, env, config, root, evidence)
        materialize(evidence / "frozen-input", config.inputs)
        materialize(evidence / "host-sources", Snapshot((
            File("fixture.py", Path(__file__).read_bytes()),
            File("transport.py", Path(transport_module.__file__).read_bytes()),
        )))
        monkeypatch.setattr(transport_module, "NativeHTTPStream",
                            observed_stream(value.trace))
        await value.start()
        yield value
    finally:
        if value is not None:
            await _settle(asyncio.create_task(value.cleanup()))
        else:
            assert root_path(root) == root.absolute()
            assert root.name == "selfmod-supervisor-qualification-" + identity
            shutil.rmtree(root)


@pytest.mark.parametrize("terminal", ["fence", "eof"])
async def test_real_supervisor_stops_native_and_retains_evidence(runtime, terminal):
    await runtime.health()
    if terminal == "fence":
        runtime.attachment.stdin.write(runtime.config.control("fence"))
        await runtime.attachment.stdin.drain()
        fenced = await runtime.frame()
        if fenced["kind"] == "terminal_collection_finished":
            fenced = await runtime.frame()
        assert fenced == {
            "kind": "fenced", "run_id": runtime.config.run_id,
            "runtime_sha256": runtime.config.sha256,
        }
    else:
        runtime.attachment.stdin.close()
    await runtime.stopped()
    _, raw, _ = await runtime.docker("inspect", runtime.identity)
    assert json.loads(raw)[0]["State"]["Running"] is True
    code, raw, _ = await runtime.read("failure.json")
    assert (code == 0) == (terminal == "eof")
    if code == 0:
        assert decode(raw)["phase"] == "control_eof"
