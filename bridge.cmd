@echo off
rem Runs bridge.ps1 without needing to change PowerShell's execution policy.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bridge.ps1" %*
