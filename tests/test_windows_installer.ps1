$ErrorActionPreference = 'Stop'
$installer = Join-Path (Split-Path $PSScriptRoot) 'setup_windows.ps1'
$tokens = $null
$parseErrors = $null
$syntax = [System.Management.Automation.Language.Parser]::ParseFile($installer, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
foreach ($definition in $syntax.FindAll({ param($ast) $ast -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}

# A corrupted cached archive must be rejected before extraction or execution.
$ToolsDir = Join-Path (Split-Path $PSScriptRoot) ('.tools\installer-test-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path (Join-Path $ToolsDir 'cache') | Out-Null
Set-Content -LiteralPath (Join-Path $ToolsDir 'cache\ffmpeg-9.0.2.zip') -Value 'not a trusted archive' -Encoding Ascii
function Get-FFmpegBin { return $null }
$rejected = $false
try { Install-FFmpeg | Out-Null } catch {
    if ($_.Exception.Message -notmatch 'SHA256') { throw }
    $rejected = $true
}
if (-not $rejected) { throw 'El instalador acepto un archivo con una suma SHA256 incorrecta.' }
if (Test-Path (Join-Path $ToolsDir 'ffmpeg')) { throw 'Se extrajo un archivo antes de verificar su integridad.' }
Write-Host 'PASS: sintaxis de Windows PowerShell y rechazo de archivo FFmpeg alterado.'
