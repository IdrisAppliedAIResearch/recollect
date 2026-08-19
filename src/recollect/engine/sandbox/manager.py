"""One sandboxed opencode server per chat session.

Each recollect session that delegates research gets an ``opencode serve``
process in its own workdir under ``sandbox_root/<session_id>/`` (a
machine-local directory, deliberately outside any repository - see
``RecollectConfig.sandbox_root`` for why). The process is spawned with
the generated config (``configgen``), a random per-spawn password, and
an ephemeral local port; the workdir is its only reachable filesystem.

Because opencode stores its sessions against the workdir, a restarted
server can re-attach to the session it had before: after the idle reaper
(or a server crash) tears a sandbox down, the next delegation re-spawns
it and finds the stored conversation again. That is what keeps the
sandbox persistent per user session in the Dispatch sense: context
survives, the process does not have to.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
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


class SandboxStartError(RuntimeError):
    """The sandbox server did not come up; the delegation can still run
    on whatever the caller falls back to, with the reason surfaced."""


@dataclass
class SandboxHandle:
    """The live state of one session's sandbox server."""

    session_id: str
    workdir: Path
    port: int
    password: str
    process: asyncio.subprocess.Process | None
    client: httpx.AsyncClient
    oc_session_id: str | None = None
    busy: bool = False
    last_used: float = field(default_factory=time.monotonic)

    def alive(self) -> bool:
        return self.process is None or self.process.returncode is None


_START_TIMEOUT_S = 60.0
_HEALTH_POLL_S = 0.5
_KILL_GRACE_S = 5.0
_REAPER_INTERVAL_S = 60.0

#: npm-installed opencode is a .cmd shim around prebuilt opencode.exe.
_EXE_RE = re.compile(r'"((?:[^"\\]|\\.)+?\.exe)"', re.IGNORECASE)

# A factory seam (used by tests): (port, workdir, password) -> argv.
CommandFactory = Callable[[int, Path, str], list[str]]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _resolve_opencode(bin_name: str) -> str:
    """Resolve the opencode binary to an actual executable.

    Spawning the npm .cmd shim directly makes process management
    unreliable (kill would leave the wrapped exe holding the port), so
    when PATH resolves to a shim, the exe the shim wraps is extracted.
    An explicit path - via ``RECOLLECT_SANDBOX_OPENCODE_BIN`` - passes
    through as given.
    """
    resolved = shutil.which(bin_name) or bin_name
    if os.name != "nt" or resolved.lower().endswith(".exe"):
        return resolved
    if resolved.lower().endswith((".cmd", ".bat")):
        try:
            text = Path(resolved).read_text(encoding="utf-8", errors="replace")
        except OSError:
            raise SandboxStartError(f"cannot read shim {resolved!r}") from None
        match = _EXE_RE.search(text)
        if match:
            # npm shims quote the wrapped exe as "%dp0%\..." where dp0 is
            # the shim's own directory; expand it before exec'ing.
            target = match.group(1).replace("%dp0%", str(Path(resolved).parent))
            if Path(target).is_file():
                return target
    raise SandboxStartError(
        f"cannot resolve {bin_name!r} to an opencode executable; point "
        "RECOLLECT_SANDBOX_OPENCODE_BIN at opencode.exe"
    )


class SandboxManager:
    """Owns every live sandbox; one per recollect session."""

    def __init__(
        self,
        config: RecollectConfig,
        *,
        command_factory: CommandFactory | None = None,
    ) -> None:
        self._config = config
        # Absolute on purpose: a relative workdir lands the opencode
        # project in whichever directory the server happens to run in,
        # and the session's stored directory (absolute) would never
        # match for re-attachment.
        self._root = Path(config.sandbox_root).resolve()
        self._handles: dict[str, SandboxHandle] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._reaper: asyncio.Task | None = None
        self._commands = command_factory

    # -- lifecycle -------------------------------------------------------

    async def ensure(self, session_id: str) -> SandboxHandle:
        """A live sandbox for this session, spawning (or re-attaching) if
        necessary. Safe under concurrent delegation attempts from the
        same session."""
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            handle = self._handles.get(session_id)
            if handle is not None and handle.alive():
                return handle
            if handle is not None:
                await self._shutdown(handle)
            self._handles[session_id] = await self._spawn(session_id)
            return self._handles[session_id]

    async def _spawn(self, session_id: str) -> SandboxHandle:
        cfg = self._config
        workdir = self._root / session_id
        workdir.mkdir(parents=True, exist_ok=True)
        port = _free_port()
        password = uuid.uuid4().hex
        config_path = configgen.write_config(
            workdir,
            base_url=cfg.generator_base_url,
            model=cfg.generator_model,
            api_key=cfg.generator_api_key,
            steps=cfg.sandbox_steps,
        )
        if self._commands is not None:
            argv = self._commands(port, workdir, password)
        else:
            argv = [
                _resolve_opencode(cfg.sandbox_opencode_bin),
                "serve",
                "--port",
                str(port),
                "--hostname",
                "127.0.0.1",
            ]
        env = {
            **os.environ,
            "OPENCODE_CONFIG": str(config_path),
            # The password is per spawn: a stale handle cannot replay
            # against a fresh server, and nothing outside this process
            # holds the credential.
            "OPENCODE_SERVER_PASSWORD": password,
        }
        # The child inherits the log fd, so the parent may close its own
        # copy straight after exec.
        with open(workdir / "opencode-stderr.log", "ab") as err_file:
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(workdir),
                    env=env,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=err_file,
                )
            except (OSError, ValueError) as error:
                raise SandboxStartError(f"exec {argv[0]!r}: {error}") from error

        client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            auth=("opencode", password),
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        handle = SandboxHandle(
            session_id=session_id,
            workdir=workdir,
            port=port,
            password=password,
            process=process,
            client=client,
        )
        try:
            await self._wait_healthy(handle)
            handle.oc_session_id = await self._session_id(handle)
        except BaseException:
            await self._shutdown(handle)
            raise
        return handle

    async def _wait_healthy(self, handle: SandboxHandle) -> None:
        deadline = time.monotonic() + _START_TIMEOUT_S
        last = ""
        while time.monotonic() < deadline:
            if handle.process is not None and handle.process.returncode is not None:
                raise SandboxStartError(
                    f"opencode serve exited early (code "
                    f"{handle.process.returncode}); see "
                    f"{handle.workdir / 'opencode-stderr.log'}"
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

    async def _session_id(self, handle: SandboxHandle) -> str:
        """This sandbox's opencode session, re-attached when one exists.

        opencode lists sessions globally, so the match is directory plus
        the per-recollect-session title, taking the most recently updated
        entry if a workdir somehow accumulates more than one.
        """
        want_dir = str(handle.workdir)
        title = f"recollect:{handle.session_id}"
        response = await handle.client.get("/session", timeout=10.0)
        response.raise_for_status()
        match: str | None = None
        match_updated = 0
        for entry in response.json():
            if (
                isinstance(entry, dict)
                and entry.get("title") == title
                and entry.get("directory") == want_dir
            ):
                updated = (entry.get("time") or {}).get("updated", 0)
                if updated >= match_updated:
                    match = str(entry["id"])
                    match_updated = updated
        if match:
            return match
        created = await handle.client.post(
            "/session", json={"title": title}, timeout=10.0
        )
        created.raise_for_status()
        return str(created.json()["id"])

    async def teardown(self, session_id: str) -> None:
        """Stop one sandbox (workdir kept for re-attachment)."""
        async with self._locks.setdefault(session_id, asyncio.Lock()):
            handle = self._handles.pop(session_id, None)
            if handle is not None:
                await self._shutdown(handle)

    async def _shutdown(self, handle: SandboxHandle) -> None:
        handle.busy = False
        process = handle.process
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=_KILL_GRACE_S)
        await handle.client.aclose()

    async def close_all(self) -> None:
        """Server shutdown: stop every sandbox and the reaper."""
        await self.stop_reaper()
        for session_id in list(self._handles):
            await self.teardown(session_id)

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
        for session_id in list(self._handles):
            handle = self._handles[session_id]
            if handle.alive() and not handle.busy and now - handle.last_used > ttl:
                await self.teardown(session_id)
