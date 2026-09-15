"""Real pinned OpenCode, scripted inference; not production containment proof."""

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import shutil
import uuid
from dataclasses import asdict, replace
from pathlib import Path

import httpx
import pytest

from recollect.selfmod import native_history_reader
from recollect.selfmod.contracts import ChangePolicy, File, Snapshot
from recollect.selfmod.development import Binding
from recollect.selfmod.journal import Journal, encode, inspect_archive, root_path
from recollect.selfmod.native import NativeSession, _durable, _settle
from recollect.selfmod.native_admission import NativeRun
from recollect.selfmod.native_broker import (
    TOKEN_CAP_FIELDS,
    BrokerIdentity,
    BrokerSettings,
    NativeModelBroker,
)
from recollect.selfmod.native_capture import (
    NativeCaptureSpec,
    capture_request,
    verify_capture,
    verify_stop,
)
from recollect.selfmod.native_history import iter_event_rows
from tests import selfmod_native_server
from tests.test_selfmod_native import settings

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("RECOLLECT_RUN_SELFMOD_DOCKER_TESTS") != "1",
        reason="opt in to real selfmod Docker qualification",
    ),
]

RELAY = """
import base64,json,sys,urllib.request,urllib.error
def emit(value):
    print(json.dumps(value),flush=True)
value=json.load(sys.stdin)
request=urllib.request.Request('http://127.0.0.1:4096'+value['path'],
    data=base64.b64decode(value['body']) or None,method=value['method'],
    headers={'Content-Type':'application/json','Accept-Encoding':'identity'})
try:
    response=urllib.request.urlopen(request)
except urllib.error.HTTPError as error:
    response=error
with response:
    emit({'status':response.status,'headers':dict(response.headers)})
    total=0
    while body:=response.read1(8192):
        room=16*1024*1024-total
        emit({'chunk':base64.b64encode(body[:room]).decode()})
        total+=len(body)
        if total>16*1024*1024:
            raise ValueError('native relay response exceeds bound')
    if response.length not in (None,0):
        raise ValueError('native response ended before Content-Length')
    emit({'end':True})
"""


async def drain(stream):
    while await stream.read(8192):
        pass


async def docker(*args, data=None, check=True):
    executable = shutil.which("docker")
    assert executable is not None
    spawning = asyncio.create_task(asyncio.create_subprocess_exec(
        executable, *args, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    ))
    tasks = []
    try:
        process = await asyncio.shield(spawning)

        async def read(stream):
            result = bytearray()
            while chunk := await stream.read(8192):
                if len(result) + len(chunk) > 24 * 1024 * 1024:
                    raise ValueError("Docker qualification output exceeds bound")
                result.extend(chunk)
            return bytes(result)

        async def send():
            if data:
                process.stdin.write(data)
                await process.stdin.drain()
            process.stdin.close()

        tasks = [asyncio.create_task(coro) for coro in (
            read(process.stdout), read(process.stderr), send(), process.wait(),
        )]
        out, err, _, _ = await asyncio.gather(*tasks)
    finally:
        async def finish():
            # Cancelling the CLI does not stop its exec child in the container.
            # The enclosing test must still reconcile/remove its owned container.
            process = await spawning
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(drain(process.stdout), drain(process.stderr))
            await process.wait()

        await _settle(asyncio.create_task(finish()))
    if check:
        assert process.returncode == 0, err.decode(errors="replace")
    return process.returncode, out, err


class Relay(httpx.AsyncBaseTransport):
    def __init__(self, name, *, provider=False):
        self.name = name
        self.provider = provider

    async def handle_async_request(self, request):
        executable = shutil.which("docker")
        assert executable is not None
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(
            executable, "exec", "-i", self.name, "python", "-I", "-S", "-c",
            RELAY.replace(":4096", ":4097") if self.provider else RELAY,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=65536,
        ))
        stream = None
        try:
            process = await asyncio.shield(spawning)
            stream = RelayStream(process)
            process.stdin.write(encode({
                "method": request.method,
                "path": "/scripted" if self.provider else request.url.raw_path.decode(),
                "body": base64.b64encode(request.content).decode(),
            }))
            await process.stdin.drain()
            process.stdin.close()
            header = await stream.frame()
            if set(header) != {"status", "headers"}:
                raise httpx.ReadError("Invalid native relay headers")
            return httpx.Response(header["status"], headers=header["headers"],
                                  stream=stream)
        except BaseException:
            async def finish():
                nonlocal stream
                process = await spawning
                if stream is None:
                    stream = RelayStream(process)
                await stream.aclose()

            await _settle(asyncio.create_task(finish()))
            raise


class RelayStream(httpx.AsyncByteStream):
    def __init__(self, process):
        self.process = process
        self.stderr = asyncio.create_task(self._stderr())
        self.closing = None

    async def _stderr(self):
        prefix = bytearray()
        while chunk := await self.process.stderr.read(8192):
            prefix.extend(chunk[:max(0, 65537 - len(prefix))])
        return bytes(prefix)

    async def frame(self):
        try:
            line = await self.process.stdout.readline()
            value = json.loads(line)
            if type(value) is not dict:
                raise ValueError("not an object")
            return value
        except (ValueError, OSError) as error:
            raise httpx.ReadError("Incomplete or invalid native relay frame") from error

    async def __aiter__(self):
        while True:
            frame = await self.frame()
            if frame == {"end": True}:
                if await self.process.stdout.read(1):
                    raise httpx.ReadError("Trailing native relay output")
                code = await self.process.wait()
                stderr = await self.stderr
                if code != 0 or stderr:
                    raise httpx.ReadError("Native relay did not finish cleanly")
                return
            if set(frame) != {"chunk"} or not isinstance(frame["chunk"], str):
                raise httpx.ReadError("Invalid native relay chunk")
            try:
                chunk = base64.b64decode(frame["chunk"], validate=True)
                if len(chunk) > 8192:
                    raise ValueError("oversized frame")
            except ValueError as error:
                raise httpx.ReadError("Invalid native relay bytes") from error
            yield chunk

    async def aclose(self):
        async def finish():
            if self.process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()
            if not self.stderr.done():
                self.stderr.cancel()
            await asyncio.gather(self.stderr, return_exceptions=True)
            await asyncio.gather(drain(self.process.stdout), drain(self.process.stderr))
            await self.process.wait()

        if self.closing is None:
            self.closing = asyncio.create_task(finish())
        await _settle(self.closing)


async def cleanup(name, root, session, journal, archive):
    """Independent cleanup phases; no inputs deleted until absence is confirmed."""
    errors = []
    for filename in ("opencode.log", "model.jsonl"):
        try:
            code, content, stderr = await docker("exec", name, "cat",
                                                 "/evidence/" + filename, check=False)
            (archive / filename).write_bytes(content)
            if code:
                (archive / (filename + ".error")).write_bytes(stderr)
                raise RuntimeError("Could not archive native " + filename)
        except Exception as error:
            errors.append(error)
    try:
        if session is not None:
            await session.close()
    except Exception as error:
        errors.append(error)
    try:
        if journal is not None:
            journal.close()
    except Exception as error:
        errors.append(error)
    try:
        await docker("rm", "--force", name, check=False)
        _, remaining, _ = await docker("ps", "-a", "--filter", "name=^/" + name + "$",
                                        "--format", "{{.Names}}")
        if remaining.strip():
            raise RuntimeError("Owned native container still exists; inputs retained")
        expected = "selfmod-native-qualification-" + name.removeprefix(
            "recollect-selfmod-native-"
        )
        assert root.name == expected and root_path(root) == root.absolute()
        shutil.rmtree(root)
    except Exception as error:
        errors.append(error)
    if errors:
        raise ExceptionGroup("Native qualification cleanup failed", errors)


async def reconcile_native_launch(name, identity, root, image):
    """Only positive absence or independently matched ownership permits cleanup."""
    _, remaining, _ = await docker(
        "ps", "-a", "--filter", "name=^/" + name + "$", "--format", "{{.Names}}",
    )
    if not remaining.strip():
        return False
    assert remaining.strip() == name.encode(), "Ambiguous native container lookup"
    _, raw, _ = await docker("inspect", name)
    items = json.loads(raw)
    assert len(items) == 1
    item = items[0]
    assert item["Name"] == "/" + name and item["Image"] == image
    assert item["Config"]["Labels"]["recollect.selfmod"] == identity
    binds = [m for m in item["Mounts"] if m["Type"] == "bind"]
    assert len(binds) == 1 and binds[0]["Destination"] == "/authority"
    assert binds[0]["RW"] is False
    assert Path(binds[0]["Source"]).resolve() == root.resolve()
    return True


async def preserve_native_state(name, archive):
    """Freeze writers and retain raw DB/WAL even when semantic capture failed."""
    _, state, _ = await docker(
        "exec", name, "python", "-I", "-S", "-c",
        "import os,signal,json; from pathlib import Path; peers={}\n"
        "for p in Path('/proc').iterdir():\n"
        " if not p.name.isdigit(): continue\n"
        " try: f=dict(s.split(':',1) for s in (p/'status').read_text()"
        ".splitlines() if ':' in s)\n"
        " except FileNotFoundError: continue\n"
        " if f['Uid'].split()[0]=='65532': peers[p.name]=f['State']\n"
        "pidfile=Path('/evidence/native.pid'); "
        "pid=pidfile.read_text() if pidfile.exists() else None\n"
        "assert not peers or list(peers)==[pid],peers\n"
        "if peers:\n"
        " os.setgroups([]); os.setgid(65532); os.setuid(65532); "
        "os.kill(int(pid),signal.SIGSTOP)\n"
        "print(json.dumps({'native_pid':pid,'peers':peers}))",
    )
    await _durable((archive / "native-freeze.json").write_bytes, state)
    _, state, _ = await docker(
        "exec", name, "python", "-I", "-S", "-c",
        "import json; from pathlib import Path; peers={}\n"
        "for p in Path('/proc').iterdir():\n"
        " if not p.name.isdigit(): continue\n"
        " try: f=dict(s.split(':',1) for s in (p/'status').read_text()"
        ".splitlines() if ':' in s)\n"
        " except FileNotFoundError: continue\n"
        " if f['Uid'].split()[0]=='65532': peers[p.name]=f['State']\n"
        "assert all(v.strip().startswith('T') for v in peers.values()),peers\n"
        "print(json.dumps(peers))",
    )
    await _durable((archive / "native-frozen.json").write_bytes, state)
    for filename in ("opencode.db", "opencode.db-wal", "opencode.db-shm"):
        path = "/state/data/opencode/" + filename
        _, raw, _ = await docker(
            "exec", name, "python", "-I", "-S", "-c",
            "import json,stat; from pathlib import Path; p=Path(" + repr(path) + "); "
            "s=p.lstat() if p.exists() else None; "
            "assert s is None or (stat.S_ISREG(s.st_mode) and s.st_nlink==1); "
            "print(json.dumps({'size':s.st_size if s else None}))",
        )
        size = json.loads(raw)["size"]
        digest, chunks = hashlib.sha256(), []
        if size is not None:
            assert type(size) is int and 0 <= size <= 256 * 1024 * 1024
            for offset in range(0, size, 1024 * 1024):
                length = min(1024 * 1024, size - offset)
                _, data, _ = await docker(
                    "exec", name, "python", "-I", "-S", "-c",
                    "import sys; p=open(" + repr(path) + ",'rb'); "
                    f"p.seek({offset}); sys.stdout.buffer.write(p.read({length}))",
                )
                assert len(data) == length
                part = f"{filename}.{offset // (1024 * 1024):04d}.bin"
                await _durable((archive / part).write_bytes, data)
                digest.update(data)
                chunks.append({"file": part, "bytes": len(data),
                               "sha256": hashlib.sha256(data).hexdigest()})
        await _durable((archive / (filename + ".json")).write_bytes, encode({
            "bytes": size, "sha256": digest.hexdigest(), "chunks": chunks,
            "semantic_completeness": "unconfirmed_raw_diagnostic",
        }))


async def qualify_native(tmp_path, automatic, *, inject_failure=False,
                         capture_candidate=False, capture_fault=None,
                         clean_checkpoint=False):
    identity = uuid.uuid4().hex
    name = "recollect-selfmod-native-" + identity
    root = (Path(os.environ["LOCALAPPDATA"]) / "recollect" / "sandboxes"
            / ("selfmod-native-qualification-" + identity))
    root.mkdir()
    session = journal = history_journal = broker_journal = broker = pump = None
    launch_attempted = False
    try:
        profile = settings()
        capture_spec = None
        if capture_candidate:
            baseline = Snapshot((File("editable.py", b"value = 1\n"),
                                 File("protected.py", b"protected = True\n")))
            profile = replace(profile, policy=ChangePolicy(
                baseline.sha256, modify=("editable.py",)))
            binding = Binding("diagnostic-attempt", "diagnostic-author", 1,
                              "1" * 64, baseline.sha256, "2" * 64, None)
            run = NativeRun(
                identity, "diagnostic-controller", "diagnostic-cycle",
                "diagnostic-grant", "diagnostic-author", 1, "implement", binding,
            )
            profile = replace(profile, authority=encode({
                **json.loads(profile.authority), "run": asdict(run),
                "policy": asdict(profile.policy), "baseline_sha256": baseline.sha256,
            }))
            capture_spec = NativeCaptureSpec(run, profile, baseline)
            for file in capture_spec.inputs.files:
                (root / file.path).write_bytes(file.content)
        (root / "opencode.json").write_bytes(encode(profile.config))
        (root / "task.json").write_bytes(profile.authority)
        if automatic:
            (root / "automatic.json").write_bytes(b"{}\n")
        (root / "broker.json").write_bytes(b"{}\n")
        shutil.copyfile(selfmod_native_server.__file__, root / "server.py")
        shutil.copyfile(native_history_reader.__file__, root / "history.py")
        journal = Journal.create(tmp_path / "native-archive")
        history_journal = Journal.create(tmp_path / "history-archive")
        broker_journal = Journal.create(tmp_path / "broker-archive")
        broker = NativeModelBroker(
            BrokerSettings("http://127.0.0.1:7777/v1", profile.model), broker_journal,
            transport=Relay(name, provider=True),
        )

        async def reader(request):
            code, data, error = await docker(
                "exec", "-i", "--user", "0:0", name, "python", "-I", "-S",
                "/authority/history.py", data=encode(request), check=False,
            )
            if code:
                raise native_history_reader.HistoryReadError(
                    error.decode(errors="replace") or "Native history read failed",
                    raw_prefix=data,
                )
            return data

        session = NativeSession(profile, journal, transport=Relay(name),
                                history_reader=reader, history_journal=history_journal)
        _, image, _ = await docker("image", "inspect",
                                   "recollect-opencode-sandbox:1.18.18",
                                   "--format", "{{.Id}}")
        launch_attempted = True
        await _settle(asyncio.create_task(docker(
            "run", "--detach", "--pull=never", "--name", name,
            "--label", "recollect.selfmod=" + identity,
            "--user", "0:0", "--read-only", "--network", "none", "--ipc", "none",
            "--cap-drop", "ALL", "--cap-add", "CHOWN", "--cap-add", "SETUID",
            "--cap-add", "SETGID", "--cap-add", "DAC_READ_SEARCH",
            *(["--cap-add", "KILL"] if capture_candidate else []),
            "--security-opt", "no-new-privileges:true",
            "--memory", "1024m", "--memory-swap", "1024m", "--cpus", "1",
            "--pids-limit", "256", "--ulimit", "nofile=1024:1024",
            "--log-driver", "none", "--restart", "no",
            "--tmpfs", "/work:rw,noexec,nosuid,nodev,size=32m,mode=0755",
            "--tmpfs", "/state:rw,noexec,nosuid,nodev,size=256m,mode=0700,uid=65532",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
            "--tmpfs", "/evidence:rw,noexec,nosuid,nodev,size=32m,mode=0700",
            "--mount", f"type=bind,source={root},target=/authority,readonly",
            "--entrypoint", "python", image.decode().strip(),
            "-I", "-S", "/authority/server.py",
        )))
        _, inspection, _ = await docker("inspect", name)
        item = json.loads(inspection)[0]
        assert item["Image"] == image.decode().strip()
        assert item["HostConfig"]["NetworkMode"] == "none"
        assert item["HostConfig"]["ReadonlyRootfs"] is True
        assert item["HostConfig"]["Memory"] == 1024 * 1024 * 1024
        assert sorted(c.removeprefix("CAP_")
                      for c in item["HostConfig"]["CapAdd"]) == sorted([
            "CHOWN", "DAC_READ_SEARCH", "SETGID", "SETUID",
            *(["KILL"] if capture_candidate else []),
        ])
        assert [(m["Destination"], m["RW"]) for m in item["Mounts"]
                if m["Type"] == "bind"] == [("/authority", False)]
        await _durable(journal.append, "native_fixture", {
            "image_id": image.decode().strip(), "controller_eligible": False,
        }, Snapshot((File("inspection.json", inspection), File(
            "provider.py", Path(selfmod_native_server.__file__).read_bytes(),
        ), File("relay.py", RELAY.encode()))))
        while True:
            code, _, _ = await docker(
                "exec", name, "python", "-I", "-S", "-c",
                "import urllib.request; urllib.request.urlopen("
                "'http://127.0.0.1:4096/global/health', timeout=5).read()", check=False,
            )
            if code == 0:
                break
            _, exited, _ = await docker(
                "exec", name, "python", "-I", "-S", "-c",
                "from pathlib import Path; "
                "print(Path('/evidence/native-exit.json').exists())",
            )
            assert exited.strip() == b"False", "Native process exited during startup"
            _, running, _ = await docker("inspect", name, "--format",
                                         "{{.State.Running}}")
            assert running.strip() == b"true", "Native server exited during startup"
            await asyncio.sleep(0.1)
        await session.start()

        async def model_pump():
            while True:
                _, raw, _ = await _settle(asyncio.create_task(docker(
                    "exec", name, "python", "-I", "-S", "-c",
                    "import sys; from pathlib import Path; "
                    "p=Path('/evidence/request.json')\n"
                    "if p.exists():\n"
                    " sys.stdout.buffer.write(p.read_bytes()); p.unlink()",
                )))
                if not raw:
                    await asyncio.sleep(0.01)
                    continue
                await session.history.capture()
                watermark = session.history.durable_head
                head, chunks = [], []

                async def response(value, head=head):
                    head.append(value)

                async def chunk(value, chunks=chunks):
                    # Only this scripted qualification bridge buffers a reply.
                    # Production must use streaming IPC with backpressure.
                    assert sum(map(len, chunks)) + len(value) < 1024 * 1024
                    chunks.append(value)

                await broker.forward(
                    base64.b64decode(json.loads(raw)["body"], validate=True),
                    identity=BrokerIdentity(identity, session.session_id,
                                            watermark["seq"], watermark["sha256"]),
                    guard=lambda: None, on_response=response, on_chunk=chunk,
                )
                await docker(
                    "exec", "-i", name, "python", "-I", "-S", "-c",
                    "import sys,os; from pathlib import Path; "
                    "p=Path('/evidence/response.tmp'); "
                    "p.write_bytes(sys.stdin.buffer.read()); "
                    "os.replace(p,'/evidence/response.json')",
                    data=encode({"status": head[0].status_code,
                                 "body": base64.b64encode(b''.join(chunks)).decode()}),
                )

        pump = asyncio.create_task(model_pump())

        async def invoke(operation):
            task = asyncio.create_task(operation)
            try:
                done, _ = await asyncio.wait({task, pump},
                                             return_when=asyncio.FIRST_COMPLETED)
                if pump in done:
                    await pump
                    raise AssertionError("Model bridge exited during native work")
                return await task
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        first = await invoke(session.prompt(
            "QUALIFY_NATIVE_EDIT: implement the frozen task",
        ))
        assert first["info"]["finish"] == "stop"
        if inject_failure:
            # Complete another native turn without the wrapper's final capture.
            await invoke(session._request(
                "POST", f"/session/{session.session_id}/message", {
                    "messageID": "msg_" + uuid.uuid4().hex, "agent": "build",
                    "model": {"providerID": "recollect", "modelID": profile.model},
                    "system": profile.authority.decode(),
                    "parts": [{"type": "text", "text": "UNARCHIVED_FINAL_MARKER"}],
                },
            ))

            async def broken_reader(request):
                raise native_history_reader.HistoryReadError("injected reader failure")

            session.history.reader = broken_reader
            await session.history.capture()
            raise AssertionError("Injected reader did not fail")
        if not automatic:
            await invoke(session.compact())
        await invoke(session.prompt("Continue the original task after compaction"))
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
        _, content, _ = await docker("exec", name, "cat", "/work/editable.py")
        assert content == b"value = 2\n"
        _, authority, _ = await docker("exec", name, "cat", "/authority/task.json")
        assert authority == profile.authority
        _, model, _ = await docker("exec", name, "cat", "/evidence/model.jsonl")
        requests = [json.loads(line) for line in model.splitlines()]
        # Capacity still informs native compaction; only the host wire is uncapped.
        assert all(request.get("max_tokens") == profile.output_limit
                   for request in requests)
        _, forwarded, _ = await docker("exec", name, "cat", "/evidence/forwarded.jsonl")
        (tmp_path / "forwarded.jsonl").write_bytes(forwarded)
        forwarded_requests = [json.loads(line) for line in forwarded.splitlines()]
        assert len(forwarded_requests) == len(requests)
        for original, sent in zip(requests, forwarded_requests, strict=True):
            assert not TOKEN_CAP_FIELDS & sent.keys()
            assert sent == {k: v for k, v in original.items()
                            if k not in TOKEN_CAP_FIELDS}
        assert all("title generator" not in json.dumps(r) for r in requests)
        assert any(not request.get("tools") for request in requests)
        native_tools = {t["function"]["name"] for request in requests
                        for t in request.get("tools", [])}
        assert {"read", "edit"} <= native_tools
        assert not {"bash", "task", "webfetch", "websearch"} & native_tools
        assert any("Summary: abandon original task" in json.dumps(request)
                   for request in requests)
        assert any(profile.authority.decode() in m.get("content", "")
                   for m in requests[-1]["messages"] if m["role"] == "system")
        records = journal.verify()
        histories = [json.loads(f.content) for r in records
                     if r.value["kind"] == "native_response"
                     for f in r.files.files if f.content.startswith(b"[")]
        parts = [p for history in histories for m in history for p in m["parts"]]
        assert any(p["type"] == "compaction" for p in parts)
        assert any(p["type"] == "compaction" and p["auto"] is automatic
                   for p in parts)
        assert {"read", "edit"} <= {p.get("tool") for p in parts
                                    if p.get("state", {}).get("status") == "completed"}
        events = list(iter_event_rows(history_journal.verify(),
                                      session_id=session.session_id))
        assert [e["seq"] for e in events] == list(range(len(events)))
        assert any("value = 1" in e["data"] for e in events)
        assert any("Edit applied successfully" in e["data"] for e in events)
        _, credentials, _ = await docker(
            "exec", name, "python", "-I", "-S", "-c",
            "from pathlib import Path; pid=Path('/evidence/native.pid').read_text(); "
            "assert pid.isdigit(); print(Path('/proc/'+pid+'/status').read_text())",
        )
        fields = dict(line.split(":", 1) for line in credentials.decode().splitlines()
                      if ":" in line)
        assert set(fields["Uid"].split()) == {"65532"}
        assert all(int(fields[k], 16) == 0 for k in ("CapEff", "CapPrm", "CapAmb"))
        for code_text in (
            "from pathlib import Path; Path('/work/protected.py').write_text('drift')",
            "from pathlib import Path; Path('/work/editable.py').unlink()",
            "from pathlib import Path; "
            "Path('/authority/task.json').write_text('drift')",
        ):
            code, _, _ = await docker("exec", "--user", "65532:65532", name,
                                      "python", "-I", "-S", "-c", code_text,
                                      check=False)
            assert code != 0
        if capture_candidate:
            _, raw_native, _ = await docker(
                "exec", name, "python", "-I", "-S", "-c",
                "import json; from pathlib import Path; "
                "pid=int(Path('/evidence/native.pid').read_text()); "
                "s=Path('/proc/'+str(pid)+'/stat').read_text(); "
                "print(json.dumps({'pid':pid,'start':int(s[s.rfind(')')+2:]"
                ".split()[19])}))",
            )
            native = json.loads(raw_native)
            _, stop_raw, _ = await docker(
                "exec", "-i", name, "python", "-I", "-S", "-u", "-B",
                "/authority/capture_worker.py",
                data=capture_spec.stop_request(native),
            )
            await _durable(journal.append, "native_terminal_stop", {},
                           Snapshot((File("stop.json", stop_raw),)))
            verify_stop(stop_raw, capture_spec, native)
            await session.history.finalize()
            request = await _durable(capture_request, capture_spec, stop_raw,
                                      native, session.history)
            faults = {
                "symlink": "p.unlink(); p.symlink_to('/authority/task.json')",
                "hardlink": "p.unlink(); os.link('/work/protected.py',p)",
                "protected_drift": (
                    "q=Path('/work/protected.py'); q.chmod(0o644); "
                    "q.write_bytes(b'drift'); q.chmod(0o444)"),
                "mode": "p.chmod(0o666)",
                "history": (
                    "import sqlite3; "
                    "c=sqlite3.connect('/state/data/opencode/opencode.db'); "
                    "c.execute(\"UPDATE event SET data=data||' '\"); c.commit(); "
                    "c.close()"),
            }
            if capture_fault == "history":
                await docker("exec", "--user", "65532:65532", name,
                             "python", "-I", "-S", "-c", faults[capture_fault])
            elif capture_fault is not None:
                await docker("exec", name, "python", "-I", "-S", "-c",
                             "import os; from pathlib import Path; "
                             "p=Path('/work/editable.py'); os.chmod('/work',0o755); "
                             + faults[capture_fault] + "; os.chmod('/work',0o555)")
            if clean_checkpoint:
                await docker(
                    "exec", "--user", "65532:65532", name,
                    "python", "-I", "-S", "-c", "import sqlite3; "
                    "c=sqlite3.connect('/state/data/opencode/opencode.db'); "
                    "assert c.execute('PRAGMA wal_checkpoint(TRUNCATE)')"
                    ".fetchone()[0]==0; c.close()",
                )
            if clean_checkpoint or capture_fault == "history":
                _, diagnostic, _ = await docker(
                    "exec", name, "python", "-I", "-S", "-c",
                    "import sqlite3; from pathlib import Path; "
                    "p=Path('/state/data/opencode/opencode.db'); "
                    "assert not any(Path(str(p)+s).exists() "
                    "for s in ('-wal','-shm')); "
                    "c=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)\n"
                    "try: c.execute('SELECT count(*) FROM event').fetchone()\n"
                    "except sqlite3.OperationalError as e:\n"
                    " assert 'readonly' in str(e); print(str(e))\n"
                    "else: raise AssertionError('Expected original readonly failure')\n"
                    "finally: c.close()",
                )
                await _durable(journal.append, "native_readonly_reproduction", {},
                               Snapshot((File("sqlite.txt", diagnostic),)))
            state_probe = (
                "import hashlib,json; from pathlib import Path; result={}\n"
                "for p in Path('/state/data/opencode').glob('opencode.db*'):\n"
                " s=p.stat(); h=hashlib.sha256()\n"
                " with p.open('rb') as f:\n"
                "  while data:=f.read(1048576): h.update(data)\n"
                " result[p.name]=[s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_size,"
                "s.st_mtime_ns,s.st_ctime_ns,h.hexdigest()]\n"
                "print(json.dumps(result,sort_keys=True))"
            )
            _, state_before, _ = await docker(
                "exec", name, "python", "-I", "-S", "-c", state_probe,
            )
            code, captured_raw, capture_error = await docker(
                "exec", "-i", name, "python", "-I", "-S", "-u", "-B",
                "/authority/capture_worker.py", data=request, check=False,
            )
            _, state_after, _ = await docker(
                "exec", name, "python", "-I", "-S", "-c", state_probe,
            )
            assert state_after == state_before
            await docker(
                "exec", name, "python", "-I", "-S", "-c",
                "from pathlib import Path; "
                "assert not list(Path('/evidence').glob('native-history-*'))",
            )
            await _durable(journal.append, "native_source_capture", {
                "execution_receipt": False, "exitcode": code,
            }, Snapshot((File("capture.json", captured_raw),
                         File("capture.stderr", capture_error))))
            if capture_fault is not None:
                assert code != 0 and capture_error
                assert not captured_raw
                expected_error = {
                    "symlink": b"Linked or special source entry",
                    "hardlink": b"Linked or special source entry",
                    "protected_drift": b"Protected source drift",
                    "mode": b"Source ownership/mode mismatch",
                    "history": b"Prior row identity mismatch",
                }[capture_fault]
                assert expected_error in capture_error
                return
            assert code == 0, capture_error.decode(errors="replace")
            captured = await _durable(verify_capture, captured_raw, capture_spec,
                                      stop_raw, native, session.history)
            assert {f.path: f.content for f in captured.files} == {
                "editable.py": b"value = 2\n", "protected.py": b"protected = True\n",
            }
            return
        # Stop the native writer's entire thread group, then independently verify
        # that no other unprivileged processes remain before the final DB read.
        await docker(
            "exec", name, "python", "-I", "-S", "-c",
            "import os,signal; from pathlib import Path; "
            "pid=int(Path('/evidence/native.pid').read_text()); "
            "os.setgroups([]); os.setgid(65532); os.setuid(65532); "
            "os.kill(pid,signal.SIGSTOP)",
        )
        _, stopped, _ = await docker(
            "exec", name, "python", "-I", "-S", "-c",
            "import json; from pathlib import Path; peers={}\n"
            "for p in Path('/proc').iterdir():\n"
            " if not p.name.isdigit(): continue\n"
            " try: fields=dict(s.split(':',1) for s in (p/'status').read_text()"
            ".splitlines() if ':' in s)\n"
            " except FileNotFoundError: continue\n"
            " if fields['Uid'].split()[0]=='65532': peers[p.name]=fields['State']\n"
            "pid=Path('/evidence/native.pid').read_text(); "
            "assert list(peers)==[pid] and peers[pid].strip().startswith('T'); "
            "print(json.dumps(peers))",
        )
        await _durable(history_journal.append, "native_writers_frozen", {},
                       Snapshot((File("processes.json", stopped),)))
        await session.history.finalize()
        assert session.history.final_metadata["final"] is True
    finally:
        async def finish():
            errors = []
            preserve_failed = False
            if pump is not None and not pump.done():
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
            try:
                if broker is not None:
                    await broker.close()
            except Exception as error:
                errors.append(error)
            if launch_attempted:
                preserve_failed = True
                try:
                    exists = await reconcile_native_launch(
                        name, identity, root, image.decode().strip(),
                    )
                    if exists:
                        await preserve_native_state(name, tmp_path)
                    preserve_failed = False
                    if (exists and session is not None and session.history is not None
                            and not session.history.poisoned
                            and not session.history.final_metadata["final"]):
                        await session.history.finalize()
                except Exception as error:
                    errors.append(error)
            try:
                if preserve_failed:
                    raise RuntimeError(
                        "Native state capture failed; frozen container and inputs "
                        "retained: " + name + " " + str(root),
                    )
                await cleanup(name, root, session, journal, tmp_path)
            except Exception as error:
                errors.append(error)
            for archive in (history_journal, broker_journal):
                try:
                    if archive is not None:
                        archive.close()
                except Exception as error:
                    errors.append(error)
            if errors:
                raise ExceptionGroup("Native bridge cleanup failed", errors)

        await _settle(asyncio.create_task(finish()))


@pytest.mark.parametrize("automatic", [False, True])
async def test_native_read_edit_compact_continue_with_protected_authority(
    tmp_path, automatic,
):
    await qualify_native(tmp_path, automatic)


async def test_failed_history_capture_preserves_native_database_and_wal(tmp_path):
    with pytest.raises(native_history_reader.HistoryReadError, match="injected"):
        await qualify_native(tmp_path, True, inject_failure=True)
    retained = bytearray()
    restored = tmp_path / "raw-state-readback"
    restored.mkdir()
    for filename in ("opencode.db", "opencode.db-wal", "opencode.db-shm"):
        manifest = json.loads((tmp_path / (filename + ".json")).read_bytes())
        data = b"".join((tmp_path / p["file"]).read_bytes() for p in manifest["chunks"])
        assert hashlib.sha256(data).hexdigest() == manifest["sha256"]
        if manifest["bytes"] is not None:
            assert len(data) == manifest["bytes"]
            (restored / filename).write_bytes(data)
        retained.extend(data)
    assert b"UNARCHIVED_FINAL_MARKER" in retained
    failed = inspect_archive(tmp_path / "history-archive")[-1].value["data"]
    readback = native_history_reader.read_page(
        restored / "opencode.db", failed["native_session_id"],
    )
    assert readback["head"] > failed["durable_sequence"]


@pytest.mark.parametrize("clean_checkpoint", [False, True])
async def test_native_terminal_capture_returns_exact_immutable_candidate(
    tmp_path, clean_checkpoint,
):
    await qualify_native(tmp_path, True, capture_candidate=True,
                         clean_checkpoint=clean_checkpoint)


@pytest.mark.parametrize("fault", ["symlink", "hardlink", "protected_drift", "mode",
                                   "history"])
async def test_native_capture_rejects_tampered_source_or_history(tmp_path, fault):
    await qualify_native(tmp_path, True, capture_candidate=True, capture_fault=fault)
