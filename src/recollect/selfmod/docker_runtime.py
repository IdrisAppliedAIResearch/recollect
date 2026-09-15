"""Local-only Docker fixture adapter. No startup, pull, build or host fallback."""

import json
import os
import re
import threading
import uuid
from pathlib import Path

from .checkpoints import materialize, verify_materialized
from .containment import MAX_WIRE_BYTES, create_arguments, frozen_input, verify_ready
from .contracts import File, Snapshot
from .controller import current_stamp
from .executor import Collected, Deadline, Prepared, Termination
from .journal import IntegrityError, encode, regular, root_path, sha256
from .process import PipeCommand


class DockerFixtureRuntime:
    def __init__(
        self,
        executable: Path,
        shared_root: Path,
        endpoint: str,
        *,
        clock=current_stamp,
        command_factory=PipeCommand,
    ):
        if not executable.is_absolute() or executable.name.lower() not in {
            "docker",
            "docker.exe",
        }:
            raise ValueError("Freeze an absolute native Docker CLI path")
        regular(executable)
        if not (
            re.fullmatch(r"npipe:////\./pipe/[A-Za-z0-9_-]+", endpoint)
            or re.fullmatch(r"unix:///[A-Za-z0-9_./-]+", endpoint)
        ):
            raise ValueError("Only explicit local Docker endpoints are permitted")
        self._root = root_path(shared_root)
        self._config = self._root / ("fixture-cli-" + uuid.uuid4().hex)
        self._config_files = Snapshot((File("config.json", b'{"auths":{}}\n'),))
        materialize(self._config, self._config_files)
        self._argv = [
            str(executable),
            "--config",
            str(self._config),
            "--host",
            endpoint,
        ]
        # No inherited contexts, proxy injection, TLS paths, auth or CLI overrides.
        names = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "PATHEXT"}
        self._env = {k: v for k, v in os.environ.items() if k.upper() in names}
        self._clock, self._factory = clock, command_factory
        self._cleanup_clock = current_stamp
        self._cancelled = threading.Event()
        self._spec = self._inputs = self._worker = self._attachment = None
        self._create_started = False
        self._trace = []
        self._termination = None

    def cancel(self):
        self._cancelled.set()

    def _check(self, deadline, *, cleanup=False):
        if not cleanup and self._cancelled.is_set():
            raise InterruptedError("Fixture runtime cancelled")
        now = self._cleanup_clock() if cleanup else self._clock()
        if now.boot_id != deadline.boot_id or (
            deadline.monotonic_ns is not None
            and now.monotonic_ns >= deadline.monotonic_ns
        ):
            raise TimeoutError("Fixture runtime deadline exhausted")
        verify_materialized(self._config, self._config_files)

    def _command(self, args, deadline, *, cleanup=False):
        self._check(deadline, cleanup=cleanup)
        command = self._factory(
            [*self._argv, *args],
            deadline,
            stdout_limit=65536,
            cancelled=None if cleanup else self._cancelled,
            clock=self._cleanup_clock if cleanup else self._clock,
            env=dict(self._env),
        )
        try:
            stdout, stderr, code = command.finish()
            if code != 0:
                raise IntegrityError("Docker command failed: " + args[0])
            self._check(deadline, cleanup=cleanup)
            return stdout
        finally:
            try:
                command.close()
            finally:
                stdout, stderr = command.evidence()
                index = len(self._trace)
                self._trace.extend(
                    (
                        File(f"cli/{index}.stdout", stdout),
                        File(f"cli/{index}.stderr", stderr),
                    )
                )

    def prepare(self, spec, inputs, deadline):
        if self._spec is not None:
            raise IntegrityError("Docker runtime is single-use")
        self._spec, self._inputs = spec, inputs
        self._check(deadline)
        if inputs != frozen_input(spec):
            raise IntegrityError("Runtime inputs do not match frozen fixture")
        self._input_dir = self._root / ("selfmod-fixture-input-" + spec.run_id)
        materialize(self._input_dir, inputs)
        self._check(deadline)
        args = create_arguments(spec, self._input_dir)
        self._create_started = True
        identity = self._command(args, deadline).decode("ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise IntegrityError("Docker did not return a full container identity")
        self._worker = Prepared(identity, self._input_dir)
        return self._worker

    def _require(self, worker):
        if worker != self._worker or worker is None or self._termination is not None:
            raise IntegrityError("Stale or foreign runtime worker")

    def _inspect(self, identity, deadline, *, cleanup=False):
        value = json.loads(
            self._command(["inspect", identity], deadline, cleanup=cleanup)
        )
        if (
            not isinstance(value, list)
            or len(value) != 1
            or not isinstance(value[0], dict)
        ):
            raise IntegrityError("Ambiguous Docker inspection")
        return value[0]

    def inspect(self, worker, deadline):
        self._require(worker)
        return self._inspect(worker.container_id, deadline)

    def start(self, worker, deadline):
        self._require(worker)
        if self._attachment is not None:
            raise IntegrityError("Attachment already consumed")
        self._check(deadline)
        self._attachment = self._factory(
            [*self._argv, "start", "--attach", "--interactive", worker.container_id],
            deadline,
            stdout_limit=MAX_WIRE_BYTES + 2048,
            cancelled=self._cancelled,
            clock=self._clock,
            env=dict(self._env),
        )
        ready = self._attachment.line(2048)
        supervisor_sha = sha256(
            next(f.content for f in self._inputs.files if f.path == "supervisor.py")
        )
        verify_ready(ready, self._spec, supervisor_sha)
        self._check(deadline)
        return ready

    def read_inputs(self, worker, deadline):
        self._require(worker)
        self._check(deadline)
        verify_materialized(worker.input_dir, self._inputs)
        self._check(deadline)
        return self._inputs

    def release(self, worker, record, deadline):
        self._require(worker)
        self._check(deadline)
        if self._attachment is None:
            raise IntegrityError("No waiting attachment")
        self._attachment.send(record)
        self._check(deadline)

    def collect(self, worker, limit, deadline):
        self._require(worker)
        self._check(deadline)
        stdout, stderr, code = self._attachment.finish()
        if len(stdout) > limit or stderr:
            raise IntegrityError("Unexpected attachment output")
        self._check(deadline)
        return Collected(stdout, code)

    def _ids(self, spec, deadline):
        ids = set()
        for selector in (
            "label=recollect.selfmod=" + spec.run_id,
            "name=^/" + spec.name + "$",
        ):
            raw = self._command(
                [
                    "container",
                    "ls",
                    "--all",
                    "--no-trunc",
                    "--filter",
                    selector,
                    "--format",
                    "{{.ID}}",
                ],
                deadline,
                cleanup=True,
            )
            for line in raw.decode("ascii").splitlines():
                if not re.fullmatch(r"[0-9a-f]{64}", line):
                    raise IntegrityError("Invalid reconciliation identity")
                ids.add(line)
        return ids

    def _owned(self, value, spec, identity):
        config = value.get("Config", {})
        labels = config.get("Labels") or {}
        if (
            value.get("Id") != identity
            or value.get("Name") != "/" + spec.name
            or value.get("Image") != spec.image_id
            or labels.get("recollect.selfmod") != spec.run_id
            or labels.get("recollect.spec") != spec.sha256
        ):
            raise IntegrityError("Refusing to mutate an unowned container")

    def terminate(self, spec, worker, timeout_ms):
        if self._spec is not None and self._spec != spec:
            raise IntegrityError("Refusing foreign runtime termination")
        if self._termination is not None:
            return self._termination
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= 3000:
            raise ValueError("Freeze bounded cleanup timeout")
        deadline = None
        observed = []
        confirmed, identity = False, worker.container_id if worker else None
        try:
            stamp = self._cleanup_clock()
            deadline = Deadline(
                stamp.monotonic_ns + timeout_ms * 1_000_000, stamp.boot_id
            )
            ids = self._ids(spec, deadline) if self._create_started else set()
            if not ids:
                # No immediate lookup can rule out a delayed create request.
                confirmed = not self._create_started
            elif len(ids) == 1:
                found = ids.pop()
                if identity is not None and found != identity:
                    raise IntegrityError("Reconciliation found another container")
                identity = found
                value = self._inspect(identity, deadline, cleanup=True)
                self._owned(value, spec, identity)
                observed.append(value)
                state = value.get("State", {})
                if state.get("Running") is True or state.get("Restarting") is True:
                    self._command(
                        ["kill", "--signal=KILL", identity], deadline, cleanup=True
                    )
                value = self._inspect(identity, deadline, cleanup=True)
                self._owned(value, spec, identity)
                observed.append(value)
                state = value.get("State", {})
                if (
                    state.get("Status") not in {"created", "exited"}
                    or state.get("Running") is not False
                    or state.get("Restarting") is not False
                    or type(state.get("Pid")) is not int
                    or state["Pid"] != 0
                ):
                    raise IntegrityError("Container has not stopped")
                # Removing this full ID fences a delayed start on that ID. Never
                # remove by name or use force to conceal an uncertain stop.
                self._command(["rm", identity], deadline, cleanup=True)
                confirmed = not self._ids(spec, deadline)
        except Exception as exc:
            observed.append({"reconciliation_error": type(exc).__name__})
        finally:
            if self._attachment is not None:
                try:
                    self._attachment.close()
                except Exception as exc:
                    confirmed = False
                    observed.append({"attachment_error": type(exc).__name__})
            try:
                if deadline is None:
                    raise IntegrityError("No cleanup clock available")
                self._check(deadline, cleanup=True)
            except Exception:
                confirmed = False
        attachment_files = ()
        if self._attachment is not None:
            stdout, stderr = self._attachment.evidence()
            attachment_files = (
                File("attachment.stdout", stdout),
                File("attachment.stderr", stderr),
            )
        evidence = Snapshot(
            (
                *self._trace,
                *attachment_files,
                File(
                    "termination.json",
                    encode(
                        {
                            "observations": observed,
                            "confirmed": confirmed,
                            "create_started": self._create_started,
                        }
                    ),
                ),
            )
        )
        result = Termination(spec.run_id, spec.sha256, identity, confirmed, evidence)
        if confirmed:
            self._termination = result
        return result
