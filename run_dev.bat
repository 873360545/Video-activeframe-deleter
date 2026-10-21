@echo off
rem 开发运行：确保虚拟环境与依赖后启动 GUI
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" py -3.14 -m venv .venv
".venv\Scripts\python.exe" -m pip install -r requirements.txt
".venv\Scripts\python.exe" -m app.main
