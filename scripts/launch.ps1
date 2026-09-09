#requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet('standalone', 'host', 'stop')]
    [string]$Mode = 'standalone',
    [string]$BindHost = '',
    [ValidateRange(1, 65535)]
    [int]$Port = 8080,
    [string]$TokenFile = '',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-PortProcess([int]$ListenPort, [string]$BindAddress = '127.0.0.1') {
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $ListenPort `
        -ErrorAction SilentlyContinue | Where-Object {
            # A listener on a separate interface does not own our loopback port.
            # Wildcard listeners can overlap the requested IPv4 binding.
            $BindAddress -eq '0.0.0.0' -or
            $_.LocalAddress -in @($BindAddress, '0.0.0.0', '::')
        })
    $owners = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($owners.Count -gt 1) {
        throw "Port $ListenPort has multiple owners; resolve the conflict before launching."
    }
    if ($owners.Count -eq 0) { return $null }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($owners[0])"
    if (-not $process -or -not $process.CommandLine) {
        throw "Cannot verify the process owning port $ListenPort."
    }
    return $process
}

function Test-QwenProcess($Process, [string]$Executable, [string]$ModelPath,
                          [int]$Slots = 1, [int]$ContextTokens = 32768, [int]$ModelPort = 8000) {
    if ($Process.ExecutablePath -ine $Executable) { return $false }
    $command = $Process.CommandLine
    $escapedModel = [regex]::Escape($ModelPath)
    if ($command -notmatch "(?:^|\s)--model\s+(?:`"$escapedModel`"|$escapedModel)(?:\s|$)") {
        return $false
    }
    foreach ($setting in @(
        '--host\s+127\.0\.0\.1', ('--port\s+' + $ModelPort),
        ('--ctx-size\s+' + ($ContextTokens * $Slots)),
        ('--parallel\s+' + $Slots), '--n-gpu-layers\s+999', '--cache-type-k\s+q8_0',
        '--cache-type-v\s+q8_0', '--flash-attn\s+on', '--jinja', '--no-webui'
    )) {
        if ($command -notmatch "(?:^|\s)$setting(?:\s|$)") { return $false }
    }
    return $true
}

function Test-RecollectProcess($Process, [string]$Root, [string]$BasePython = '') {
    $environmentPaths = @(
        (Join-Path $Root '.venv\Scripts\python.exe'),
        (Join-Path $Root '.venv\Scripts\recollect.exe')
    )
    # Windows' venv redirector runs the base Python binary with the venv command line.
    $executables = @($environmentPaths) + @($BasePython)
    $belongs = $false
    foreach ($path in $environmentPaths) {
        $escaped = [regex]::Escape($path)
        if ($Process.CommandLine -match "(?:^|\s)(?:`"$escaped`"|$escaped)(?:\s|$)") {
            $belongs = $true
        }
    }
    return ($belongs -and $Process.ExecutablePath -iin $executables -and
        $Process.CommandLine -match '(?:recollect(?:\.exe|\.cli)?["'']?\s+|recollect\.cli\s+)serve(?:\s|$)')
}

function Assert-QwenHealth($Health, $Models, [string]$ModelName, [string]$ModelPath) {
    $identities = @($Models.data | ForEach-Object { $_.id })
    # llama-server may advertise the exact model path when no alias is configured.
    if ($Health.status -ne 'ok' -or $identities.Count -ne 1 -or
        ($identities[0] -cne $ModelName -and $identities[0] -cne $ModelPath)) {
        throw "Qwen health/model mismatch: expected '$ModelName' or '$ModelPath'; received '$($identities -join ', ')', status '$($Health.status)'."
    }
}

function Invoke-WithQwenCuda([string]$Directory, [scriptblock]$Action) {
    foreach ($name in @('cublas64_13.dll', 'cublasLt64_13.dll', 'cudart64_13.dll')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Directory $name) -PathType Leaf)) {
            throw "Qwen CUDA dependency is missing: $name in $Directory"
        }
    }
    $originalPath = $env:PATH
    try {
        # Only Qwen inherits these libraries; Whisper uses a different CUDA runtime.
        $env:PATH = $Directory + ';' + $originalPath
        & $Action
    } finally { $env:PATH = $originalPath }
}

function Get-Json([string]$Url, [string]$PairingFile = '') {
    if (-not $PairingFile) {
        return Invoke-RestMethod -Uri $Url -TimeoutSec 10
    }
    $probe = @'
import json
import sys
import httpx
from recollect.pairing import load_pairing

url, path = sys.argv[1:]
pairing = load_pairing(path)
with httpx.Client(verify=pairing.ssl_context(), trust_env=False, timeout=10) as client:
    response = client.get(url, headers={"Authorization": "Bearer " + pairing.token},
                          extensions=pairing.request_extensions())
    response.raise_for_status()
    print(json.dumps(response.json()))
'@
    $result = $probe | & uv run --no-sync python -I - $Url $PairingFile
    if ($LASTEXITCODE -ne 0) { throw 'Authenticated HTTPS readiness probe failed.' }
    return ($result -join "`n") | ConvertFrom-Json
}

function Wait-Json([string]$Url, [string]$PairingFile = '', [int]$Seconds = 120) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        try { return Get-Json $Url $PairingFile } catch { }
        Write-Host "Waiting for $Url ..."
        Start-Sleep -Seconds 3
    } while ((Get-Date) -lt $deadline)
    throw "Readiness timed out at $Url. Inspect the launch logs; services were left running."
}

function Assert-RecollectHealth($Health, [string]$Sentinel) {
    if (-not $Health.ok -or -not $Health.embedder.sentinel_matches_research -or
        $Health.embedder.sentinel_sha256 -ne $Sentinel -or
        -not $Health.generator.reachable) {
        throw 'Recollect health did not verify the research embedder and reachable generator.'
    }
}

function Get-HostPairing([string]$Path) {
    $metadata = & uv run --no-sync python -I -m recollect.host_pairing --bundle $Path
    if ($LASTEXITCODE -ne 0) { throw 'Private HTTPS pairing initialization failed.' }
    return ($metadata -join "`n") | ConvertFrom-Json
}

function Test-UiBuildNeeded([string]$UiRoot) {
    $index = Join-Path $UiRoot 'dist\index.html'
    if (-not (Test-Path -LiteralPath $index -PathType Leaf)) { return $true }
    $built = (Get-Item -LiteralPath $index).LastWriteTimeUtc
    $inputs = @('src', 'public', 'index.html', 'package.json', 'package-lock.json')
    foreach ($inputPath in $inputs) {
        $path = Join-Path $UiRoot $inputPath
        if (Test-Path -LiteralPath $path) {
            $files = @(Get-Item -LiteralPath $path)
            if ($files[0].PSIsContainer) {
                $files = @(Get-ChildItem -LiteralPath $path -File -Recurse)
            }
            if (@($files | Where-Object LastWriteTimeUtc -gt $built).Count) {
                return $true
            }
        }
    }
    return @(
        Get-ChildItem -LiteralPath $UiRoot -File |
            Where-Object { $_.Name -match '^(vite|tsconfig)' -and $_.LastWriteTimeUtc -gt $built }
    ).Count -gt 0
}

function Invoke-RecollectLaunch {
    $recollectRoot = Split-Path -Parent $PSScriptRoot
    if (-not $BindHost) {
        if ($Mode -eq 'host') { $BindHost = '0.0.0.0' } else { $BindHost = '127.0.0.1' }
    }
    $address = $null
    if (-not [Net.IPAddress]::TryParse($BindHost, [ref]$address) -or
        $address.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
        throw '-BindHost must be a local IPv4 address or 0.0.0.0.'
    }
    if ($Mode -eq 'standalone' -and -not [Net.IPAddress]::IsLoopback($address)) {
        throw 'Standalone mode binds to loopback. Use -Mode host for another device.'
    }
    if ($Port -eq 8001) { throw 'Port 8001 is reserved for the loopback Qwen server.' }
    if ($Mode -eq 'standalone' -and $TokenFile) {
        throw '-TokenFile is used only with -Mode host.'
    }
    $probeHost = $BindHost
    if ($probeHost -eq '0.0.0.0') { $probeHost = '127.0.0.1' }
    $scheme = 'http'
    if ($Mode -eq 'host') { $scheme = 'https' }
    $baseUrl = "${scheme}://${probeHost}:$Port"
    $sentinel = 'baecf77627380f36f75a69c4454b064d886133f04255c5e5b4d3f24f00e7c4b8'
    $modelName = 'Qwen3.8-27B-UD-Q4_K_XL.gguf'
    $modelServerExe = Join-Path $env:USERPROFILE '.unsloth\llama.cpp\build\bin\Release\llama-server.exe'
    $chatModel = Join-Path $env:USERPROFILE ".cache\huggingface\hub\models--unsloth--Qwen3.8-27B-GGUF\snapshots\f1bfb127c64f7072bdd2cad55f258b9c8b2910fe\$modelName"
    $runtimeLogs = Join-Path $recollectRoot 'var\logs'
    $launchStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $journalPath = Join-Path $runtimeLogs "deployment-$Mode-$launchStamp.json"
    $journal = [ordered]@{
        mode = $Mode; bind_host = $BindHost; port = $Port; ready = $false
        started_at = (Get-Date).ToString('o'); qwen = $null; recollect = $null
    }
    Push-Location $recollectRoot
    try {
        foreach ($command in @('uv', 'docker', 'npm', 'nvidia-smi')) {
            Get-Command $command -ErrorAction Stop | Out-Null
        }
        foreach ($path in @(
            '.env', '.venv\Scripts\python.exe', '.venv\Scripts\recollect.exe',
            '..\contextDecayWindow\episodic', $modelServerExe, $chatModel
        )) {
            if (-not (Test-Path -LiteralPath $path)) {
                throw "Required installed asset is missing: $path. See AGENTS.md provisioning instructions."
            }
        }
        # Only selected deployment settings leave the dotenv reader; API credentials never do.
        $preflight = @'
import json
import sys
from pathlib import Path
from recollect.config import RecollectConfig

if sys.version_info[:2] != (3, 13):
    raise SystemExit("Use the existing Python 3.13 .venv.")
config = RecollectConfig.from_env()
expected = {
    "generator_base_url": "http://127.0.0.1:8001/v1",
    "generator_model": "Qwen3.8-27B-UD-Q4_K_XL.gguf",
    "embedding_threads": 8,
    "subagent_enabled": True,
    "subagent_backend": "opencode",
    "sandbox_container_runtime": "docker",
    "sandbox_container_image": "recollect-opencode-sandbox:1.18.18",
    "voice_wake_phrase": "hey idris",
    "voice_device": "cuda",
    "voice_asr_backend": "whisper",
    "voice_asr_device": "cuda",
    "voice_asr_compute_type": "float16",
    "voice_end_s": 1.4,
    "voice_max_utterance_s": 120.0,
}
for name, value in expected.items():
    if getattr(config, name) != value:
        raise SystemExit(f"Effective {name} differs from AGENTS.md; fix the setting before launching.")
expected_embedding = Path.home() / ".cache/huggingface/hub/Qwen3-Embedding-0.6B-GGUF/Qwen3-Embedding-0.6B-Q8_0.gguf"
if config.embedding_model_path.resolve() != expected_embedding.resolve():
    raise SystemExit("Embedding path differs from the pinned desktop artifact.")
required = [
    config.embedding_model_path,
    config.voice_model_dir / "vosk-model-small-en-us-0.15/am/final.mdl",
    config.voice_model_dir / "kokoro-v1.0.onnx",
    config.voice_model_dir / "voices-v1.0.bin",
    config.voice_model_dir / "silero-vad.onnx",
    config.voice_asr_model_dir / "model.bin",
    config.voice_asr_model_dir / "config.json",
    config.voice_asr_model_dir / "tokenizer.json",
    config.voice_asr_model_dir / "preprocessor_config.json",
    config.voice_asr_model_dir / "vocabulary.json",
]
for path in required:
    if not path.is_file():
        raise SystemExit(f"Required model asset is missing: {path}")
if config.voice_cuda_dll_dir is None or not config.voice_cuda_dll_dir.is_dir():
    raise SystemExit("Configured Kokoro CUDA DLL directory is missing.")
if config.voice_asr_cuda_dll_dir is not None and not config.voice_asr_cuda_dll_dir.is_dir():
    raise SystemExit("Configured Whisper CUDA DLL directory is missing.")
print(json.dumps({"image": config.sandbox_container_image,
                  "qwen_cuda_dll_dir": str(config.voice_cuda_dll_dir),
                  "model_slots": config.generator_parallel_slots,
                  "context_tokens": config.generator_context_tokens,
                  "base_python_executable": sys._base_executable}))
'@
        $settingsJson = $preflight | & uv run --no-sync python -
        if ($LASTEXITCODE -ne 0) { throw 'Desktop configuration preflight failed.' }
        $settings = ($settingsJson -join "`n") | ConvertFrom-Json
        New-Item -ItemType Directory -Force -Path $runtimeLogs | Out-Null
        $pairingFile = ''
        if ($Mode -eq 'host') {
            if (-not $TokenFile) { $TokenFile = 'var\deployment-token.txt' }
            if (-not [IO.Path]::IsPathRooted($TokenFile)) {
                $TokenFile = Join-Path $recollectRoot $TokenFile
            }
            $TokenFile = [IO.Path]::GetFullPath($TokenFile)
            $pairingMetadata = Get-HostPairing $TokenFile
            $pairingFile = $TokenFile
            $journal.token_file = $TokenFile
            $journal.certificate_file = $pairingMetadata.certificate_path
        }
        $qwenOwner = Get-PortProcess 8001
        $appOwner = Get-PortProcess $Port $BindHost
        if ($qwenOwner -and -not (Test-QwenProcess $qwenOwner $modelServerExe $chatModel $settings.model_slots $settings.context_tokens 8001)) {
            throw 'Port 8001 belongs to an incompatible process. Stop or reconfigure it explicitly.'
        }
        if ($appOwner) {
            if (-not (Test-RecollectProcess $appOwner $recollectRoot $settings.base_python_executable)) {
                throw "Port $Port belongs to another process. Choose a free port."
            }
            try {
                $deployment = Get-Json "$baseUrl/api/deployment" $pairingFile
                if ($deployment.mode -ne $Mode) { throw 'Deployment mode differs.' }
                Assert-RecollectHealth (Get-Json "$baseUrl/api/health" $pairingFile) $sentinel
            } catch {
                throw "Existing Recollect on port $Port is incompatible or unhealthy. Stop it explicitly, then relaunch."
            }
            $bindings = @(Get-NetTCPConnection -State Listen -LocalPort $Port)
            if ($BindHost -notin @($bindings | Select-Object -ExpandProperty LocalAddress)) {
                throw 'Existing Recollect uses a different bind address. Stop it explicitly, then relaunch.'
            }
        }
        if ($qwenOwner) {
            $qwenHealth = Get-Json 'http://127.0.0.1:8001/health'
            $qwenModels = Get-Json 'http://127.0.0.1:8001/v1/models'
            Assert-QwenHealth $qwenHealth $qwenModels $modelName $chatModel
        }

        Write-Host 'Checking Linux Docker engine ...'
        $dockerReady = $false
        try {
            $dockerType = & docker info --format '{{.OSType}}' 2>$null
            $dockerReady = ($LASTEXITCODE -eq 0 -and $dockerType -eq 'linux')
        } catch { }
        if (-not $dockerReady) {
            $dockerDesktop = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\frontend\Docker Desktop.exe'
            if (-not (Test-Path -LiteralPath $dockerDesktop -PathType Leaf)) {
                throw 'Linux Docker is unavailable and the configured Docker Desktop executable is missing.'
            }
            Start-Process -FilePath $dockerDesktop -WindowStyle Hidden
            $deadline = (Get-Date).AddSeconds(120)
            do {
                Start-Sleep -Seconds 3
                try {
                    $dockerType = & docker info --format '{{.OSType}}' 2>$null
                    $dockerReady = ($LASTEXITCODE -eq 0 -and $dockerType -eq 'linux')
                } catch { }
                if ($dockerReady) { break }
                Write-Host 'Waiting for Linux Docker engine ...'
            } while ((Get-Date) -lt $deadline)
            if (-not $dockerReady) { throw 'Linux Docker did not become ready within two minutes.' }
        }
        $inputHashes = foreach ($path in @(
            '.dockerignore', 'deploy/opencode-sandbox/Dockerfile', 'deploy/opencode-sandbox/requirements.lock',
            'src/recollect/engine/mcp_research.py', 'src/recollect/engine/webtools.py'
        )) {
            "$path $((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash)"
        }
        $hasher = [Security.Cryptography.SHA256]::Create()
        try {
            $digest = $hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($inputHashes -join "`n"))
            $imageHash = ([BitConverter]::ToString($digest)).Replace('-', '').ToLowerInvariant()
        } finally { $hasher.Dispose() }
        $imageLabel = ''
        try {
            $imageJson = & docker image inspect $settings.image 2>$null
            if ($LASTEXITCODE -eq 0) {
                $imageInfo = ($imageJson -join "`n") | ConvertFrom-Json
                $labels = $imageInfo[0].Config.Labels
                if ($labels -and $labels.PSObject.Properties['io.recollect.build-inputs.sha256']) {
                    $imageLabel = $labels.'io.recollect.build-inputs.sha256'
                }
            }
        } catch { }
        if ($imageLabel -ne $imageHash) {
            Write-Host 'Building the sandbox image for the current research-tool inputs ...'
            & docker build -f deploy/opencode-sandbox/Dockerfile -t $settings.image `
                --label "io.recollect.build-inputs.sha256=$imageHash" .
            if ($LASTEXITCODE -ne 0) { throw 'Sandbox image build failed.' }
        }
        $journal.docker_image = $settings.image
        $journal.docker_inputs_sha256 = $imageHash

        if ($qwenOwner) {
            Write-Host "Reusing matching Qwen process $($qwenOwner.ProcessId)."
            $journal.qwen = @{ pid = $qwenOwner.ProcessId; reused = $true }
        } else {
            Write-Host 'Starting Qwen on the desktop GPU ...'
            $modelArgs = @(
                '--model', ('"{0}"' -f $chatModel), '--host', '127.0.0.1', '--port', '8001',
                '--ctx-size', [string]($settings.context_tokens * $settings.model_slots),
                '--parallel', [string]$settings.model_slots, '--n-gpu-layers', '999',
                '--cache-type-k', 'q8_0', '--cache-type-v', 'q8_0',
                '--flash-attn', 'on', '--jinja', '--metrics', '--no-webui',
                # This build maps native GPU-offload information to trace level.
                '--verbosity', '4'
            )
            $qwenStdout = Join-Path $runtimeLogs "qwen-$launchStamp.stdout.log"
            $qwenStderr = Join-Path $runtimeLogs "qwen-$launchStamp.stderr.log"
            $qwenProcess = Invoke-WithQwenCuda $settings.qwen_cuda_dll_dir {
                $devices = & $modelServerExe --list-devices
                if ($LASTEXITCODE -ne 0 -or ($devices -join "`n") -notmatch '(?m)^\s*CUDA[0-9]+:') {
                    throw 'Qwen cannot discover a CUDA device; check its installed CUDA libraries before launching.'
                }
                Start-Process -FilePath $modelServerExe -ArgumentList $modelArgs `
                    -WorkingDirectory $recollectRoot -WindowStyle Hidden -PassThru `
                    -RedirectStandardOutput $qwenStdout -RedirectStandardError $qwenStderr
            }
            $journal.qwen = @{
                pid = $qwenProcess.Id; reused = $false; stdout = $qwenStdout; stderr = $qwenStderr
            }
            $qwenHealth = Wait-Json 'http://127.0.0.1:8001/health' '' 180
            $qwenModels = Get-Json 'http://127.0.0.1:8001/v1/models'
            Assert-QwenHealth $qwenHealth $qwenModels $modelName $chatModel
            $startup = (Get-Content -LiteralPath $qwenStderr -Raw) + (Get-Content -LiteralPath $qwenStdout -Raw)
            if ($startup -notmatch 'offloaded\s+[1-9][0-9]*/[0-9]+\s+layers to GPU') {
                throw "Qwen startup did not confirm GPU layer offload. Inspect $qwenStderr"
            }
        }
        $gpuProcesses = & nvidia-smi --query-compute-apps=pid --format=csv,noheader,nounits
        if ($LASTEXITCODE -ne 0 -or [string]$journal.qwen.pid -notin @($gpuProcesses | ForEach-Object { $_.Trim() })) {
            throw 'nvidia-smi did not confirm Qwen as a GPU process.'
        }

        Write-Host 'Verifying the pinned in-process embedding identity ...'
        $doctorOutput = & uv run --no-sync recollect doctor
        $doctorExit = $LASTEXITCODE
        $doctorOutput | ForEach-Object { Write-Host $_ }
        if ($doctorExit -ne 0 -or ($doctorOutput -join "`n") -notmatch [regex]::Escape($sentinel)) {
            throw 'Doctor failed or the pinned research sentinel was absent. See docs/EMBEDDER.md.'
        }
        $uiRoot = Join-Path $recollectRoot 'ui'
        if (Test-UiBuildNeeded $uiRoot) {
            Push-Location $uiRoot
            try {
                & npm run build
                if ($LASTEXITCODE -ne 0) { throw 'UI build failed.' }
            } finally { Pop-Location }
        }
        if ($appOwner) {
            Write-Host "Reusing matching Recollect process $($appOwner.ProcessId)."
            $journal.recollect = @{ pid = $appOwner.ProcessId; reused = $true }
        } else {
            $appArgs = @('serve', '--mode', $Mode, '--host', $BindHost, '--port', $Port)
            if ($Mode -eq 'host') {
                $appArgs += @(
                    '--token-file', ('"{0}"' -f $TokenFile),
                    '--ssl-certfile', ('"{0}"' -f $pairingMetadata.certificate_path),
                    '--ssl-keyfile', ('"{0}"' -f $pairingMetadata.private_key_path)
                )
            }
            $appStdout = Join-Path $runtimeLogs "recollect-$launchStamp.stdout.log"
            $appStderr = Join-Path $runtimeLogs "recollect-$launchStamp.stderr.log"
            $appProcess = Start-Process -FilePath (Join-Path $recollectRoot '.venv\Scripts\recollect.exe') `
                -ArgumentList $appArgs -WorkingDirectory $recollectRoot -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput $appStdout -RedirectStandardError $appStderr
            $journal.recollect = @{
                pid = $appProcess.Id; reused = $false; stdout = $appStdout; stderr = $appStderr
            }
        }
        Assert-RecollectHealth (Wait-Json "$baseUrl/api/health" $pairingFile 120) $sentinel
        $deployment = Get-Json "$baseUrl/api/deployment" $pairingFile
        if ($deployment.mode -ne $Mode) { throw 'Serving deployment mode differs from the requested mode.' }

        Write-Host 'Warming Whisper and Kokoro inside the serving process ...'
        $warmVoice = @'
import asyncio
import json
import sys

import httpx
from websockets.asyncio.client import connect
from recollect.pairing import load_pairing

async def warm():
    base, token_file = sys.argv[1:]
    headers = {}
    extensions = {}
    websocket_options = {}
    verify = True
    if token_file != "-":
        pairing = load_pairing(token_file)
        headers["Authorization"] = "Bearer " + pairing.token
        extensions = pairing.request_extensions()
        websocket_options = pairing.websocket_options()
        verify = pairing.ssl_context()
    async with httpx.AsyncClient(timeout=10, headers=headers, trust_env=False, verify=verify) as client:
        async def status():
            response = await client.get(base + "/api/voice/status", extensions=extensions)
            response.raise_for_status()
            return response.json()

        def ready(value):
            return (value.get("available") and value.get("error") is None
                    and value.get("asr_backend") == "whisper" and value.get("asr_ready")
                    and value.get("asr_device") == "cuda"
                    and value.get("asr_compute_type") == "float16"
                    and value.get("provider") == "CUDAExecutionProvider")

        value = await status()
        if not ready(value):
            async with connect(
                base.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/api/voice/listen",
                additional_headers=headers, open_timeout=15, close_timeout=5, proxy=None,
                **websocket_options,
            ) as socket:
                event = json.loads(await asyncio.wait_for(socket.recv(), 60))
                if event.get("type") != "state" or event.get("state") != "waiting":
                    value = await status()
                    if not ready(value):
                        raise RuntimeError(f"Voice warm-up did not reach waiting: {event}")
            value = await status()
        if not ready(value):
            raise RuntimeError(f"GPU voice is not ready: {value}")
        print(json.dumps(value, indent=2))

asyncio.run(warm())
'@
        $warmTokenFile = '-'
        if ($Mode -eq 'host') { $warmTokenFile = $TokenFile }
        $voiceOutput = $warmVoice | & uv run --no-sync python -I - $baseUrl $warmTokenFile
        if ($LASTEXITCODE -ne 0) { throw 'Serving-process voice warm-up failed; the launch is not ready.' }
        $voiceOutput | ForEach-Object { Write-Host $_ }
        $journal.voice = ($voiceOutput -join "`n") | ConvertFrom-Json
        & nvidia-smi
        if ($LASTEXITCODE -ne 0) { throw 'Final GPU check failed.' }
        $sessions = @(Get-Json "$baseUrl/api/sessions" $pairingFile)
        if ($Mode -eq 'standalone') {
            $page = Invoke-WebRequest -Uri "$baseUrl/" -UseBasicParsing -TimeoutSec 10
            if ($page.StatusCode -ne 200 -or $page.Content -notmatch '<div id="root">') {
                throw 'Built inspector page did not load.'
            }
            Start-Process "$baseUrl/"
        }
        $journal.ready = $true
        $journal.session_count = $sessions.Count
        Write-Host "Ready: $Mode on ${BindHost}:$Port; $($sessions.Count) saved sessions available."
        Write-Host "Docker image ready; Qwen GPU healthy; embedding sentinel verified; Whisper CUDA float16 and Kokoro CUDA ready."
        if ($Mode -eq 'host') {
            Write-Host "Pair the Ubuntu client with this desktop's HTTPS LAN address on port $Port and pairing file $TokenFile."
        }
    } finally {
        if (Test-Path -LiteralPath $runtimeLogs -PathType Container) {
            $journal | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $journalPath -Encoding UTF8
            Write-Host "Launch status and process/log paths: $journalPath"
        }
        Pop-Location
    }
}

function Test-RecollectContainer($Container, [string]$Image, [string]$SandboxRoot) {
    if ($Container.Name -notmatch '^/recollect-subagent-[a-f0-9]+$' -or
        $Container.Config.Image -cne $Image) { return $false }
    $mounts = @($Container.Mounts)
    if ($mounts.Count -ne 2) { return $false }
    $root = [IO.Path]::GetFullPath($SandboxRoot).TrimEnd('\', '/') + '\'
    $parents = @()
    foreach ($mount in $mounts) {
        if ($mount.Type -ne 'bind' -or $mount.Destination -notin @('/workspace', '/config')) {
            return $false
        }
        $source = [IO.Path]::GetFullPath($mount.Source)
        if (-not $source.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { return $false }
        $leaf = Split-Path $source -Leaf
        if (($mount.Destination -eq '/workspace' -and ($leaf -ne 'workspace' -or -not $mount.RW)) -or
            ($mount.Destination -eq '/config' -and ($leaf -ne 'config' -or $mount.RW))) { return $false }
        $parents += Split-Path $source -Parent
    }
    return ($parents[0] -ieq $parents[1] -and
        @($mounts.Destination | Select-Object -Unique).Count -eq 2)
}

function Stop-VerifiedProcess($Expected) {
    $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($Expected.ProcessId)"
    if (-not $current) { return }
    # A recycled PID must never become a shutdown target.
    if ($current.CreationDate -ne $Expected.CreationDate -or
        $current.ExecutablePath -cne $Expected.ExecutablePath -or
        $current.CommandLine -cne $Expected.CommandLine) {
        throw "Process $($Expected.ProcessId) changed identity; shutdown refused."
    }
    $handle = Get-Process -Id $current.ProcessId -ErrorAction SilentlyContinue
    if (-not $handle) { return }
    try {
        if ($handle.HasExited) { return }
        Stop-Process -InputObject $handle -ErrorAction Stop
        # Windows may retain an exited process in enumeration until handles close.
        if (-not $handle.WaitForExit(15000)) {
            throw "Process $($current.ProcessId) did not stop."
        }
    } finally { $handle.Dispose() }
}

function Invoke-RecollectStop {
    $recollectRoot = Split-Path -Parent $PSScriptRoot
    Push-Location $recollectRoot
    try {
        $python = Join-Path $recollectRoot '.venv\Scripts\python.exe'
        $settingsJson = & $python -I -c 'import json,sys; from recollect.config import RecollectConfig; c=RecollectConfig.from_env(); print(json.dumps(dict(base_python=sys._base_executable,image=c.sandbox_container_image,root=str(c.sandbox_root.resolve()),context_tokens=c.generator_context_tokens)))'
        if ($LASTEXITCODE -ne 0) { throw 'Could not read shutdown configuration.' }
        $settings = $settingsJson | ConvertFrom-Json
        $modelExe = Join-Path $env:USERPROFILE '.unsloth\llama.cpp\build\bin\Release\llama-server.exe'
        $model = Join-Path $env:USERPROFILE '.cache\huggingface\hub\models--unsloth--Qwen3.8-27B-GGUF\snapshots\f1bfb127c64f7072bdd2cad55f258b9c8b2910fe\Qwen3.8-27B-UD-Q4_K_XL.gguf'
        $processes = @(Get-CimInstance Win32_Process)
        $apps = @($processes | Where-Object { Test-RecollectProcess $_ $recollectRoot $settings.base_python })
        $models = @($processes | Where-Object {
            (Test-QwenProcess $_ $modelExe $model 1 $settings.context_tokens 8001) -or
            (Test-QwenProcess $_ $modelExe $model 2 $settings.context_tokens 8001)
        })
        $targetIds = @(@($apps) + @($models) | ForEach-Object { $_.ProcessId })
        # Inspect all targets before stopping anything, including an occupied API port.
        foreach ($listenPort in @($Port, 8001)) {
            $targetAddress = if ($listenPort -eq 8001) { '127.0.0.1' } else { '0.0.0.0' }
            $owner = Get-PortProcess $listenPort $targetAddress
            if ($owner -and $owner.ProcessId -notin $targetIds) {
                throw "Port $listenPort belongs to another process; no services stopped."
            }
        }
        $containers = @()
        if (Get-Command docker -ErrorAction SilentlyContinue) {
            $ids = @(& docker ps -aq --filter 'name=recollect-subagent-' 2>$null)
            if ($LASTEXITCODE -ne 0) {
                throw 'Docker is unavailable; cannot verify research container cleanup. No services stopped.'
            }
            foreach ($containerId in $ids) {
                $inspection = & docker inspect $containerId
                if ($LASTEXITCODE -ne 0) { throw "Cannot inspect container $containerId." }
                $container = @($inspection | ConvertFrom-Json)[0]
                if (-not (Test-RecollectContainer $container $settings.image $settings.root)) {
                    throw "Container $containerId has unexpected ownership; no services stopped."
                }
                $containers += $container
            }
        }
        Write-Host "Verified targets: $($apps.Count) Recollect processes, $($models.Count) Qwen processes, $($containers.Count) research containers."
        if ($DryRun) { Write-Host 'Preview only; nothing stopped.'; return }
        # Stop the serving child before its venv redirectors/console launcher.
        foreach ($app in @($apps | Sort-Object CreationDate -Descending)) { Stop-VerifiedProcess $app }
        foreach ($container in $containers) {
            $remaining = @(& docker ps -aq --filter "id=$($container.Id)")
            if ($LASTEXITCODE -ne 0) { throw 'Could not check research container cleanup.' }
            if ($remaining.Count) {
                & docker stop --time 5 $container.Id | Out-Null
                if ($LASTEXITCODE -ne 0) { throw 'Could not stop research container.' }
                $remaining = @(& docker ps -aq --filter "id=$($container.Id)")
                if ($remaining.Count) {
                    & docker rm $container.Id | Out-Null
                    if ($LASTEXITCODE -ne 0) { throw 'Could not remove stopped research container.' }
                }
            }
        }
        foreach ($modelProcess in $models) { Stop-VerifiedProcess $modelProcess }
        foreach ($listenPort in @($Port, 8001)) {
            $targetAddress = if ($listenPort -eq 8001) { '127.0.0.1' } else { '0.0.0.0' }
            if (Get-PortProcess $listenPort $targetAddress) { throw "Port $listenPort remains occupied." }
        }
        Write-Host 'Recollect and its models stopped. Docker Desktop remains running. Saved history and model files are preserved.'
    } finally { Pop-Location }
}

if ($MyInvocation.InvocationName -ne '.') {
    if ($Mode -eq 'stop') { Invoke-RecollectStop } else { Invoke-RecollectLaunch }
}
