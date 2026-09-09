"""Port ownership follows the bind address, without relaxing process checks."""

import pytest

from tests.test_launch_script import pytestmark, run_script  # noqa: F401


def test_separate_network_listener_does_not_block_loopback():
    result = run_script(r'''
function Get-NetTCPConnection {
    [pscustomobject]@{LocalAddress='100.113.91.124'; OwningProcess=7616}
    [pscustomobject]@{LocalAddress='fd7a:115c:a1e0::1'; OwningProcess=7616}
}
function Get-CimInstance { throw 'Must not inspect unrelated process' }
ConvertTo-Json -Compress -InputObject ($null -eq (Get-PortProcess 8000))
''')
    assert result is True


@pytest.mark.parametrize("address", ["127.0.0.1", "0.0.0.0", "::"])
def test_overlapping_listener_still_requires_identity(address):
    result = run_script(r'''
function Get-NetTCPConnection {
    [pscustomobject]@{LocalAddress='ADDRESS'; OwningProcess=123}
}
function Get-CimInstance { [pscustomobject]@{CommandLine=$null} }
try { Get-PortProcess 8000; $message='incorrectly accepted' }
catch { $message=$_.Exception.Message }
ConvertTo-Json -Compress -InputObject $message
'''.replace("ADDRESS", address))
    assert result == "Cannot verify the process owning port 8000."


@pytest.mark.parametrize("binding", ["100.113.91.124", "0.0.0.0"])
def test_host_binding_checks_relevant_network_listener(binding):
    result = run_script(r'''
function Get-NetTCPConnection {
    [pscustomobject]@{LocalAddress='100.113.91.124'; OwningProcess=123}
}
function Get-CimInstance {
    [pscustomobject]@{ProcessId=123; CommandLine='verified command'}
}
ConvertTo-Json -Compress -InputObject (Get-PortProcess 8080 'BINDING').ProcessId
'''.replace("BINDING", binding))
    assert result == 123


def test_dedicated_qwen_port_rejects_normal_local_model():
    result = run_script(r'''
$process = [pscustomobject]@{
    ExecutablePath = 'C:\models\llama-server.exe'
    CommandLine = 'llama-server --model "C:\models\chat.gguf" ' +
        '--host 127.0.0.1 --port 8001 --ctx-size 32768 --parallel 1 ' +
        '--n-gpu-layers 999 --cache-type-k q8_0 --cache-type-v q8_0 ' +
        '--flash-attn on --jinja --no-webui'
}
$model = 'C:\models\chat.gguf'
$matches = @(Test-QwenProcess $process $process.ExecutablePath $model 1 32768 8001)
$process.CommandLine = $process.CommandLine.Replace('--port 8001', '--port 8000')
$matches += Test-QwenProcess $process $process.ExecutablePath $model 1 32768 8001
ConvertTo-Json -Compress -InputObject $matches
''')
    assert result == [True, False]
