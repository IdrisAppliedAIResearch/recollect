"""The Surface entry point must work without any desktop model packages."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from recollect.cli import main


@pytest.fixture
def clean_deployment_env(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    for key in tuple(os.environ):
        if key.startswith("RECOLLECT_") or key == "CDW_EMBEDDING_MODEL_PATH":
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


def test_client_cli_passes_settings_without_embedding_config(
    clean_deployment_env, monkeypatch, tmp_path, capsys,
):
    token = "pairing-token-" + "x" * 32
    token_file = tmp_path / "token"
    token_file.write_text(token, encoding="utf-8")
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: calls.append(
        (args, kwargs),
    ))
    assert main([
        "serve", "--mode", "client", "--desktop-url", "https://192.0.2.1:8080",
        "--token-file", str(token_file), "--port", "8090",
    ]) == 0
    assert calls == [(('recollect.deployment:create_app',), {
        "factory": True, "host": "127.0.0.1", "port": 8090,
        "reload": False, "proxy_headers": False,
        "ws_max_size": 65_536, "ws_max_queue": 1, "ws_per_message_deflate": False,
    })]
    assert os.environ["RECOLLECT_DEPLOYMENT_TOKEN"] == token
    assert os.environ["RECOLLECT_DEPLOYMENT_MODE"] == "client"
    assert token not in capsys.readouterr().out


def test_explicit_cli_options_override_env_file(
    clean_deployment_env, monkeypatch, tmp_path,
):
    env_file = tmp_path / "client.env"
    env_file.write_text(
        "RECOLLECT_DEPLOYMENT_MODE=host\nRECOLLECT_PORT=9000\n"
        "RECOLLECT_DEPLOYMENT_TOKEN=" + "x" * 32 + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    assert main([
        "serve", "--env-file", str(env_file), "--mode", "client", "--port", "8091",
        "--desktop-url", "https://192.0.2.1:8080",
    ]) == 0
    assert os.environ["RECOLLECT_PORT"] == "8091"
    assert os.environ["RECOLLECT_DEPLOYMENT_MODE"] == "client"


def test_invalid_client_configuration_never_starts_server(
    clean_deployment_env, monkeypatch, capsys,
):
    def unexpected_run(*args, **kwargs):
        pytest.fail("Invalid deployment must not start Uvicorn")

    monkeypatch.setattr("uvicorn.run", unexpected_run)
    assert main(["serve", "--mode", "client"]) == 1
    assert "Deployment configuration failed" in capsys.readouterr().err


def test_lightweight_imports_without_model_packages(tmp_path):
    source = Path(__file__).resolve().parents[1] / "src"
    script = f"""
import importlib.abc
import sys

class BlockModels(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {{
            'episodic', 'numpy', 'llama_cpp', 'onnxruntime', 'vosk',
            'spacy', 'faster_whisper', 'ctranslate2', 'cryptography',
        }}:
            raise RuntimeError('Client imported a model package: ' + fullname)

sys.meta_path.insert(0, BlockModels())
sys.path.insert(0, {str(source)!r})
import recollect.cli
import recollect.client
import recollect.deployment
assert 'recollect.config' not in sys.modules
assert 'recollect.api' not in sys.modules
print('Client imports without model packages')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script], cwd=tmp_path,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "Client imports without model packages" in result.stdout


def test_public_package_exports_remain_available():
    from recollect import RecollectConfig, TurnSummary, TurnTrace
    from recollect.config import RecollectConfig as Config
    from recollect.trace import TurnSummary as Summary
    from recollect.trace import TurnTrace as Trace

    assert (RecollectConfig, TurnSummary, TurnTrace) == (Config, Summary, Trace)


@pytest.mark.parametrize("mode", ["host", "client"])
def test_secure_pairing_cli_preserves_launch_flow(
    clean_deployment_env, monkeypatch, tmp_path, mode, capsys,
):
    from recollect.deployment import DeploymentConfig
    from recollect.host_pairing import ensure_host_pairing
    from recollect.pairing import load_pairing

    metadata = ensure_host_pairing(tmp_path / "desktop.token")
    credential = load_pairing(metadata["bundle_path"])
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: calls.append(kwargs))
    args = ["serve", "--mode", mode, "--token-file", metadata["bundle_path"]]
    if mode == "host":
        args += ["--host", "0.0.0.0"]
    else:
        args += ["--desktop-url", "http://192.168.1.20:8080"]
    assert main(args) == 0
    assert len(calls) == 1
    assert calls[0]["ws_max_size"] == 65_536
    assert calls[0]["ws_max_queue"] == 1
    assert calls[0]["ws_per_message_deflate"] is False
    config = DeploymentConfig.from_env(env_file=None)
    assert config.certificate_pem == credential.certificate_pem
    if mode == "host":
        assert calls[0]["ssl_certfile"] == metadata["certificate_path"]
        assert calls[0]["ssl_keyfile"] == metadata["private_key_path"]
    else:
        assert config.desktop_url == "https://192.168.1.20:8080"
        assert "ssl_certfile" not in calls[0]
    assert credential.token not in capsys.readouterr().out
