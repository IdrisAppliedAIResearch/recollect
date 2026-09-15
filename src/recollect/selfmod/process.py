"""Bounded pipes for a trusted CLI, never a candidate host-code runner."""

import contextlib
import subprocess
import threading

from .clock import current_stamp
from .executor import Deadline
from .journal import IntegrityError


class PipeCommand:
    def __init__(
        self,
        argv: list[str],
        deadline: Deadline,
        *,
        stdout_limit: int,
        stderr_limit: int = 65536,
        cancelled=None,
        clock=current_stamp,
        env=None,
    ):
        self._clock, self._deadline, self._cancelled = clock, deadline, cancelled
        self._last = None
        self._condition = threading.Condition()
        self._done = threading.Event()
        self._error = None
        self._buffers = [bytearray(), bytearray()]
        self._eof = [False, False]
        self._offset = 0
        self._sent = False
        self._threads = []
        self._check_time()
        self.process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            shell=False,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            for index, stream, cap in (
                (0, self.process.stdout, stdout_limit),
                (1, self.process.stderr, stderr_limit),
            ):
                self._start(self._read, index, stream, cap)
            self._start(self._watch)
        except BaseException:
            self.process.kill()
            self.process.wait(timeout=1)
            raise

    def _start(self, target, *args):
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _check_time(self):
        if self._cancelled is not None and self._cancelled.is_set():
            raise InterruptedError("CLI operation cancelled")
        now = self._clock()
        if (
            now.boot_id != self._deadline.boot_id
            or self._last is not None
            and now.monotonic_ns < self._last
        ):
            raise IntegrityError("CLI clock continuity lost")
        self._last = now.monotonic_ns
        if (self._deadline.monotonic_ns is not None
                and now.monotonic_ns >= self._deadline.monotonic_ns):
            raise TimeoutError("CLI deadline exhausted")

    def _fail(self, exc):
        with self._condition:
            if self._error is None:
                self._error = exc
            self._condition.notify_all()
        if self.process.poll() is None:
            with contextlib.suppress(OSError):
                self.process.kill()

    def _watch(self):
        while not self._done.wait(0.01):
            try:
                with self._condition:
                    self._check_time()
            except BaseException as exc:
                self._fail(exc)
                return

    def _read(self, index, stream, cap):
        try:
            while data := stream.read(8192):
                with self._condition:
                    room = cap - len(self._buffers[index])
                    self._buffers[index].extend(data[:room])
                    if len(data) > room:
                        raise IntegrityError("CLI output limit exceeded")
                    self._condition.notify_all()
        except BaseException as exc:
            self._fail(exc)
        finally:
            stream.close()
            with self._condition:
                self._eof[index] = True
                self._condition.notify_all()

    def _wait(self, predicate):
        with self._condition:
            while True:
                if self._error is not None:
                    raise self._error
                self._check_time()
                if predicate():
                    return
                self._condition.wait(0.01)

    def line(self, limit: int) -> bytes:
        def ready():
            value = self._buffers[0]
            newline = value.find(b"\n", self._offset)
            if newline >= 0:
                if newline + 1 - self._offset > limit:
                    raise IntegrityError("CLI readiness line exceeds bound")
                return True
            if len(value) - self._offset >= limit or self._eof[0]:
                raise IntegrityError("Missing bounded CLI readiness record")
            return False

        self._wait(ready)
        with self._condition:
            end = self._buffers[0].index(b"\n", self._offset) + 1
            result = bytes(self._buffers[0][self._offset : end])
            self._offset = end
            return result

    def send(self, data: bytes):
        if self._sent or type(data) is not bytes or len(data) > 2048:
            raise IntegrityError("CLI input must be one bounded release")
        self._sent = True
        written = threading.Event()

        def write():
            try:
                position = 0
                while position < len(data):
                    count = self.process.stdin.write(data[position:])
                    if not count:
                        raise BrokenPipeError("CLI release did not complete")
                    position += count
            except BaseException as exc:
                self._fail(exc)
            finally:
                written.set()

        self._wait(lambda: True)
        self._start(write)
        self._wait(written.is_set)

    def finish(self) -> tuple[bytes, bytes, int]:
        if not self._sent:
            self.process.stdin.close()
        self._wait(lambda: all(self._eof) and self.process.poll() is not None)
        with self._condition:
            return (
                bytes(self._buffers[0][self._offset :]),
                bytes(self._buffers[1]),
                self.process.returncode,
            )

    def evidence(self) -> tuple[bytes, bytes]:
        with self._condition:
            return tuple(bytes(value) for value in self._buffers)

    def close(self):
        """Bounded CLI teardown; this alone is NOT container termination."""
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=0.5)
        self._done.set()
        for thread in self._threads:
            thread.join(timeout=0.1)
        if any(t.is_alive() for t in self._threads):
            raise IntegrityError("CLI pipe teardown remains uncertain")
        if not self.process.stdin.closed:
            self.process.stdin.close()
