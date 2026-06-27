# Run from an elevated PowerShell. Replaces the per-user Ollama autostart
# with a real Windows Service running as SYSTEM, so the daemon survives
# logouts and reboots.
#
# Models stay under C:\Users\alex\.ollama\models (set via OLLAMA_MODELS
# env var) so anything you pulled as your user is reused. SYSTEM has read
# access to that profile path.

Start-Transcript -Path "$PSScriptRoot\register-ollama.log" -Force
$ErrorActionPreference = "Stop"

$ollamaExe   = "C:\Users\alex\AppData\Local\Programs\Ollama\ollama.exe"
$modelsDir   = "C:\Users\alex\.ollama\models"
$logDir      = "C:\ProgramData\Ollama"
$serviceName = "Ollama"

# nssm from winget isn't on PATH until a fresh shell, so fall back to the
# known WinGet package location if Get-Command can't find it.
$nssmExe = (Get-Command nssm -ErrorAction SilentlyContinue).Source
if (-not $nssmExe) {
    $nssmExe = Get-ChildItem "C:\Users\alex\AppData\Local\Microsoft\WinGet\Packages\NSSM.NSSM_*\*\win64\nssm.exe" -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
}
if (-not $nssmExe) { throw "nssm not found on PATH or in WinGet packages. Run 'winget install NSSM.NSSM' first." }
if (-not (Test-Path $ollamaExe)) { throw "ollama.exe not found at $ollamaExe" }
if (-not (Test-Path $modelsDir)) { throw "Models dir not found at $modelsDir; pull a model first via 'ollama pull <name>'." }

Write-Host "Stopping user-context Ollama processes..."
Get-Process -Name ollama* -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

Write-Host "Removing user Startup shortcut so the per-user copy doesn't relaunch..."
$startup = [Environment]::GetFolderPath('Startup')
$lnk = Join-Path $startup "Ollama.lnk"
if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Host "  removed $lnk" }

Write-Host "Removing per-user HKCU Run autostart (tray app) so it can't bind 11434..."
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
foreach ($name in @("Ollama","OllamaApp")) {
    if (Get-ItemProperty -Path $runKey -Name $name -ErrorAction SilentlyContinue) {
        Remove-ItemProperty -Path $runKey -Name $name -Force
        Write-Host "  removed Run\$name"
    }
}

Write-Host "Ensuring log dir exists..."
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

if (Get-Service -Name $serviceName -ErrorAction SilentlyContinue) {
    Write-Host "Removing existing service..."
    & $nssmExe stop   $serviceName confirm 2>&1 | Out-Null
    & $nssmExe remove $serviceName confirm 2>&1 | Out-Null
    Start-Sleep -Seconds 2
}

Write-Host "Installing service via NSSM..."
& $nssmExe install $serviceName $ollamaExe serve
& $nssmExe set $serviceName AppEnvironmentExtra "OLLAMA_MODELS=$modelsDir" "OLLAMA_KEEP_ALIVE=24h"
& $nssmExe set $serviceName AppStdout         "$logDir\ollama.out.log"
& $nssmExe set $serviceName AppStderr         "$logDir\ollama.err.log"
& $nssmExe set $serviceName AppRotateFiles    1
& $nssmExe set $serviceName AppRotateOnline   1
& $nssmExe set $serviceName AppRotateBytes    10485760
& $nssmExe set $serviceName Start             SERVICE_AUTO_START
& $nssmExe set $serviceName ObjectName        LocalSystem
& $nssmExe set $serviceName Description       "Ollama LLM daemon - 127.0.0.1:11434"

Write-Host "Starting service..."
Start-Service -Name $serviceName
Start-Sleep -Seconds 5

Write-Host "---Service status---"
Get-Service -Name $serviceName | Format-List Name, Status, StartType, DisplayName

Write-Host "---Listeners on 11434---"
netstat -ano -p TCP | findstr ":11434"

Write-Host "---Models visible to SYSTEM-context daemon---"
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:11434/api/tags" -UseBasicParsing -TimeoutSec 5
    ($r.Content | ConvertFrom-Json).models | Select-Object name, size | Format-Table -AutoSize | Out-String | Write-Host
} catch {
    Write-Host "API check failed: $($_.Exception.Message)"
}

Stop-Transcript
Start-Sleep -Seconds 8
