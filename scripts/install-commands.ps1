#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$BinDirectory = (Join-Path $env:USERPROFILE '.local\bin'),
    [switch]$NoPathUpdate
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Add-RecollectPathEntry([string]$ExistingPath, [string]$Directory) {
    $target = [IO.Path]::GetFullPath($Directory).TrimEnd('\', '/')
    foreach ($entry in ($ExistingPath -split ';')) {
        if (-not $entry.Trim()) { continue }
        try {
            $expanded = [Environment]::ExpandEnvironmentVariables($entry.Trim().Trim('"'))
            $normalized = [IO.Path]::GetFullPath($expanded).TrimEnd('\', '/')
            if ($normalized -ieq $target) { return $ExistingPath }
        } catch { }
    }
    if (-not $ExistingPath) { return $Directory }
    if ($ExistingPath.EndsWith(';')) { return $ExistingPath + $Directory }
    return $ExistingPath + ';' + $Directory
}

function Install-RecollectCommands {
    $recollectRoot = Split-Path -Parent $PSScriptRoot
    $destination = [IO.Path]::GetFullPath($BinDirectory)
    $recollectExe = Join-Path $recollectRoot '.venv\Scripts\recollect.exe'
    $pythonExe = Join-Path $recollectRoot '.venv\Scripts\python.exe'
    foreach ($required in @(
        $recollectExe, $pythonExe,
        (Join-Path $recollectRoot 'src\recollect\launch.py'),
        (Join-Path $recollectRoot 'scripts\launch.ps1')
    )) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "Required command target is missing: $required. Preserve the existing .venv; finish setup before installing commands."
        }
    }
    if (Test-Path -LiteralPath $destination) {
        if (-not (Test-Path -LiteralPath $destination -PathType Container)) {
            throw "Command directory is not a directory: $destination"
        }
    }
    $marker = 'rem Managed by Recollect scripts/install-commands.ps1'
    # Percent signs in repository paths are literal batch-file data, not variables.
    $recollectTarget = $recollectExe.Replace('%', '%%')
    $hostTarget = $pythonExe.Replace('%', '%%')
    $wrappers = [ordered]@{
        'recollect.cmd' = ('"{0}" %*' -f $recollectTarget)
        'recollect-host.cmd' = ('"{0}" -I -m recollect.launch host %*' -f $hostTarget)
    }
    # Check both command names before replacing either existing wrapper.
    foreach ($name in $wrappers.Keys) {
        $path = Join-Path $destination $name
        if (Test-Path -LiteralPath $path) {
            $item = Get-Item -LiteralPath $path -Force
            if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw "Refusing to replace a directory or link: $path"
            }
            $lines = [IO.File]::ReadAllLines($path)
            if ($lines.Length -lt 2 -or $lines[1] -cne $marker) {
                throw "Refusing to overwrite a command not managed by Recollect: $path"
            }
        }
    }
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    foreach ($name in $wrappers.Keys) {
        $path = Join-Path $destination $name
        $content = @(
            '@echo off', $marker, 'setlocal DisableDelayedExpansion',
            $wrappers[$name], 'exit /b %errorlevel%', ''
        ) -join "`r`n"
        if (-not (Test-Path -LiteralPath $path) -or
            [IO.File]::ReadAllText($path) -cne $content) {
            [IO.File]::WriteAllText($path, $content, [Text.UTF8Encoding]::new($false))
        }
        Write-Host "Installed $path"
    }
    if (-not $NoPathUpdate) {
        $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
        $updatedUserPath = Add-RecollectPathEntry $userPath $destination
        if ($updatedUserPath -cne $userPath) {
            [Environment]::SetEnvironmentVariable('Path', $updatedUserPath, 'User')
        }
        $env:Path = Add-RecollectPathEntry $env:Path $destination
    }
    Write-Host 'Commands installed: recollect and recollect-host. No services were started.'
}

if ($MyInvocation.InvocationName -ne '.') { Install-RecollectCommands }
