param(
  [string]$Channel = '',
  [switch]$Move,
  [switch]$Stay,
  [string]$Instance = '',
  [switch]$Yes,
  [switch]$NoOpen
)
$HubInstallKeyIn = '@@HUB_DELIVER_KEY@@'
# The one-line install of the hub on Windows (one-line spec sections 3-5), PowerShell 5.1 or newer:
#   irm https://<delivery server>/i/<build key>/install.ps1 | iex
#   & ([scriptblock]::Create((irm https://<delivery server>/i/<build key>/install.ps1))) -Channel main -Move
#   irm https://<pages of the open build>/install.ps1 | iex      (the open build: no key)
# The delivery server writes the build key into this file as it hands it out (the param() block stays first); the open
# build's copy (tools/package/pages.py: the open flavor written in) needs none and sends none - its pages are open to
# anyone, its environments are assets of its GitHub releases, downloaded through their redirects (every one https to
# $AssetHosts). The
# script picks the environment build (windows-x64 or windows-arm64), checks the machine, reads releases.json, downloads
# the environment with resume, checks its size and sha256, takes only Python out of it with tar.exe and lets that Python
# unpack the whole environment through the long-path prefix (tar.exe silently drops paths over 260 characters) into
# %LOCALAPPDATA%\Programs\agents-hub\shells\<runtime>-<flavor>\, checking every file against the archive; then it hands
# over to the environment's own Python: install.py. Nothing before that point changes the hub.
#
# Parameters (also from $env:HUB_INSTALL = "-Channel main -Move" or "--channel main --move"; parameters win):
#   -Channel stable|main   taken and ignored: a hub installed by the line has one channel (stable)
#   -Move | -Stay          the answer to "found a hub from git" without the question
#   -Instance <name>       a second hub on this machine
#   -Yes                   no questions: every answer is the safe default (a move only with -Move)
#   -NoOpen                do not open the browser
# Environment: HUB_DELIVER_URL (another delivery server: https, or http on this machine only), HUB_DELIVER_KEY (the
# build key when the server did not write one in), HUB_STATE_DIR (the hub's state folder); the system proxy is used.
#
# The hand-over to install.py (contract 1; deploy/oneline/README.md):
#   <env>\runtime\python\python.exe -I -B -X utf8 <env>\install.py --contract 1 --env <env> --server <url>
#       --key-file <file> --platform <p> --flavor <f> --runtime <n> --channel stable|nightly [--state <dir>]
#       [--move|--stay] [--instance <name>] [--yes] [--no-open] [--no-input]
#   the console is its own; --no-input when nobody can answer (-NonInteractive, redirected input, -Yes); HUB_LANG is the
#   system's language when it is not set; HUB_INSTALL_LOG names the install log. Its exit code is this script's.
# The outcome is in $LASTEXITCODE: 0 done; 1 refused or failed (nothing of the hub changed); 3 install.py's: nobody to ask about a hub from
# git (run again with -Move or -Stay) or about a second one with no main hub; 64 a wrong parameter.
# Never `exit`: inside `irm | iex` it would close the person's PowerShell window with the reason in it. The body is one
# function called on the last line, so a download cut short runs nothing. ASCII only: the words a person reads after the
# unpacking are install.py's, in the system's language.

function Install-AgentsHub {
  param([string]$Channel, [bool]$Move, [bool]$Stay, [string]$Instance, [bool]$Yes, [bool]$NoOpen, [string]$KeyIn)

  $Contract = 1
  $Server0 = '@@HUB_DELIVER_URL@@'   # the release writes in the server it puts the script on (upload.py script)
  $Flavor = 'team'
  # the open build's environments: an address its releases.json names off its own pages, and every redirect of that
  # download, is https to one of these exactly (core/fetch.py ASSET_HOSTS holds the same list)
  $AssetHosts = @('github.com', 'objects.githubusercontent.com', 'release-assets.githubusercontent.com')
  $NeedBytes = 2GB
  $LockWait = 600
  $UA = 'agents-hub-install/1 (WindowsPowerShell)'
  $ErrorActionPreference = 'Stop'
  Set-StrictMode -Version 2
  # what the helpers below share: under `irm | iex` this runs in the person's own scope, so nothing is kept in the
  # script or global scope (but the exit code)
  $S = @{ Log = $null; FetchError = ''; Server = ''; Key = ''; Platform = '' }

  function Note([string]$text) {
    if ($S.Log) {
      # UTF-8 without a BOM (Add-Content of PowerShell 5.1 starts a new file with one), the time as install.sh writes it
      $line = "{0}Z {1}`n" -f [DateTime]::UtcNow.ToString('s'), $text
      try { [IO.File]::AppendAllText($S.Log, $line, (New-Object Text.UTF8Encoding($false))) } catch { }
    }
  }
  function Say([string]$text) { Write-Host $text; Note $text }
  function Refuse([string]$text, [int]$code = 1) { throw "HUBREFUSE:${code}:$text" }

  function Run-Native([string]$exe, [string[]]$argv) {   # -> @{code; out}: stderr is output, never an error record
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
      $out = & $exe @argv 2>&1 | ForEach-Object { "$_" }
      return @{ code = $LASTEXITCODE; out = (@($out) -join "`n") }
    } finally { $ErrorActionPreference = $old }
  }


  function Native-Arch {   # the machine's processor, not this PowerShell's (an x64 or x86 one may run emulated)
    $seen = @()
    if ($env:PROCESSOR_ARCHITEW6432) { $seen += $env:PROCESSOR_ARCHITEW6432 }
    try {
      $reg = Get-ItemProperty -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment' -Name PROCESSOR_ARCHITECTURE -ErrorAction Stop
      $seen += $reg.PROCESSOR_ARCHITECTURE
    } catch { }
    try { $seen += [string][System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture } catch { }
    if ($env:PROCESSOR_ARCHITECTURE) { $seen += $env:PROCESSOR_ARCHITECTURE }
    foreach ($s in $seen) { if ($s -match '^(?i)arm64$') { return 'ARM64' } }
    foreach ($s in $seen) { if ($s -match '^(?i)(amd64|x64)$') { return 'AMD64' } }
    if ($seen.Count) { return [string]$seen[0] }
    return 'unknown'
  }

  function Pick-Platform {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
      Refuse 'this script is for Windows; on macOS and Linux run: curl -fsSL <install line>/install.sh | bash'
    }
    $v = $PSVersionTable.PSVersion
    if ($v.Major -lt 5 -or ($v.Major -eq 5 -and $v.Minor -lt 1)) { Refuse "PowerShell $v is older than 5.1, which the install needs" }
    $build = [Environment]::OSVersion.Version.Build
    if ($build -lt 17134) { Refuse "Windows build $build is older than Windows 10 1803 (17134), which the hub needs" }
    $arch = Native-Arch
    if ($arch -eq 'ARM64') {
      if ($build -lt 22000) {
        Refuse 'Windows 10 on ARM cannot run the hub: its environment needs x64 emulation, which only Windows 11 on ARM has'
      }
      return 'windows-arm64'
    }
    if ($arch -eq 'AMD64') { return 'windows-x64' }
    Refuse "this processor ($arch) is not one the hub is built for (x64 and ARM64 are)"
  }

  function Check-Machine {
    foreach ($scope in 'MachinePolicy', 'UserPolicy') {
      $p = [string](Get-ExecutionPolicy -Scope $scope)
      if ($p -eq 'AllSigned' -or $p -eq 'Restricted') {
        Refuse "running scripts is closed by your organisation's policy ($scope = $p): the hub cannot run its scripts here"
      }
    }
    $tar = Join-Path $env:SystemRoot 'System32\tar.exe'
    if (-not (Test-Path -LiteralPath $tar)) { Refuse 'this Windows has no tar.exe (it comes with Windows 10 1803 and newer)' }
    try {
      $me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
      if ($me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Say 'Note: this PowerShell runs as an administrator; the hub is installed for your user all the same.'
      }
    } catch { }
    return $tar
  }

  function Server-Url {
    $u = $Server0
    if ($env:HUB_DELIVER_URL) { $u = $env:HUB_DELIVER_URL }
    if ($u -like '@@*@@') { Refuse 'this copy of the script names no delivery server: take the install line from the team chat (or set HUB_DELIVER_URL)' }
    $u = $u.TrimEnd('/')
    if ($u -notmatch '^https://' -and $u -notmatch '^http://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?(/.*)?$') {
      Refuse "HUB_DELIVER_URL must be an https address (http only to this machine): $u"
    }
    if ($u -notmatch '^[A-Za-z0-9.:/\[\]_-]+$') { Refuse "HUB_DELIVER_URL has characters an address does not: $u" }
    return $u
  }

  function Build-Key {
    $k = $KeyIn
    if ($k -like '@@*@@') { $k = [string]$env:HUB_DELIVER_KEY }
    if (-not $k) { Refuse 'no build key: take the install line from the team chat' }
    if ($k -notmatch '^[A-Za-z0-9_-]{1,128}$') { Refuse 'the build key has characters a key does not: take the install line from the team chat' }
    return $k
  }

  function State-Dir {
    if ($env:HUB_STATE_DIR) { return $env:HUB_STATE_DIR }
    foreach ($f in (Join-Path $env:APPDATA 'hub-core\state-dir'), (Join-Path $HOME '.config\hub-core\state-dir')) {
      if (Test-Path -LiteralPath $f) {
        $got = (Get-Content -LiteralPath $f -TotalCount 1 -Encoding UTF8)
        if ($got) { return ([string]$got).Trim() }
      }
    }
    return (Join-Path $env:LOCALAPPDATA 'hub-core')
  }

  function Non-Interactive {   # nobody can answer a question: -NonInteractive, no user session, input redirected
    if (-not [Environment]::UserInteractive) { return $true }
    foreach ($a in [Environment]::GetCommandLineArgs()) { if ($a -match '^(?i)[-/]noni') { return $true } }
    try { if ([Console]::IsInputRedirected) { return $true } } catch { }
    return $false
  }

  function Server-Host {   # the host (and port) of the server
    return (($S.Server -replace '^[a-z]+://', '') -split '[/?]')[0]
  }

  function Asset-Ok([string]$u) {   # the open build may download from it: https to the pages' own host or to one of
    # $AssetHosts, the host as it is (no port, no login); a stand (the server http to this machine) - http to this machine
    # as well (core/fetch.py asset_ok answers the same)
    if ($u -cnotmatch '^[A-Za-z0-9._~:/?&=%+\[\]-]+$') { return $false }
    if ($u -cmatch '^https://([^/?]*)') {
      $auth = $Matches[1]
      if ($auth -ceq (Server-Host)) { return $true }
      foreach ($h in $AssetHosts) { if ($auth -ceq $h) { return $true } }
      return $false
    }
    if ($u -cmatch '^http://([^/?]*)') {
      $auth = $Matches[1]
      if ($S.Server -cnotmatch '^http://') { return $false }
      return ($auth -cmatch '^(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$')
    }
    return $false
  }

  function Resolve-Url([string]$u) {   # the address the file is at once its redirects are followed, each one checked by
    # Asset-Ok before it is asked; '' - one of them is not to be asked ($S.FetchError says which)
    for ($n = 1; $n -le 8; $n++) {
      if (-not (Asset-Ok $u)) { $S.FetchError = "not an address to download from: $u"; Note $S.FetchError; return '' }
      $resp = $null
      try {
        $req = [Net.HttpWebRequest]::Create($u)
        $req.UserAgent = $UA
        $req.AllowAutoRedirect = $false
        $req.Timeout = 30000
        if ($req.Proxy) { $req.Proxy.Credentials = [Net.CredentialCache]::DefaultCredentials }
        $req.AddRange([long]0, [long]0)
        $resp = $req.GetResponse()
      } catch {
        $e = $_.Exception
        while ($e -and -not ($e -is [Net.WebException])) { $e = $e.InnerException }
        if ($e -and $e.Response) { $resp = $e.Response } else { return $u }   # no answer: the download says so
      }
      try {
        $code = [int]$resp.StatusCode
        $loc = $resp.Headers['Location']
      } finally { $resp.Close() }
      if (@(301, 302, 303, 307, 308) -notcontains $code) { return $u }
      if (-not $loc) { return $u }
      $u = ([Uri]::new([Uri]$u, $loc)).AbsoluteUri
    }
    $S.FetchError = "too many redirects: $u"
    return ''
  }

  function Fetch([string]$path, [string]$file, [long]$from, [long]$total) {
    # GET into a file (from: continue at that byte) - a path of the server, or an address; -> the HTTP code, 0 when
    # there was no answer
    $S.FetchError = ''
    $resp = $null
    $url = $S.Server + $path
    if ($path -match '^https?://') { $url = $path }
    try {
      $req = [Net.HttpWebRequest]::Create($url)
      $req.UserAgent = $UA
      if ($S.Key) { $req.Headers['Authorization'] = 'Bearer ' + $S.Key }
      $req.AllowAutoRedirect = $false
      $req.Timeout = 30000
      $req.ReadWriteTimeout = 120000
      if ($req.Proxy) { $req.Proxy.Credentials = [Net.CredentialCache]::DefaultCredentials }
      if ($from -gt 0) { $req.AddRange([long]$from) }
      $resp = $req.GetResponse()
    } catch {
      $e = $_.Exception
      while ($e -and -not ($e -is [Net.WebException])) { $e = $e.InnerException }
      if ($e -and $e.Response) { return [int]$e.Response.StatusCode }
      $S.FetchError = $_.Exception.Message
      return 0
    }
    try {
      $code = [int]$resp.StatusCode
      $mode = [IO.FileMode]::Create
      if ($from -gt 0 -and $code -eq 206) { $mode = [IO.FileMode]::Append }
      $in = $resp.GetResponseStream()
      $out = New-Object IO.FileStream($file, $mode, [IO.FileAccess]::Write, [IO.FileShare]::None)
      try {
        $buf = New-Object byte[] (1MB)
        $done = $out.Length
        $step = if ($total -gt 0) { [long]($total / 10) } else { 0 }
        $next = $done + $step
        while (($n = $in.Read($buf, 0, $buf.Length)) -gt 0) {
          $out.Write($buf, 0, $n)
          $done += $n
          if ($step -gt 0 -and $done -ge $next) {
            Write-Host ("  {0}%" -f [int](100 * $done / $total))
            $next += $step
          }
        }
      } finally { $out.Close(); $in.Close() }
      return $code
    } catch {
      $S.FetchError = $_.Exception.Message
      return 0
    } finally { $resp.Close() }
  }

  function Fetch-Hint {
    if ($S.FetchError -match '(?i)trust|certificate|SSL|TLS') {
      return "the server's certificate is not trusted here ($($S.FetchError)); if your network inspects TLS, its root certificate has to be in the Windows store"
    }
    return "no answer from the delivery server ($($S.Server)): check the connection and run the line again ($($S.FetchError))"
  }

  function Read-Release([string]$work, [string]$platform) {
    $file = Join-Path $work 'releases.json'
    $code = Fetch '/v1/releases.json' $file 0 0
    if (($code -eq 401 -or $code -eq 403) -and $S.Key) { Refuse 'the delivery server does not take this build key: take the install line from the team chat' }
    if ($code -eq 401 -or $code -eq 403) { Refuse "the server refused releases.json (HTTP $code): $($S.Server)" }
    if ($code -eq 404) { Refuse "nothing is published on the delivery server yet ($($S.Server))" }
    if ($code -eq 0) { Refuse (Fetch-Hint) }
    if ($code -ne 200) { Refuse "the delivery server answered $code to releases.json; run the line again later" }
    $doc = [IO.File]::ReadAllText($file, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $entry = $null
    $runtime = $doc.PSObject.Properties['runtime']
    if ($runtime -and $runtime.Value -and $runtime.Value.PSObject.Properties['shells']) {
      $p = $runtime.Value.shells.PSObject.Properties[$platform]
      if ($p) { $entry = $p.Value }
    }
    if (-not $entry) { Refuse "the delivery server has no environment for $platform yet" }
    $r = @{}
    foreach ($k in 'url', 'sha256', 'size', 'runtime') {
      $f = $entry.PSObject.Properties[$k]
      $r[$k] = if ($f) { [string]$f.Value } else { '' }
    }
    $r.archive = (($r.url -split '\?')[0] -split '/')[-1]
    $where = $r.url -match '^/v1/app/[A-Za-z0-9][A-Za-z0-9._+-]*\.tar\.gz$'
    if (-not $where -and $Flavor -eq 'open' -and $r.url -cmatch '^https?://') {   # the open build's: an asset of its releases
      $where = (Asset-Ok $r.url) -and $r.archive -cmatch '^[A-Za-z0-9][A-Za-z0-9._+-]*\.tar\.gz$'
    }
    if (-not $where -or $r.sha256 -notmatch '^[0-9a-f]{64}$' -or
        $r.size -notmatch '^[0-9]+$' -or $r.runtime -notmatch '^[0-9]+$') {
      Refuse "the delivery server's entry for $platform is not of the form this script knows"
    }
    $r.size = [long]$r.size
    return $r
  }

  function Remove-Tree([string]$path) {   # long paths too (Remove-Item of PowerShell 5.1 trips on them)
    if (Test-Path -LiteralPath $path) { [void](Run-Native "$env:SystemRoot\System32\cmd.exe" @('/d', '/c', 'rd', '/s', '/q', "\\?\$path")) }
    if (Test-Path -LiteralPath $path) { Refuse "cannot remove $path" }
  }

  function Download($rel, [string]$root) {
    $dir = Join-Path $root 'downloads'
    [void](New-Item -ItemType Directory -Force -Path $dir)
    $part = Join-Path $dir ($rel.archive + '.part')
    $said = $false
    for ($try = 1; $try -le 4; $try++) {
      $have = 0
      if (Test-Path -LiteralPath $part) { $have = (Get-Item -LiteralPath $part).Length }
      if ($have -gt $rel.size) { Remove-Item -LiteralPath $part -Force; $have = 0 }
      if ($have -eq $rel.size) { break }
      if (-not $said) { Say ("Downloading the hub's environment for {0} ({1} MB)..." -f $S.Platform, [int]($rel.size / 1000000)); $said = $true }
      if ($have -gt 0) { Note "continuing $($rel.archive) from $have" }
      $from = $rel.url
      if ($from -match '^https?://') {   # its redirects followed again each try: a release's file storage signs its addresses for minutes
        $from = Resolve-Url $from
        if (-not $from) { break }
      }
      $code = Fetch $from $part $have $rel.size
      if ($code -eq 416) { Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue }
      if ($code -ne 200 -and $code -ne 206) {
        Note "download try ${try}: code $code $($S.FetchError)"
        if ($try -lt 4) { Start-Sleep -Seconds 3 }
      }
    }
    $have = 0
    if (Test-Path -LiteralPath $part) { $have = (Get-Item -LiteralPath $part).Length }
    if ($have -ne $rel.size) {
      Refuse ("the download of the environment did not complete ({0} of {1} MB): {2} - the line continues where it stopped" -f [int]($have / 1000000), [int]($rel.size / 1000000), (Fetch-Hint))
    }
    $got = (Get-FileHash -LiteralPath $part -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($got -ne $rel.sha256) {
      Remove-Item -LiteralPath $part -Force
      Refuse "the downloaded environment does not match its sha256 (got $got); it was removed - run the line again"
    }
    return $part
  }

  function Shell-Ok([string]$envDir, [string]$helper) {   # its mark, platform, installer, a Python that starts, and
    # every file it unpacked there as unpacked (its sheet)
    $mark = Join-Path $envDir '.agents-hub-shell.json'
    $py = Join-Path $envDir 'runtime\python\python.exe'
    if (-not (Test-Path -LiteralPath $mark) -or -not (Test-Path -LiteralPath $py) -or
        -not (Test-Path -LiteralPath (Join-Path $envDir 'install.py'))) { return $false }
    $text = [IO.File]::ReadAllText($mark)
    if ($text -notmatch ('"platform": *"' + [regex]::Escape($S.Platform) + '"')) { return $false }
    if ((Run-Native $py @('-I', '-B', '-c', 'import ssl, sqlite3')).code -ne 0) { Note "${envDir}: its Python does not start"; return $false }
    $r = Run-Native $py @('-I', '-B', '-X', 'utf8', $helper, 'check', $envDir)
    if ($r.code -eq 0 -and $r.out -match '(?m)^ok ') { return $true }
    Note ("$envDir is broken: " + @($r.out -split "`n")[-1])
    return $false
  }

  function Pick-Env([string]$state, [string]$shells, [string]$base, [string]$helper) {
    # -> @{dir; ok}: the environment of this release's runtime the hub runs on, else the default folder; a broken one is
    # left as it is (the running hub holds its files) and a new one goes next to it - the pointer moves
    $cur = ''
    try {
      $item = Get-Item -LiteralPath (Join-Path $state 'app') -Force -ErrorAction Stop
      if ($item.Target) { $cur = ([string]@($item.Target)[0]) -replace '^\\(\\\?|\?\?)\\', '' }
    } catch { }
    $leaf = Split-Path $base -Leaf
    if ($cur -and ((Split-Path $cur -Parent).TrimEnd('\') -ieq $shells.TrimEnd('\')) -and
        ((Split-Path $cur -Leaf) -match ('^' + [regex]::Escape($leaf) + '-\d+$'))) {
      if (Shell-Ok $cur $helper) { return @{ dir = $cur; ok = $true } }
    }
    if (Shell-Ok $base $helper) { return @{ dir = $base; ok = $true } }
    if (Test-Path -LiteralPath $base) {
      $n = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
      while ((Test-Path -LiteralPath "$base-$n") -or (Test-Path -LiteralPath "$base-$n.part")) { $n++ }
      return @{ dir = "$base-$n"; ok = $false }
    }
    return @{ dir = $base; ok = $false }
  }

  $unpackPy = @'
# --- unpack.py (the same in install.sh and install.ps1) ---
"""unpack|verify <archive> <part folder> [<final folder>]: unpack the archive into the part folder (Windows: through
the long-path prefix, without which tar.exe and Python lose files) or take what tar unpacked there; check that
every member of the archive is there as itself (a folder, a file of its size, a link to its target), one top folder of
the archive dropped; write the sheet of the tree (SHEET: every file's size and sha256, every link's target) and the
mark (HUB_SHELL_MARK, JSON) with the count; rename the part to the final folder when given.
check <folder>: the folder against its sheet: every file of it there with its size and sum, every link to its target
(what the environment adds itself, such as the bytecode (its Python's too: own_bytecode) and Windows' links, is not in
the sheet and not looked at).
Prints "ok <files>" or the reason, exits 0 or 1."""
import hashlib
import json
import os
import stat
import sys
import tarfile

WIN = os.name == "nt"
SHEET = ".agents-hub-shell.files"


def long(path):
    path = os.path.abspath(path)
    return "\\\\?\\" + path if WIN and not path.startswith("\\\\?\\") else path


def rel_of(name):
    parts = name.replace("\\", "/").split("/")
    while len(parts) > 1 and parts[0] == ".":
        parts = parts[1:]
    if parts[0] in ("", ".", ".."):
        return None, parts[0]
    rest = [p for p in parts[1:] if p not in ("", ".")]
    if not rest:
        return "", parts[0]
    if any(p == ".." or ":" in p for p in rest):
        raise ValueError("a member reaches outside its folder: " + name)
    return "/".join(rest), parts[0]


def inside(rel, target):
    base = os.path.dirname(rel)
    joined = os.path.normpath(os.path.join(base, target)).replace("\\", "/")
    return not (target.startswith(("/", "\\")) or joined == ".." or joined.startswith("../"))


def own_bytecode(rel):
    """The bytecode of the environment's Python: the Python comes with .pyc older than their sources (a build the
    factory's machine cannot start keeps them) and the environment's installer compiles them again (install.py
    prepare). Not written into the sheet; one an earlier sheet holds is not looked at."""
    return rel.startswith("runtime/python/") and rel.endswith(".pyc")


def place(root, rel):
    return os.path.join(root, *rel.split("/")) if rel else root


def copy(src, path, umask, mode):
    """src's bytes into a new file at path -> their sha256."""
    h = hashlib.sha256()
    with open(path, "wb") as out:
        while True:
            block = src.read(1 << 20)
            if not block:
                break
            h.update(block)
            out.write(block)
    os.chmod(path, (mode & 0o777) & ~umask)
    return h.hexdigest()


def sum_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(1 << 20)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def main(argv):
    mode, archive, part = argv[1], argv[2], argv[3]
    final = argv[4] if len(argv) > 4 else None
    root = long(part)
    top = None
    files = 0
    sizes = {}
    sheet = {"files": {}, "links": {}}
    umask = os.umask(0o077)
    os.umask(umask)
    with tarfile.open(archive, "r:gz") as t:
        for m in t:
            rel, first = rel_of(m.name)
            if rel is None:
                return "a member outside the archive's top folder: " + m.name
            if top is None:
                top = first
            elif first != top:
                return "the archive has more than one top folder: " + first
            if rel == "":
                continue
            target = place(root, rel)
            got = None
            if mode == "unpack":
                if m.isdir():
                    os.makedirs(target, exist_ok=True)
                elif m.isreg():
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    got = copy(t.extractfile(m), target, umask, m.mode)
                elif m.issym() and not WIN:
                    if not inside(rel, m.linkname):
                        return "a link reaches outside its folder: " + m.name
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    os.symlink(m.linkname, target)
                elif m.islnk():
                    src_rel, _ = rel_of(m.linkname)
                    if not src_rel:
                        return "a hard link outside the archive's folder: " + m.name
                    with open(place(root, src_rel), "rb") as a:
                        got = copy(a, target, umask, m.mode)
                else:
                    return "a member of a kind the environment never has: " + m.name
            try:
                st = os.lstat(target)
            except OSError:
                return "missing after unpacking: " + rel
            if m.isdir():
                ok = stat.S_ISDIR(st.st_mode)
            elif m.isreg():
                ok, sizes[rel] = stat.S_ISREG(st.st_mode) and st.st_size == m.size, m.size
                files += 1
            elif m.issym():
                ok = stat.S_ISLNK(st.st_mode) and os.readlink(target) == m.linkname and inside(rel, m.linkname)
                sheet["links"][rel] = m.linkname
                files += 1
            elif m.islnk():
                src_rel, _ = rel_of(m.linkname)
                ok = stat.S_ISREG(st.st_mode) and st.st_size == sizes.get(src_rel, -1)
                files += 1
            else:
                ok = False
            if not ok:
                return "not as in the archive: " + rel
            if (m.isreg() or m.islnk()) and not own_bytecode(rel):
                sheet["files"][rel] = [st.st_size, got or sum_of(target)]
    if top is None:
        return "the archive is empty"
    with open(os.path.join(root, SHEET), "w", encoding="utf-8") as f:
        json.dump(sheet, f, separators=(",", ":"), sort_keys=True)
    mark = json.loads(os.environ.get("HUB_SHELL_MARK") or "{}")
    mark.update(files=files, top=top)
    with open(os.path.join(root, ".agents-hub-shell.json"), "w", encoding="utf-8") as f:
        json.dump(mark, f, indent=1, sort_keys=True)
    if final:
        os.replace(root, long(final))
    print("ok", files)
    return None


def check(folder):
    root = long(folder)
    try:
        with open(os.path.join(root, SHEET), encoding="utf-8") as f:
            sheet = json.load(f)
        items, links = sheet["files"], sheet["links"]
        if not (isinstance(items, dict) and isinstance(links, dict)):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        return "no sheet of the environment's files (" + SHEET + ")"
    for rel, target in links.items():
        path = place(root, rel)
        if not os.path.islink(path) or os.readlink(path) != target:
            return "not as unpacked: " + rel
    items = {rel: entry for rel, entry in items.items() if not own_bytecode(rel)}
    for rel, entry in items.items():
        path = place(root, rel)
        try:
            st = os.lstat(path)
        except OSError:
            return "missing: " + rel
        if not stat.S_ISREG(st.st_mode) or st.st_size != entry[0] or sum_of(path) != entry[1]:
            return "not as unpacked: " + rel
    print("ok", len(items) + len(links))
    return None


if __name__ == "__main__":
    try:
        why = check(sys.argv[2]) if sys.argv[1:2] == ["check"] else main(sys.argv)
    except (OSError, ValueError, tarfile.TarError, EOFError, IndexError) as e:
        why = f"{type(e).__name__}: {e}"
    if why:
        print(why)
        sys.exit(1)
# --- end of unpack.py ---
'@

  function Set-Env([string]$name, $value) {   # -> the value it had (this is the person's session: put it back after)
    $was = [Environment]::GetEnvironmentVariable($name, 'Process')
    [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    return $was
  }

  function Unpack($rel, [string]$archive, [string]$envDir, [string]$tar, [string]$work) {
    # tar.exe takes out Python alone (a shallow tree, short paths); that Python unpacks everything through \\?\
    # tar.exe runs in the archive's folder and is given names relative to it: it reads its arguments in the ANSI code
    # page, where a folder of a user named in other letters becomes "????" it cannot open
    $dl = Split-Path $archive -Parent
    $name = Split-Path $archive -Leaf
    $py0name = 'p' + $PID
    $py0 = Join-Path $dl $py0name
    Remove-Tree $py0
    [void](New-Item -ItemType Directory -Force -Path $py0)
    # the archive's one top folder, from the archive itself (the name it was published under may differ)
    $first = ''
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    Push-Location -LiteralPath $dl
    try { $first = @(& $tar -tzf $name 2>$null | Select-Object -First 3) }
    finally { $ErrorActionPreference = $old; Pop-Location }
    $lead = ''
    $top = ''
    foreach ($n in $first) {   # "top/..." as the factory packs it; "./top/..." as `tar -C dir .` would
      $n = [string]$n
      if ($n -match '^(\./)?([^/.][^/]*)/') { $lead = [string]$Matches[1]; $top = $Matches[2]; break }
    }
    if (-not $top) { Remove-Tree $py0; Refuse 'the environment archive has no top folder (tar.exe -t)' }
    $strip = if ($lead) { '2' } else { '1' }
    $top = $lead + $top
    Say 'Unpacking...'
    Push-Location -LiteralPath $dl
    try { $r = Run-Native $tar @('-xzf', $name, '-C', $py0name, '--strip-components', $strip, "$top/runtime/python") }
    finally { Pop-Location }
    $python = Join-Path $py0 'runtime\python\python.exe'
    if ($r.code -ne 0 -or -not (Test-Path -LiteralPath $python)) {
      Remove-Tree $py0
      Refuse "the environment's Python did not come out of the archive (tar.exe: $($r.out))"
    }
    $helper = Join-Path $work 'unpack.py'
    $part = "$envDir.part"
    Remove-Tree $part
    $mark = '{"platform": "' + $S.Platform + '", "flavor": "' + $Flavor + '", "runtime": ' + $rel.runtime +
      ', "archive": "' + $rel.archive + '", "sha256": "' + $rel.sha256 + '", "at": "' + [DateTime]::UtcNow.ToString('s') + 'Z"}'
    $was = Set-Env 'HUB_SHELL_MARK' $mark
    try { $r = Run-Native $python @('-I', '-B', '-X', 'utf8', $helper, 'unpack', $archive, $part, $envDir) }
    finally { [void](Set-Env 'HUB_SHELL_MARK' $was) }
    Remove-Tree $py0
    if ($r.code -ne 0 -or $r.out -notmatch '(?m)^ok ') {
      Remove-Tree $part
      $why = @($r.out -split "`n")[-1]
      Refuse "the environment did not unpack whole: $why"
    }
    Note "checked: $($r.out)"
    Remove-Item -LiteralPath $archive -Force
  }

  function Parse-Flags($tokens) {   # $env:HUB_INSTALL words: -Channel main -Move or --channel main --move
    $o = @{}
    for ($i = 0; $i -lt $tokens.Count; $i++) {
      $t = [string]$tokens[$i]
      $name = ($t -replace '^-{1,2}', '').ToLowerInvariant()
      $value = $null
      if ($name -match '^([a-z-]+)=(.*)$') { $name = $Matches[1]; $value = $Matches[2] }
      switch ($name) {
        'channel' { if ($null -eq $value) { if ($i + 1 -ge $tokens.Count) { Refuse '-Channel needs a value: stable or main' 64 }; $i++; $value = $tokens[$i] }; $o.Channel = $value }
        'instance' { if ($null -eq $value) { if ($i + 1 -ge $tokens.Count) { Refuse '-Instance needs a name' 64 }; $i++; $value = $tokens[$i] }; $o.Instance = $value }
        'move' { $o.Move = $true }
        'stay' { $o.Stay = $true }
        'yes' { $o.Yes = $true }
        'y' { $o.Yes = $true }
        'noopen' { $o.NoOpen = $true }
        'no-open' { $o.NoOpen = $true }
        default { Refuse "unknown flag in HUB_INSTALL: $t" 64 }
      }
    }
    return $o
  }

  $code = 0
  $lock = $null
  $lockOwner = $null
  $work = $null
  $restore = @{}
  try {
    # first: in a restricted language mode (AppLocker, WDAC) the .NET calls below would fail without saying why
    $mode = [string]$ExecutionContext.SessionState.LanguageMode
    if ($mode -ne 'FullLanguage') { Refuse "PowerShell runs in $mode mode here (an organisation's policy): the install needs the full language" }
    $fromEnv = @{}
    if ($env:HUB_INSTALL) { $fromEnv = Parse-Flags @($env:HUB_INSTALL -split '\s+' | Where-Object { $_ }) }
    if (-not $Channel -and $fromEnv.ContainsKey('Channel')) { $Channel = $fromEnv.Channel }
    if (-not $Instance -and $fromEnv.ContainsKey('Instance')) { $Instance = $fromEnv.Instance }
    # the parameters win: their -Move or -Stay over the one in HUB_INSTALL
    $keys = @('Yes', 'NoOpen')
    if (-not ($Move -or $Stay)) { $keys += @('Move', 'Stay') }
    foreach ($k in $keys) {
      if ($fromEnv.ContainsKey($k)) { Set-Variable -Name $k -Value $true }
    }
    if (-not $Channel) { $Channel = 'stable' }
    switch ($Channel.ToLowerInvariant()) {
      'stable' { $Channel = 'stable' }
      'main' { $Channel = 'nightly' }
      'nightly' { $Channel = 'nightly' }
      default { Refuse "-Channel is stable or main, not $Channel" 64 }
    }
    if ($Move -and $Stay) { Refuse '-Move and -Stay are two answers to one question: give one' 64 }
    if ($Instance -and $Instance -cnotmatch '^[a-z0-9][a-z0-9-]{0,31}$') {
      Refuse "-Instance takes a name of small letters, digits and dashes, up to 32: $Instance" 64
    }

    $S.Platform = Pick-Platform
    $tar = Check-Machine
    $S.Server = Server-Url
    if ($Flavor -ne 'open') { $S.Key = Build-Key }   # the open build has no key
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    if (-not $env:LOCALAPPDATA) { Refuse 'LOCALAPPDATA is not set: this is not a usual Windows user session' }
    $root = Join-Path $env:LOCALAPPDATA 'Programs\agents-hub'
    [void](New-Item -ItemType Directory -Force -Path $root)
    $S.Log = Join-Path $root 'install.log'
    Note "start: $($S.Platform) channel=$Channel server=$($S.Server)"
    try {
      $free = (New-Object IO.DriveInfo([IO.Path]::GetPathRoot($root))).AvailableFreeSpace
      if ($free -lt $NeedBytes) {
        Refuse ("the hub needs about {0} GB free in {1}, and there are {2} MB" -f [int]($NeedBytes / 1GB), $root, [int]($free / 1MB))
      }
    } catch { if ("$_" -like 'HUBREFUSE:*') { throw } }

    $state = State-Dir
    $quiet = $Yes -or (Non-Interactive)
    # what the state folder holds is install.py's to read, with or without a console: a hub from git, a hub's data nothing
    # runs, another install's - each gets its own words there (and the line of a second hub from git); the script cannot tell them apart
    $work = Join-Path $root ('downloads\w' + $PID)
    Remove-Tree $work
    [void](New-Item -ItemType Directory -Force -Path $work)
    $keyFile = Join-Path $work 'key'
    if ($S.Key) { [IO.File]::WriteAllText($keyFile, $S.Key + "`n", (New-Object Text.UTF8Encoding($false))) }

    $lockPath = Join-Path $root 'install.lock'
    $lockOwner = "$lockPath.owner"
    $deadline = [DateTime]::UtcNow.AddSeconds($LockWait)
    $saidWait = $false
    while (-not $lock) {
      try { $lock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
      catch {
        $who = ''
        try { $who = [IO.File]::ReadAllText($lockOwner).Trim() } catch { }
        if (-not $saidWait) { Say "Another install of the hub is running ($who); waiting for it, up to $($LockWait / 60) min..."; $saidWait = $true }
        if ([DateTime]::UtcNow -gt $deadline) { Refuse "another install still runs ($who); run the line again when it is over" }
        Start-Sleep -Seconds 2
      }
    }
    [IO.File]::WriteAllText($lockOwner, "pid=$PID host=$env:COMPUTERNAME since=$([DateTime]::UtcNow.ToString('s'))Z")

    $rel = Read-Release $work $S.Platform
    $shells = Join-Path $root 'shells'
    [void](New-Item -ItemType Directory -Force -Path $shells)
    $helper = Join-Path $work 'unpack.py'
    [IO.File]::WriteAllText($helper, $unpackPy, (New-Object Text.UTF8Encoding($false)))
    $picked = Pick-Env $state $shells (Join-Path $shells ("{0}-{1}" -f $rel.runtime, $Flavor)) $helper
    $envDir = $picked.dir
    if ($picked.ok) { Note "environment $envDir is in place" }
    else {
      $archive = Download $rel $root
      Unpack $rel $archive $envDir $tar $work
    }
    $installer = Join-Path $envDir 'install.py'
    if (-not (Test-Path -LiteralPath $installer)) { Refuse 'this build of the environment has no installer (install.py): the delivery server has an older build' }
    $py = Join-Path $envDir 'runtime\python\python.exe'
    $argv = @('-I', '-B', '-X', 'utf8', $installer, '--contract', "$Contract", '--env', $envDir, '--server', $S.Server)
    if ($S.Key) { $argv += @('--key-file', $keyFile) }
    $argv += @('--platform', $S.Platform, '--flavor', $Flavor, '--runtime', "$($rel.runtime)", '--channel', $Channel)
    if ($env:HUB_STATE_DIR) { $argv += @('--state', $env:HUB_STATE_DIR) }
    if ($Move) { $argv += '--move' }
    if ($Stay) { $argv += '--stay' }
    if ($Instance) { $argv += @('--instance', $Instance) }
    if ($Yes) { $argv += '--yes' }
    if ($NoOpen) { $argv += '--no-open' }
    if ($quiet) { $argv += '--no-input' }
    $lang = ''
    try { $lang = (Get-UICulture).Name } catch { }
    if (-not $env:HUB_LANG -and $lang) { $restore.HUB_LANG = Set-Env 'HUB_LANG' $lang }
    $restore.HUB_INSTALL_LOG = Set-Env 'HUB_INSTALL_LOG' $S.Log
    Note ("install.py " + ($argv[5..($argv.Count - 1)] -join ' '))
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $py @argv; $code = $LASTEXITCODE } finally { $ErrorActionPreference = $old }
    Note "install.py exit $code"
  } catch {
    $m = "$_"
    if ($m -match '^HUBREFUSE:(\d+):(.*)$') {
      $code = [int]$Matches[1]
      Say "Hub by Icons8 install: $($Matches[2])"
    } else {
      $code = 1
      Say "Hub by Icons8 install failed: $m"
    }
  } finally {
    foreach ($k in $restore.Keys) { [void](Set-Env $k $restore[$k]) }
    if ($lock) {
      $lock.Close()
      Remove-Item -LiteralPath $lockOwner -Force -ErrorAction SilentlyContinue
    }
    if ($work -and (Test-Path -LiteralPath $work)) { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue }
    $global:LASTEXITCODE = $code
  }
}

# the key leaves the person's session with the function (the outcome stays in $LASTEXITCODE)
Install-AgentsHub -Channel $Channel -Move ([bool]$Move) -Stay ([bool]$Stay) -Instance $Instance -Yes ([bool]$Yes) -NoOpen ([bool]$NoOpen) -KeyIn $HubInstallKeyIn; Remove-Variable HubInstallKeyIn -ErrorAction SilentlyContinue; Remove-Item Function:\Install-AgentsHub -ErrorAction SilentlyContinue
