@echo off
rem Double-click to install or update Claude Bridge.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
pause
