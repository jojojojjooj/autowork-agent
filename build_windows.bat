@echo off
setlocal
cd /d "%~dp0"

if not exist .venv\READY.txt (
    echo Run install_windows.bat first, including the approved offline wheel directory if required.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat
if errorlevel 1 goto :error

python -m PyInstaller --noconfirm --clean --onefile --windowed --name AutoWorkAgent app\main.py
if errorlevel 1 goto :error

echo.
echo Build complete: dist\AutoWorkAgent.exe
pause
exit /b 0

:error
echo Build failed. Check the error above.
pause
exit /b 1
