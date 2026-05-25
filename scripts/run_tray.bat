@echo off
REM ============================================================================
REM run_tray.bat -- launch the notes-pipeline tray app from Task Scheduler.
REM
REM Resolves the Python interpreter (preferring pyenv-win 3.11.5), changes to
REM the notes-pipeline root so package imports resolve, and runs the tray
REM module via pythonw.exe so no console window appears at login.
REM ============================================================================

REM Force UTF-8 so emoji-containing paths (e.g. "🎙 Audio") don't blow up
REM cp1252 encoders in subprocess output / logging.
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM cd into the notes-pipeline root (parent of scripts\). The /d switch
REM handles drive changes; %~dp0 is this batch file's directory with a
REM trailing slash, so "%~dp0..\" is the project root.
cd /d "%~dp0..\"

REM --- Resolve interpreter -----------------------------------------------------
REM 0) Honor an explicit override (set NOTES_PYTHONW to a pythonw.exe path).
if defined NOTES_PYTHONW (
    set "PYTHON_EXE=%NOTES_PYTHONW%"
    if exist "%PYTHON_EXE%" goto :run
)

REM 1) Prefer a pyenv-win pythonw.exe (no console flicker). %USERPROFILE% keeps
REM    this portable across machines; change the version if yours differs.
set "PYTHON_EXE=%USERPROFILE%\.pyenv\pyenv-win\versions\3.11.5\pythonw.exe"
if exist "%PYTHON_EXE%" goto :run

REM 2) Fall back to py launcher (windowed variant)
where pyw >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    set "PYTHON_EXE=pyw"
    set "PY_ARGS=-3.11"
    goto :run
)

REM 3) Fall back to system pythonw
where pythonw >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    set "PYTHON_EXE=pythonw"
    goto :run
)

REM 4) Last resort: regular python via `start /b` so no console sticks around
where python >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    start "" /b python -m recorder.tray
    exit /b 0
)

echo [run_tray] ERROR: no python interpreter found on PATH or at expected pyenv path. 1>&2
exit /b 1

:run
"%PYTHON_EXE%" %PY_ARGS% -m recorder.tray
exit /b %ERRORLEVEL%
