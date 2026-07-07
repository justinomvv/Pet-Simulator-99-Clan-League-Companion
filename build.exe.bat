@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python not found on PATH. Install it from https://python.org and re-run this file.
    pause
    exit /b 1
)

if not exist "icon.ico" (
    echo Couldn't find icon.ico next to this script. Put it in the same folder and re-run.
    pause
    exit /b 1
)

echo Installing/updating dependencies...
pip install -r requirements.txt --no-warn-script-location
pip install --upgrade pyinstaller --no-warn-script-location

echo.
echo Cleaning up any previous build...
if exist "build" rd /s /q "build"
if exist "dist" rd /s /q "dist"
if exist "PS99Overlay.spec" del /q "PS99Overlay.spec"

echo.
echo Building PS99Overlay.exe (standalone, single file) ...
REM --noupx: skips UPX compression. UPX-packed exes are one of the biggest
REM triggers for antivirus heuristics flagging (and sometimes quarantining/
REM corrupting) freshly-built PyInstaller onefile exes. Skipping it makes
REM the exe a bit bigger but far less likely to get mangled.
python -m PyInstaller --noconfirm --onefile --windowed --noupx ^
    --name "PS99Overlay" ^
    --icon "icon.ico" ^
    ps99_overlay.py

echo.
if exist "dist\PS99Overlay.exe" (
    echo Done! Your exe is at dist\PS99Overlay.exe — that single file is all you need to share.
    echo.
    echo If Windows Defender still flags or corrupts it, add this project folder
    echo to Defender's exclusions ^(Windows Security -^> Virus ^& threat protection
    echo -^> Manage settings -^> Add or remove exclusions^) BEFORE building, then
    echo re-run this script.
) else (
    echo Something went wrong — scroll up for the PyInstaller error.
)
pause
