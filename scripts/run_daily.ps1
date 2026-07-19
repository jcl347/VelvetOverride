# run_daily.ps1 — one daily VelvetOverride run, logged to data/logs/.
# Invoked by the scheduled task registered via register_schedule.ps1.
#
# It respects settings.yaml (dry_run, max_applications, filters). To go live,
# set bot.dry_run: false in config/settings.yaml (or pass --live below) once
# you've calibrated with dry runs.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot   # project root (parent of scripts/)
Set-Location $Root

$LogDir = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$Log = Join-Path $LogDir "run_$Stamp.log"

"[$(Get-Date -Format o)] VelvetOverride daily run starting" | Tee-Object -FilePath $Log

# Use the module entry point so we don't depend on PATH having the console script.
# --live         : actually submit (overrides settings.yaml dry_run)
# -d past_24h    : only jobs posted in the last 24h — matches the daily cadence,
#                  so each run picks up that day's new postings.
# --no-keep-open : exit cleanly instead of hanging on an open browser.
# Roles/locations/experience come from config/settings.yaml.
#
# IMPORTANT (Windows PowerShell 5.1): the bot logs to stderr via structlog.
# With $ErrorActionPreference = "Stop", redirecting a native command's stderr
# wraps each stderr line in a NativeCommandError and TERMINATES the script after
# the first log line — killing the run instantly. Drop to "Continue" for the
# native call. Use *>> (all streams -> one file handle); do NOT use separate
# 1>>/2>> redirects to the same file (they fail with a file-in-use error).
$ErrorActionPreference = "Continue"
& python -m velvetoverride.main run --live -d past_24h --no-keep-open *>> $Log
$exitCode = $LASTEXITCODE
$ErrorActionPreference = "Stop"

"[$(Get-Date -Format o)] Done (exit $exitCode)" | Tee-Object -FilePath $Log -Append
exit $exitCode
