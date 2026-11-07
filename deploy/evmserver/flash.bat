@echo off
rem Flash an NBE node image over USB on Windows: double-click, or  flash.bat GLS03 [COM5]
rem Runs flash.ps1 next to this file (Bypass: Windows blocks unsigned downloaded scripts by default).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0flash.ps1" %*
set rc=%errorlevel%
rem double-clicked (no arguments): keep the window open so the result can be read
if "%~1"=="" pause
exit /b %rc%
