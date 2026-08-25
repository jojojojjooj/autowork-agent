@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    set "PYTHON=py -3"
) else (
    set "PYTHON=python"
)

if not exist .venv (
    %PYTHON% -m venv .venv
    if errorlevel 1 goto :error
)

call .venv\Scripts\activate.bat
if errorlevel 1 goto :error

if "%~1"=="" (
    echo Installing pinned dependencies from requirements.lock.txt...
    python -m pip install --requirement requirements.lock.txt
) else (
    echo Installing approved offline wheels from: %~1
    python -m pip install --no-index --find-links "%~1" --requirement requirements.lock.txt
)
if errorlevel 1 goto :error

python -c "from pathlib import Path; Path('.venv/READY.txt').write_text('dependencies installed\n', encoding='utf-8')"
if errorlevel 1 goto :error

echo Installation complete. Run run_windows.bat to start AutoWork Agent.
exit /b 0

:error
echo.
echo Installation failed. Verify Python, the approved package source, and the wheel directory.
pause
exit /b 1
