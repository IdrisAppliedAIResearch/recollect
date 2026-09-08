"""One globally shared, warm, isolated OpenCode server.

The container and external llama.cpp server stay warm between calls, but
conversation and scratch state do not. ``begin_invocation`` scrubs the
workspace and creates a unique OpenCode session; ``finish_invocation``
deletes that session and scrubs again. Production never runs OpenCode
under the host user's token: ``isolation`` builds and attests a hardened
container, and startup fails closed when that boundary is unavailable.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ...config import RecollectConfig
from . import configgen
from .isolation import (
    ContainerLaunch,
    IsolationError,
    attest_container,
    build_container_launch,
    container_model_url,
)


class SandboxStartError(RuntimeError):
    """The sandbox server did not come up; the delegation can still run
    on whatever the caller falls back to, with the reason surfaced."""


@dataclass
class SandboxHandle:
    """The live state of the shared sandbox server."""

    workdir: Path
    port: int
    password: str
    process: asyncio.subprocess.Process | None
    client: httpx.AsyncClient
    config_dir: Path | None = None
    container: ContainerLaunch | None = None
    isolation: str = "test"
    oc_session_id: str | None = None
    busy: bool = False
    last_used: float = field(default_factory=time.monotonic)

    def alive(self) -> bool:
        return self.process is None or self.process.returncode is None


@dataclass(frozen=True)
class SandboxInvocation:
    """Fresh state for exactly one delegated call."""

    handle: SandboxHandle
    invocation_id: str
    oc_session_id: str
    process_reused: bool


_START_TIMEOUT_S = 60.0
_RUNTIME_CHECK_TIMEOUT_S = 10.0
_HEALTH_POLL_S = 0.5
_KILL_GRACE_S = 5.0
_REAPER_INTERVAL_S = 60.0

# A factory seam (used by tests): (port, workdir, password) -> argv.
CommandFactory = Callable[[int, Path, str], list[str]]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SandboxManager:
    """Owns one warm sandbox and serializes every delegated invocation."""

    def __init__(
        self,
        config: RecollectConfig,
        *,
        command_factory: CommandFactory | None = None,
        model_slot: asyncio.Lock | None = None,
    ) -> None:
        self._config = config
        # Absolute on purpose: a relative workdir lands the opencode
        # project in whichever directory the server happens to run in,
        # and the session's stored directory (absolute) would never
        # match for re-attachment.
        self._root = Path(config.sandbox_root).resolve()
        self._handle: SandboxHandle | None = None
        self._active: SandboxInvocation | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._invocation_lock = asyncio.Lock()
        self._model_slot = model_slot or asyncio.Lock()
        self._reaper: asyncio.Task | None = None
        self._commands = command_factory

    # -- lifecycle -------------------------------------------------------

    async def ensure(self) -> SandboxHandle:
        """Return the warm server, spawning it when necessary."""
        async with self._lifecycle_lock:
            return await self._ensure_locked()

    async def _ensure_locked(self) -> SandboxHandle:
        handle = self._handle
        if handle is not None and handle.alive():
            return handle
        if handle is not None:
            await self._shutdown(handle)
        self._handle = await self._spawn()
        return self._handle

    async def begin_invocation(self, session_id: str) -> SandboxInvocation:
        """Create fresh conversation and scratch state on the warm server."""
        await self._invocation_lock.acquire()
        model_acquired = False
        try:
            # OpenCode may use several model calls, including native child
            # agents. Hold the single hardware slot for the whole run.
            await self._model_slot.acquire()
            model_acquired = True
            async with self._lifecycle_lock:
                current = self._handle
                process_reused = current is not None and current.alive()
                handle = await self._ensure_locked()
                await asyncio.to_thread(self._scrub_workspace, handle.workdir)
                invocation_id = uuid.uuid4().hex
                try:
                    response = await handle.client.post(
                        "/session",
                        json={"title": f"recollect:{session_id}:{invocation_id}"},
                        timeout=10.0,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    oc_session_id = (
                        str(payload.get("id", "")).strip()
                        if isinstance(payload, dict)
                        else ""
                    )
                    if not oc_session_id:
                        raise SandboxStartError(
                            "opencode returned no id for the fresh session"
                        )
                except (SandboxStartError, httpx.HTTPError, ValueError):
                    # Unknown server state after a failed create is not reusable.
                    self._handle = None
                    await self._shutdown(handle)
                    raise
                handle.oc_session_id = oc_session_id
                handle.busy = True
                handle.last_used = time.monotonic()
                invocation = SandboxInvocation(
                    handle=handle,
                    invocation_id=invocation_id,
                    oc_session_id=oc_session_id,
                    process_reused=process_reused,
                )
                self._active = invocation
                return invocation
        except BaseException:
            if model_acquired:
                self._model_slot.release()
            self._invocation_lock.release()
            raise

    async def finish_invocation(self, invocation: SandboxInvocation) -> None:
        """Delete one call's conversation and scratch state."""
        handle = invocation.handle
        release_slots = False
        try:
            async with self._lifecycle_lock:
                if self._active is not invocation:
                    return
                release_slots = True
                try:
                    if self._handle is handle:
                        response = await handle.client.delete(
                            f"/session/{invocation.oc_session_id}", timeout=10.0
                        )
                        response.raise_for_status()
                except httpx.HTTPError:
                    # The next call creates a distinct session, so an orphaned
                    # conversation is not a reason to take down warm shared
                    # infrastructure. Idle teardown remains eventual cleanup.
                    pass
                finally:
                    handle.oc_session_id = None
                    handle.busy = False
                    handle.last_used = time.monotonic()
                    self._active = None
                    with contextlib.suppress(OSError):
                        await asyncio.to_thread(
                            self._scrub_workspace, handle.workdir
                        )
        finally:
            if release_slots:
                self._model_slot.release()
                self._invocation_lock.release()

    @staticmethod
    def _scrub_workspace(workdir: Path) -> None:
        root = workdir.resolve(strict=True)
        for child in root.iterdir():
            if child.is_symlink() or child.is_file():
                child.unlink()
            elif child.is_dir():
                shutil.rmtree(child)

    async def _spawn(self) -> SandboxHandle:
        cfg = self._config
        if self._commands is None:
            await self._check_container_runtime()
        root = self._root / "shared"
        workdir = root / "workspace"
        config_dir = root / "config"
        workdir.mkdir(parents=True, exist_ok=True)
        config_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._scrub_workspace, config_dir)
        with contextlib.suppress(OSError):
            (root / "opencode-stderr.log").unlink()
        port = _free_port()
        password = uuid.uuid4().hex
        container: ContainerLaunch | None = None
        if self._commands is None:
            base_url = container_model_url(cfg.generator_base_url)
            runtime_workdir = "/workspace"
            prompt_dir = "/config"
        else:
            base_url = cfg.generator_base_url
            runtime_workdir = str(workdir)
            prompt_dir = str(config_dir)
        config_path = configgen.write_config(
            config_dir,
            base_url=base_url,
            model=cfg.generator_model,
            api_key=cfg.generator_api_key,
            steps=cfg.sandbox_steps,
            runtime_workdir=runtime_workdir,
            runtime_python="/usr/local/bin/python" if self._commands is None else None,
            prompt_dir=prompt_dir,
        )
        if self._commands is not None:
            argv = self._commands(port, workdir, password)
            isolation = "test"
        else:
            try:
                container = build_container_launch(
                    runtime=cfg.sandbox_container_runtime,
                    image=cfg.sandbox_container_image,
                    name=f"recollect-subagent-{uuid.uuid4().hex[:12]}",
                    host_port=port,
                    workspace=workdir,
                    config_dir=config_dir,
                    password=password,
                    memory_mb=cfg.sandbox_container_memory_mb,
                    pids=cfg.sandbox_container_pids,
                    cpus=cfg.sandbox_container_cpus,
                )
            except IsolationError as error:
                raise SandboxStartError(str(error)) from error
            argv = container.argv
            isolation = "container"
        env = {
            **os.environ,
            "OPENCODE_CONFIG": str(config_path),
            # The password is per spawn: a stale handle cannot replay
            # against a fresh server, and nothing outside this process
            # holds the credential.
            "OPENCODE_SERVER_PASSWORD": password,
        }
        # Do not persist OpenCode diagnostics: provider errors can include
        # delegated text, which is required to remain ephemeral.
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(workdir),
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (OSError, ValueError) as error:
            raise SandboxStartError(f"exec {argv[0]!r}: {error}") from error

        client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            auth=("opencode", password),
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        handle = SandboxHandle(
            workdir=workdir,
            port=port,
            password=password,
            process=process,
            client=client,
            config_dir=config_dir,
            container=container,
            isolation=isolation,
        )
        try:
            await self._wait_healthy(handle)
            if container is not None:
                await self._attest(handle)
        except BaseException:
            await self._shutdown(handle)
            raise
        return handle

    async def _check_container_runtime(self) -> None:
        runtime = shutil.which(self._config.sandbox_container_runtime)
        if runtime is None:
            raise SandboxStartError(
                "Docker executable was not found; install Docker and retry research."
            )
        try:
            process = await asyncio.create_subprocess_exec(
                runtime, "info", "--format", "{{.OSType}}",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as error:
            raise SandboxStartError(
                "Docker could not start. Check the Docker installation "
                "and retry research."
            ) from error
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=_RUNTIME_CHECK_TIMEOUT_S,
            )
        except TimeoutError as error:
            raise SandboxStartError(
                "Docker engine did not respond. Start Docker Desktop or Docker Engine "
                "and retry research when it is ready."
            ) from error
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=_KILL_GRACE_S)
        # Keep diagnostics fixed: container CLI errors can reveal host paths,
        # and no delegated text should become a persisted startup diagnostic.
        if process.returncode:
            raise SandboxStartError(
                "Cannot reach the Docker engine. Start Docker Desktop or "
                "Docker Engine, check its connection permissions, and retry research."
            )
        if stdout.strip() != b"linux":
            raise SandboxStartError(
                "Research requires Linux containers. Switch Docker Desktop to Linux "
                "containers or connect to a Linux Docker Engine, then retry research."
            )

    async def _wait_healthy(self, handle: SandboxHandle) -> None:
        deadline = time.monotonic() + _START_TIMEOUT_S
        last = ""
        while time.monotonic() < deadline:
            if handle.process is not None and handle.process.returncode is not None:
                raise SandboxStartError(
                    f"opencode serve exited early (code "
                    f"{handle.process.returncode})"
                )
            try:
                response = await handle.client.get(
                    "/global/health", timeout=2.0
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("healthy"):
                    return
                last = str(payload)
            except (httpx.HTTPError, ValueError) as error:
                last = f"{type(error).__name__}"
            await asyncio.sleep(_HEALTH_POLL_S)
        raise SandboxStartError(f"opencode serve not healthy: {last!r}")

    async def _attest(self, handle: SandboxHandle) -> None:
        container = handle.container
        config_dir = handle.config_dir
        if container is None or config_dir is None:
            raise SandboxStartError("container metadata is missing")
        process = await asyncio.create_subprocess_exec(
            container.runtime,
            "inspect",
            container.name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode:
            raise SandboxStartError(
                f"cannot inspect sandbox container: {stderr.decode(errors='replace')}"
            )
        try:
            inspection = json.loads(stdout)
            attest_container(
                inspection,
                name=container.name,
                image=self._config.sandbox_container_image,
                workspace=handle.workdir,
                config_dir=config_dir,
                host_port=handle.port,
                memory_mb=self._config.sandbox_container_memory_mb,
                pids=self._config.sandbox_container_pids,
                cpus=self._config.sandbox_container_cpus,
            )
        except (json.JSONDecodeError, IsolationError) as error:
            raise SandboxStartError(f"sandbox attestation failed: {error}") from error

    async def teardown(self) -> None:
        """Stop the shared sandbox after any active invocation finishes."""
        async with self._invocation_lock, self._lifecycle_lock:
            handle = self._handle
            self._handle = None
            if handle is not None:
                await self._shutdown(handle)

    async def _shutdown(self, handle: SandboxHandle) -> None:
        handle.busy = False
        if handle.container is not None:
            with contextlib.suppress(OSError):
                cleanup = await asyncio.create_subprocess_exec(
                    handle.container.runtime,
                    "rm",
                    "--force",
                    handle.container.name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(cleanup.wait(), timeout=_KILL_GRACE_S)
        process = handle.process
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=_KILL_GRACE_S)
        await handle.client.aclose()

    async def close_all(self) -> None:
        """Server shutdown: stop the sandbox and the reaper."""
        await self.stop_reaper()
        await self.teardown()

    # -- idle reaping ------------------------------------------------------

    async def start_reaper(self) -> None:
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_loop())

    async def stop_reaper(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(_REAPER_INTERVAL_S)
            await self._reap()

    async def _reap(self) -> None:
        now = time.monotonic()
        ttl = self._config.sandbox_idle_ttl_s
        async with self._lifecycle_lock:
            handle = self._handle
            if (
                handle is not None
                and handle.alive()
                and not handle.busy
                and now - handle.last_used > ttl
            ):
                self._handle = None
                await self._shutdown(handle)
