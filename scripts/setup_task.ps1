# Run this in PowerShell as Administrator
# Sets up a daily 3:30 PM weekday task to run daily_scan.py --slot close via WSL

$taskName   = "GapFadeAlgo_DailyScan_Close"
$wslCommand = 'cd /home/aaravpant01/2026/Trade_suggestion && python3 scripts/daily_scan.py --slot close >> scripts/logs/close_cron.log 2>&1'

$action  = New-ScheduledTaskAction `
    -Execute "wsl.exe" `
    -Argument "-d Ubuntu -- bash -c `"$wslCommand`""

$trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At "3:30PM"

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -RunOnlyIfNetworkAvailable $true `
    -StartWhenAvailable $true `
    -WakeToRun $false `
    -DontStopIfGoingOnBatteries $true `
    -RunOnlyIfIdle $false

Register-ScheduledTask `
    -TaskName   $taskName `
    -Action     $action `
    -Trigger    $trigger `
    -Settings   $settings `
    -RunLevel   Highest `
    -Force

Write-Host "Task '$taskName' registered. Runs Mon-Fri at 3:30 PM." -ForegroundColor Green
Write-Host "To test it now: Start-ScheduledTask -TaskName '$taskName'" -ForegroundColor Cyan
Write-Host "To remove it:   Unregister-ScheduledTask -TaskName '$taskName' -Confirm:`$false" -ForegroundColor Yellow
