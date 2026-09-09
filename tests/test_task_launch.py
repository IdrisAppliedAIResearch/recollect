"""The optional two-slot launch preserves each conversation's token envelope."""

from tests.test_launch_script import pytestmark, run_script  # noqa: F401


def test_two_slot_reuse_requires_doubled_total_context():
    result = run_script(r'''
$process = [pscustomobject]@{
    ExecutablePath = 'C:\models\llama-server.exe'
    CommandLine = 'llama-server --model "C:\models\chat.gguf" ' +
        '--host 127.0.0.1 --port 8000 --ctx-size 65536 --parallel 2 ' +
        '--n-gpu-layers 999 --cache-type-k q8_0 --cache-type-v q8_0 ' +
        '--flash-attn on --jinja --no-webui'
}
$model = 'C:\models\chat.gguf'
$matches = @(Test-QwenProcess $process $process.ExecutablePath $model 2 32768)
$matches += Test-QwenProcess $process $process.ExecutablePath $model
$process.CommandLine = $process.CommandLine.Replace(
    '--ctx-size 65536', '--ctx-size 32768')
$matches += Test-QwenProcess $process $process.ExecutablePath $model 2 32768
ConvertTo-Json -Compress -InputObject $matches
''')
    assert result == [True, False, False]
