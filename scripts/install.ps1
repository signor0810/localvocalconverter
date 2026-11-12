# Local Typeless installer (idempotent: safe to run again).
#
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -SttProfile cpu_accurate -Shortcut
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -SkipModel
#
# Network use: the Python package index (dependencies) and, unless -SkipModel is given,
# the one-time download of the speech model weights. No audio or text is sent anywhere.
[CmdletBinding()]
param(
    [string]$SttProfile = "cpu_fast",   # which STT profile's model to download
    [switch]$SkipModel,                 # do not download the STT model (offline install)
    [switch]$Shortcut                   # also create the desktop shortcut
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Fail([string]$Message) {
    Write-Host "[FAILED] $Message" -ForegroundColor Red
    exit 1
}

function Run([string]$Exe, [string[]]$Arguments, [string]$What) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { Fail "$What (exit code $LASTEXITCODE)" }
}

Write-Host "Local Typeless install - $root" -ForegroundColor Cyan

# 1. Python >= 3.10 -----------------------------------------------------------
Write-Host "`n[1/5] Python" -ForegroundColor Yellow
$pyExe = $null
$pyArgs = @()
if (Get-Command python -ErrorAction SilentlyContinue) {
    $pyExe = "python"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $pyExe = "py"; $pyArgs = @("-3")
} else {
    Fail "Python was not found. Install Python 3.10 or newer and tick 'Add python.exe to PATH'."
}
$version = (& $pyExe @pyArgs -c "import sys; print('%d.%d' % sys.version_info[:2])").Trim()
if ($LASTEXITCODE -ne 0) { Fail "Could not run Python." }
$parts = $version.Split(".")
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 10)) {
    Fail "Python 3.10 or newer is required (found $version)."
}
Write-Host "Python $version"
& $pyExe @pyArgs -c "import tkinter" 2> $null
if ($LASTEXITCODE -ne 0) {
    Fail "This Python has no Tkinter (needed for the panel). Reinstall Python from python.org with 'tcl/tk and IDLE' ticked."
}

# 2. Virtual environment --------------------------------------------------------
Write-Host "`n[2/5] Virtual environment (.venv)" -ForegroundColor Yellow
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    Write-Host "Already exists."
} else {
    Run $pyExe ($pyArgs + @("-m", "venv", ".venv")) "Creating .venv"
    if (-not (Test-Path $venvPython)) { Fail ".venv was not created." }
    Write-Host "Created."
}

# 3. Install the project ----------------------------------------------------------
Write-Host "`n[3/5] Install packages" -ForegroundColor Yellow
& $venvPython -m pip --version *> $null
if ($LASTEXITCODE -eq 0) {
    Run $venvPython @("-m", "pip", "install", "-e", ".") "pip install"
} elseif (Get-Command uv -ErrorAction SilentlyContinue) {
    Run "uv" @("pip", "install", "--python", $venvPython, "-e", ".") "uv pip install"
} else {
    Fail "The virtual environment has no pip and 'uv' is not installed. Delete .venv and run this script again."
}

# 4. Config file ------------------------------------------------------------------
Write-Host "`n[4/5] Config file" -ForegroundColor Yellow
$config = Join-Path $root "config\config.yaml"
if (Test-Path $config) {
    Write-Host "config\config.yaml already exists (left unchanged)."
} else {
    Copy-Item -LiteralPath (Join-Path $root "config\config.example.yaml") -Destination $config
    Write-Host "Created config\config.yaml from the example."
}

# 5. STT model --------------------------------------------------------------------
Write-Host "`n[5/5] Speech model" -ForegroundColor Yellow
if ($SkipModel) {
    Write-Host "Skipped (-SkipModel). Set model_path in config\config.yaml to a local model folder,"
    Write-Host "or run later: .venv\Scripts\python.exe -m local_typeless.cli download-model --stt-profile $SttProfile"
} else {
    Run $venvPython @("-m", "local_typeless.cli", "download-model", "--stt-profile", $SttProfile) "Downloading the STT model for '$SttProfile'"
}

if ($Shortcut) {
    Run "powershell" @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $root "scripts\create_desktop_shortcut.ps1")) "Creating the desktop shortcut"
}

Write-Host "`nInstalled." -ForegroundColor Green
Write-Host "Next steps:"
Write-Host "  1. Edit config\config.yaml: set the LLM base_url/model of your profile and its API key variable."
Write-Host "  2. Check the setup:   .venv\Scripts\python.exe -m local_typeless.cli check"
Write-Host "  3. Start the panel:   double-click LocalTypeless.vbs  (or start_ui.bat to see a console)"
Write-Host "  Full guide: docs\安裝指南.md"
