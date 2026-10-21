@echo off
rem 一键构建便携包（实际流程见 build.ps1）
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*
