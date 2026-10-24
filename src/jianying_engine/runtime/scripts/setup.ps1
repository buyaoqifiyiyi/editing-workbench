param([string]$Python = 'python', [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$skillRoot = Split-Path -Parent $PSScriptRoot
if ((Split-Path -Leaf $skillRoot) -eq 'runtime') {
    throw 'This is packaged engine runtime data. Create a virtual environment outside the engine package, then use its Python -m pip install <engine-wheel-or-project-path>. Do not create runtime/.venv. Legacy Skill-path setup remains available only from its original Skill directory.'
}
$skillEnvironment = Join-Path $skillRoot '.venv'
$skillPython = Join-Path $skillEnvironment 'Scripts\python.exe'
$requiredPython = '3.12'
function Invoke-PythonQuiet([string]$Executable, [string]$Code) {
    # Windows PowerShell 5.1 turns redirected native stderr into a terminating error under 'Stop';
    # this function-local preference keeps the exit-code check in charge.
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $Executable -c $Code 2>$null
        $exitCode = $LASTEXITCODE
    } catch {
        # Launch failures (missing/invalid executable) still terminate the statement.
        $output = ''
        $exitCode = -1
    }
    [pscustomobject]@{ ExitCode = $exitCode; Output = [string]$output }
}
if (-not (Test-Path -LiteralPath $skillPython)) {
    if ($CheckOnly) { throw 'Skill .venv is absent. Run setup.ps1 -Python <new-device-python-path> to create it locally.' }
    $bootstrapPython = Get-Command -Name $Python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $bootstrapPython -or $bootstrapPython.Source -match '[\\/]WindowsApps[\\/]') {
        throw 'A real Python executable is required. Install Python on this device and pass its full path with -Python; Windows Store aliases are not invoked.'
    }
    # No double quotes in -c code: Windows PowerShell 5.1 does not escape them for native commands.
    $bootstrapVersion = Invoke-PythonQuiet $bootstrapPython.Source 'import sys; print(*sys.version_info[:2], sep=chr(46))'
    if ($bootstrapVersion.ExitCode -ne 0 -or $bootstrapVersion.Output.Trim() -ne $requiredPython) {
        throw "Python $requiredPython is required (matches constraints.txt and the verified .venv); $($bootstrapPython.Source) reports '$($bootstrapVersion.Output.Trim())'. Pass a Python $requiredPython executable with -Python."
    }
    & $bootstrapPython.Source -m venv $skillEnvironment
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create the skill Python environment.' }
}
$environmentHealthy = $false
try {
    $environmentPrefix = Invoke-PythonQuiet $skillPython 'import sys; print(sys.prefix)'
    $environmentHealthy = $environmentPrefix.ExitCode -eq 0 -and [IO.Path]::GetFullPath($environmentPrefix.Output.Trim()) -eq [IO.Path]::GetFullPath($skillEnvironment)
} catch { $environmentHealthy = $false }
if (-not $environmentHealthy) {
    throw 'Copied .venv cannot run in this location. Keep it as a backup outside the active .venv, then rerun setup.ps1 with the new device Python. Do not copy virtual environments between devices.'
}
if ($CheckOnly) {
    & $skillPython (Join-Path $PSScriptRoot 'editor.py') doctor
    if ($LASTEXITCODE -ne 0) { throw 'Environment needs setup; follow doctor migration_instructions.' }
    return
}
& $skillPython -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt') -c (Join-Path $PSScriptRoot 'constraints.txt')
if ($LASTEXITCODE -ne 0) { throw 'Cannot install pyJianYingDraft dependencies.' }
& $skillPython (Join-Path $PSScriptRoot 'editor.py') doctor
if ($LASTEXITCODE -ne 0) { throw 'Skill doctor failed; inspect the output.' }
