# Sets up the CTP500 PC helper (worker.py) on Windows and starts it at logon. Safe to run again (to update too).
#
#   powershell -ExecutionPolicy Bypass -File desktop\install.ps1 -Server http://raspberrypi.local:8500
#
# It writes desktop\helper.json (the print server's address; add "token" there if the server has one), makes the
# helper's own Python in -HelperHome (a few GB with an NVIDIA card: put it on a roomy drive), installs what it needs,
# downloads the AI models and adds the scheduled task "CTP500 helper" (at logon, no window, below-normal priority,
# restarted if it stops). Needs Python 3.10-3.12 from python.org (the "py" launcher) or uv.
#   -Cpu      no NVIDIA graphics card (or skip the 3 GB of CUDA): drawings run on the processor, still faster than a Pi
#   -NoTask   set up only; start it yourself with: <home>\.venv\Scripts\python.exe desktop\worker.py
# Remove it: Unregister-ScheduledTask "CTP500 helper"; then delete the home folder.

param(
    [string]$Server,
    [string]$HelperHome,
    [switch]$Cpu,
    [switch]$NoTask
)
$here = $PSScriptRoot
$repo = Split-Path $here -Parent
$settings = Join-Path $here "helper.json"

# ---- settings
if (Test-Path $settings) {
    $cfg = Get-Content $settings -Raw -ErrorAction Stop | ConvertFrom-Json
    if ($Server -and $cfg.server -ne $Server.TrimEnd("/")) { $cfg.server = $Server.TrimEnd("/"); $changed = $true }
} else {
    if (-not $Server) { throw "Give the print server's address: -Server http://<pi>:8500" }
    $cfg = [pscustomobject]@{ server = $Server.TrimEnd("/"); token = "" }
    $changed = $true
}
if ($HelperHome -and $cfg.home -ne $HelperHome) { $cfg | Add-Member home $HelperHome -Force; $changed = $true }
if ($changed) {
    # no byte-order mark: Windows PowerShell's utf8 adds one
    [IO.File]::WriteAllText($settings, ($cfg | ConvertTo-Json), (New-Object Text.UTF8Encoding $false))
    Write-Output "Saved $settings"
}
$home_ = if ($env:CTP500_HELPER_HOME) { $env:CTP500_HELPER_HOME } elseif ($cfg.home) { $cfg.home } `
         else { Join-Path $env:LOCALAPPDATA "ctp500-helper" }
New-Item -ItemType Directory -Force $home_ -ErrorAction Stop | Out-Null
try {
    Invoke-RestMethod "$($cfg.server)/api/status" -TimeoutSec 5 | Out-Null
    Write-Output "Print server answers at $($cfg.server)"
} catch {
    Write-Warning "Can't reach $($cfg.server) right now; the helper keeps trying once it runs."
}

# ---- Python
$venv = Join-Path $home_ ".venv"
$python = Join-Path $venv "Scripts\python.exe"
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not (Test-Path $python)) {
    Write-Output "Making the helper's Python in $venv"
    if ($uv) {
        & uv venv $venv --python 3.12
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        $ver = (& py -3.12 -c "print(1)" 2>$null); $pick = if ($ver) { "-3.12" } else { "-3" }
        & py $pick -m venv $venv
    } else {
        throw "Install Python 3.12 from python.org (with the py launcher) or uv first."
    }
    if (-not (Test-Path $python)) { throw "Couldn't make the Python environment in $venv" }
}
function Install-Packages([string[]]$packages, [string[]]$extra = @()) {
    if ($uv) { & uv pip install --python $python @packages @extra }
    else { & $python -m pip install --disable-pip-version-check -q @packages @extra }
    if ($LASTEXITCODE) { throw "Installing $($packages -join ' ') failed" }
}
$nvidia = [bool](Get-CimInstance Win32_VideoController | Where-Object { $_.Name -match "NVIDIA" })
if ($Cpu -or -not $nvidia) {
    if (-not $Cpu) { Write-Output "No NVIDIA graphics card found: setting up for the processor" }
    Install-Packages @("onnxruntime", "numpy", "pillow")
} else {
    Write-Output "Installing PyTorch's CUDA libraries + onnxruntime-gpu (about 3 GB, once)"
    Install-Packages @("torch") @("--index-url", "https://download.pytorch.org/whl/cu128")
    # onnxruntime-gpu 1.24+ wants CUDA 13 and quietly falls back to the processor with these libraries
    Install-Packages @("onnxruntime-gpu==1.23.2", "numpy", "pillow")
}

# ---- models (checked against pinned SHA-256s)
Push-Location $repo
try { & $python -m ctp500 models } finally { Pop-Location }
if ($LASTEXITCODE) { Write-Warning "The AI models didn't all download; drawings fall back to the Pi until they do." }

# ---- start at logon
if ($NoTask) {
    Write-Output "Set up. Start it with: `"$python`" `"$(Join-Path $here 'worker.py')`""
    return
}
$pythonw = Join-Path $venv "Scripts\pythonw.exe"
$worker = Join-Path $here "worker.py"
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$worker`"" -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$taskSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -Priority 7
Register-ScheduledTask -TaskName "CTP500 helper" -Action $action -Trigger $trigger -Settings $taskSettings -Force `
    -Description "Heavy jobs for the CTP500 print server (AI drawings, face sharpening, Bambu Studio slicing). $worker" | Out-Null
Stop-ScheduledTask -TaskName "CTP500 helper" -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName "CTP500 helper"
Start-Sleep -Seconds 15
$log = Join-Path $home_ "worker.log"
if (Test-Path $log) { Get-Content $log -Tail 3 }
Write-Output "The helper runs now and at every logon. Check it in the web app: Status -> helper, or $($cfg.server)/api/status"
