"""Install and execute isolated Windows command wrappers, without the application."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from recollect import launch

INSTALLER = (
    Path(__file__).resolve().parents[1] / "scripts" / "install-commands.ps1"
)
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
CSC = (
    Path(os.environ.get("WINDIR", "C:/Windows"))
    / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
)
pytestmark = pytest.mark.skipif(
    os.name != "nt" or not POWERSHELL, reason="Windows command installation",
)


@pytest.fixture
def installation():
    with tempfile.TemporaryDirectory(prefix="recollect-commands-") as temporary:
        root = Path(temporary)
        repo = root / "repo with spaces & ! %RECOLLECT_TEST_PATH%"
        scripts = repo / "scripts"
        scripts.mkdir(parents=True)
        installer = scripts / INSTALLER.name
        shutil.copyfile(INSTALLER, installer)
        (scripts / "launch.ps1").write_text("throw 'Never launch services in tests'")
        package = repo / "src" / "recollect"
        package.mkdir(parents=True)
        (package / "launch.py").write_text("raise RuntimeError('Never import app')")
        executables = repo / ".venv" / "Scripts"
        executables.mkdir(parents=True)
        for name in ("python.exe", "recollect.exe"):
            (executables / name).write_bytes(b"not invoked during installation")
        bin_directory = root / "user bin"
        caller = root / "another working directory"
        caller.mkdir()
        yield installer, executables, bin_directory, caller


def install(installer, destination, cwd):
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(installer),
         "-BinDirectory", str(destination), "-NoPathUpdate"],
        cwd=cwd, text=True, capture_output=True, timeout=20,
    )


def test_installation_is_idempotent_and_preserves_unrelated_files(installation):
    installer, _, destination, caller = installation
    destination.mkdir()
    unrelated = destination / "another-command.cmd"
    unrelated.write_text("echo Leave this command alone")
    first = install(installer, destination, caller)
    assert first.returncode == 0, first.stderr
    wrappers = [destination / name for name in ("recollect.cmd", "recollect-host.cmd")]
    original = [(path.read_bytes(), path.stat().st_mtime_ns) for path in wrappers]

    second = install(installer, destination, caller)

    assert second.returncode == 0, second.stderr
    after = [(path.read_bytes(), path.stat().st_mtime_ns) for path in wrappers]
    assert after == original
    assert unrelated.read_text() == "echo Leave this command alone"


@pytest.mark.parametrize("conflicting_name", ["recollect.cmd", "recollect-host.cmd"])
def test_existing_unowned_command_prevents_every_write(installation, conflicting_name):
    installer, _, destination, caller = installation
    destination.mkdir()
    conflict = destination / conflicting_name
    conflict.write_text("@echo off\necho User's own command\n")
    before = conflict.read_bytes()

    result = install(installer, destination, caller)

    assert result.returncode != 0
    assert "not managed by Recollect" in result.stderr
    assert list(destination.iterdir()) == [conflict]
    assert conflict.read_bytes() == before


def test_missing_target_prevents_installation(installation):
    installer, executables, destination, caller = installation
    (executables / "python.exe").unlink()

    result = install(installer, destination, caller)

    assert result.returncode != 0
    assert "Required command target is missing" in result.stderr
    assert not destination.exists()


@pytest.mark.skipif(not CSC.is_file(), reason="Windows .NET C# compiler unavailable")
def test_wrappers_preserve_caller_arguments_directory_and_exit_code(installation):
    installer, executables, destination, caller = installation
    source = executables / "EchoArguments.cs"
    source.write_text("""
using System;
using System.Text;
class EchoArguments {
    static void Emit(string value) {
        Console.WriteLine(Convert.ToBase64String(Encoding.UTF8.GetBytes(value)));
    }
    static int Main(string[] arguments) {
        Emit(Environment.CurrentDirectory);
        foreach (string argument in arguments) Emit(argument);
        return 23;
    }
}
""")
    compiled = subprocess.run(
        [str(CSC), "/nologo", "/target:exe",
         f"/out:{executables / 'recollect.exe'}", str(source)],
        text=True, capture_output=True, timeout=20,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    shutil.copyfile(executables / "recollect.exe", executables / "python.exe")
    installed = install(installer, destination, caller)
    assert installed.returncode == 0, installed.stderr
    arguments = [
        "-TokenFile", "relative token/pairing.txt", "literal&value", "bang!value",
    ]
    environment = {**os.environ, "RECOLLECT_TEST_PATH": "must-not-replace-path"}
    for name, prefix in (
        ("recollect.cmd", []),
        ("recollect-host.cmd", ["-I", "-m", "recollect.launch", "host"]),
    ):
        command = '"' + " ".join(
            f'"{argument}"' for argument in [str(destination / name), *arguments]
        ) + '"'
        result = subprocess.run(
            f'"{os.environ["COMSPEC"]}" /d /s /c {command}',
            cwd=caller, env=environment, text=True, capture_output=True, timeout=20,
        )
        assert result.returncode == 23, result.stdout + result.stderr
        values = [
            base64.b64decode(line).decode() for line in result.stdout.splitlines()
        ]
        assert Path(values[0]) == caller
        assert values[1:] == prefix + arguments


def test_path_update_is_case_insensitive_and_preserves_every_existing_entry():
    escaped = str(INSTALLER).replace("'", "''")
    source = r"""
$original = 'C:\Tools;C:\Other Tools;C:\Tools;'
$appended = Add-RecollectPathEntry $original 'C:\Users\Person\.local\bin'
$unchanged = Add-RecollectPathEntry $appended 'c:\users\person\.local\bin\'
@{appended = $appended; unchanged = $unchanged} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
         f". '{escaped}'\n{source}"],
        text=True, capture_output=True, timeout=20, check=True,
    )
    paths = json.loads(result.stdout)
    assert paths == {
        "appended": r"C:\Tools;C:\Other Tools;C:\Tools;C:\Users\Person\.local\bin",
        "unchanged": r"C:\Tools;C:\Other Tools;C:\Tools;C:\Users\Person\.local\bin",
    }


def test_standalone_ignores_executables_in_caller_directory(monkeypatch):
    with (
        tempfile.TemporaryDirectory(prefix="recollect-hostile-cwd-") as temporary,
        monkeypatch.context() as patched,
    ):
        hostile = Path(temporary)
        (hostile / "powershell.exe").write_bytes(b"Never execute caller files")
        patched.chdir(hostile)
        patched.setenv("SystemRoot", str(hostile))
        calls = []
        patched.setattr(
            launch.subprocess, "run",
            lambda arguments, **kwargs: calls.append(arguments)
            or subprocess.CompletedProcess(arguments, 0),
        )
        assert launch.standalone_main([]) == 0
        executable = Path(calls[0][0])
        assert executable.is_absolute() and executable.is_file()
        assert hostile not in executable.parents
        assert executable.parts[-3:] == (
            "WindowsPowerShell", "v1.0", "powershell.exe",
        )


def test_isolated_host_bootstrap_ignores_caller_python_packages():
    python = INSTALLER.parents[1] / ".venv" / "Scripts" / "python.exe"
    with tempfile.TemporaryDirectory(prefix="recollect-hostile-package-") as temporary:
        package = Path(temporary) / "recollect"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "launch.py").write_text("raise RuntimeError('UNTRUSTED_CWD')")
        result = subprocess.run(
            [str(python), "-I", "-m", "recollect.launch", "host", "--help"],
            cwd=temporary, capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0, result.stderr
        assert "complete Windows Recollect host stack" in result.stdout
        assert "UNTRUSTED_CWD" not in result.stderr
