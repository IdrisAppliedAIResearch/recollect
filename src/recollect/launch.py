"""Short desktop commands delegate to the verified Windows launch sequence."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def standalone_main(argv: list[str] | None = None) -> int:
    return _desktop("standalone", argv)


def host_main(argv: list[str] | None = None) -> int:
    return _desktop("host", argv)


def _powershell_path() -> Path:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_directory = kernel.GetSystemDirectoryW
    get_directory.argtypes = (wintypes.LPWSTR, wintypes.UINT)
    get_directory.restype = wintypes.UINT
    directory = ctypes.create_unicode_buffer(32_768)
    length = get_directory(directory, len(directory))
    if not length or length >= len(directory):
        raise OSError("Windows system directory could not be resolved.")
    return Path(directory.value) / "WindowsPowerShell/v1.0/powershell.exe"


def _desktop(mode: str, argv: list[str] | None) -> int:
    command = "recollect-host" if mode == "host" else "recollect"
    parser = argparse.ArgumentParser(
        prog=command,
        description=f"Launch the complete Windows Recollect {mode} stack.",
    )
    parser.add_argument("--host", help="local IPv4 interface to bind")
    parser.add_argument("--port", type=int, help="Recollect API port (default: 8080)")
    if mode == "host":
        parser.add_argument(
            "--token-file", type=Path,
            help="pairing token file (default: var/deployment-token.txt)",
        )
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        print(
            f"{command} starts the Windows desktop stack. "
            "On Ubuntu use recollect-deploy for the Surface client.", file=sys.stderr,
        )
        return 1
    root = Path(__file__).resolve().parents[2]
    script = root / "scripts" / "launch.ps1"
    try:
        powershell = _powershell_path()
    except OSError as error:
        print(str(error), file=sys.stderr)
        return 1
    if not powershell.is_file() or not script.is_file():
        print("PowerShell and scripts/launch.ps1 are required.", file=sys.stderr)
        return 1
    arguments = [str(powershell), "-NoProfile", "-File", str(script), "-Mode", mode]
    if args.host is not None:
        arguments.extend(["-BindHost", args.host])
    if args.port is not None:
        arguments.extend(["-Port", str(args.port)])
    if mode == "host" and args.token_file is not None:
        arguments.extend(["-TokenFile", str(args.token_file.expanduser().absolute())])
    try:
        return subprocess.run(arguments, cwd=root, check=False).returncode
    except OSError as error:
        print(f"Could not start the desktop launcher: {error}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {"standalone", "host"}:
        return _desktop(argv[0], argv[1:])
    parser = argparse.ArgumentParser(prog="python -m recollect.launch")
    parser.add_argument("mode", choices=("standalone", "host"))
    parser.parse_args(argv)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
