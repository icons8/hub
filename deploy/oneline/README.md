# The one-line install

```sh
curl -fsSL https://<delivery server>/i/<build key>/install.sh | bash          # macOS 13.5+, Linux (glibc 2.28+)
irm https://<delivery server>/i/<build key>/install.ps1 | iex                  # Windows 10 1803+ / 11, PowerShell 5.1+
curl -fsSL https://<account>.github.io/<repo>/install.sh | bash                # the open build: no key
irm https://<account>.github.io/<repo>/install.ps1 | iex
```

The open build has no delivery server and no key: `tools/package/pages.py` writes its pages — the release's own
scripts with the pages' address and `HUB_INSTALL_FLAVOR=open` (`$Flavor = 'open'`) written in, `v1/releases.json`,
the layer `v1/layer/code-<version>-open.tar.gz`, `min.json` — and puts its environments apart, as the files of the
GitHub release of the tag (an environment is 190–285 MB, the pages take no file over 100 MB). Such a script asks for
no key and sends none; `runtime.shells` names each environment by its absolute address, and the script follows its
redirects itself, each checked before it is asked: https to the pages' own host or to one of
`HUB_INSTALL_ASSET_HOSTS` (`github.com`, `objects.githubusercontent.com`, `release-assets.githubusercontent.com` — the
list of `core/fetch.py` `ASSET_HOSTS`, the host exactly, no port, no login; a stand over http to this machine — http to
this machine). The size and sha256 are checked as the team build's. The update of such a hub reads the same pages
without a key (`layers/deliver.json` is `{"url"}` alone) under the same rule.

The release puts these files on the delivery server with that server's address written in at their
`@@HUB_DELIVER_URL@@` mark (`tools/package/upload.py <server> script`: the core names no server of its own), and the
server (`deliver/server.py`) hands them out with the build key written in at `@@HUB_DELIVER_KEY@@`. Each script does only what has to happen before the environment's Python exists, the
same way on its OS, and then hands over to `install.py` of the environment:

1. the build of this machine, one of six: `mac-arm64` (also under Rosetta: `sysctl hw.optional.arm64` = 1), `mac-x64`
   (on a real Intel Mac that key is "unknown oid" with a non-zero code — an answer, not a failure), `linux-x64`,
   `linux-arm64`, `windows-x64`, `windows-arm64` (the machine's processor: `PROCESSOR_ARCHITEW6432`, the system's
   `PROCESSOR_ARCHITECTURE` in the registry, `OSArchitecture` — an x64 PowerShell on ARM sees AMD64);
2. refusals before anything is made: macOS older than 13.5, glibc older than 2.28, musl, Windows 10 on ARM, root,
   missing programs (`curl` or `wget`, `tar`, `gzip`, sha256, and `git` on Linux) that the script could not install
   itself (on Linux: `sudo` and the system's package manager — apt, dnf, yum, zypper or pacman — with the password
   asked on the terminal, or none at all with `sudo -n`; no terminal and no passwordless sudo: the refusal names the
   command), under about 2 GB free,
   PowerShell in a restricted language mode or scripts closed by a group policy;
3. `releases.json` by the build key (a header from a 0600 file, never on a command line; the open build: no key):
   `runtime.shells[<build>]` — the address, sha256, size and runtime number of the environment archive (the team
   build: a path of the server, `/v1/app/…`; the open build: an asset of its GitHub release, see above);
4. one install at a time: `<data>/install.lock` (a folder with its holder on macOS and Linux, an open file on
   Windows); a second one waits up to 10 minutes and says who holds it;
5. the archive into `<data>/downloads/<name>.part`, continued when a run before was cut short; size and sha256 checked,
   a wrong sum removes it;
6. unpacked into `<data>/shells/<runtime>-<flavor>.part` — `tar` on macOS and Linux; on Windows `tar.exe` takes out
   Python alone and that Python unpacks the rest through the `\\?\` prefix (`tar.exe` drops a path over 260 characters
   with code 0) — then every member of the archive is checked on disk (a folder, a file of its size, a link to its
   target), the sheet `.agents-hub-shell.files` (every file's size and sha256, every link's target; not the bytecode
   of the environment's Python, which its installer compiles again) and the mark
   `.agents-hub-shell.json` written and the folder renamed into place. The unpack program is
   `deploy/oneline/unpack.py`; each script carries the same text (a test holds the three equal).
7. a run again: the environment of this runtime the hub runs on (`<state>/app`), else `shells/<runtime>-<flavor>`, is
   taken as it is when its mark, its Python and its whole tree against the sheet check out; a broken one is not touched
   (a running hub holds its files; Windows refuses to rename them) and a whole one is unpacked next to it as
   `<runtime>-<flavor>-<n>` — setup.py moves the pointer and restarts the hub, the broken one goes later.

The scripts refuse nothing for what the state folder holds (step 7 reads only its pointer): a hub from git, a hub's data nothing runs and another install's hub are told
apart (and said, with or without a terminal) by install.py's `setup.py`.

`<data>`: `~/.local/share/agents-hub` (Mac), `${XDG_DATA_HOME:-~/.local/share}/agents-hub` (Linux),
`%LOCALAPPDATA%\Programs\agents-hub` (Windows). The steps go to `<data>/install.log`, never the key.

## The hand-over to install.py — contract 1

```
<env>/runtime/python/bin/python3 -I -B <env>/install.py      (Windows: <env>\runtime\python\python.exe -I -B -X utf8)
    --contract 1 --env <env> --server <url> [--key-file <file>]   (the team build's; the open build has no key)
    --platform <build> --flavor team|open --runtime <n> --channel stable|nightly
    [--state <dir>]            only when HUB_STATE_DIR is set; else install.py finds the state folder as the core does
    [--move | --stay]          the answer to "found a hub from git", given by a flag
    [--instance <name>]        a second hub (small letters, digits, dashes, up to 32): a new one, or the one from
                               git in ~/.hub-<name> (an installer run with HUB_INSTANCE) — asked about and moved
    [--yes]                    no questions: the safe answer to each (a move only with --move)
    [--no-open]                do not open the browser
    [--no-input]               nobody can answer: an unanswered question is "later" (nothing changes)
    [--as-instance <name>]     only from `hub instance new` of a hub of this install, with --state: that second
                               hub's own service, commands and settings of its anchor (never from the scripts)
```

- `--key-file` is a 0600 file in the script's temporary folder, removed when the script ends: install.py keeps the key
  where the hub reads it (`layers/deliver.json`, 0600) before it returns. The open build's line gives none
  (`--flavor open`): `layers/deliver.json` holds its pages' address alone.
- `-B`: the environment folder is not written into (a check from the unpacked tree found a `.pyc` its own Python had
  rewritten). Making it read-only is install.py's.
- stdin: the terminal (`/dev/tty`) when there is one, else `/dev/null` with `--no-input`; on Windows the console, with
  `--no-input` when PowerShell runs `-NonInteractive`, input is redirected or `-Yes` was given.
- environment: `HUB_LANG` — the system's language (`AppleLanguages`, `LC_ALL`/`LC_MESSAGES`/`LANG`, `Get-UICulture`) when
  it was not set, for install.py's words through the core's catalog (the scripts are ASCII and English); `HUB_INSTALL_LOG`
  — the install log to append to; the person's proxy and `SSL_CERT_FILE` as they were.
- The script's exit code (`$LASTEXITCODE` on Windows) is install.py's.

## After the hand-over: install.py and setup.py

`install.py` is the environment's (frozen at build time, `tools/package/install.py`), and an environment stays on a
machine across many releases, so it only gets the environment ready — Windows: the links the factory took out
(`links.json`); the bytecode of its Python's standard library; the folder read-only (`.agents-hub-sealed` marks it
done) — and runs `deploy/oneline/setup.py` of the newest code there is: the hub's current layer when it is at least the
environment's seed and unedited, else the seed's. That one is the release's own, and of a seed only its first step
runs: before it reads anything of the state folder, it reads the channel's latest release and, when that one is newer,
downloads and checks it (sha256, size, MANIFEST, the commit) into `<data>/downloads/layer/`, apart from every hub, and
hands over to its `setup.py` with the same words and `--handed` — the refusals, the questions and the rest are the
latest release's, whatever environment the server gives; that one copies the layer into `<state>/layers/` and its
archive into `layers/archives/` (a repair's source, the update of a run again takes it instead of a download); the
archive stays in `downloads/layer/` only when that run did not end well (the next one downloads nothing again). No answer, a layer that does not check out or a folder
that cannot be written: the code it has goes on (said once). The arguments are contract 1's throughout; a `setup.py` before this one (1.8.90) reads the state folder
first and hands over after the question, into `<state>/layers/` (`--handed`, and `--move` for a move) — both are
taken.

`setup.py` (in order; each step is harmless again):

1. what the state folder holds — nothing; this install's hub (`<state>/app`, the pointer to the environment); a hub from
   git (`hub app find`: the question, `--move`/`--stay`, nobody to ask — exit 3); any other hub — refused. A second
   hub from git (`--instance <name>` with ~/.hub-<name> holding it, or `--state` on its folder) is that hub: its
   settings folder, its service and commands `hub-<name>`, the main hub not touched; the plain line names, for each
   such hub, the line that moves it (`… | bash -s -- --instance <name>`; the key in the terminal only), and with no
   main hub of this install in the default state folder it asks: move `<name>` / a new main hub next to it / later
   (`--move` with one such hub moves it, `--stay` installs the main hub; nobody to ask, or `--move` with several —
   those lines, nothing installed, exit 3);
2. the key of the line into `layers/deliver.json` (0600) — before the script removes its file;
3. the layer: `<state>/code` → it — a layer handed over from apart is copied into `<state>/layers/` first (a run again: an edited layer gets the released files back, its edit kept aside);
   `<state>/app` → the environment, `<state>/bin/python3`, on a Mac `<state>/bin/git` (dugite's git needs
   `GIT_EXEC_PATH`); `hub.json` once — the environment's browser MCP and ffmpeg named through `<state>/app`;
4. the commands `hub`, `hub-telegram` in `~/.local/bin` and one marked line in the shell's profile (`hub-core:
   ~/.local/bin`); Windows — `bin\hub.cmd` and the user's PATH;
5. the service, through the pointer (a new environment needs no new service): a Mac — the launchd agent `com.hub-core`
   through the signed `Agents Hub.app` running `<state>/app/start.py`; Linux — `agents-hub.service` of systemd
   `--user` (and `loginctl enable-linger` for oneself, never waiting for a password — refused, the one `sudo` line;
   a shell without a login session, `sudo -iu` or `su`, reaches the manager linger starts at `/run/user/<uid>`; no
   systemd — the hub in the background); Windows — the task "Agents Hub" of
   `deploy\windows\install.ps1 -App <state>\app -Shell`. Rewritten and restarted only when missing or different;
6. the page (`/api/hello`, 90 s), then the menu shortcut "Hub by Icons8" (Linux: `~/.local/share/applications/agents-hub.desktop`;
   Windows: `Hub by Icons8.lnk` in the Start menu, this hub's `Agents Hub.lnk` of before removed; a Mac has none; a second
   hub: `agents-hub-<name>.desktop`, `Hub by Icons8 (<name>)`;
   it runs `hub open` of its hub: the page, the hub turned on first when it is off), then `hub open` (`--no-open`: the address only); a move — `hub app move --done`, or
   `hub app undo --failed`; a run again with a newer release — `hub update` (as the Update button).

A run again whose environment is not the one the hub runs on: that one broken — the pointer moves to the new one and
the hub restarts (the page does not answer then and the one left was whole — the pointer goes back); that one whole
and older, its watchdog able to move pointers and the hub up — the pointer stays and the newer one is left to the
update (a record "preparing" keeps it from the pruning). The environments no hub of the machine needs any more go at
the end (core/envs.py).

A new environment later (a release whose layer needs a newer runtime): the update downloads `runtime.shells` of this
build, unpacks and checks it next to the current one with `unpack.py`, has its installer's own steps done
(`install.py prepare --env <new>`) and gives it to the current environment's `shellswap.py prepare`; at the full
restart that watchdog moves the pointer, the service starts the hub through it (a scheduler task or a hub with no
service — the watchdog starts it), and the pointer goes back when the core does not come up. The previous environment
stays until the next replacement.

`start.py` (the service's): the environment's `launch.py` (the layer, `PACKAGED`, a watchdog of an edited layer, the
key of the line kept), then `hub-up` of `<state>/code` with the environment's programs first in PATH (Windows:
`--prepare`, `hub-up.cmd` runs `hub-up`).

A failed step after the key is kept goes to the team's Sentry as `install.<step>` (the update's reporter).

## A server: `--server`

A cloud VM's user data runs the line as root; by SSH it is root or a user with `sudo` without a password (the script
hands itself to `sudo -n`). Only this part runs as root (`install.sh` `server_*`), Ubuntu or Debian with systemd only:

1. one run at a time (`/run/agents-hub-server.lock`: the user data's run and a person's by SSH may meet; in /run it
   is gone at a reboot and never removed by a run);
2. refusals before anything is changed: another system, no systemd, a machine other than x86_64 or arm64, under 3.5 GB
   of memory, under 10 GB free for the home folders; `--instance`, `--move` and `--stay` do not go with it;
3. the hub's user (`hub`, `--user` another name): made here with no password and no sudo, or the one made here before;
   a user of that name made by somebody else is refused, so is one in any group besides its own (docker, lxd, sudo are
   root in disguise, and the hub's sessions run without asking);
4. its `~/.ssh/authorized_keys`, when it has none: the keys of the one who ran the line — the user `sudo` came from
   (`SUDO_USER`), else root — and of every user named by `--keys-from <user>`, only lines without options: a limited
   key (`from=`, `command=`, `restrict`) gets no shell as the hub's user. A cloud's root line that sends you to its
   default user (`command="echo 'Please login as the user \"ubuntu\"…`) counts as root's when that user has the same
   key unlimited (the user data's run on EC2). Nobody else's keys: on a shared server the others reach the hub's user
   only by `--keys-from`; a run again leaves a file that is there as it is, and with `--keys-from` adds to it only the
   keys it lacks. Another user's file is read as that user (`runuser -u`); the hub's home is its sessions', so it is
   looked at and written as the hub's user, and a link there (`~/.ssh` or the file) is never followed;
5. git, curl, unattended-upgrades by apt; Google Chrome on x86_64 (fetched by root into its own folder) and
   `/etc/apt/apt.conf.d/51agents-hub-chrome`, so the unattended upgrades take Chrome's updates as well; Debian's
   `20auto-upgrades` when it is missing;
6. a 4 GB `/swapfile` (and its `/etc/fstab` line) on a machine under 7 GB with no swap; linger for the hub's user;
7. the login message `/etc/update-motd.d/90-agents-hub` (it reads `server.json` only): installing, done with the SSH
   command to the page, or the failure with its log;
8. the hub: this same script, as `self_text` prints it (its settings, its functions) into a file root owns and the hub's
   user can only read, run by `runuser -l <user>` with `--yes --no-open`; the delivery server, its key, the proxy, the
   language and `SSH_CONNECTION` (the address the tunnel names) are handed on inside that file, never on a command line
   every account sees (`ps`); its words go to `/var/log/agents-hub-server.log` (0600) too. The SSH command it says last
   (setup.py's `say_tunnel`, `core/remote.tunnel`) goes into `server.json` for the login message.

`/var/lib/agents-hub/server.json` (one line, read without jq) writes down what was put on — the user and whether it was
made here, linger, the swap file, the files — and the last run's status, code and SSH command. A run again changes
nothing it made. `--server --remove` takes off exactly that: the user with its home folder (the hub and its data) after
its processes are stopped, linger, the swap file and its fstab line, the files, `server.json` and its log; the
packages stay.
A cloud role on the machine (AWS IAM, asked at the metadata address with IMDSv2, one second) is said as a warning.

## Uninstall

`hub uninstall` (`core/uninstall.py`) takes off what the install put on: the service, the commands `hub` and
`hub-telegram` (Windows: `bin\hub.cmd` and the user's PATH), the hub's menu shortcut (a file of someone else's under the
name stays), the marked PATH line of the shell's profiles — only when
`~/.local/bin` keeps nothing else (Claude Code's `claude` keeps it), and a profile left empty goes, as the install made
it — the pointer `<state>/app` and the git and Python links through it, and every environment in `<data>/shells` no
other hub of this user stands on (their `<state>/app`, a `layers/shell-update.json` that names one): write rights back
first; the hub's own processes that outlived hub-up (a setup step) are stopped, a program of another hub keeps its
environment. When nothing of `<data>` is in use, all of it goes (the downloads, the log) and linger, if the install
turned it on (`<data>/linger.json`). Windows: the environment the command itself runs on is deleted by a PowerShell
apart once the command has ended — only a folder that still holds its mark `.agents-hub-removing`, taken aside
first, so a line run at once is not touched (`%TEMP%\agents-hub-uninstall.log`). The data (`<state>`, `hub.json`) stays: the line
takes such a hub as its own again (its mark `<state>/layers/oneline.json`, written by setup.py); `--purge` deletes it too. Never touched: Claude Code,
`~/.claude`, Codex and `~/.codex`, Chrome.

## Flags

| macOS, Linux | Windows | |
|---|---|---|
| `bash -s -- --channel main` | `& ([scriptblock]::Create((irm <line>))) -Channel main` | the nightly channel |
| `--move` / `--stay` | `-Move` / `-Stay` | the answer about a hub from git |
| `--instance <name>` | `-Instance <name>` | a second hub |
| `--yes` | `-Yes` | no questions |
| `--no-open` | `-NoOpen` | no browser |
| `--server [--user <name>]` | — | a Linux server (below) |
| `--keys-from <user>` | — | with `--server`: that user's SSH keys reach the hub's user too |
| `--server --remove` | — | take off what `--server` put on |

`HUB_INSTALL="--channel main --move"` gives the same words (either spelling on Windows); the command line wins: its
`--channel`, `--instance` and its `--move` or `--stay` over those of `HUB_INSTALL` (`--yes` and `--no-open` add up).
`HUB_DELIVER_URL` points at another delivery server (https, or http to this machine for a stand), `HUB_DELIVER_KEY`
gives the key to a script the server did not write one into (the team build's; the open build's script reads none).

Exit codes: 0 done; 1 refused or failed; 3 a hub from git and nobody to ask (also a second one with no main hub); 4 installed, the page did not answer; 5 a
move that did not come up, back on the clone; 64 a wrong flag or contract.
