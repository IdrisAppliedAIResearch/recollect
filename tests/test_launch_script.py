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


def test_qwen_identity_accepts_exact_name_or_path_without_accepting_other_models():
    result = run_script(r"""
$health = [pscustomobject]@{status = 'ok'}
$modelPath = 'C:\models\chat.gguf'
$ids = @('chat.gguf', $modelPath, 'C:\other\chat.gguf', 'chat.gguf.backup', '')
$accepted = foreach ($id in $ids) {
    $models = [pscustomobject]@{data = @([pscustomobject]@{id = $id})}
    try { Assert-QwenHealth $health $models 'chat.gguf' $modelPath; $true }
    catch { $false }
}
$models = [pscustomobject]@{data = @([pscustomobject]@{id = 'chat.gguf'})}
$health.status = 'loading'
try { Assert-QwenHealth $health $models 'chat.gguf' $modelPath; $accepted += $true }
catch { $accepted += $false }
$health.status = 'ok'
$models.data += [pscustomobject]@{id = 'other.gguf'}
try { Assert-QwenHealth $health $models 'chat.gguf' $modelPath; $accepted += $true }
catch { $accepted += $false }
ConvertTo-Json -Compress -InputObject $accepted
""")
    assert result == [True, True, False, False, False, False, False]


def test_qwen_cuda_path_is_scoped_and_restored_even_when_startup_fails(tmp_path):
    directory = tmp_path / "cuda libraries"
    directory.mkdir()
    for name in ("cublas64_13.dll", "cublasLt64_13.dll", "cudart64_13.dll"):
        (directory / name).touch()
    escaped = str(directory).replace("'", "''")
    result = run_script(r"""
$original = $env:PATH
$directory = 'DIRECTORY'
$during = Invoke-WithQwenCuda $directory { $env:PATH }
$restored = $env:PATH -ceq $original
try { Invoke-WithQwenCuda $directory { throw 'fixture failure' } }
catch { $failed = $true }
@{prefixed = $during.StartsWith($directory + ';'); restored = $restored;
  failed = $failed; restored_after_error = ($env:PATH -ceq $original)} |
    ConvertTo-Json -Compress
""".replace("DIRECTORY", escaped))
    assert result == {
        "prefixed": True, "restored": True, "failed": True,
        "restored_after_error": True,
    }


def test_missing_qwen_cuda_dependency_prevents_process_creation(tmp_path):
    escaped = str(tmp_path).replace("'", "''")
    result = run_script(r"""
$script:started = $false
try { Invoke-WithQwenCuda 'DIRECTORY' { $script:started = $true } }
catch { $message = $_.Exception.Message }
@{started = $script:started; message = $message} | ConvertTo-Json -Compress
""".replace("DIRECTORY", escaped))
    assert result["started"] is False
    assert "Qwen CUDA dependency is missing" in result["message"]


def test_stop_refuses_recycled_process_id():
    result = run_script(r"""
$expected = [pscustomobject]@{ProcessId=42; CreationDate='old';
    ExecutablePath='app'; CommandLine='serve'}
function Get-CimInstance { [pscustomobject]@{ProcessId=42; CreationDate='new';
    ExecutablePath='app'; CommandLine='serve'} }
$script:stopped = $false
function Stop-Process { $script:stopped = $true }
try { Stop-VerifiedProcess $expected; $refused = $false } catch { $refused = $true }
@{refused=$refused; stopped=$script:stopped} | ConvertTo-Json -Compress
""")
    assert result == {"refused": True, "stopped": False}


def test_stop_container_requires_image_and_matching_private_mounts():
    result = run_script(r"""
$c = [pscustomobject]@{Name='/recollect-subagent-abc123';
 Config=[pscustomobject]@{Image='expected'}; Mounts=@(
 [pscustomobject]@{Type='bind'; Destination='/workspace';
     Source='C:\sandboxes\shared\workspace'; RW=$true},
 [pscustomobject]@{Type='bind'; Destination='/config';
     Source='C:\sandboxes\shared\config'; RW=$false}
)}
$results = @(Test-RecollectContainer $c 'expected' 'C:\sandboxes')
$c.Config.Image='other'
$results += Test-RecollectContainer $c 'expected' 'C:\sandboxes'
$c.Config.Image='expected'
$c.Mounts[0].Source='C:\sandboxes-other\shared\workspace'
$results += Test-RecollectContainer $c 'expected' 'C:\sandboxes'
$c.Mounts[0].Source='C:\sandboxes\shared\workspace'
$c.Mounts[1].RW=$true
$results += Test-RecollectContainer $c 'expected' 'C:\sandboxes'
ConvertTo-Json -Compress -InputObject $results
""")
    assert result == [True, False, False, False]


def test_stop_verified_process_terminates_only_owned_child():
    result = run_script(r"""
$exe = (Get-Process -Id $PID).Path
$child = Start-Process -FilePath $exe -WindowStyle Hidden -PassThru `
    -ArgumentList '-NoProfile -NonInteractive -Command Start-Sleep -Seconds 30'
try {
    $expected = Get-CimInstance Win32_Process -Filter "ProcessId = $($child.Id)"
    Stop-VerifiedProcess $expected
    Stop-VerifiedProcess $expected
    $child.HasExited | ConvertTo-Json
} finally {
    if (-not $child.HasExited) { $child.Kill() }
    $child.Dispose()
}
""")
    assert result is True
