"""Opt-in qualification against real Linux Docker; no model or provider calls."""

import asyncio
import base64
import json
import os
import shutil
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from recollect.selfmod.containment import (
    MAX_WIRE_BYTES,
    attest,
    create_arguments,
    frozen_input,
    release_record,
    verified_snapshot,
    verify_ready,
)
from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.files import materialize, verify_materialized
from recollect.selfmod.journal import IntegrityError, Journal, decode, sha256
from tests.selfmod_containment_helpers import spec

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("RECOLLECT_RUN_SELFMOD_DOCKER_TESTS") != "1",
        reason="opt in to real selfmod Docker qualification",
    ),
]


async def docker(*args):
    runtime = shutil.which("docker")
    assert runtime, "Docker CLI is required; no host fallback"
    process = await asyncio.create_subprocess_exec(
        runtime, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise RuntimeError(stderr.decode("utf-8", "replace"))
    return stdout


class RunningFixture:
    def __init__(self, config, input_dir, journal):
        self.config, self.input_dir, self.journal = config, input_dir, journal
        self.container_id = None
        self.process = None
        self.ready = b""
        self.stderr = None
        self.supervisor_sha = None
        self.started = time.monotonic()

    async def inspect(self):
        return json.loads(await docker("inspect", self.container_id))[0]

    async def start(self):
        captured = frozen_input(self.config)
        self.supervisor_sha = sha256(
            next(f.content for f in captured.files if f.path == "supervisor.py")
        )
        await asyncio.to_thread(self.journal.append, "fixture_input", {}, captured)
        await asyncio.to_thread(materialize, self.input_dir, captured)
        self.container_id = (
            (await docker(*create_arguments(self.config, self.input_dir)))
            .decode()
            .strip()
        )
        initial = await self.inspect()
        attest(initial, self.config, self.input_dir, self.container_id)
        self.process = await asyncio.create_subprocess_exec(
            shutil.which("docker"),
            "start",
            "--attach",
            "--interactive",
            self.container_id,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=MAX_WIRE_BYTES + 2048,
        )
        self.stderr = asyncio.create_task(self.process.stderr.read(65537))
        self.ready = await asyncio.wait_for(self.process.stdout.readline(), 8)
        verify_ready(self.ready, self.config, self.supervisor_sha)
        running = await self.inspect()
        attest(running, self.config, self.input_dir, self.container_id)
        assert running["State"]["Running"] and not running["State"]["Restarting"]
        await asyncio.to_thread(verify_materialized, self.input_dir, captured)
        await asyncio.to_thread(
            self.journal.append,
            "fixture_attested",
            running,
            Snapshot((File("ready.json", self.ready),)),
        )

    async def release(self, maximum_ms=None):
        remaining = (
            self.config.timeout_ms - int((time.monotonic() - self.started) * 1000) - 1
            if self.config.timeout_ms is not None else None
        )
        if maximum_ms is not None:
            remaining = min(remaining, maximum_ms)
        wire = release_record(self.config, self.supervisor_sha, remaining)
        await asyncio.to_thread(
            self.journal.append,
            "fixture_release_intent",
            {},
            Snapshot((File("release.json", wire),)),
        )
        # Account for durable recording too; never send the earlier larger interval.
        if remaining is not None:
            elapsed_ms = int((time.monotonic() - self.started) * 1000)
            remaining = min(
                remaining, self.config.timeout_ms - elapsed_ms - 1,
            )
        actual_wire = release_record(self.config, self.supervisor_sha, remaining)
        self.process.stdin.write(actual_wire)
        await self.process.stdin.drain()
        await asyncio.to_thread(
            self.journal.append,
            "fixture_release_sent",
            {},
            Snapshot((File("release.json", actual_wire),)),
        )

    async def finish(self):
        wire = await asyncio.wait_for(self.process.stdout.readline(), 15)
        tail = await asyncio.wait_for(self.process.stdout.read(MAX_WIRE_BYTES + 1), 5)
        await asyncio.wait_for(self.process.wait(), 5)
        stderr = await asyncio.wait_for(self.stderr, 5)
        state = await self.inspect()
        await asyncio.to_thread(
            self.journal.append,
            "fixture_observed",
            state,
            Snapshot(
                (
                    File("stdout.bin", self.ready + wire + tail),
                    File("stderr.bin", stderr),
                )
            ),
        )
        assert self.process.returncode == 0, stderr.decode("utf-8", "replace")
        assert not tail and len(stderr) <= 65536
        assert state["State"]["Status"] == "exited"
        assert not state["State"]["Running"] and state["State"]["Pid"] == 0
        assert state["State"]["ExitCode"] == 0 and not state["State"]["OOMKilled"]
        return wire

    async def close(self):
        if self.container_id:
            state = await self.inspect()
            assert state["Id"] == self.container_id
            assert state["Config"]["Labels"]["recollect.selfmod"] == self.config.run_id
            assert state["Config"]["Labels"]["recollect.spec"] == self.config.sha256
            if state["State"]["Running"]:
                await docker("kill", "--signal=KILL", self.container_id)
                state = await self.inspect()
            assert state["State"]["Status"] in {"created", "exited"}
            await asyncio.to_thread(self.journal.append, "fixture_cleanup", state)
            await docker("rm", self.container_id)
        if self.process and self.process.returncode is None:
            self.process.kill()
            await self.process.wait()
        if self.stderr:
            await asyncio.wait_for(self.stderr, 5)


@pytest.fixture
async def launch(tmp_path):
    assert (await docker("info", "--format", "{{.OSType}}")).strip() == b"linux"
    image = json.loads(
        await docker("image", "inspect", "recollect-opencode-sandbox:1.18.18")
    )[0]
    assert not image["Config"].get("Volumes"), "Image-declared volumes are forbidden"
    active = []

    async def start(code, maximum_ms=None, *, unbounded=False):
        identity = uuid.uuid4().hex
        config = replace(
            spec(code.encode(), timeout_ms=None if unbounded else 10_000),
            run_id=identity,
            image_id=image["Id"],
            image_environment=tuple(image["Config"].get("Env") or ()),
        )
        journal = await asyncio.to_thread(Journal.create, tmp_path / identity)
        # Use only the documented Docker Desktop share; never mount the repository.
        shared = (
            Path(os.environ["LOCALAPPDATA"]) / "recollect" / "sandboxes"
            if os.name == "nt"
            else tmp_path
        )
        shared.mkdir(parents=True, exist_ok=True)
        input_dir = shared / ("selfmod-fixture-input-" + identity)
        instance = RunningFixture(config, input_dir, journal)
        active.append(instance)
        await instance.start()
        await instance.release(maximum_ms)
        return instance

    yield start
    for instance in reversed(active):
        try:
            await instance.close()
        finally:
            instance.journal.close()
            # Only remove this test's fresh, exact input directory; raw evidence
            # was archived separately before the container or input was removed.
            if instance.input_dir.exists():
                assert (
                    instance.input_dir.name
                    == "selfmod-fixture-input-" + instance.config.run_id
                )
                await asyncio.to_thread(shutil.rmtree, instance.input_dir)


async def test_kernel_denies_protected_changes_and_worker_escalation(launch):
    instance = await launch("""
import os, signal, socket
from pathlib import Path
assert os.geteuid() == os.getegid() == 65532 and os.getgroups() == []
for operation in (
    lambda: Path('protected.py').write_text('drift'),
    lambda: Path('protected.py').chmod(0o777),
    lambda: Path('protected.py').unlink(),
    lambda: Path('protected.py').rename('other.py'),
    lambda: Path('editable.py').chmod(0o777),
    lambda: Path('editable.py').unlink(),
    lambda: Path('/input/spec.json').write_text('forged'),
    lambda: Path('/proc/1/fd/1').open('wb'),
    lambda: os.kill(1, signal.SIGKILL),
    lambda: os.setuid(0),
):
    try:
        operation()
    except OSError:
        pass
    else:
        raise AssertionError('forbidden operation succeeded')
assert not Path('/var/run/docker.sock').exists()
try:
    socket.create_connection(('127.0.0.1', 8001), timeout=.2)
except OSError:
    pass
else:
    raise AssertionError('host model unexpectedly reachable')
Path('editable.py').write_text('value = 2\\n')
Path('generated/new.py').write_text('fixture source\\n')
print('only fixture output')
""")
    wire = await instance.finish()
    snapshot = verified_snapshot(wire, instance.config, instance.supervisor_sha)
    contents = {f.path: f.content for f in snapshot.files}
    assert contents["editable.py"] == b"value = 2\n"
    assert contents["generated/new.py"] == b"fixture source\n"


@pytest.mark.parametrize(
    "code,reason",
    [
        ("while True: pass", "watchdog_timeout"),
        ("import os\nwhile True: os.write(1, b'x' * 8192)", "output_limit"),
        (
            "import os, time\nif os.fork() == 0:\n os.setsid()\n"
            " while True: time.sleep(.01)",
            "descendants_after_exit",
        ),
    ],
)
async def test_runaway_or_detached_work_never_yields_candidate(launch, code, reason):
    instance = await launch(code, 500)
    wire = await instance.finish()
    assert decode(wire)["reason"] == reason
    with pytest.raises(IntegrityError):
        verified_snapshot(wire, instance.config, instance.supervisor_sha)


@pytest.mark.parametrize(
    "operation",
    [
        "p.chmod(0)",
        "os.link(p, 'generated/link')",
        "os.symlink('/etc/passwd', 'generated/link')",
        "os.mkfifo('generated/pipe')",
        "Path('generated/A.py').write_text('a'); "
        "Path('generated/a.py').write_text('b')",
    ],
)
async def test_invalid_files_cannot_become_candidate_snapshots(launch, operation):
    instance = await launch(
        "import os\nfrom pathlib import Path\np = Path('generated/file')\n"
        "p.write_text('evidence')\n" + operation
    )
    wire = await instance.finish()
    with pytest.raises(ValueError):
        verified_snapshot(wire, instance.config, instance.supervisor_sha)


async def test_attachment_death_has_bounded_orphan_lifetime(launch):
    instance = await launch("while True: pass", 500)
    instance.process.kill()
    await instance.process.wait()
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        state = await instance.inspect()
        if not state["State"]["Running"]:
            break
        await asyncio.sleep(0.05)
    assert state["State"]["Status"] == "exited" and state["State"]["Pid"] == 0
    await asyncio.to_thread(
        instance.journal.append, "orphan_termination_observed", state
    )


async def test_worker_cannot_forge_supervisor_result_channel(launch):
    instance = await launch(
        'print(\'{\\"kind\\":\\"result\\",\\"reason\\":\\"completed\\"}\')'
    )
    wire = await instance.finish()
    result = decode(wire)
    assert b'"kind":"result"' in base64.b64decode(result["stdout"])
    assert verified_snapshot(wire, instance.config, instance.supervisor_sha)


@pytest.mark.parametrize("unbounded", [False, True])
async def test_watchdog_is_independent_of_stopped_supervisor(launch, unbounded):
    instance = await launch(
        "while True: pass", None if unbounded else 2000, unbounded=unbounded,
    )
    deadline = time.monotonic() + 6
    injection = await docker(
        "exec",
        "--user",
        "0:0",
        instance.container_id,
        "/usr/local/bin/python",
        "-I",
        "-S",
        "-c",
        "import json, os, signal, time; from pathlib import Path; "
        "children = Path('/proc/1/task/1/children').read_text().split(); "
        "matches = [int(p) for p in children if "
        "Path('/proc/' + p + '/cmdline').read_bytes().rstrip(b'\\0').split(b'\\0')"
        "[-1] == b'/input/supervisor.py']; "
        "assert len(matches) == 1, matches; child = matches[0]; "
        "status = Path('/proc/' + str(child) + '/status'); "
        "assert 'Uid:\\t0\\t0\\t0\\t0' in status.read_text(); "
        "os.kill(child, signal.SIGSTOP); stop_deadline = time.monotonic() + .5\n"
        "while 'State:\\tT' not in status.read_text() "
        "and time.monotonic() < stop_deadline:\n time.sleep(.005)\n"
        "assert 'State:\\tT' in status.read_text(); "
        "print(json.dumps({'supervisor_pid': child, 'state': 'stopped'}))",
    )
    await asyncio.to_thread(
        instance.journal.append, "watchdog_fault_injected", json.loads(injection)
    )
    if unbounded:
        # Explicit disconnect, not supervisor silence, stops unbounded work.
        instance.process.stdin.close()
        await instance.process.stdin.wait_closed()
    while time.monotonic() < deadline:
        state = await instance.inspect()
        if not state["State"]["Running"]:
            break
        await asyncio.sleep(0.05)
    assert state["State"]["Status"] == "exited" and state["State"]["Pid"] == 0
    assert state["State"]["ExitCode"] == (125 if unbounded else 124)
    await asyncio.to_thread(
        instance.journal.append, "stalled_supervisor_terminated", state
    )


async def test_diagnostic_output_survives_normal_worker_timeout(launch):
    instance = await launch("print('before timeout')\nwhile True: pass", 500)
    wire = await instance.finish()
    assert decode(wire)["reason"] == "watchdog_timeout"
    assert base64.b64decode(decode(wire)["stdout"]) == b"before timeout\n"


async def test_cgroup_v2_memory_limits_and_effective_oom_killing(launch):
    if (await docker("info", "--format", "{{.CgroupVersion}}")).strip() != b"2":
        pytest.skip("This qualification case measures cgroup v2 kernel controls")
    instance = await launch("""
import json, os, signal, time
from pathlib import Path
root = Path('/sys/fs/cgroup')
assert (root / 'cgroup.controllers').exists()
limits = {name: (root / name).read_text().strip()
          for name in ('memory.max', 'memory.swap.max')}
assert limits == {'memory.max': '268435456', 'memory.swap.max': '0'}, limits
for name in limits:
    try:
        descriptor = os.open(root / name, os.O_WRONLY)
    except OSError:
        pass
    else:
        os.close(descriptor)
        raise AssertionError('worker can change memory limits')
def events():
    values = dict(line.split() for line in
                  (root / 'memory.events').read_text().splitlines())
    return {key: int(values[key]) for key in ('max', 'oom', 'oom_kill')}
before = events()
children = []
for _ in range(2):
    pid = os.fork()
    if pid == 0:
        allocation = bytearray(144 * 1024 * 1024)
        for offset in range(0, len(allocation), 4096):
            allocation[offset] = 1
        time.sleep(2)
        os._exit(0)
    children.append(pid)
statuses = [os.waitpid(pid, 0)[1] for pid in children]
after = events()
assert any(os.WIFSIGNALED(s) and os.WTERMSIG(s) == signal.SIGKILL
           for s in statuses), statuses
assert all(after[key] > before[key] for key in before), (before, after)
print(json.dumps({'limits': limits, 'events_before': before,
                  'events_after': after, 'child_statuses': statuses}))
""")
    # Docker marks the container OOMKilled even when only a child was killed.
    # The normal candidate gate must reject that run; inspect its failure evidence.
    with pytest.raises(AssertionError):
        await instance.finish()
    records = await asyncio.to_thread(instance.journal.verify)
    record = next(r for r in records if r.value["kind"] == "fixture_observed")
    state = record.value["data"]["State"]
    assert state["OOMKilled"] is True and state["ExitCode"] == 0
    assert state["Status"] == "exited" and state["Pid"] == 0
    streams = {f.path: f.content for f in record.files.files}
    ready, wire = streams["stdout.bin"].splitlines()
    assert ready + b"\n" == instance.ready
    observed = json.loads(base64.b64decode(decode(wire + b"\n")["stdout"]))
    assert observed["limits"] == {"memory.max": "268435456", "memory.swap.max": "0"}
    assert all(observed["events_after"][key] > observed["events_before"][key]
               for key in ("max", "oom", "oom_kill"))
