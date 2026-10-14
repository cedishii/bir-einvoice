@echo off
rem One-time setup: installs what the app needs and puts a "Micronet e-Invoice"
rem shortcut on the desktop and in the Start menu. The shortcut opens the app
rem in its own window. run.bat still opens it in the browser.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Setting up for the first time...
    python -m venv .venv || (echo Python 3 is required: https://www.python.org/downloads/ & pause & exit /b 1)
    ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
)
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || (pause & exit /b 1)
if not exist ".env" copy ".env.example" ".env" >nul

".venv\Scripts\python.exe" tools\create_shortcut.py
echo.
echo Done. Open "Micronet e-Invoice" from your desktop or the Start menu.
pause
