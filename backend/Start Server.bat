@echo off
REM Double-click this file to start the server. Works no matter where
REM this folder is located on the PC (Desktop, D: drive, anywhere).

REM Move into this script's own folder first, so run_server.py finds
REM the app code and its saved token regardless of where this .bat
REM file itself was double-clicked from.
cd /d "%~dp0"

REM Find a working Python launcher without assuming a specific PATH
REM setup. The "py" launcher is the most reliable on Windows; fall
REM back to "python" if that's what's available instead.
where py >nul 2>nul
if %errorlevel% == 0 (
    py run_server.py
    goto :end
)

where python >nul 2>nul
if %errorlevel% == 0 (
    python run_server.py
    goto :end
)

echo ============================================================
echo   Python was not found on this PC.
echo   Install it from https://www.python.org/downloads/
echo   IMPORTANT: during install, check the box that says
echo   "Add python.exe to PATH" -- then double-click this file again.
echo ============================================================
pause
exit /b 1

:end
pause
