@echo off
setlocal
cd /d "%~dp0"
title OngakuDownloader
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0setup_windows.ps1"
set "ONGAKU_EXIT=%ERRORLEVEL%"
if not "%ONGAKU_EXIT%"=="0" (
  echo.
  echo No se pudo iniciar OngakuDownloader. Revisa el mensaje anterior.
  pause
)
exit /b %ONGAKU_EXIT%
