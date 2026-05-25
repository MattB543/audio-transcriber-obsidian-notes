#Requires -Version 5.1
<#
.SYNOPSIS
    Uninstalls the notes-pipeline tray app's auto-start hook.

.DESCRIPTION
    Removes the "notes-pipeline-tray" Task Scheduler task and best-effort kills
    any currently-running tray process. Does NOT remove pip-installed packages,
    the Obsidian vault, transcripts, audio files, or the .env file.

.NOTES
        cd path\to\audio-transcriber-obsidian-notes
        .\scripts\uninstall.ps1
#>

[CmdletBinding()]
param(
    [string]$TaskName = "notes-pipeline-tray"
)

$ErrorActionPreference = "Continue"  # keep going even if pieces are already gone

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

function Write-Note([string]$text) {
    Write-Host "[ NOTE ] $text" -ForegroundColor DarkYellow
}

Write-Banner "Uninstalling notes-pipeline tray auto-start..."

# 1) Stop the running task (if any) ---------------------------------------

Write-Step "Stopping scheduled task '$TaskName' (if running)"

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    try {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Write-Pass "Stop signal sent."
    } catch {
        Write-Note "Stop-ScheduledTask did not succeed (may not have been running)."
    }
} else {
    Write-Note "Task '$TaskName' is not registered -- nothing to stop."
}

# 2) Unregister the task --------------------------------------------------

Write-Step "Unregistering scheduled task '$TaskName'"

if ($existing) {
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        Write-Pass "Task '$TaskName' removed."
    } catch {
        Write-Host "[ FAIL ] Could not unregister task: $_" -ForegroundColor Red
        Write-Host "Try running PowerShell as the same user that ran install.ps1." -ForegroundColor Yellow
    }
} else {
    Write-Note "No task to unregister."
}

# 3) Best-effort: kill any running tray python process --------------------

Write-Step "Killing any running tray python process"

$killed = 0
try {
    # Match python(w).exe whose command line includes 'recorder.tray'.
    # Get-CimInstance is safer than (Get-Process).CommandLine which is empty
    # for processes started by another session.
    $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -match '^pythonw?\.exe$' -and
            $_.CommandLine -and
            $_.CommandLine -match 'recorder\.tray'
        }
    foreach ($p in $procs) {
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
            Write-Host "  killed PID $($p.ProcessId): $($p.CommandLine)"
            $killed++
        } catch {
            Write-Note "  could not kill PID $($p.ProcessId): $_"
        }
    }
} catch {
    Write-Note "Process enumeration failed: $_"
}
if ($killed -eq 0) {
    Write-Note "No running tray process found."
} else {
    Write-Pass "Killed $killed tray process(es)."
}

# ---------------------------------------------------------------- summary

Write-Banner "Uninstall complete"

Write-Host "Removed:"
Write-Host "  - Task Scheduler task '$TaskName' (auto-start at logon)"
Write-Host "  - Any running 'python -m recorder.tray' processes"
Write-Host ""
Write-Host "Preserved (intentionally not touched):"
Write-Host "  - pip-installed packages (may be used by other projects)"
Write-Host "  - Obsidian vault and any transcripts/audio"
Write-Host "  - .env file and GEMINI_API_KEY"
Write-Host "  - notes-pipeline source code"
Write-Host ""
Write-Host "To reinstall: .\scripts\install.ps1"
Write-Host ""

exit 0
