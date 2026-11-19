@echo off
rem HomeSmartTerminal launcher
rem Usage: start.bat [cli|wake]
rem   no args: start web server (http://<PC-IP>:8000)
rem   cli: start command-line voice mode
rem   wake: start wake-word listener (say "小马")
cd /d %~dp0
if /i "%~1"=="cli" (
    .venv\Scripts\python.exe main.py
) else if /i "%~1"=="wake" (
    .venv\Scripts\python.exe main.py wake
) else (
    .venv\Scripts\python.exe server.py
)
if errorlevel 1 pause
