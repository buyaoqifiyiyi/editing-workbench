<#
.SYNOPSIS
    One-command fresh-PC setup for OpenEdit Engine (Python deps, external tools, and offline AI weights).
.EXAMPLE
    .\scripts\setup.ps1 -Tier ai -Gpu -HfMirror -SyncSkill
#>
[CmdletBinding()]
param(
    [ValidateSet('core', 'ai', 'full', 'gpu-heavy')]
    [string]$Tier = 'ai',
    [switch]$Gpu,
    [switch]$HfMirror,
    [switch]$DryRun,
    [switch]$SyncSkill,
    [string]$VenvDir = (Join-Path $env:LOCALAPPDATA 'OpenEditEngine\venv'),
    [string]$ToolsDir = (Join-Path $env:LOCALAPPDATA 'OpenEditTools'),
    [string]$EngineDir = (Join-Path $env:LOCALAPPDATA 'OpenEditEngine')
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

function Find-Python312 {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        try {
            $ver = & py -3.12 -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $ver.Trim() -eq '3.12') {
                return @('py', '-3.12')
            }
        } catch {}
    }
    $pyCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pyCmd) {
        $ver = & $pyCmd.Source -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $ver.Trim() -eq '3.12') {
            return @($pyCmd.Source)
        }
    }
    throw 'Python 3.12 is required (openedit-engine requires >=3.12,<3.13). Install Python 3.12 first.'
}

$basePy = Find-Python312
$venvPy = Join-Path $VenvDir 'Scripts\python.exe'
$extras = if ($Gpu) { '.[all,gpu]' } else { '.[all]' }

Write-Host "[OpenEdit Setup] Repo: $RepoRoot | Tier: $Tier | Venv: $VenvDir | Tools: $ToolsDir"

if (-not (Test-Path -LiteralPath $venvPy)) {
    if ($DryRun) {
        Write-Host "[DryRun] Would create venv: $($basePy -join ' ') -m venv `"$VenvDir`""
    } else {
        New-Item -ItemType Directory -Force -Path (Split-Path $VenvDir -Parent) | Out-Null
        if ($basePy.Count -gt 1) {
            & $basePy[0] $basePy[1] -m venv $VenvDir
        } else {
            & $basePy[0] -m venv $VenvDir
        }
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
}

if ($DryRun) {
    Write-Host "[DryRun] Would install package: `"$venvPy`" -m pip install -e `"$extras`""
} else {
    Push-Location $RepoRoot
    try {
        & $venvPy -m pip install --upgrade pip
        & $venvPy -m pip install -e $extras
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    } finally {
        Pop-Location
    }
}

$bootArgs = @('-m', 'openedit_engine', 'bootstrap', '--tier', $Tier, '--tools-dir', $ToolsDir, '--engine-dir', $EngineDir)
if ($HfMirror) { $bootArgs += @('--hf-endpoint', 'https://hf-mirror.com') }
if ($DryRun) { $bootArgs += '--dry-run' }

if (Test-Path -LiteralPath $venvPy) {
    & $venvPy @bootArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} else {
    Write-Host "[DryRun] Would run bootstrap: openedit $($bootArgs[2..($bootArgs.Count-1)] -join ' ')"
}

if ($SyncSkill) {
    $srcSkill = Join-Path $RepoRoot 'adapters\skills\editing-workbench'
    $dstSkill = Join-Path $env:USERPROFILE '.agents\skills\editing-workbench'
    if ($DryRun) {
        Write-Host "[DryRun] Would copy Skill template '$srcSkill' -> '$dstSkill'"
    } else {
        New-Item -ItemType Directory -Force -Path $dstSkill | Out-Null
        foreach ($sub in @('SKILL.md', 'agents', 'modules')) {
            $s = Join-Path $srcSkill $sub
            $d = Join-Path $dstSkill $sub
            if (Test-Path $s -PathType Container) {
                if (Test-Path $d) { Remove-Item $d -Recurse -Force }
                Copy-Item $s $d -Recurse -Force
            } else {
                Copy-Item $s $d -Force
            }
        }
        Write-Host "[OpenEdit Setup] Synced Skill template to $dstSkill"
    }
}

if (-not $DryRun -and (Test-Path -LiteralPath $venvPy)) {
    Write-Host '[OpenEdit Setup] Running openedit doctor...'
    & $venvPy -m openedit_engine doctor
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
