@echo off
rem One-command launcher. All logic lives in start.py (secret from data\.secret_key,
rem account DB check, then handing over to `agentworkbench serve`).
rem
rem This file is ASCII-only on purpose: cmd parses .bat bytes in the active code page,
rem so UTF-8 Chinese comments here split command lines in half ("'xxx' is not recognized").
rem
rem Usage:  start.bat
rem         start.bat --host 0.0.0.0 --port 8080
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [x] .venv not found. Run these first:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -e .
    exit /b 2
)

.venv\Scripts\python.exe "scripts\start.py" %*
