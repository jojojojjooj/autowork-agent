@echo off
setlocal
cd /d "%~dp0"

if not exist .venv (
    echo Run run_windows.bat once first.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat
python -m pip install --upgrade pyinstaller
if errorlevel 1 goto :error

python -m PyInstaller --noconfirm --clean --onefile --windowed --name AutoWorkAgent --add-data "README.md;." app\main.py
if errorlevel 1 goto :error

echo.
echo Build complete: dist\AutoWorkAgent.exe
pause
exit /b 0

:error
echo Build failed. Check the error above.
pause
exit /b 1
