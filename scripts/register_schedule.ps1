# register_schedule.ps1 — register a daily VelvetOverride run at 9:00 AM PST.
#
# Windows Task Scheduler fires at the machine's LOCAL time. This script computes
# the local-time equivalent of 9:00 AM America/Los_Angeles so it stays correct
# even if your PC is in another timezone (and across PST/PDT).
#
# Usage (run in PowerShell, no admin needed for a per-user task):
#   powershell -ExecutionPolicy Bypass -File scripts\register_schedule.ps1
#
# To remove it later:
#   Unregister-ScheduledTask -TaskName "VelvetOverride Daily" -Confirm:$false

param(
    [string]$TaskName = "VelvetOverride Daily",
    [int]$PacificHour = 9,     # 9 AM Pacific
    [int]$PacificMinute = 0
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$RunScript = Join-Path $Root "scripts\run_daily.ps1"

# ── Convert 9:00 AM Pacific to this machine's local time ──
try {
    $pacificTz = [System.TimeZoneInfo]::FindSystemTimeZoneById("Pacific Standard Time")
    $todayPacific = [System.TimeZoneInfo]::ConvertTime((Get-Date), $pacificTz)
    $pacificTarget = Get-Date -Year $todayPacific.Year -Month $todayPacific.Month -Day $todayPacific.Day `
        -Hour $PacificHour -Minute $PacificMinute -Second 0
    # Interpret that wall-clock as Pacific, convert to local
    $pacificTargetUtc = [System.TimeZoneInfo]::ConvertTimeToUtc(
        [DateTime]::SpecifyKind($pacificTarget, [DateTimeKind]::Unspecified), $pacificTz)
    $localTarget = $pacificTargetUtc.ToLocalTime()
    $TriggerTime = Get-Date -Hour $localTarget.Hour -Minute $localTarget.Minute -Second 0
    Write-Host "9:00 AM Pacific = $($localTarget.ToString('HH:mm')) local time on this machine."
} catch {
    Write-Warning "Could not resolve Pacific timezone; scheduling at 09:00 local instead."
    $TriggerTime = Get-Date -Hour 9 -Minute 0 -Second 0
}

$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -File `"$RunScript`""
$Trigger = New-ScheduledTaskTrigger -Daily -At $TriggerTime
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Description "Daily LinkedIn application run (VelvetOverride)" -Force

Write-Host "Registered scheduled task '$TaskName' (daily at $($TriggerTime.ToString('HH:mm')) local)."
Write-Host "Test it now with:  Start-ScheduledTask -TaskName '$TaskName'"
