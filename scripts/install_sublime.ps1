<#
.SYNOPSIS
    Installs the Local Typeless plugin into Sublime Text via junction link.

.DESCRIPTION
    Idempotent installer:
    - Auto-detects Sublime Text Packages directory (%APPDATA%\Sublime Text\Packages or %APPDATA%\Sublime Text 3\Packages).
    - Creates a directory junction: <PackagesDir>\LocalTypeless -> <repo>\sublime\LocalTypeless.
    - Ensures prompts_root directory exists (aligned with backend config / USERPROFILE).

.PARAMETER PackagesDir
    Optional custom path to the Sublime Text Packages directory.

.PARAMETER PromptsRoot
    Optional custom path to the prompts workspace root directory (defaults to configured prompts_root or ~/Documents/prompts).
#>
[CmdletBinding()]
param(
    [string]$PackagesDir = "",
    [string]$PromptsRoot = ""
)

$ErrorActionPreference = "Stop"

function Fail([string]$Message) {
    Write-Host "[FAILED] $Message" -ForegroundColor Red
    exit 1
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$sourceDir = Join-Path $repoRoot "sublime\LocalTypeless"
if (-not (Test-Path $sourceDir)) {
    Fail "Source plugin directory not found: $sourceDir"
}

Write-Host "Local Typeless Sublime Plugin Installer" -ForegroundColor Cyan
Write-Host "Repository root: $repoRoot"

# 1. Resolve PackagesDir
if (-not $PackagesDir) {
    $candidates = @(
        "$env:APPDATA\Sublime Text\Packages",
        "$env:APPDATA\Sublime Text 3\Packages"
    )
    foreach ($cand in $candidates) {
        if (Test-Path $cand) {
            $PackagesDir = $cand
            break
        }
    }
}

if (-not $PackagesDir -or -not (Test-Path $PackagesDir)) {
    Fail "Could not auto-detect Sublime Text Packages directory. Please specify -PackagesDir (e.g. -PackagesDir `"$env:APPDATA\Sublime Text 3\Packages`")."
}

Write-Host "Packages directory: $PackagesDir" -ForegroundColor Gray

# 2. Setup Junction for LocalTypeless
$targetJunction = Join-Path $PackagesDir "LocalTypeless"

if (Test-Path $targetJunction) {
    $item = Get-Item -LiteralPath $targetJunction -Force
    $isReparsePoint = [bool]($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)
    if ($isReparsePoint) {
        Write-Host "Existing junction detected at $targetJunction. Safely removing old junction..." -ForegroundColor Yellow
        cmd.exe /c "rmdir `"$targetJunction`"" | Out-Null
        if (Test-Path $targetJunction) {
            Fail "Could not remove existing junction: $targetJunction"
        }
    } else {
        Fail "Target $targetJunction exists and is a regular directory (not a junction). Refusing to overwrite."
    }
}

Write-Host "Creating junction: $targetJunction -> $sourceDir" -ForegroundColor Green
$cmdOutput = cmd.exe /c "mklink /J `"$targetJunction`" `"$sourceDir`""
if ($LASTEXITCODE -ne 0) {
    Fail "mklink /J failed: $cmdOutput"
}

# 3. Resolve and create prompts_root
if (-not $PromptsRoot) {
    $venvPy = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) {
        Push-Location $repoRoot
        try {
            $pyCmd = "from local_typeless.editor.config import load_editor_config; print(load_editor_config().prompts_root, end='')"
            $detected = & $venvPy -c $pyCmd 2>$null
            if ($LASTEXITCODE -eq 0 -and $detected) {
                $PromptsRoot = $detected.Trim()
            }
        } catch {
            $PromptsRoot = ""
        } finally {
            Pop-Location
        }
    }
    if (-not $PromptsRoot) {
        $PromptsRoot = Join-Path $env:USERPROFILE "Documents\prompts"
    }
} else {
    if ($PromptsRoot.StartsWith("~")) {
        $homeDir = $env:USERPROFILE
        $PromptsRoot = Join-Path $homeDir $PromptsRoot.Substring(1).TrimStart("\", "/")
    }
}

if (-not (Test-Path $PromptsRoot)) {
    New-Item -ItemType Directory -Force -Path $PromptsRoot | Out-Null
    Write-Host "Created prompts_root: $PromptsRoot" -ForegroundColor Green
} else {
    Write-Host "prompts_root already exists: $PromptsRoot" -ForegroundColor Gray
}

# 4. Installation summary
Write-Host "`nInstallation successful!" -ForegroundColor Green
Write-Host "--------------------------------------------------------"
Write-Host "  Plugin Junction : $targetJunction"
Write-Host "  Source Directory: $sourceDir"
Write-Host "  Prompts Root    : $PromptsRoot"
Write-Host "--------------------------------------------------------"
Write-Host "Next steps:"
Write-Host "  1. Launch or switch to Sublime Text."
Write-Host "  2. Start the local-typeless backend: local-typeless serve"
Write-Host "  3. In Sublime Text, press Ctrl+Alt+N to create a new prompt."
Write-Host "  4. Use Ctrl+V in managed files for smart image paste, and Ctrl+Alt+R to refine."