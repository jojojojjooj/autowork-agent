@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    set PYTHON=py -3
) else (
    set PYTHON=python
)

if not exist .venv (
    echo [1/3] Creating virtual environment...
    %PYTHON% -m venv .venv
    if errorlevel 1 goto :error
)

call .venv\Scripts\activate.bat
if errorlevel 1 goto :error

echo [2/3] Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo [3/3] Starting AutoWork Agent...
python app\main.py
if errorlevel 1 goto :error
exit /b 0

:error
echo.
echo AutoWork Agent could not start. Check Python installation and the error above.
pause
exit /b 1
