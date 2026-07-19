# register_schedule.ps1 — register a recurring VelvetOverride run at 9:00 AM PST.
#
# Windows Task Scheduler fires at the machine's LOCAL time. This script computes
# the local-time equivalent of 9:00 AM America/Los_Angeles so it stays correct
# even if your PC is in another timezone (and across PST/PDT).
#
# Runs every -DaysInterval days (default 1 = daily).
#
# Usage (run in PowerShell, no admin needed for a per-user task):
#   powershell -ExecutionPolicy Bypass -File scripts\register_schedule.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\register_schedule.ps1 -DaysInterval 1
#
# To remove it later:
#   Unregister-ScheduledTask -TaskName "VelvetOverride" -Confirm:$false

param(
    [string]$TaskName = "VelvetOverride",
    [int]$DaysInterval = 1,    # run once every N days (1 = daily)
    [int]$PacificHour = 9,     # 9 AM Pacific
    [int]$PacificMinute = 0,
    [int]$StartInDays = 1      # first run is this many days out (1 = tomorrow)
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$RunScript = Join-Path $Root "scripts\run_daily.ps1"

# ── Convert 9:00 AM Pacific to this machine's local time ──
try {
    $pacificTz = [System.TimeZoneInfo]::FindSystemTimeZoneById("Pacific Standard Time")
    $todayPacific = [System.TimeZoneInfo]::ConvertTime((Get-Date), $pacificTz)
    # First-run Pacific date is StartInDays out (1 = tomorrow), at the target hour.
    $startPacific = $todayPacific.AddDays($StartInDays)
    $pacificTarget = Get-Date -Year $startPacific.Year -Month $startPacific.Month -Day $startPacific.Day `
        -Hour $PacificHour -Minute $PacificMinute -Second 0
    # Interpret that wall-clock as Pacific, convert to local (keeps the full date)
    $pacificTargetUtc = [System.TimeZoneInfo]::ConvertTimeToUtc(
        [DateTime]::SpecifyKind($pacificTarget, [DateTimeKind]::Unspecified), $pacificTz)
    $TriggerTime = $pacificTargetUtc.ToLocalTime()
    Write-Host "First run: $($TriggerTime.ToString('yyyy-MM-dd HH:mm')) local (9:00 AM Pacific)."
} catch {
    Write-Warning "Could not resolve Pacific timezone; scheduling at 09:00 local instead."
    $TriggerTime = (Get-Date -Hour 9 -Minute 0 -Second 0).AddDays($StartInDays)
}

$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -File `"$RunScript`""
$Trigger = New-ScheduledTaskTrigger -Daily -DaysInterval $DaysInterval -At $TriggerTime
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Description "LinkedIn application run every $DaysInterval days (VelvetOverride)" -Force

Write-Host "Registered scheduled task '$TaskName' (every $DaysInterval days, first run $($TriggerTime.ToString('yyyy-MM-dd HH:mm')) local)."
Write-Host "Test it now with:  Start-ScheduledTask -TaskName '$TaskName'"
