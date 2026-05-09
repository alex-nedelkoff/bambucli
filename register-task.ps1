# Run from an elevated PowerShell. Stops any existing uvicorn on :8000,
# (re)registers the BambuCLI Scheduled Task to run as SYSTEM at boot, and
# kicks it off so we don't have to reboot to test.

Start-Transcript -Path "$PSScriptRoot\register-task.log" -Force
$ErrorActionPreference = "Stop"
$repo = $PSScriptRoot
$taskName = "BambuCLI uvicorn"

Write-Host "Stopping any existing process on TCP 8000..."
# netstat + taskkill is more reliable than Get-NetTCPConnection +
# Stop-Process when the listener is owned by SYSTEM. We parse only
# IPv4 lines via a strict regex (the IPv6 [::] listener can show as
# a kernel pseudo-PID like 6 that taskkill refuses to touch). We
# discard taskkill's stderr to $null instead of piping through
# PowerShell so a "not found" doesn't abort the script under
# $ErrorActionPreference = Stop.
foreach ($line in (netstat -ano -p TCP)) {
    if ($line -match '^\s*TCP\s+\d+\.\d+\.\d+\.\d+:8000\s+\S+\s+LISTENING\s+(\d+)') {
        $listenerPid = [int]$Matches[1]
        Write-Host "  Stopping PID $listenerPid"
        & taskkill /F /PID $listenerPid 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "    (taskkill exit $LASTEXITCODE; skipping)"
        }
    }
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
