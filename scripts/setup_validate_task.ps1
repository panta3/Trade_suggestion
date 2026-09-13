# Run this in PowerShell as Administrator
# Schedules intraday_validate.py --close-eod to run Mon-Fri at 4:15 PM ET
# (15 min after market close so Yahoo Finance has final bar data)

$taskName   = "GapFadeAlgo_IntradayValidate"
$wslCommand = 'cd /home/aaravpant01/2026/Trade_suggestion && python3 scripts/intraday_validate.py --close-eod >> data/validate.log 2>&1'

$action = New-ScheduledTaskAction `
    -Execute "wsl.exe" `
    -Argument "-d Ubuntu -- bash -c `"$wslCommand`""

$trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At "4:15PM"

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -RunOnlyIfNetworkAvailable $true `
    -StartWhenAvailable $true `
    -WakeToRun $false `
    -DontStopIfGoingOnBatteries $true `
    -RunOnlyIfIdle $false

Register-ScheduledTask `
    -TaskName $taskName `
    -Action   $action `
    -Trigger  $trigger `
    -Settings $settings `
    -RunLevel Highest `
    -Force

Write-Host ""
Write-Host "Task '$taskName' registered. Runs Mon-Fri at 4:15 PM." -ForegroundColor Green
Write-Host ""
Write-Host "Useful commands:" -ForegroundColor Cyan
Write-Host "  Run now:   Start-ScheduledTask -TaskName '$taskName'"
Write-Host "  Check log: Get-Content '$env:USERPROFILE\AppData\Local\Temp\validate_last.log'"
Write-Host "  Remove:    Unregister-ScheduledTask -TaskName '$taskName' -Confirm:`$false"
Write-Host "  Status:    Get-ScheduledTaskInfo -TaskName '$taskName'"
