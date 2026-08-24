@echo off
rem One-click launcher for the walkie GUI.
rem First run: creates pc_client\.venv and installs the dependencies.
rem After that it starts the GUI with no console window.
setlocal
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
    echo Creating virtual environment...
    py -3 -m venv .venv 2>nul || python -m venv .venv || goto :err
)

.venv\Scripts\python -c "import PySide6, sounddevice, zeroconf, serial, pyogg" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies ^(first run only^)...
    .venv\Scripts\python -m pip install -r requirements.txt || goto :err
)

start "" .venv\Scripts\pythonw.exe walkie_gui.py
exit /b 0

:err
echo.
echo Setup failed - see the messages above.
pause
exit /b 1
