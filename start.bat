@echo off
setlocal
cd /d "%~dp0"
title OngakuDownloader
set "PYTHONPATH=%~dp0vendor\packages;%PYTHONPATH%"
python -c "import flask, yt_dlp, mutagen, curl_cffi, importlib.metadata as m; m.version('yt-dlp-ejs')" >nul 2>&1
if errorlevel 1 (
  echo Instalando dependencias y solucionador EJS...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo No se pudieron instalar las dependencias.
    pause
    exit /b 1
  )
)
python -c "import importlib.metadata as m; assert m.version('bgutil-ytdlp-pot-provider') == '2.0.2'" >nul 2>&1
if errorlevel 1 (
  echo Instalando el conector del proveedor de tokens...
  if not exist "vendor\packages" mkdir "vendor\packages"
  python -m pip install --upgrade --target "vendor\packages" bgutil-ytdlp-pot-provider==2.0.2
  if errorlevel 1 (
    echo No se pudo instalar el conector del proveedor de tokens.
    pause
    exit /b 1
  )
)
python setup_runtime.py
if errorlevel 1 (
  echo No se pudo preparar el proveedor de acceso de YouTube.
  pause
  exit /b 1
)
python app.py
pause
