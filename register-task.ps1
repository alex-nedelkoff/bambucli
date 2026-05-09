# Run from an elevated PowerShell. Stops any existing uvicorn on :8000,
# (re)registers the BambuCLI Scheduled Task to run as SYSTEM at boot, and
# kicks it off so we don't have to reboot to test.

Start-Transcript -Path "$PSScriptRoot\register-task.log" -Force
$ErrorActionPreference = "Stop"
$repo = $PSScriptRoot
$taskName = "BambuCLI uvicorn"

Write-Host "Stopping any existing process on TCP 8000..."
$conns = Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue
foreach ($c in $conns) {
    Write-Host "  Stopping PID $($c.OwningProcess)"
    Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 1

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
