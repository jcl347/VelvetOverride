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
# Change "run" to "run --live" here (or flip dry_run in settings.yaml) when ready.
# --no-keep-open so the scheduled run exits cleanly instead of hanging.
#
# IMPORTANT (Windows PowerShell 5.1): the bot logs to stderr via structlog.
# With $ErrorActionPreference = "Stop", redirecting a native command's stderr
# wraps each stderr line in a NativeCommandError and TERMINATES the script after
# the first log line — killing the run instantly. Drop to "Continue" for the
# native call. Use *>> (all streams -> one file handle); do NOT use separate
# 1>>/2>> redirects to the same file (they fail with a file-in-use error).
$ErrorActionPreference = "Continue"
& python -m velvetoverride.main run --no-keep-open *>> $Log
$exitCode = $LASTEXITCODE
$ErrorActionPreference = "Stop"

"[$(Get-Date -Format o)] Done (exit $exitCode)" | Tee-Object -FilePath $Log -Append
exit $exitCode
