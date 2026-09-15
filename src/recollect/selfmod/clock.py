"""Process clock identity for journaled observations."""

import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

_BOOT_ID = f"{os.getpid()}-{uuid.uuid4().hex}"


@dataclass(frozen=True)
class Stamp:
    monotonic_ns: int
    utc: str
    boot_id: str

    def __post_init__(self):
        if (
            type(self.monotonic_ns) is not int
            or self.monotonic_ns < 0
            or not isinstance(self.boot_id, str)
            or not self.boot_id
            or datetime.fromisoformat(self.utc).utcoffset() != UTC.utcoffset(None)
        ):
            raise ValueError("Valid process-clock identity and UTC stamp required")


def current_stamp() -> Stamp:
    return Stamp(time.monotonic_ns(), datetime.now(UTC).isoformat(), _BOOT_ID)
