# Takes the hub off this Windows user the way deploy\windows\install.ps1 put it on, with the hub's own `hub uninstall`
# (core/uninstall.py): the scheduler task "Agents Hub" and the hub's folder in the user's PATH go, hub-up is stopped and
# waited for. With -Purge the data goes too - the state folder (%LOCALAPPDATA%\hub-core: the journal, the conversations
# of sessions, logs, the hub command and launcher) and the settings (%APPDATA%\hub-core) - after a typed confirmation,
# or at once with -Yes. Never touched: Python, Claude Code and its sign-in, this code folder. -DryRun only shows the plan.
#   powershell -ExecutionPolicy Bypass -File deploy\windows\uninstall.ps1 [-Purge] [-Yes] [-DryRun]
param([switch]$Purge, [switch]$Yes, [switch]$DryRun)
$ErrorActionPreference = "Continue"
$Repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$State = Join-Path $env:LOCALAPPDATA "hub-core"
$HubCmd = Join-Path $State "bin\hub.cmd"
$hubArgs = @("uninstall")
if ($Purge) { $hubArgs += "--purge" }
if ($Yes) { $hubArgs += "--yes" }
if ($DryRun) { $hubArgs += "--dry-run" }

if (Test-Path $HubCmd) {
    # the installed hub's own command: its Python, its code, the app's environment. It lies in the folder -Purge
    # deletes, and cmd.exe reads a batch file line by line - one deleted under it ends with "The batch file cannot be
    # found" - so a copy of it runs
    $copy = Join-Path $env:TEMP "hub-uninstall-$PID.cmd"
    Copy-Item $HubCmd $copy -Force
    try { & $copy @hubArgs; $code = $LASTEXITCODE } finally { Remove-Item $copy -Force -ErrorAction SilentlyContinue }
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    # no hub command (an install that broke off, a state folder deleted by hand): this copy's code takes off what is left
    & py -3 "$Repo\bin\hub" @hubArgs; $code = $LASTEXITCODE
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python "$Repo\bin\hub" @hubArgs; $code = $LASTEXITCODE
} else {
    Write-Host "No hub command in $State and no Python to run $Repo\bin\hub - nothing to take off here"
    $code = 1
}
exit $code
