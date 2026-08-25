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
    echo [1/2] Creating virtual environment...
    %PYTHON% -m venv .venv
    if errorlevel 1 goto :error
)

call .venv\Scripts\activate.bat
if errorlevel 1 goto :error

if not exist .venv\READY.txt (
    echo Dependencies are not installed in this environment.
    echo Run install_windows.bat first, or use the approved offline wheel directory.
    goto :error
)

echo [2/2] Starting AutoWork Agent...
python app\main.py
if errorlevel 1 goto :error
exit /b 0

:error
echo.
echo AutoWork Agent could not start. Install dependencies with install_windows.bat and check the error above.
pause
exit /b 1
