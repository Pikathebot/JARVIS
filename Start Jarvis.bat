@echo off
title Jarvis
cd /d "%~dp0"

rem Windows PowerShell 5.1 rather than pwsh: the Appx cmdlets this needs are
rem in-box there, and reach PowerShell 7 only through a compatibility shim.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-jarvis.ps1" %*

if errorlevel 1 (
    echo.
    echo Jarvis could not start. The details above are also in launcher-winui.log.
    echo.
    pause
)
