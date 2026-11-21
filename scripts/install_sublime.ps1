<#
.SYNOPSIS
    Installs the Local Typeless plugin into Sublime Text via junction link.

.DESCRIPTION
    Idempotent installer:
    - Auto-detects Sublime Text Packages directory (%APPDATA%\Sublime Text\Packages or %APPDATA%\Sublime Text 3\Packages).
    - Creates a directory junction: <PackagesDir>\LocalTypeless -> <repo>\sublime\LocalTypeless.
    - Ensures prompts_root directory exists (aligned with backend config / USERPROFILE).
    - Optionally creates a startup shortcut for background serve.

.PARAMETER PackagesDir
    Optional custom path to the Sublime Text Packages directory.

.PARAMETER PromptsRoot
    Optional custom path to the prompts workspace root directory (defaults to configured prompts_root or ~/Documents/prompts).

.PARAMETER StartupShortcut
    If specified, creates a shortcut to LocalTypelessServe.vbs in the Windows Startup folder so the backend starts on login.
#>
[CmdletBinding()]
param(
    [string]$PackagesDir = "",
    [string]$PromptsRoot = "",
    [switch]$StartupShortcut
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

# 4. Optional: Create Windows Startup Shortcut
if ($StartupShortcut) {
    $vbsPath = Join-Path $repoRoot "LocalTypelessServe.vbs"
    if (Test-Path $vbsPath) {
        $startupDir = [Environment]::GetFolderPath("Startup")
        $shortcutPath = Join-Path $startupDir "LocalTypelessServe.lnk"
        $wshShell = New-Object -ComObject WScript.Shell
        $shortcut = $wshShell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = "wscript.exe"
        $shortcut.Arguments = "`"$vbsPath`""
        $shortcut.WorkingDirectory = $repoRoot
        $shortcut.Description = "Local Typeless Background Serve"
        $shortcut.Save()
        Write-Host "Created startup shortcut: $shortcutPath" -ForegroundColor Green
    } else {
        Write-Host "Warning: LocalTypelessServe.vbs not found, skipping startup shortcut." -ForegroundColor Yellow
    }
}

# 5. Installation summary
Write-Host "`nInstallation successful!" -ForegroundColor Green
Write-Host "--------------------------------------------------------"
Write-Host "  Plugin Junction : $targetJunction"
Write-Host "  Source Directory: $sourceDir"
Write-Host "  Prompts Root    : $PromptsRoot"
Write-Host "--------------------------------------------------------"
Write-Host "Features available in Sublime Text:"
Write-Host "  • Ctrl+Alt+N      : Create new prompt file"
Write-Host "  • Ctrl+V          : Smart paste (images saved to assets, files formatted)"
Write-Host "  • Ctrl+Alt+R      : AI Refine (Clean / Prompt / Summary / Slide / Transform)"
Write-Host "  • Ctrl+Alt+Enter  : Send prompt (copy refined result to clipboard)"
Write-Host "  • Ctrl+Alt+P      : Paste refined prompt directly to previous active window"
Write-Host "  • Ctrl+Alt+H      : Quick panel prompt history"
Write-Host "  • Ctrl+Alt+,      : Open Tk Settings / Dictionary GUI"
Write-Host "  • Hover Preview   : Hover over image paths to see inline image popups"
Write-Host "`nManual Control Scripts:"
Write-Host "  • Background Serve: double-click LocalTypelessServe.vbs"
Write-Host "  • Settings GUI    : double-click LocalTypelessSettings.vbs (or start_ui.bat)"
