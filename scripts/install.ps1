#Requires -Version 5.1
<#
.SYNOPSIS
    Installs the notes-pipeline tray app.

.DESCRIPTION
    1. Resolves a Python 3.11 interpreter (prefers pyenv-win 3.11.5).
    2. Installs all packages from requirements.txt.
    3. Smoke-checks that GEMINI_API_KEY is set and that recorder.tray imports.
    4. Registers a Windows Task Scheduler task ("notes-pipeline-tray") that
       launches scripts\run_tray.bat at every logon for the current user.

.NOTES
    Run from the repo root, e.g.
        cd path\to\audio-transcriber-obsidian-notes
        .\scripts\install.ps1

    No admin rights required to register a current-user logon task. If your
    PowerShell execution policy blocks scripts, run:
        powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
#>

[CmdletBinding()]
param(
    [string]$TaskName = "notes-pipeline-tray"
)

# Continue rather than Stop because native commands (pip, python) often write
# benign WARNINGs to stderr; PS 5.1 treats every stderr write as a terminating
# error under "Stop", which kills the install on harmless lines like
# "WARNING: The scripts pip.exe ... is not on PATH".
# Cmdlets that need throwing behavior set -ErrorAction Stop explicitly below;
# native commands are checked via $LASTEXITCODE.
$ErrorActionPreference = "Continue"

# ---------------------------------------------------------------- helpers

function Write-Banner([string]$text) {
    $line = ("=" * 72)
    Write-Host ""
    Write-Host $line -ForegroundColor Cyan
    Write-Host $text -ForegroundColor Cyan
    Write-Host $line -ForegroundColor Cyan
}

function Write-Step([string]$text) {
    Write-Host ""
    Write-Host "[ STEP ] $text" -ForegroundColor Yellow
}

function Write-Pass([string]$text) {
    Write-Host "[  OK  ] $text" -ForegroundColor Green
}

function Write-Fail([string]$text) {
    Write-Host "[ FAIL ] $text" -ForegroundColor Red
}

function Resolve-Python {
    # Returns the absolute path to a Python interpreter, or $null.
    # Honor an explicit override first.
    if ($env:NOTES_PYTHON -and (Test-Path $env:NOTES_PYTHON)) {
        return $env:NOTES_PYTHON
    }

    # Prefer a pyenv-win 3.11.x interpreter under the current user profile.
    $pyenvCandidate = Join-Path $env:USERPROFILE ".pyenv\pyenv-win\versions\3.11.5\python.exe"
    if (Test-Path $pyenvCandidate) {
        return $pyenvCandidate
    }

    # Try `py -3.11`
    $pyLauncher = (Get-Command py -ErrorAction SilentlyContinue)
    if ($pyLauncher) {
        try {
            $resolved = & py -3.11 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $resolved) {
                return $resolved.Trim()
            }
        } catch { }
    }

    # Fall back to whichever python is on PATH.
    $sysPython = (Get-Command python -ErrorAction SilentlyContinue)
    if ($sysPython) {
        return $sysPython.Source
    }
    return $null
}

function Resolve-Pythonw {
    # Returns the path to pythonw.exe (no-console variant) for the resolved Python.
    # Used by the Task Scheduler action so launching the tray doesn't flash a cmd window.
    param([string]$pythonExe)
    $pythonwCandidate = $pythonExe -replace 'python\.exe$', 'pythonw.exe'
    if (Test-Path $pythonwCandidate) {
        return $pythonwCandidate
    }
    return $null
}

# ---------------------------------------------------------------- main

Write-Banner "Installing notes-pipeline..."

$root = (Resolve-Path -ErrorAction Stop (Join-Path $PSScriptRoot "..")).Path
Write-Host "Project root: $root"

# 1) Resolve Python --------------------------------------------------------

Write-Step "Resolving Python interpreter"

$python = Resolve-Python
if (-not $python) {
    Write-Fail "No Python interpreter found. Install Python 3.11 (e.g. via pyenv-win) and re-run."
    exit 1
}
Write-Host "  -> $python"

try {
    $pyVersion = & $python --version 2>&1
    Write-Pass "Python OK: $pyVersion"
} catch {
    Write-Fail "Could not invoke '$python --version': $_"
    exit 1
}

# 2) pip install ----------------------------------------------------------

Write-Step "Installing Python dependencies (pip install -r requirements.txt)"

$reqPath = Join-Path $root "requirements.txt"
if (-not (Test-Path $reqPath)) {
    Write-Fail "requirements.txt not found at $reqPath"
    exit 1
}

$pipLog = Join-Path $root ".logs\install-pip.log"
New-Item -ItemType Directory -Force -Path (Split-Path $pipLog) | Out-Null

# Run pip via the resolved interpreter. Capture both stdout/stderr.
& $python -m pip install --upgrade pip 2>&1 | Tee-Object -FilePath $pipLog | Out-Host
if ($LASTEXITCODE -ne 0) {
    Write-Fail "pip self-upgrade failed. See $pipLog"
    exit 1
}

& $python -m pip install -r $reqPath 2>&1 | Tee-Object -FilePath $pipLog -Append | Out-Host
if ($LASTEXITCODE -ne 0) {
    Write-Fail "pip install -r requirements.txt failed. See $pipLog"
    exit 1
}
Write-Pass "Dependencies installed (log: $pipLog)"

# 3) Validate GEMINI_API_KEY ---------------------------------------------

Write-Step "Validating GEMINI_API_KEY (offline check, no network call)"

Push-Location $root
try {
    $envCheck = & $python -c "from config import GEMINI_API_KEY; assert GEMINI_API_KEY, 'GEMINI_API_KEY not set in .env'; print('GEMINI_API_KEY length:', len(GEMINI_API_KEY))" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "GEMINI_API_KEY check failed:"
        Write-Host $envCheck
        Write-Host ""
        Write-Host "Fix: copy .env.example to .env in the repo root and set" -ForegroundColor Yellow
        Write-Host "  GEMINI_API_KEY=AIza...   (get a key at https://aistudio.google.com/apikey )" -ForegroundColor Yellow
        exit 1
    }
    Write-Pass ("GEMINI_API_KEY present ({0})" -f ($envCheck.Trim()))
} finally {
    Pop-Location
}

# 4) Smoke-check tray import ----------------------------------------------

Write-Step "Smoke-checking 'from recorder.tray import main'"

Push-Location $root
try {
    $smoke = & $python -c "from recorder.tray import main; print('tray import OK')" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "recorder.tray failed to import:"
        Write-Host $smoke
        exit 1
    }
    Write-Pass $smoke.Trim()
} finally {
    Pop-Location
}

# 4b) Smoke-check publisher + yaml imports --------------------------------
# The tray imports the watcher inside a try/except, so a missing PyYAML
# would silently disable auto-publish instead of failing install. Check it
# here explicitly so we catch this class of issue at install time.

Write-Step "Smoke-checking 'import yaml' and publisher imports"

Push-Location $root
try {
    $pubSmoke = & $python -c "import yaml; from publisher.publish import publish_note; from publisher.watcher import run_once; print('yaml + publisher import OK (PyYAML ' + yaml.__version__ + ')')" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "yaml / publisher import failed:"
        Write-Host $pubSmoke
        Write-Host ""
        Write-Host "Fix: ensure PyYAML is listed in requirements.txt and reinstall." -ForegroundColor Yellow
        exit 1
    }
    Write-Pass $pubSmoke.Trim()
} finally {
    Pop-Location
}

# 5) Register Task Scheduler task -----------------------------------------

Write-Step "Registering Task Scheduler task '$TaskName'"

$batPath = Join-Path $root "scripts\run_tray.bat"
if (-not (Test-Path $batPath)) {
    Write-Fail "Expected wrapper not found: $batPath"
    exit 1
}

# Prefer pythonw.exe so Task Scheduler doesn't flash a cmd / console window
# at logon. Falls back to cmd + bat wrapper if pythonw isn't reachable.
$pythonw = Resolve-Pythonw -pythonExe $python
if ($pythonw) {
    $taskExecute  = $pythonw
    # -X utf8 enables Python's UTF-8 mode (equivalent to PYTHONUTF8=1) so the
    # emoji-named "🎙 Audio" folder roundtrips cleanly.
    $taskArgument = "-X utf8 -m recorder.tray"
    Write-Host "  Task action: $pythonw -X utf8 -m recorder.tray   (no console window)"
} else {
    Write-Host "  pythonw.exe not found at expected path -- falling back to cmd + bat" -ForegroundColor Yellow
    $taskExecute  = "cmd.exe"
    $taskArgument = '/c "{0}"' -f $batPath
}

try {
    $action = New-ScheduledTaskAction `
        -Execute $taskExecute `
        -Argument $taskArgument `
        -WorkingDirectory $root

    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Hours 0)

    if ($env:USERDOMAIN) {
        $principalUser = '{0}\{1}' -f $env:USERDOMAIN, $env:USERNAME
    } else {
        $principalUser = $env:USERNAME
    }
    $principal = New-ScheduledTaskPrincipal `
        -UserId $principalUser `
        -LogonType Interactive `
        -RunLevel Limited

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "Launches the notes-pipeline voice-note tray app at user logon." `
        -Force `
        -ErrorAction Stop | Out-Null

    Write-Pass "Task '$TaskName' registered for $principalUser"
} catch {
    Write-Fail "Register-ScheduledTask failed: $_"
    Write-Host ""
    Write-Host "If you see 'Access is denied', either:"
    Write-Host "  1. Re-open PowerShell as the same user and try again, or"
    Write-Host "  2. Right-click PowerShell -> 'Run as administrator' and re-run install.ps1"
    exit 1
}

# 6) Verification ---------------------------------------------------------

Write-Step "Verifying installation"

$batExists  = Test-Path $batPath
$xmlExists  = Test-Path (Join-Path $root "scripts\notes-pipeline-tray.xml")
$logDirOk   = Test-Path (Join-Path $root ".logs")

# Pre-compute status strings (avoid inline $(if {}) -- fragile in Windows PowerShell 5.1)
$batStatus = "MISSING"
if ($batExists) { $batStatus = "present" }
$xmlStatus = "MISSING"
if ($xmlExists) { $xmlStatus = "present" }
$logStatus = "MISSING"
if ($logDirOk)  { $logStatus = "present" }

Write-Host "  scripts\run_tray.bat            : $batStatus"
Write-Host "  scripts\notes-pipeline-tray.xml : $xmlStatus"
Write-Host "  .logs directory                 : $logStatus"

try {
    $taskInfo = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop |
        Select-Object TaskName, State
    Write-Host ""
    Write-Host "Registered task:"
    $taskInfo | Format-Table -AutoSize | Out-Host
} catch {
    Write-Fail "Could not read back task '$TaskName': $_"
    exit 1
}

if (-not ($batExists -and $xmlExists -and $logDirOk)) {
    Write-Fail "One or more required files are missing -- see above."
    exit 1
}

# ---------------------------------------------------------------- summary

Write-Banner "Install complete"

Write-Host "Next steps:"
Write-Host "  Start now:    " -NoNewline
Write-Host "Start-ScheduledTask -TaskName '$TaskName'" -ForegroundColor Green
Write-Host "  Or just log out / log back in -- the tray will auto-start."
Write-Host ""
Write-Host "Hotkey:         Win+Alt+Space (toggle record / stop)"
Write-Host "Logs:           $root\.logs\tray.log"
Write-Host "Uninstall:      .\scripts\uninstall.ps1"
Write-Host ""

exit 0
