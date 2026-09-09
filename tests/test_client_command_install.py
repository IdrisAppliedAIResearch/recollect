"""Install and invoke the Ubuntu short command using isolated fake runtimes."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

BASH = shutil.which("bash")
if BASH is None and Path("C:/Program Files/Git/bin/bash.exe").is_file():
    BASH = "C:/Program Files/Git/bin/bash.exe"

pytestmark = pytest.mark.skipif(BASH is None, reason="Bash is required for shell tests")
ROOT = Path(__file__).resolve().parents[1]


def shell_path(path):
    value = path.as_posix()
    return "/" + value[0].lower() + value[2:] if path.drive else value


@pytest.fixture
def installation(tmp_path):
    root = tmp_path / "Surface '$(touch INJECTED)' $cash; checkout"
    (root / "scripts").mkdir(parents=True)
    for name in ("install-client-command.sh", "launch-client.sh"):
        shutil.copyfile(ROOT / "scripts" / name, root / "scripts" / name)
    package = root / "src" / "recollect"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    shutil.copyfile(
        ROOT / "src/recollect/private_files.py", package / "private_files.py",
    )
    ui = root / "ui"
    for name in ("dist", "src", "public"):
        (ui / name).mkdir(parents=True)
    for name in (
        "index.html", "package.json", "package-lock.json", "vite.config.ts",
        "tsconfig.json", "src/App.tsx",
    ):
        path = ui / name
        path.write_text("fixture", encoding="utf-8")
        os.utime(path, (1000, 1000))
    (ui / "dist" / "index.html").write_text("built", encoding="utf-8")
    os.utime(ui / "dist" / "index.html", (2000, 2000))

    home = tmp_path / "private user"
    home.mkdir()
    (home / ".profile").write_text("existing user profile\n", encoding="utf-8")
    token = tmp_path / "token '$(touch TOKEN_INJECTED)' $money;"
    token.write_text("actual-secret-must-not-be-copied", encoding="utf-8")
    runtime_bin = tmp_path / "fake runtime"
    runtime_bin.mkdir()
    uv = runtime_bin / "uv"
    uv.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\0\' "$PWD" "$@" > "$UV_CAPTURE"\n',
        encoding="utf-8", newline="\n",
    )
    uv.chmod(0o755)
    python = runtime_bin / "python3"
    python_body = '#!/usr/bin/env bash\n'
    if os.name == "nt":
        python_body += (
            'converted=()\nfor value in "$@"; do\n'
            '  if [[ "$value" == /[a-z]/* ]]; then\n'
            '    converted+=("$(cygpath -w -- "$value")")\n'
            '  else converted+=("$value"); fi\ndone\n'
            'exec "$TEST_PYTHON" "${converted[@]}"\n'
        )
    else:
        python_body += 'exec "$TEST_PYTHON" "$@"\n'
    python.write_text(
        python_body,
        encoding="utf-8", newline="\n",
    )
    python.chmod(0o755)
    capture = tmp_path / "uv.args"
    environment = dict(os.environ)
    environment.update({
        "HOME": shell_path(home), "XDG_CONFIG_HOME": "",
        "TEST_BIN": shell_path(runtime_bin), "UV_CAPTURE": shell_path(capture),
        "TEST_PYTHON": shell_path(Path(sys.executable)),
    })
    return {
        "root": root, "home": home, "token": token, "capture": capture,
        "env": environment, "outside": tmp_path,
        "installer": root / "scripts" / "install-client-command.sh",
        "command": home / ".local" / "bin" / "recollect-deploy",
        "config": home / ".config" / "recollect" / "client.args",
    }


def run_script(installation, path, *args):
    return subprocess.run(
        [BASH, "-c", 'export PATH="$TEST_BIN:$PATH"; exec bash "$@"',
         "test-install-client", shell_path(path), *args],
        cwd=installation["outside"], env=installation["env"],
        capture_output=True, text=True, timeout=15,
    )


def install(installation, *args):
    return run_script(
        installation, installation["installer"],
        "--desktop-url", "http://desktop.example:8080/",
        "--token-file", shell_path(installation["token"]), *args,
    )


def test_installed_command_remembers_connection_and_works_from_any_directory(
    installation,
):
    result = install(installation)
    assert result.returncode == 0, result.stderr
    assert "recollect-deploy" in result.stdout
    assert "export PATH=" in result.stdout
    assert not installation["capture"].exists()
    saved = installation["config"].read_bytes().split(b"\0")
    assert saved == [
        b"recollect-client-connection-v1", b"http://desktop.example:8080",
        shell_path(installation["token"]).encode(), b"",
    ]
    result = run_script(installation, installation["command"])
    assert result.returncode == 0, result.stderr
    args = installation["capture"].read_bytes().decode().split("\0")
    assert args == [
        shell_path(installation["root"]), "run", "--locked", "--script",
        "--python", "3.13",
        shell_path(installation["root"] / "deploy" / "client.py"),
        "--desktop-url", "http://desktop.example:8080", "--token-file",
        shell_path(installation["token"]), "",
    ]
    assert not (installation["outside"] / "INJECTED").exists()
    assert not (installation["outside"] / "TOKEN_INJECTED").exists()
    assert (installation["home"] / ".profile").read_text() == "existing user profile\n"
    secret = installation["token"].read_bytes()
    assert secret not in installation["command"].read_bytes()
    assert secret not in installation["config"].read_bytes()
    assert secret.decode() not in result.stdout + result.stderr
    if os.name != "nt":
        assert stat.S_IMODE(installation["config"].stat().st_mode) == 0o600
        assert stat.S_IMODE(installation["config"].parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(installation["command"].stat().st_mode) == 0o755


def test_explicit_launch_flags_override_saved_defaults(installation):
    assert install(installation).returncode == 0
    result = run_script(
        installation, installation["command"], "--desktop-url",
        "http://other.example:8081", "--port", "8090",
    )
    assert result.returncode == 0, result.stderr
    args = installation["capture"].read_bytes().decode().split("\0")
    assert args[-5:] == [
        "--desktop-url", "http://other.example:8081", "--port", "8090", "",
    ]


def test_reinstall_is_idempotent_and_preserves_saved_choices(installation):
    assert install(installation).returncode == 0
    original_config = installation["config"].read_bytes()
    original_command = installation["command"].read_bytes()
    result = run_script(installation, installation["installer"])
    assert result.returncode == 0, result.stderr
    assert installation["config"].read_bytes() == original_config
    assert installation["command"].read_bytes() == original_command
    assert not list(installation["command"].parent.glob(".recollect-deploy.*"))
    assert not list(installation["config"].parent.glob(".client.args.*"))


def test_custom_install_directories_are_supported(installation):
    bin_dir = installation["outside"] / "custom $bin; folder"
    config_dir = installation["outside"] / "custom 'config' folder"
    result = install(
        installation, "--bin-dir", shell_path(bin_dir),
        "--config-dir", shell_path(config_dir),
    )
    assert result.returncode == 0, result.stderr
    assert (bin_dir / "recollect-deploy").is_file()
    assert (config_dir / "client.args").is_file()
    assert not installation["command"].exists()
    result = run_script(installation, bin_dir / "recollect-deploy")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("url", [
    "https://user:password@desktop.example", "http://desktop.example/path",
    "http://desktop.example?other", "file:///tmp/host", "http://desktop:0",
    "http://desktop:65536", "http://desktop:bad", "http://$(touch INJECTED)",
])
def test_invalid_desktop_url_does_not_install_any_files(installation, url):
    result = install(installation, "--desktop-url", url)
    assert result.returncode != 0
    assert not installation["command"].exists()
    assert not installation["config"].exists()
    assert not installation["capture"].exists()


def test_missing_token_file_prevents_installation(installation):
    result = install(installation, "--token-file", "/a/missing/token/file")
    assert result.returncode != 0
    assert "readable token" in result.stderr
    assert not installation["command"].exists()
    assert not installation["config"].exists()


def test_installer_refuses_to_overwrite_an_unrelated_command(installation):
    command = installation["command"]
    command.parent.mkdir(parents=True)
    command.write_text("#!/bin/sh\necho user command\n", encoding="utf-8")
    result = install(installation)
    assert result.returncode != 0
    assert "unrelated command" in result.stderr
    assert command.read_text() == "#!/bin/sh\necho user command\n"
    assert not installation["config"].exists()


def test_saved_configuration_is_data_and_cannot_execute_commands(installation):
    assert install(installation).returncode == 0
    installation["config"].write_text("touch INJECTED\n", encoding="utf-8")
    result = run_script(installation, installation["command"])
    assert result.returncode != 0
    assert "connection is invalid" in result.stderr
    assert not installation["capture"].exists()
    assert not (installation["outside"] / "INJECTED").exists()
    assert not (installation["root"] / "INJECTED").exists()
    result = install(installation)
    assert result.returncode != 0
    assert "unrelated configuration" in result.stderr
    assert installation["config"].read_text() == "touch INJECTED\n"


def test_help_performs_no_installation_or_launch(installation):
    result = run_script(installation, installation["installer"], "--help")
    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout
    assert not installation["command"].exists()
    assert not installation["config"].exists()
    assert install(installation).returncode == 0
    installation["config"].unlink()
    result = run_script(installation, installation["command"], "--help")
    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout
    assert not installation["capture"].exists()


def test_installer_bash_syntax():
    result = subprocess.run(
        [BASH, "-n", str(ROOT / "scripts/install-client-command.sh")],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
