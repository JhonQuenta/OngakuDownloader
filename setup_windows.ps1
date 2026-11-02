param(
    [switch]$InstallOnly,
    [string]$PythonExecutable
)

$ErrorActionPreference = 'Stop'
# A parent PowerShell 7 process can pass incompatible module paths through cmd.exe.
# Use the built-in modules of this PowerShell process; no machine setting is changed.
$env:PSModulePath = "$PSHOME\Modules"
$AppDir = $PSScriptRoot
$ToolsDir = Join-Path $AppDir '.tools'
$VenvPython = Join-Path $AppDir '.venv\Scripts\python.exe'

function Refresh-ToolPath {
    $paths = @($env:PATH, [Environment]::GetEnvironmentVariable('Path', 'Machine'), [Environment]::GetEnvironmentVariable('Path', 'User'))
    $paths += @("$env:ProgramFiles\nodejs", "$env:ProgramFiles\Git\cmd", "$env:LOCALAPPDATA\Programs\Git\cmd")
    $env:PATH = ($paths | Where-Object { $_ }) -join ';'
}

function Get-UsablePython {
    param([string]$Preferred)
    $candidates = @()
    if ($Preferred) { $candidates += $Preferred }
    if (Test-Path -LiteralPath $VenvPython) { $candidates += $VenvPython }
    foreach ($name in @('py.exe', 'python.exe', 'python3.exe')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) { $candidates += $command.Source }
    }
    foreach ($registry in @('HKCU:\Software\Python\PythonCore', 'HKLM:\Software\Python\PythonCore', 'HKLM:\Software\WOW6432Node\Python\PythonCore')) {
        foreach ($version in @(Get-ChildItem $registry -ErrorAction SilentlyContinue)) {
            $install = Get-Item (Join-Path $version.PSPath 'InstallPath') -ErrorAction SilentlyContinue
            if ($install -and $install.GetValue('')) { $candidates += Join-Path ($install.GetValue('')) 'python.exe' }
        }
    }
    foreach ($candidate in @($candidates | Select-Object -Unique)) {
        try {
            $found = & $candidate -c 'import sys; assert sys.version_info >= (3,12); import venv, ensurepip, tkinter; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $found -and (Test-Path -LiteralPath ($found | Select-Object -Last 1))) {
                return ($found | Select-Object -Last 1)
            }
        } catch { }
    }
    return $null
}

function Install-WingetPackage {
    param([string]$Id, [string]$ManualUrl)
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) { throw "Falta $Id. Instala desde $ManualUrl y vuelve a ejecutar instalar.bat. WinGet no esta disponible." }
    Write-Host "Instalando $Id con WinGet. Windows puede pedir autorizacion."
    & $winget.Source install --id $Id --exact --source winget
    if ($LASTEXITCODE -ne 0) { throw "No se pudo instalar $Id. Puedes instalarlo desde $ManualUrl y repetir instalar.bat." }
    Refresh-ToolPath
}

function Test-Node {
    try {
        $command = Get-Command node.exe -ErrorAction SilentlyContinue
        if (-not $command) { return $false }
        & $command.Source -e "process.exit(Number(process.versions.node.split('.')[0]) >= 22 ? 0 : 1)"
        return ($LASTEXITCODE -eq 0 -and [bool](Get-Command npm.cmd -ErrorAction SilentlyContinue))
    } catch { return $false }
}

function Get-FFmpegBin {
    $ffmpeg = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
    $ffprobe = Get-Command ffprobe.exe -ErrorAction SilentlyContinue
    if ($ffmpeg -and $ffprobe) { return Split-Path $ffmpeg.Source }
    $local = Join-Path $ToolsDir 'ffmpeg\ffmpeg-9.0.2-essentials_build\bin'
    if ((Test-Path (Join-Path $local 'ffmpeg.exe')) -and (Test-Path (Join-Path $local 'ffprobe.exe'))) { return $local }
    return $null
}

function Install-FFmpeg {
    $bin = Get-FFmpegBin
    if ($bin) { return $bin }
    # Fixed vendor archive and published SHA256. No global PATH changes.
    $url = 'https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip'
    $expected = '60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba'
    $cache = Join-Path $ToolsDir 'cache'
    New-Item -ItemType Directory -Force -Path $cache | Out-Null
    $archive = Join-Path $cache 'ffmpeg-9.0.2.zip'
    if (-not (Test-Path -LiteralPath $archive)) {
        Write-Host 'Descargando FFmpeg y FFprobe portables (aproximadamente 110 MB)...'
        $part = "$archive.part"
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $part
        if ((Get-FileHash -LiteralPath $part -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
            throw "FFmpeg no coincide con la suma SHA256 esperada. No se ejecutara. Archivo: $part"
        }
        Move-Item -LiteralPath $part -Destination $archive -Force
    }
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
        throw "La copia de FFmpeg en cache no supera SHA256. No se ejecutara. Revisa o elimina $archive y repite."
    }
    Expand-Archive -LiteralPath $archive -DestinationPath (Join-Path $ToolsDir 'ffmpeg') -Force
    $bin = Get-FFmpegBin
    if (-not $bin) { throw 'No se encontraron FFmpeg y FFprobe despues de extraer el archivo.' }
    return $bin
}

function Invoke-Setup {
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'Se requiere Windows de 64 bits.' }
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    Refresh-ToolPath
    $python = Get-UsablePython -Preferred $PythonExecutable
    if (-not $python) {
        Install-WingetPackage 'Python.Python.3.12' 'https://www.python.org/downloads/windows/'
        $python = Get-UsablePython
        if (-not $python) { throw 'No se encontro Python 3.12+ con venv y tkinter. Cierra esta ventana y vuelve a ejecutar instalar.bat.' }
    }
    if (-not (Test-Node)) {
        Install-WingetPackage 'OpenJS.NodeJS.LTS' 'https://nodejs.org/en/download'
        if (-not (Test-Node)) { throw 'No se encontro Node.js 22+ con npm. Reabre instalar.bat despues de instalar Node.js LTS.' }
    }
    if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) {
        Install-WingetPackage 'Git.Git' 'https://git-scm.com/download/win'
        if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) { throw 'No se encontro Git. Reabre instalar.bat despues de instalarlo.' }
    }
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        Write-Host 'Creando el entorno Python privado del proyecto (.venv)...'
        & $python -m venv (Join-Path $AppDir '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'No se pudo crear .venv.' }
    }
    $env:PATH = "$(Split-Path $VenvPython);$env:PATH"
    # Do not inherit unrelated Python packages from the caller.
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $requirements = Join-Path $AppDir 'requirements.txt'
    $fingerprint = (Get-FileHash -LiteralPath $requirements -Algorithm SHA256).Hash
    $marker = Join-Path $AppDir '.venv\.ongaku-requirements-sha256'
    $installed = (Test-Path $marker) -and ((Get-Content $marker -Raw).Trim() -eq $fingerprint)
    if ($installed) {
        try {
            & $VenvPython -c "import flask, yt_dlp, mutagen, curl_cffi, yt_dlp_plugins.extractor.getpot_bgutil; import importlib.metadata as m; m.version('yt-dlp-ejs')" 2>$null
            $installed = $LASTEXITCODE -eq 0
        } catch { $installed = $false }
    }
    if (-not $installed) {
        Write-Host 'Instalando dependencias Python y solucionador EJS...'
        & $VenvPython -m pip install --upgrade pip
        if ($LASTEXITCODE -ne 0) { throw 'No se pudo actualizar pip.' }
        & $VenvPython -m pip install -r $requirements
        if ($LASTEXITCODE -ne 0) { throw 'No se pudieron instalar las dependencias Python.' }
        Set-Content -LiteralPath $marker -Value $fingerprint -Encoding Ascii
    }
    & $VenvPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Las dependencias Python tienen conflictos.' }
    $ffmpegBin = Install-FFmpeg
    $env:PATH = "$ffmpegBin;$env:PATH"
    & $VenvPython -c "import shutil, tkinter; assert shutil.which('ffmpeg') and shutil.which('ffprobe')"
    if ($LASTEXITCODE -ne 0) { throw 'FFmpeg, FFprobe o tkinter no estan disponibles.' }
    & $VenvPython (Join-Path $AppDir 'setup_runtime.py')
    if ($LASTEXITCODE -ne 0) { throw 'No se pudo preparar el proveedor de tokens.' }
    Write-Host 'Instalacion completa. Para abrir la aplicacion usa start.bat.' -ForegroundColor Green
    if ($InstallOnly) { return }
    try {
        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8766/api/health' -TimeoutSec 2
        if ($health.ok -and $health.yt_dlp -and $health.default_download_path -eq (Join-Path $AppDir 'downloads')) {
            Start-Process 'http://127.0.0.1:8766'
            return
        }
    } catch { }
    & $VenvPython (Join-Path $AppDir 'app.py')
    if ($LASTEXITCODE -ne 0) { throw "La aplicacion termino con codigo $LASTEXITCODE." }
}

try {
    Push-Location $AppDir
    try { Invoke-Setup } finally { Pop-Location }
    exit 0
} catch {
    Write-Host ""
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
