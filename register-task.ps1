# Run from an elevated PowerShell. Stops any existing uvicorn on :8000,
# (re)registers the BambuCLI Scheduled Task to run as SYSTEM at boot, and
# kicks it off so we don't have to reboot to test.

Start-Transcript -Path "$PSScriptRoot\register-task.log" -Force
$ErrorActionPreference = "Stop"
$repo = $PSScriptRoot
$taskName = "BambuCLI uvicorn"

Write-Host "Stopping any existing process on TCP 8000..."
# netstat + taskkill — more reliable than Get-NetTCPConnection +
# Stop-Process when the listener is owned by SYSTEM and the calling
# session has a quirky token. taskkill /F also kills SYSTEM-owned
# processes when invoked from an elevated shell.
$listenerPids = netstat -ano -p TCP | Select-String ":8000\s.*LISTENING" | ForEach-Object {
    ($_.ToString() -split '\s+' | Where-Object { $_ -match '^\d+$' })[-1]
} | Select-Object -Unique
foreach ($listenerPid in $listenerPids) {
    Write-Host "  Stopping PID $listenerPid"
    & taskkill /F /PID $listenerPid 2>&1 | Write-Host
}
Start-Sleep -Seconds 2

Write-Host "Removing existing task if present..."
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

Write-Host "Registering scheduled task..."
$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c `"$repo\start-uvicorn.cmd`"" `
    -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description "BambuCLI FastAPI app, serves on TCP 8000"

Write-Host "Starting task..."
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 5

Write-Host "---Task info---"
Get-ScheduledTask -TaskName $taskName | Get-ScheduledTaskInfo | Format-List TaskName, LastRunTime, LastTaskResult, NumberOfMissedRuns

Write-Host "---Listeners on port 8000---"
netstat -ano -p TCP | findstr ":8000"

Stop-Transcript
Start-Sleep -Seconds 8
