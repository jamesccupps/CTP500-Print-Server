# Puts a "CTP500 Printer" shortcut on the desktop that opens the web app in its own window (Edge app mode).
#   powershell -ExecutionPolicy Bypass -File tools\desktop-shortcut.ps1 -Url http://raspberrypi.local:8500
param([Parameter(Mandatory = $true)][string]$Url)

$edge = "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
if (-not (Test-Path $edge)) { $edge = "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe" }
if (-not (Test-Path $edge)) { throw "Microsoft Edge wasn't found" }

$icon = Join-Path $PSScriptRoot "ctp500.ico"
$lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "CTP500 Printer.lnk"
$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($lnk)
$s.TargetPath = $edge
$s.Arguments = "--app=$Url --window-size=1180,860"
$s.Description = "Print notes, pictures and QR codes on the CTP500"
if (Test-Path $icon) { $s.IconLocation = "$icon,0" }
$s.Save()
Write-Output "Created $lnk"
