$scriptDir = Split-Path -Parent $PSScriptRoot
$vbsPath = Join-Path $scriptDir "LocalTypeless.vbs"
$desktopPath = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktopPath "Local Typeless.lnk"

$wshShell = New-Object -ComObject WScript.Shell
# WScript.Shell cannot save to non-ANSI paths (e.g. OneDrive\桌面): save to TEMP, then move.
$tmpLnk = Join-Path $env:TEMP "LocalTypeless_tmp.lnk"
$shortcut = $wshShell.CreateShortcut($tmpLnk)
$shortcut.TargetPath = "wscript.exe"
$shortcut.Arguments = "`"$vbsPath`""
$shortcut.WorkingDirectory = $scriptDir
$shortcut.Description = "Local Typeless - 語音口述轉結構化文字"
$shortcut.Save()
Move-Item -LiteralPath $tmpLnk -Destination $shortcutPath -Force

Write-Host "已於桌面建立捷徑: $shortcutPath"
