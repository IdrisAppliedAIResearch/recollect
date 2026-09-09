"""Short commands select existing launch sequences without starting them in tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from recollect import cli, launch


def test_bare_recollect_selects_full_standalone(monkeypatch):
    calls = []

    def standalone(argv):
        calls.append(argv)
        return 7

    monkeypatch.setattr(launch, "standalone_main", standalone)
    monkeypatch.setattr(cli.sys, "argv", ["recollect"])
    assert cli.main() == 7
    assert calls == [[]]


def test_existing_subcommands_still_dispatch(monkeypatch):
    async def doctor():
        return 4

    monkeypatch.setattr(cli, "_doctor", doctor)
    monkeypatch.setattr(launch, "standalone_main", lambda _: pytest.fail(
        "Subcommands must not launch the standalone stack",
    ))
    assert cli.main(["doctor"]) == 4


@pytest.mark.parametrize("mode", ["standalone", "host"])
def test_desktop_commands_preserve_exit_and_use_repository_cwd(
    monkeypatch, tmp_path, mode,
):
    calls = []
    monkeypatch.setattr(launch.sys, "platform", "win32")
    powershell = tmp_path / "trusted powershell.exe"
    powershell.touch()
    monkeypatch.setattr(launch, "_powershell_path", lambda: powershell)
    monkeypatch.chdir(tmp_path)

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 3)

    monkeypatch.setattr(launch.subprocess, "run", run)
    arguments = [mode, "--host", "127.0.0.1", "--port", "8090"]
    if mode == "host":
        arguments.extend(["--token-file", "private token.txt"])
    assert launch.main(arguments) == 3
    root = Path(launch.__file__).resolve().parents[2]
    expected = [
        str(powershell), "-NoProfile", "-File", str(root / "scripts/launch.ps1"),
        "-Mode", mode, "-BindHost", "127.0.0.1", "-Port", "8090",
    ]
    if mode == "host":
        expected.extend(["-TokenFile", str(tmp_path / "private token.txt")])
    assert calls == [(expected, {"cwd": root, "check": False})]


def test_host_entrypoint_defaults_to_host(monkeypatch):
    calls = []
    monkeypatch.setattr(launch, "_desktop", lambda *args: calls.append(args) or 0)
    assert launch.host_main([]) == 0
    assert calls == [("host", [])]


def test_host_help_does_not_launch(monkeypatch, capsys):
    monkeypatch.setattr(launch.subprocess, "run", lambda *a, **kw: pytest.fail(
        "Help must not start services",
    ))
    with pytest.raises(SystemExit) as result:
        launch.main(["host", "--help"])
    assert result.value.code == 0
    assert "recollect-host" in capsys.readouterr().out


def test_desktop_command_on_ubuntu_points_to_client(monkeypatch, capsys):
    monkeypatch.setattr(launch.sys, "platform", "linux")
    assert launch.host_main([]) == 1
    assert "recollect-deploy" in capsys.readouterr().err
