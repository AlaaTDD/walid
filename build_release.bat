@echo off
REM ============================================================================
REM build_release.bat — Build a complete release for Windows
REM ============================================================================
REM
REM Usage:
REM     build_release.bat
REM
REM Prerequisites:
REM     - Python 3.10+ with venv in backend\.venv
REM     - Flutter SDK in PATH
REM     - (Optional) Inno Setup for installer creation
REM
REM Output:
REM     release\                    — Final release artifacts
REM ============================================================================

setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
set "BACKEND_DIR=%SCRIPT_DIR%backend"
set "FRONTEND_DIR=%SCRIPT_DIR%frontend"
set "RELEASE_DIR=%SCRIPT_DIR%release"

echo.
echo ══════════════════════════════════════════════════════════════
echo   Sheet Nesting App — Windows Release Build
echo ══════════════════════════════════════════════════════════════
echo.

REM ── Step 1: Build Backend with PyInstaller ────────────────────────────────

echo [BUILD] Step 1/4: Building backend with PyInstaller...

if not exist "%BACKEND_DIR%" (
    echo [ERROR] Backend directory not found: %BACKEND_DIR%
    exit /b 1
)

REM Find Python — prefer venv, fall back to system
set "PYTHON="
if exist "%BACKEND_DIR%\.venv\Scripts\python.exe" (
    set "PYTHON=%BACKEND_DIR%\.venv\Scripts\python.exe"
) else (
    where python >nul 2>&1
    if !errorlevel! equ 0 (
        set "PYTHON=python"
    ) else (
        echo [ERROR] Python not found. Please install Python 3.10+ or create a venv.
        exit /b 1
    )
)

echo [BUILD] Using Python: %PYTHON%
echo [BUILD] Installing backend dependencies...
"%PYTHON%" -m pip install "%BACKEND_DIR%"
if !errorlevel! neq 0 (
    echo [WARN] Failed to install dependencies, PyInstaller might fail or produce a broken executable.
)

"%PYTHON%" "%BACKEND_DIR%\build_backend.py"
if !errorlevel! neq 0 (
    echo [ERROR] Backend build failed!
    exit /b 1
)

set "BACKEND_DIST=%BACKEND_DIR%\dist\nesting_server"
if not exist "%BACKEND_DIST%" (
    echo [ERROR] PyInstaller output not found: %BACKEND_DIST%
    exit /b 1
)
echo [  OK ] Backend built successfully: %BACKEND_DIST%

REM ── Step 2: Build Flutter Desktop ─────────────────────────────────────────

echo [BUILD] Step 2/4: Building Flutter desktop app...

if not exist "%FRONTEND_DIR%" (
    echo [ERROR] Frontend directory not found: %FRONTEND_DIR%
    exit /b 1
)

pushd "%FRONTEND_DIR%"
call flutter pub get
if !errorlevel! neq 0 (
    echo [ERROR] flutter pub get failed!
    popd
    exit /b 1
)
call flutter build windows --release
if !errorlevel! neq 0 (
    echo [ERROR] Flutter Windows build failed!
    popd
    exit /b 1
)
popd
echo [  OK ] Flutter Windows build completed.

REM ── Step 3: Bundle Backend into Flutter App ───────────────────────────────

echo [BUILD] Step 3/4: Bundling backend into Flutter app...

set "BUNDLE_DIR=%FRONTEND_DIR%\build\windows\x64\runner\Release"
if not exist "%BUNDLE_DIR%" (
    REM Try older Flutter path structure
    set "BUNDLE_DIR=%FRONTEND_DIR%\build\windows\runner\Release"
)
if not exist "%BUNDLE_DIR%" (
    echo [ERROR] Flutter Windows bundle not found!
    exit /b 1
)

set "DEST_DIR=%BUNDLE_DIR%\nesting_server"
if exist "%DEST_DIR%" rmdir /s /q "%DEST_DIR%"
mkdir "%DEST_DIR%"
xcopy "%BACKEND_DIST%\*" "%DEST_DIR%\" /e /q /y >nul
echo [  OK ] Backend bundled into: %DEST_DIR%

REM ── Step 4: Create Distributable Package ──────────────────────────────────

echo [BUILD] Step 4/4: Creating distributable package...

if not exist "%RELEASE_DIR%" mkdir "%RELEASE_DIR%"

REM Check if Inno Setup is available
set "ISCC="
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" (
    set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
) else if exist "C:\Program Files\Inno Setup 6\ISCC.exe" (
    set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"
)

if defined ISCC (
    echo [BUILD] Inno Setup found. Creating installer...

    REM Generate a temporary Inno Setup script
    set "ISS_FILE=%RELEASE_DIR%\setup.iss"
    (
        echo [Setup]
        echo AppName=walid
        echo AppVersion=1.0
        echo DefaultDirName={autopf}\walid
        echo DefaultGroupName=walid
        echo OutputDir=%RELEASE_DIR%
        echo OutputBaseFilename=walid-Setup
        echo Compression=lzma2
        echo SolidCompression=yes
        echo ChangesAssociations=yes
        echo.
        echo [Files]
        echo Source: "%BUNDLE_DIR%\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
        echo.
        echo [Icons]
        echo Name: "{group}\walid"; Filename: "{app}\walid.exe"
        echo Name: "{commondesktop}\walid"; Filename: "{app}\walid.exe"
        echo.
        echo [Run]
        echo Filename: "{app}\walid.exe"; Flags: nowait postinstall skipifsilent; Description: "Launch walid"
    ) > "!ISS_FILE!"

    "%ISCC%" "!ISS_FILE!"
    if !errorlevel! equ 0 (
        echo [  OK ] Installer created: %RELEASE_DIR%\SheetNestingApp-Setup.exe
    ) else (
        echo [WARN ] Inno Setup failed. Falling back to ZIP...
        goto :zip_fallback
    )
) else (
    :zip_fallback
    echo [BUILD] Inno Setup not found. Creating ZIP archive...
    set "ZIP_NAME=SheetNestingApp-windows-%date:~0,4%%date:~5,2%%date:~8,2%.zip"

    pushd "%BUNDLE_DIR%\.."
    powershell -Command "Compress-Archive -Path 'Release\*' -DestinationPath '%RELEASE_DIR%\!ZIP_NAME!' -Force"
    popd

    if exist "%RELEASE_DIR%\!ZIP_NAME!" (
        echo [  OK ] ZIP archive created: %RELEASE_DIR%\!ZIP_NAME!
    ) else (
        echo [WARN ] ZIP creation may have failed. Check %RELEASE_DIR%
    )
)

echo.
echo ══════════════════════════════════════════════════════════════
echo   Build completed successfully!
echo   Platform:  Windows
echo   Output:    %RELEASE_DIR%
echo ══════════════════════════════════════════════════════════════
echo.

endlocal
