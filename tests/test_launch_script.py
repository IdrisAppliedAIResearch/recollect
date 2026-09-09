"""Exercise launcher safeguards without starting models or Docker."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "launch.ps1"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
pytestmark = pytest.mark.skipif(
    os.name != "nt" or not POWERSHELL, reason="Windows desktop launcher",
)


def run_script(source: str):
    escaped = str(SCRIPT).replace("'", "''")
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
         f". '{escaped}'\n{source}"],
        capture_output=True, text=True, timeout=20, check=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_qwen_reuse_requires_exact_model_and_gpu_settings():
    result = run_script(r"""
$process = [pscustomobject]@{
    ExecutablePath = 'C:\models\llama-server.exe'
    CommandLine = 'llama-server --model "C:\models\chat model.gguf" ' +
        '--host 127.0.0.1 --port 8000 --ctx-size 32768 --parallel 1 ' +
        '--n-gpu-layers 999 --cache-type-k q8_0 --cache-type-v q8_0 ' +
        '--flash-attn on --jinja --no-webui'
}
$original = $process.CommandLine
$modelPath = 'C:\models\chat model.gguf'
$matches = @(Test-QwenProcess $process $process.ExecutablePath $modelPath)
foreach ($change in @(
    @('--parallel 1', '--parallel 2'),
    @('--n-gpu-layers 999', '--n-gpu-layers 0'),
    @('--host 127.0.0.1', '--host 0.0.0.0'),
    @('chat model.gguf', 'chat model.gguf.backup'),
    @('--ctx-size 32768', '--ctx-size 327680')
)) {
    $process.CommandLine = $original.Replace($change[0], $change[1])
    $matches += Test-QwenProcess $process $process.ExecutablePath $modelPath
}
ConvertTo-Json -Compress -InputObject $matches
""")
    assert result == [True, False, False, False, False, False]


def test_app_reuse_rejects_another_environment_or_an_unrelated_command():
    result = run_script(r"""
$process = [pscustomobject]@{
    ExecutablePath = 'C:\repo\.venv\Scripts\python.exe'
    CommandLine = '"C:\repo\.venv\Scripts\python.exe" ' +
        '"C:\repo\.venv\Scripts\recollect.exe" serve --mode host'
}
$matches = @(Test-RecollectProcess $process 'C:\repo')
$process.ExecutablePath = 'C:\Python313\python.exe'
$matches += Test-RecollectProcess $process 'C:\repo' 'C:\Python313\python.exe'
$process.ExecutablePath = 'C:\other\.venv\Scripts\python.exe'
$matches += Test-RecollectProcess $process 'C:\repo'
$process.ExecutablePath = 'C:\repo\.venv\Scripts\python.exe'
$process.CommandLine = 'python unrelated.py'
$matches += Test-RecollectProcess $process 'C:\repo'
ConvertTo-Json -Compress -InputObject $matches
""")
    assert result == [True, True, False, False]


def test_token_generation_is_private_and_stable():
    with tempfile.TemporaryDirectory(prefix="recollect-launch-") as directory:
        token_path = Path(directory) / "pairing-token.txt"
        escaped = str(token_path).replace("'", "''")
        result = run_script(f"""
$first = Get-HostPairing '{escaped}'
$contents = [IO.File]::ReadAllText('{escaped}')
$second = Get-HostPairing '{escaped}'
$bundle = $contents | ConvertFrom-Json
$acl = [Security.AccessControl.FileSecurity]::new('{escaped}', 'Access')
@{{same = ($contents -ceq [IO.File]::ReadAllText('{escaped}'));
   length = $bundle.token.Length; protected = $acl.AreAccessRulesProtected;
   certificate = $bundle.tls.certificate_pem.Contains('BEGIN CERTIFICATE');
   contains_private_key = $contents.Contains('PRIVATE KEY')}} | ConvertTo-Json -Compress
""")
        assert result == {
            "same": True, "length": 43, "protected": True, "certificate": True,
            "contains_private_key": False,
        }


def test_ui_rebuild_detects_missing_output_and_newer_sources():
    with tempfile.TemporaryDirectory(prefix="recollect-ui-") as directory:
        ui_root = Path(directory)
        escaped = str(ui_root).replace("'", "''")
        assert run_script(f"Test-UiBuildNeeded '{escaped}' | ConvertTo-Json") is True
        (ui_root / "dist").mkdir()
        (ui_root / "src").mkdir()
        index = ui_root / "dist" / "index.html"
        source = ui_root / "src" / "App.tsx"
        index.write_text("built")
        source.write_text("source")
        os.utime(source, (1_000, 1_000))
        os.utime(index, (2_000, 2_000))
        assert run_script(f"Test-UiBuildNeeded '{escaped}' | ConvertTo-Json") is False
        os.utime(source, (3_000, 3_000))
        assert run_script(f"Test-UiBuildNeeded '{escaped}' | ConvertTo-Json") is True


def test_unhealthy_or_drifted_embedder_is_not_ready():
    result = run_script(r"""
$health = [pscustomobject]@{
    ok = $true
    embedder = [pscustomobject]@{sentinel_matches_research = $true
                               sentinel_sha256 = 'pin'}
    generator = [pscustomobject]@{reachable = $true}
}
Assert-RecollectHealth $health 'pin'
$health.embedder.sentinel_sha256 = 'drift'
try { Assert-RecollectHealth $health 'pin'; $refused = $false }
catch { $refused = $true }
$refused | ConvertTo-Json
""")
    assert result is True
