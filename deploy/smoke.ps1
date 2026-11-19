# A clean install on Windows checked end to end, the twin of deploy/smoke.sh: deploy\windows\install.ps1 of this tree,
# the page answering and `hub status` reading the journal, then deploy\windows\uninstall.ps1 -Purge -Yes and a check
# that nothing of the hub is left: the scheduler task, the PATH entry, the folders, a running hub-up, the page. One line
# per phase, "phase <name> ok|fail <s>s", then SMOKE OK or SMOKE FAIL; exit 0 only when every phase passed.
# The task "Agents Hub" is one per Windows user and the folders are the user's own, so it runs only where no hub is
# (a throwaway machine or account): it refuses when any trace of one is there. A stand-in claude on this window's PATH
# keeps the installer from asking about Claude Code; the hub itself, started by the task with the user's own PATH, does
# not see it — with no Claude Code installed its sessions wait for one, which the check does not need. Python 3.12+
# must be installed.
#   powershell -ExecutionPolicy Bypass -File deploy\smoke.ps1 [-Line <address of install.ps1>]
# -Line: the one-line install instead of this tree's installer — `irm <address> | iex` with HUB_INSTALL="-Yes -NoOpen"
# in a PowerShell of its own, from a delivery server or its stand; the hub is removed by its own `hub uninstall --purge
# --yes`, and "clean" also wants no environments folder (%LOCALAPPDATA%\Programs\agents-hub) — its last part goes a
# moment after the command, by a PowerShell apart.
param([string]$Line = "")
$ErrorActionPreference = "Continue"
$Repo = (Resolve-Path "$PSScriptRoot\..").Path
$State = Join-Path $env:LOCALAPPDATA "hub-core"
$ConfigDir = Join-Path $env:APPDATA "hub-core"
$Task = "Agents Hub"
$HubCmd = Join-Path $State "bin\hub.cmd"
$Bin = Join-Path $State "bin"
$Work = Join-Path $env:TEMP "hub-smoke-$PID"
$Data = Join-Path $env:LOCALAPPDATA "Programs\agents-hub"   # the one-line install's environments, downloads and log
$script:Failed = $false
$script:UpPid = $null

if ((Test-Path $State) -or (Test-Path $ConfigDir) -or (Test-Path $Data) -or
        (Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue)) {
    Write-Host "not a clean machine: $State, $ConfigDir, $Data or the task `"$Task`" exists - this check removes the hub at the end"
    exit 2
}
# nothing of a hub this check may run inside of
Get-ChildItem Env: | Where-Object { $_.Name -like "HUB_*" } | ForEach-Object { Remove-Item "Env:$($_.Name)" }
New-Item -ItemType Directory -Force $Work | Out-Null
Set-Content (Join-Path $Work "claude.cmd") "@echo 9.9.9 (Claude Code)" -Encoding Ascii
$env:PATH = "$Work;$env:PATH"

function Phase($name, [scriptblock]$body) {
    $start = Get-Date
    $log = Join-Path $Work "$name.log"
    try { & $body *> $log } catch { $_ | Out-File $log -Append }   # a phase passes by its mark (Pass), not its output
    $s = [int]((Get-Date) - $start).TotalSeconds
    if (Test-Path "$log.ok") { Write-Host "phase $name ok ${s}s"; return $true }
    Write-Host "phase $name fail ${s}s"
    Get-Content $log -Tail 40 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  | $_" }
    $script:Failed = $true
    return $false
}
function Pass($name) { New-Item (Join-Path $Work "$name.log.ok") -ItemType File -Force | Out-Null }

function Answers {
    try { $r = Invoke-WebRequest http://127.0.0.1:8787/ -UseBasicParsing -TimeoutSec 3; return $r.StatusCode -eq 200 }
    catch { return $false }
}
function UserPath {
    (Get-Item "HKCU:\Environment").GetValue("Path", "", "DoNotExpandEnvironmentNames") -split ";" |
        Where-Object { $_ -and $_.TrimEnd("\") -ieq $Bin }
}

$install = Phase install {
    if ($Line) {   # what a person pastes, in a PowerShell of its own (the line never exits the one it runs in)
        $env:HUB_INSTALL = "-Yes -NoOpen"
        & powershell -NoProfile -Command "irm '$($Line -replace "'", "''")' | iex; exit `$LASTEXITCODE"
        Remove-Item Env:HUB_INSTALL
    } else {
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Repo "deploy\windows\install.ps1")
    }
    if ($LASTEXITCODE -eq 0 -and (Test-Path $HubCmd)) { Pass install }
}
if ($install) {
    Phase page {
        for ($i = 0; $i -lt 60 -and -not (Answers); $i++) { Start-Sleep 1 }
        if (Answers) { Pass page }
    } | Out-Null
    Phase status {
        for ($i = 0; $i -lt 60; $i++) {
            $out = (& $HubCmd status --json) -join "`n"
            try { if ((ConvertFrom-Json $out).sessions."hub-core") { Pass status; break } } catch { }
            Start-Sleep 1
        }
        $out
    } | Out-Null
    try { $script:UpPid = (Get-Content (Join-Path $State "hub-up.json") -Raw | ConvertFrom-Json).pid } catch { }
}
Phase uninstall {
    if ($Line) {   # nothing installed — nothing to take off; "clean" says what was left
        if (-not (Test-Path $HubCmd)) { Pass uninstall; return }
        & $HubCmd uninstall --purge --yes
    } else {
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Repo "deploy\windows\uninstall.ps1") -Purge -Yes
    }
    if ($LASTEXITCODE -eq 0) { Pass uninstall }
} | Out-Null
Phase clean {
    # the environment the uninstall ran on goes by a PowerShell apart once it has ended
    for ($i = 0; $i -lt 60 -and (Test-Path $Data); $i++) { Start-Sleep 1 }
    $left = @()
    if (Test-Path $Data) { $left += $Data }
    if (Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue) { $left += "the task $Task" }
    if (Test-Path $State) { $left += $State }
    if (Test-Path $ConfigDir) { $left += $ConfigDir }
    if (UserPath) { $left += "$Bin in the user's PATH" }
    if ($script:UpPid -and (Get-Process -Id $script:UpPid -ErrorAction SilentlyContinue)) { $left += "hub-up $($script:UpPid)" }
    if (Answers) { $left += "the page on 8787" }
    if ($left) { "left: " + ($left -join "; ") } else { Pass clean }
} | Out-Null
Remove-Item $Work -Recurse -Force -ErrorAction SilentlyContinue
if ($script:Failed) { Write-Host "SMOKE FAIL"; exit 1 }
Write-Host "SMOKE OK"
exit 0
