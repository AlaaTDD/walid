@echo off
REM Double-click this file to stop the server (and its ngrok tunnel),
REM however many copies of it are running. Works no matter where this
REM folder is located on the PC (Desktop, D: drive, anywhere).

REM Move into this script's own folder first, so stop_server.py is found
REM regardless of where this .bat file itself was double-clicked from.
cd /d "%~dp0"

REM Find a working Python launcher without assuming a specific PATH
REM setup. The "py" launcher is the most reliable on Windows; fall
REM back to "python" if that's what's available instead.
where py >nul 2>nul
if %errorlevel% == 0 (
    py stop_server.py
    goto :end
)

where python >nul 2>nul
if %errorlevel% == 0 (
    python stop_server.py
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
