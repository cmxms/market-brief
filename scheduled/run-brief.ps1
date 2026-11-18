# Market brief launcher for Windows Task Scheduler.
#
#   run-brief.ps1                  production, posts to every configured webhook
#   run-brief.ps1 -Dry             checks only, posts nothing
#   run-brief.ps1 -Test            posts to the test channel
#   run-brief.ps1 -IgnoreWindow    manual run outside 07:30-08:30 Chicago
#   run-brief.ps1 -ForceWeekend    manual run on a weekend
#
# The exit code is returned to Task Scheduler unchanged, so a failed run shows up
# as failed rather than looking like a success.
param(
    [switch]$Dry,
    [switch]$Test,
    [switch]$IgnoreWindow,
    [switch]$ForceWeekend
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

# The project's own environment, created from requirements.txt. Nothing outside
# this folder is needed to run the pipeline.
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    Write-Output "LAUNCH FAILED: interpreter not found at $py"
    exit 9
}

# Ask the project which runtime folder it uses, rather than assuming a layout.
$runtime = & $py -c "import config; print(config.RUNTIME)"
$logDir = Join-Path $runtime 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$console = Join-Path $logDir ("{0}.console.log" -f (Get-Date -Format 'yyyy-MM-dd'))

$cliArgs = @((Join-Path $root 'run_brief.py'))
if ($Dry)          { $cliArgs += '--dry' }
if ($Test)         { $cliArgs += '--test' }
if ($IgnoreWindow) { $cliArgs += '--ignore-window' }
if ($ForceWeekend) { $cliArgs += '--force-weekend' }

"[{0}] launcher start: {1}" -f (Get-Date -Format 'HH:mm:ss'), ($cliArgs -join ' ') | Out-File $console -Append -Encoding utf8
& $py @cliArgs *>> $console
$code = $LASTEXITCODE
"[{0}] launcher exit: {1}" -f (Get-Date -Format 'HH:mm:ss'), $code | Out-File $console -Append -Encoding utf8
exit $code
