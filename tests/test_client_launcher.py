"""Exercise the Ubuntu launcher's commands without installing or starting anything."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

BASH = shutil.which("bash")
if BASH is None and Path("C:/Program Files/Git/bin/bash.exe").is_file():
    BASH = "C:/Program Files/Git/bin/bash.exe"

pytestmark = pytest.mark.skipif(BASH is None, reason="Bash is required for shell tests")
ROOT = Path(__file__).resolve().parents[1]


def shell_path(path):
    value = path.as_posix()
    if path.drive:
        return "/" + value[0].lower() + value[2:]
    return value


@pytest.mark.parametrize("stale_ui", [False, True])
def test_ubuntu_launcher_uses_isolated_client_environment(tmp_path, stale_ui):
    root = tmp_path / "Surface checkout"
    (root / "scripts").mkdir(parents=True)
    script = root / "scripts" / "launch-client.sh"
    shutil.copyfile(ROOT / "scripts" / "launch-client.sh", script)
    ui = root / "ui"
    for directory in ("dist", "src", "public"):
        (ui / directory).mkdir(parents=True)
    inputs = [
        ui / name for name in (
            "index.html", "package.json", "package-lock.json", "vite.config.ts",
            "tsconfig.json", "src/App.tsx",
        )
    ]
    for path in inputs:
        path.write_text("fixture", encoding="utf-8")
        os.utime(path, (1000, 1000))
    (ui / "dist/index.html").write_text("built", encoding="utf-8")
    os.utime(ui / "dist/index.html", (2000, 2000))
    if stale_ui:
        os.utime(ui / "src/App.tsx", (3000, 3000))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv_script = bin_dir / "uv"
    uv_script.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "$UV_CAPTURE"\n',
        encoding="utf-8", newline="\n",
    )
    npm_script = bin_dir / "npm"
    npm_script.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "$NPM_CAPTURE"\n'
        'if [[ "$1" == "ci" ]]; then mkdir -p node_modules; fi\n',
        encoding="utf-8", newline="\n",
    )
    uv_script.chmod(0o755)
    npm_script.chmod(0o755)
    uv_capture = tmp_path / "uv.txt"
    npm_capture = tmp_path / "npm.txt"
    environment = dict(os.environ)
    environment.update(
        TEST_BIN=shell_path(bin_dir), UV_CAPTURE=shell_path(uv_capture),
        NPM_CAPTURE=shell_path(npm_capture),
    )
    result = subprocess.run(
        [BASH, "-c", 'export PATH="$TEST_BIN:$PATH"; exec bash "$@"',
         "test-client", shell_path(script), "--desktop-url", "http://desktop:8080",
         "--token-file", "/private/token with spaces"],
        env=environment, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    args = uv_capture.read_text(encoding="utf-8").splitlines()
    assert args[:5] == ["run", "--locked", "--script", "--python", "3.13"]
    assert args[5].endswith("/Surface checkout/deploy/client.py")
    assert args[6:] == [
        "--desktop-url", "http://desktop:8080", "--token-file",
        "/private/token with spaces",
    ]
    if stale_ui:
        assert npm_capture.read_text(encoding="utf-8").splitlines() == [
            "ci", "run build",
        ]
    else:
        assert not npm_capture.exists()


def test_ubuntu_launcher_bash_syntax():
    result = subprocess.run(
        [BASH, "-n", str(ROOT / "scripts/launch-client.sh")],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
