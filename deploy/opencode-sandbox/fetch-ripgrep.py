"""Install the checksum-pinned native skill/search dependency at image build time."""

import hashlib
import io
import platform
import tarfile
import urllib.request
from pathlib import Path

RELEASES = {
    "x86_64": (
        "x86_64-unknown-linux-musl",
        "1c9297be4a084eea7ecaedf93eb03d058d6faae29bbc57ecdaf5063921491599",
    ),
    "aarch64": (
        "aarch64-unknown-linux-gnu",
        "2b661c6ef508e902f388e9098d9c4c5aca72c87b55922d94abdba830b4dc885e",
    ),
}


def main():
    target, digest = RELEASES[platform.machine()]
    name = f"ripgrep-15.1.0-{target}"
    url = f"https://github.com/BurntSushi/ripgrep/releases/download/15.1.0/{name}.tar.gz"
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("ripgrep release checksum mismatch")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        executable = archive.extractfile(f"{name}/rg")
        if executable is None:
            raise ValueError("ripgrep release has no executable")
        destination = Path("/opt/rg")
        destination.write_bytes(executable.read())
        destination.chmod(0o755)
        notices = Path("/opt/ripgrep-notices")
        notices.mkdir()
        for filename in ("COPYING", "LICENSE-MIT", "UNLICENSE"):
            member = archive.extractfile(f"{name}/{filename}")
            if member is None:
                raise ValueError(f"ripgrep release has no {filename}")
            (notices / filename).write_bytes(member.read())


if __name__ == "__main__":
    main()
