# Run from an elevated PowerShell. Registers the BambuCLI uvicorn task to run
# as the INTERACTIVE logged-in user (guten-slice\alex) at logon, instead of as
# SYSTEM at boot.
#
# Why: BambuStudio's CLI needs an OpenGL/display context to render the
# object-skip pick/top masks. As a SYSTEM service in Windows session 0 it has
# no GPU/desktop and hangs. Running in the user's interactive session (1) gives
# it real GPU access. Tradeoff: the app only runs while the user is logged on.
# This replaces the SYSTEM task; register-task.ps1 remains for the SYSTEM model.

Start-Transcript -Path "$PSScriptRoot\register-task.log" -Force
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot   # this script lives in deploy\; repo root is its parent
$taskName = "BambuCLI uvicorn"
$runUser  = "$env:USERDOMAIN\$env:USERNAME"   # resolves to guten-slice\alex when run by alex (even elevated)
Write-Host "Will register task to run as: $runUser"

Write-Host "Stopping any existing process on TCP 8000..."
foreach ($line in (netstat -ano -p TCP)) {
    if ($line -match '^\s*TCP\s+\d+\.\d+\.\d+\.\d+:8000\s+\S+\s+LISTENING\s+(\d+)') {
        $listenerPid = [int]$Matches[1]
        Write-Host "  Stopping PID $listenerPid"
        & taskkill /F /PID $listenerPid 2>$null
        if ($LASTEXITCODE -ne 0) { Write-Host "    (taskkill exit $LASTEXITCODE; skipping)" }
    }
}
Start-Sleep -Seconds 2

Write-Host "Removing existing task if present..."
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

Write-Host "Registering interactive-user scheduled task..."
$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c `"$PSScriptRoot\start-uvicorn.cmd`"" `
    -WorkingDirectory $repo
# AtLogOn for this user so it auto-starts when they sign in; we also Start it
# now (below) since the user is already logged on.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $runUser
# Interactive logon type = runs in the user's desktop session (GPU/GL available).
# RunLevel Highest so it matches the prior task's privileges (alex is admin);
# a scheduled task with Highest runs elevated without a UAC prompt.
$principal = New-ScheduledTaskPrincipal -UserId $runUser -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description "BambuCLI FastAPI app (interactive user session, GPU-capable), serves on TCP 8000"

Write-Host "Starting task in the user's session..."
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 6

Write-Host "---Task info---"
Get-ScheduledTask -TaskName $taskName | Get-ScheduledTaskInfo | Format-List TaskName, LastRunTime, LastTaskResult, NumberOfMissedRuns

Write-Host "---Listeners on port 8000 (PID + session)---"
foreach ($line in (netstat -ano -p TCP)) {
    if ($line -match '^\s*TCP\s+0\.0\.0\.0:8000\s+\S+\s+LISTENING\s+(\d+)') {
        $lp = [int]$Matches[1]
        $sess = (Get-Process -Id $lp).SessionId
        Write-Host "  PID $lp  session $sess  (session >=1 = interactive = GPU OK)"
    }
}

Stop-Transcript
Start-Sleep -Seconds 8
