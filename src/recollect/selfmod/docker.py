"""A pinned host Docker CLI: absolute executable, private config and endpoint."""

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .contracts import IntegrityError

CLI_BYTES = 64 * 1024 * 1024
STDERR_BYTES = 65536
ENVIRONMENT = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "PATHEXT"}
NO_WINDOW = ({"creationflags": subprocess.CREATE_NO_WINDOW}
             if sys.platform == "win32" else {})


async def _settle(task):
    """Let a started CLI command finish, then re-raise caller cancellation."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


@dataclass(frozen=True)
class DockerCLI:
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]

    async def run(self, *args, data=None, limit=CLI_BYTES):
        """(exit code, stdout, stderr) of one completed CLI command."""
        return await _settle(asyncio.create_task(self._run(args, data, limit)))

    async def _run(self, args, data, limit):
        process = await asyncio.create_subprocess_exec(
            *self.argv, *args, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=65536, env=dict(self.environment), **NO_WINDOW)

        async def read(stream, bound):
            result = bytearray()
            while chunk := await stream.read(65536):
                if len(result) + len(chunk) > bound:
                    raise IntegrityError("Docker CLI output exceeds bound")
                result.extend(chunk)
            return bytes(result)

        async def send():
            try:
                if data:
                    process.stdin.write(data)
                    await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

        tasks = [asyncio.create_task(read(process.stdout, limit)),
                 asyncio.create_task(read(process.stderr, STDERR_BYTES)),
                 asyncio.create_task(send())]
        try:
            out, err, _ = await asyncio.gather(*tasks)
            return await process.wait(), out, err
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await process.wait()


async def pinned_docker(root, *, executable=None, environment=None):
    """A CLI bound to the current Docker endpoint with an empty private config."""
    environment = ({k: v for k, v in os.environ.items() if k.upper() in ENVIRONMENT}
                   if environment is None else dict(environment))
    found = executable or shutil.which("docker")
    if not found:
        raise IntegrityError("No Docker CLI is available for bundle images")
    executable = Path(found).resolve()
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_bytes(b'{"auths":{}}\n')
    process = await asyncio.create_subprocess_exec(
        str(executable), "context", "inspect", "--format",
        "{{.Endpoints.docker.Host}}", stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=environment, **NO_WINDOW)
    out, err = await process.communicate()
    if process.returncode:
        raise IntegrityError("Docker CLI probe failed: "
                             + err.decode(errors="replace")[:512])
    return DockerCLI((str(executable), "--config", str(root), "--host",
                      out.decode().strip()), tuple(environment.items()))


async def resolve_image(docker, tag):
    """The immutable ID of an already-present local image."""
    code, out, err = await docker.run("image", "inspect", tag)
    if code:
        raise IntegrityError("Base image is not present locally: "
                             + err.decode(errors="replace")[:512])
    value = json.loads(out)
    if len(value) != 1:
        raise IntegrityError("Ambiguous base image identity")
    return value[0]["Id"]
