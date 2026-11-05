@echo off
rem 家庭智能终端 - 一键启动
rem   无参数：启动 Web 服务端（手机/平板访问 http://<电脑IP>:8000）
rem   cli：启动命令行交互模式
rem   wake：启动唤醒词常驻监听（说「小马」唤醒）
cd /d %~dp0
if /i "%1"=="cli" (
    .venv\Scripts\python.exe main.py
) else if /i "%1"=="wake" (
    .venv\Scripts\python.exe main.py wake
) else (
    .venv\Scripts\python.exe server.py
)
if errorlevel 1 pause
