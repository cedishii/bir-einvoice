@echo off
rem Starts the Micronet e-Invoice app and opens it in the browser.
rem First run: creates .venv, installs requirements and copies .env.example to .env.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Setting up for the first time...
    python -m venv .venv || (echo Python 3 is required: https://www.python.org/downloads/ & pause & exit /b 1)
    ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
)
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || (pause & exit /b 1)

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo Created .env from .env.example. Edit it when BIR issues the EIS Cert No. and PTU.
)

".venv\Scripts\python.exe" -m einvoice.web
pause
