# The hub on Windows — without administrator rights, modelled on deploy/mac/install.sh: Python 3.12+ and Claude Code (if
# missing — offer to install), settings in %APPDATA%\hub-core\hub.json with a hub session (once, later edited in place),
# state in %LOCALAPPDATA%\hub-core, the hub command in the user's PATH, hub-up at logon — as a scheduler task.
# Running it again is harmless: hub.json is left alone, the hub is restarted on the new launcher.
#   powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 [-Yes] [-App <app folder>]
# -App: the Windows app (Setup.exe; tools/package/windows/app.py install runs this script of the code it laid out in the
# state folder): Python is the app's own, Claude Code is not asked about here — the hub page
# downloads it — and the launcher gives the hub the app's environment (setup.py --app).
# -Code: the path the hub keeps for its code (the app's <state>\code, a junction to the layer). Setup.exe's tree cannot enter
# such a junction (RedirectionGuard: "untrusted mount point"), so app.py runs this script by the layer's own path and
# everything here that runs goes by it ($Run); the files that outlive the install (hub.json, hub.cmd, the launcher, the
# task) name the pointer, so that a new layer is followed.
# -Shell (with -App <state>\app): the one-line install (deploy/oneline/setup.py runs this): -App is the hub's pointer to its
# environment, the launcher runs the environment's start.py --prepare in place of the app's app.py launch.
# -Instance <name> (or HUB_INSTANCE): another hub on this machine (core/instance.py; `hub instance new` runs this with
# it): %LOCALAPPDATA%\hub-<name>, %APPDATA%\hub-<name>, the task "Agents Hub (<name>)", the command hub-<name> from
# <state>\cmd in PATH (<state>\bin with its hub.cmd is for its sessions), its own ports; the main hub is left alone.
# With -App <state>\app -Shell — a second hub of the one-line install (deploy/oneline/setup.py --as-instance).
param([switch]$Yes, [string]$App, [string]$Code, [string]$Instance, [switch]$Shell)
$ErrorActionPreference = "Stop"
# a caller that reads the output (deploy/oneline/setup.py, the app's app.py: their logs) asks for UTF-8, this script's
# and its Pythons':
# Windows PowerShell 5.1 writes it in the console code page and Python in the ANSI one — a dash and a non-Latin user
# name came out as "?" there. Only on that word: the code page is the console's, and the caller gives the script a
# hidden console of its own (a session's or a person's console keeps its code page)
if ($env:HUB_INSTALL_OUTPUT -eq "utf-8") {
    try { [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false) } catch { }
    $env:PYTHONIOENCODING = "utf-8"
}
$Run = (Resolve-Path "$PSScriptRoot\..\..").Path
$Repo = if ($Code) { $Code } else { $Run }
if (-not $Instance -and $env:HUB_INSTANCE) { $Instance = $env:HUB_INSTANCE }
if ($Instance -and $App -and -not $Shell) { throw "the app installs only the main hub, without -Instance" }
$Name = if ($Instance) { "hub-$Instance" } else { "hub-core" }
$State = Join-Path $env:LOCALAPPDATA $Name
$Config = Join-Path $env:APPDATA "$Name\hub.json"
$Task = if ($Instance) { "Agents Hub ($Instance)" } else { "Agents Hub" }
$Web = 8787

function Ask($question) {
    if ($Yes) { return $true }
    $answer = Read-Host "$question [Y/n]"
    return ($answer -match '^([yY]|$)')
}

# Python 3.12 or newer. python3/python from WindowsApps is most often the Microsoft Store stub, not Python
function Test-Python($path) {
    if (-not $path -or -not (Test-Path $path) -or $path -like "*\WindowsApps\*") { return $false }
    & $path -c "import sys; sys.exit(sys.version_info < (3, 12))" 2>$null
    return $LASTEXITCODE -eq 0
}
function Find-Python {
    $found = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $found += (& py -0p 2>$null) | ForEach-Object { if ($_ -match '([A-Za-z]:\\.*python\.exe)') { $Matches[1] } }
    }
    $found += Get-Command python, python3 -All -ErrorAction SilentlyContinue | ForEach-Object { $_.Source }
    $found += Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe" `
        -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | ForEach-Object { $_.FullName }
    foreach ($p in $found) { if (Test-Python $p) { return $p } }
    return $null
}

$Python = if ($App) { Join-Path $App "runtime\python\python.exe" } else { Find-Python }
if ($App -and -not (Test-Path $Python)) { throw "the app has no Python: $Python" }
if (-not $Python) {
    Write-Host "The hub needs Python 3.12 or newer, and there is none here."
    if ((Get-Command winget -ErrorAction SilentlyContinue) -and (Ask "Install Python 3.12 for this user (winget install Python.Python.3.12)?")) {
        winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
        $Python = Find-Python
    }
    if (-not $Python) {
        Write-Host "Install Python 3.12+ from https://www.python.org/downloads/windows/ and run the install again."
        exit 1
    }
}
Write-Host "Python: $Python ($(& $Python -V 2>&1))"
if ($Instance) {
    # the anchor and the ports, once (a rerun keeps them); only numbers are read back — PowerShell 5.1 reads a program's
    # output in another code page, the folders are spelled here
    $env:HUB_STATE_DIR = ""; $env:HUB_CONFIG_DIR = ""
    $ports = & $Python -I "$Run\core\instance.py" setup $Instance --repo $Repo --os windows --ports
    if ($LASTEXITCODE -ne 0) { Write-Host "Hub ${Instance}: not installed (the reason is above)"; exit 1 }
    $Web = [int](("$ports".Trim() -split " ")[0])
    # this script's own `hub` calls below (stop, setup claude) are this hub's
    $env:HUB_STATE_DIR = $State; $env:HUB_CONFIG_DIR = Split-Path $Config
}

# a scheduler task of this name that runs another hub (its action is not this state folder's launcher) stays as it is:
# Register-ScheduledTask -Force below would overwrite it (tests research 03.11). Python checks it the way `hub uninstall`
# and `hub on` do (platforms/windows.py service_unit) — by its exit code: PowerShell 5.1 reads a program's output in
# another code page
if (Get-ScheduledTask -TaskPath "\" -TaskName $Task -ErrorAction SilentlyContinue) {   # where Register puts it
    & $Python -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from platforms import windows; sys.exit(0 if windows.service_unit(sys.argv[2]) else 3)' $Run $State
    if ($LASTEXITCODE -eq 3) {
        $runs = ((Get-ScheduledTask -TaskPath "\" -TaskName $Task).Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join "; "
        Write-Host "The scheduler task `"$Task`" is not this hub's: it runs $runs, and this hub's is $State\hub-up.cmd."
        Write-Host "Leaving it alone and installing nothing."
        Write-Host "If it is an earlier install of this hub, delete the task (schtasks /Delete /TN `"$Task`") and run the install again."
        exit 1
    } elseif ($LASTEXITCODE -ne 0) {
        throw "the scheduler task `"$Task`" could not be checked (exit code $LASTEXITCODE) — installing nothing"
    }
}

# Claude Code: if missing — the release's version into the hub's own folder, with the user's consent (core/ownclaude.py:
# the signed manifest, sha256; the user's .local\bin is not touched; the same path as deploy/install.sh)
$Claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $App -and -not $Claude -and -not (Test-Path "$env:USERPROFILE\.local\bin\claude.exe")) {
    Write-Host "No Claude Code (claude) — without it the hub's sessions will not start."
    if (Ask "Download it into the hub's folder from Anthropic (hub setup claude: the signed manifest, sha256)?") {
        $was = $env:HUB_STATE_DIR; $env:HUB_STATE_DIR = $State   # into this hub's folder: the mark comes later
        & $Python "$Run\bin\hub" setup claude
        $env:HUB_STATE_DIR = $was
        # the hub is installed anyway, as on Linux and Mac; `hub` reaches PATH only in new windows — the full command
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Claude Code is not installed — the cause is above; to try again: & `"$Python`" `"$Repo\bin\hub`" setup claude"
        } else {
            Write-Host "Sign in to Claude Code on the hub's page (Setup)."
        }
    } else {
        Write-Host "Download it later: hub setup claude (in a new terminal window) — then sign in on the hub's page (Setup)."
    }
}

# the hub is already running (a reinstall) — stop it gently: the core writes a snapshot
$existing = Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
if ($existing -and $existing.State -eq "Running") {
    # Windows PowerShell 5.1 under "Stop" turns a line on stderr of a command with 2>$null into an exception: the
    # reinstall died on the "hub-up is not running" line of `hub stop` (winstand)
    try { & $Python "$Run\bin\hub" stop 2>$null | Out-Null } catch { }
    for ($i = 0; $i -lt 60 -and (Get-ScheduledTask -TaskName $Task).State -eq "Running"; $i++) { Start-Sleep 1 }
}

# files: hub.json (if missing), hub-home, bin\hub.cmd, the hub-up launcher — deploy\windows\setup.py
$appArgs = if ($App -and $Shell) { @("--app", $App, "--shell") } elseif ($App) { @("--app", $App) } else { @() }
if ($Instance) { $appArgs += @("--instance", $Instance) }   # with -App -Shell: a second hub of the one-line install
& $Python "$Run\deploy\windows\setup.py" $Repo $State $Config --run $Run @appArgs | Out-Null
if ($LASTEXITCODE -ne 0) { throw "setup.py: failed" }
# the files setup.py writes, by their place — not read back from its output: Windows PowerShell 5.1 reads a program's
# output in the console code page, Python writes it in the ANSI one, and a non-Latin user name in the path came back garbled
$HubCmd = Join-Path $State "bin\hub.cmd"
$Launcher = Join-Path $State "hub-up.cmd"

# the hub command — the state folder\bin in the user's PATH (REG_EXPAND_SZ: %USERPROFILE% in it stays as it was); an
# instance's — <state>\cmd with hub-<name>.cmd only: its bin\hub.cmd first in PATH would take `hub` of new windows
$bin = if ($Instance) { Join-Path $State "cmd" } else { Split-Path $HubCmd }
$key = Get-Item "HKCU:\Environment"
$path = $key.GetValue("Path", "", "DoNotExpandEnvironmentNames")
if (($path -split ";") -notcontains $bin) {
    $new = (@($path.TrimEnd(";"), $bin) | Where-Object { $_ }) -join ";"
    Set-ItemProperty "HKCU:\Environment" -Name Path -Value $new -Type ExpandString
    Add-Type -Namespace Hub -Name Env -MemberDefinition '[DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr SendMessageTimeout(IntPtr h, uint m, UIntPtr w, string l, uint f, uint t, out UIntPtr r);'
    $r = [UIntPtr]::Zero
    [Hub.Env]::SendMessageTimeout([IntPtr]0xffff, 0x1A, [UIntPtr]::Zero, "Environment", 2, 5000, [ref]$r) | Out-Null
    Write-Host "The $(if ($Instance) { "hub-$Instance" } else { "hub" }) command is in PATH (new terminal windows): $bin"
}

# hub-up at logon: a scheduler task on behalf of the user, windowless (conhost --headless), restarted if it fails.
# A repeat every 5 minutes: a hub killed as a whole (`taskkill /IM python.exe` kills both hub-up and the supervisor, and the task
# ends "successfully", so RestartCount does not fire) comes back by itself; a live one — the repeat is skipped by IgnoreNew, and a hub-up
# started outside the task leaves the second instance as is (it exits by itself). A hub stopped with `hub stop` / `hub off`
# is not brought back by the repeat: the launcher calls hub-up with --scheduled, which sees the stop mark and exits; `hub on` clears it
# The name comes from whoami: the domain from the environment under SSH and in services is not the right account (error 0x80070534)
$me = (whoami)
$action = New-ScheduledTaskAction -Execute "conhost.exe" -Argument "--headless cmd.exe /c `"$Launcher`"" -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $me
$again = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $Task -Action $action -Trigger @($trigger, $again) -Principal $principal -Settings $settings -Force | Out-Null
# the installation starts the hub: clear the stop mark (`hub stop` above, the earlier `hub off`) — otherwise the launcher with --scheduled
# would exit at once and the hub would stay down after a reinstall
Remove-Item (Join-Path $State "stopped") -Force -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName $Task

if (-not $env:HUB_INSTALL_NO_WAIT) {
    for ($i = 0; $i -lt 60; $i++) {
        try { Invoke-WebRequest "http://127.0.0.1:$Web/" -UseBasicParsing -TimeoutSec 2 | Out-Null; break } catch { Start-Sleep 1 }
    }
}
$cmd = if ($Instance) { "hub-$Instance" } else { "hub" }
Write-Host "The hub is running: page http://127.0.0.1:$Web/, settings $Config, state $State"
Write-Host "To stop it: $cmd stop (or $cmd off); to start it again: $cmd on (it also starts at Windows sign-in unless stopped)"
