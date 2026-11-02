@echo off
setlocal
cd /d "%~dp0"
title Instalar OngakuDownloader
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0setup_windows.ps1" -InstallOnly
set "ONGAKU_EXIT=%ERRORLEVEL%"
echo.
if not "%ONGAKU_EXIT%"=="0" echo La instalacion no termino. Revisa el mensaje anterior.
pause
exit /b %ONGAKU_EXIT%
