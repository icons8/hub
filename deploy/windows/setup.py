"""The hub installation files on Windows — install.ps1 writes them with this script (using the Python the hub will have):
the hub.json settings (once, later edited in place — as in deploy/mac/install.sh), the hub-home of the hub session, the
`hub` command and the hub-up launcher for the task scheduler.

  python setup.py <repo> <state folder> <hub.json> [--run <folder>] [--app <app folder> [--shell]] [--instance <name>]
                                                       → prints the paths of hub.cmd and of the launcher

--run: where the code can be entered from when <repo> (what the files name) is a junction this process may not follow
(Setup.exe's tree, RedirectionGuard): the default is <repo>.

--app: the hub of the Windows app (Setup.exe, installer spec S6): the repo is the code in the state folder, Python and
the programs are the app's own (runtime\\), the launcher first runs the app's app.py launch (the seed, a trial after a
reboot) and gives the hub and its sessions the app's environment (app_env).

--shell (with --app <state>\\app): the one-line install (deploy/oneline/setup.py): the app folder is the hub's pointer to
its environment, and the launcher runs the environment's start.py --prepare in place of app.py launch.

--instance: another hub on this machine (core/instance.py; its anchor <state>\\instance.json is written before this):
the launcher and its sessions' bin\\hub.cmd carry its state folder, settings folder and ports; <state>\\cmd\\hub-<name>.cmd
is the terminal command (install.ps1 puts that folder into PATH), refused from a session of another hub. With --app
<state>\\app --shell: a second hub of the one-line install (`hub instance new`), the environment's variables after its own.
"""
import json
import ntpath
import os
import re
import sys

HELD = "stopped"   # core/restart.HELD: `hub stop` / `hub off` left the hub stopped; `hub on` and install.ps1 lift it


def default_config(state, app=None):
    cfg = {
        "hub": {"enabled": True, "name": "hub-core", "cwd": os.path.join(state, "hub-home")},  # "hub" may be taken
        "sets": [], "base": ["core:hub-base"], "hub_tools": ["core:hub-coordinator"],
        "mcp": {}, "connectors": False, "setting_sources": "project,local",
        # the IANA zone name cannot be learned on Windows without extra packages: "local" is the machine's local time (core/zones)
        "timezone": "local",
        "projects": {},   # a session without a project works in its own folder (core/workspace.py), not a shared one
    }
    if app:   # the app's own browser MCP and ffmpeg, as the .deb's first run names its own (tools/package/first_run.py)
        cfg.update({k: dict(v) for k, v in app_settings(app).items()})
    return cfg


def app_paths(app):
    """The app's programs as Windows paths, from the shell's map of them (paths.json of the factory — one source, so a
    change of the layout cannot leave this file behind)."""
    with open(os.path.join(app, "paths.json"), encoding="utf-8") as f:
        rel = json.load(f)
    return {k: ntpath.join(app, *v.split("/")) for k, v in rel.items() if isinstance(v, str)}


def app_settings(app):
    """What hub.json names in the app: the browser MCP (node with its script — Windows has no .cmd links of it) and
    ffmpeg; a build without one of them leaves its setting out."""
    p, out = app_paths(app), {}
    if "node" in p and "mcp" in p:
        out["browser"] = {"mcp": [p["node"], p["mcp"]]}
    if "ffmpeg" in p:
        out["voice"] = {"ffmpeg": p["ffmpeg"]}
    return out


class _Unchanged(Exception):
    pass


def refresh_config(path, repo, app):
    """An existing hub.json (a reinstall, a newer app): the app's own settings follow the app — browser.mcp and
    voice.ffmpeg that are unset, name only programs of this app folder, or a program that is gone (an app folder of
    before) become this build's; what the owner set otherwise stays. Written as the hub writes it (core/catalog: the
    lock, the file's permissions); nothing to change — nothing written. A hub.json that cannot be read stays as it is:
    the install goes on (the hub says what is wrong with it). True — changed."""
    sys.path.insert(0, repo)
    from core import catalog
    inside = ntpath.normcase(ntpath.join(app, ""))

    def ours(value):
        items = value if isinstance(value, list) else [value]
        if value is None:
            return True
        if not items or not all(isinstance(v, str) for v in items):
            return False
        # a program by its full path that is gone (an app folder of before); a bare name ("npx") is the owner's
        return all(ntpath.normcase(v).startswith(inside) for v in items) or (
            os.path.isabs(items[0]) and not os.path.exists(items[0]))

    try:
        with catalog.edit_settings(path) as cfg:
            changed = False
            for section, values in app_settings(app).items():
                have = cfg.setdefault(section, {})
                if not isinstance(have, dict):
                    continue   # not a section of settings: the owner's, leave it
                for key, value in values.items():
                    if ours(have.get(key)) and have.get(key) != value:
                        have[key], changed = value, True
            if not changed:
                raise _Unchanged
    except _Unchanged:
        return False
    except Exception as e:  # noqa: BLE001 — see above
        print(f"hub.json is not read ({e}) — leaving it as it is", file=sys.stderr)
        return False
    return True


def app_env(app, state):
    """(PATH folders before the machine's, after it, variables) — the app's environment for the hub and its sessions.
    Its git comes first: Claude Code's Bash needs its bash (S0-2: without one Claude Code falls back to PowerShell 5.1,
    and the hub's rules are written for bash) and its system config takes long paths. Python, Node, gh and ffmpeg come
    after the machine's own: a person's Python with their packages stays the `python` of their sessions, the app's is
    there when there is none; the hub itself names its programs by path (hub.json, HUB_GH). HUB_STATE_DIR is the app's
    state folder: one set in the person's environment (a hub from git) would send the app's hub and its commands there."""
    p = app_paths(app)
    return ([ntpath.dirname(p["git"])] if "git" in p else [],
            [ntpath.dirname(p[k]) for k in ("python", "node", "gh", "ffmpeg") if k in p],
            {"HUB_STATE_DIR": state,
             **{k: p[v] for k, v in (("CLAUDE_CODE_GIT_BASH_PATH", "bash"), ("HUB_GH", "gh")) if v in p}})


def env_lines(app, state):
    before, after, names = app_env(app, state)
    lines = [f'set "PATH={"".join(d + ";" for d in before)}%PATH%{"".join(";" + d for d in after)}"']
    lines += [f'set "{k}={v}"' for k, v in names.items()]
    return "".join(line + "\r\n" for line in lines)


def write_config(path, state, app=None, instance=None, repo=None):
    """hub.json with a hub session, if there is none yet. If there is — leave it (the owner's settings). True — written.
    An instance's gets its own part (core/instance.py settings: ports, browsers, update, engine)."""
    if os.path.exists(path):
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cfg = default_config(state, app)
    if instance:
        sys.path.insert(0, repo)
        from core import instance as inst
        cfg = inst.settings(cfg, inst.load(state), state, inst._settings(os.path.join(inst._default_config(),
                                                                                      "hub.json")))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return True


def instance_lines(state, config):
    """The variables of an instance for its .cmd files: its state folder, settings folder and ports (its anchor)."""
    with open(os.path.join(state, "instance.json"), encoding="utf-8") as f:
        ports = json.load(f).get("ports") or {}
    names = {"HUB_STATE_DIR": state, "HUB_CONFIG_DIR": config, "HUB_WEB_PORT": ports.get("web"),
             "HUB_FILES_PORT": ports.get("files")}
    return "".join(f'set "{k}={v}"\r\n' for k, v in names.items() if v)


def hub_home(repo, state):
    """CLAUDE.md and ONBOARDING.md of the hub session — from the repo, fresh after every update. A symbolic link on Windows
    needs administrator rights or developer mode; without them — a file with an @path import (Claude Code reads the
    imported file itself), not a copy that would go stale with the first update."""
    home = os.path.join(state, "hub-home")
    for d in (home, os.path.join(state, "logs"), os.path.join(state, "bin")):
        os.makedirs(d, exist_ok=True)
    for name in ("CLAUDE.md", "ONBOARDING.md"):
        src, dst = os.path.join(repo, "hub-home", name), os.path.join(home, name)
        if os.path.lexists(dst):
            os.remove(dst)
        try:
            os.symlink(src, dst)
        except OSError:
            with open(dst, "w", encoding="utf-8") as f:
                f.write(f"@{src.replace(os.sep, '/')}\n")


def scripts(repo, state, python, encoding=None, app=None, instance=None, config=None, shell=False, environ=None):
    """bin\\hub.cmd (the terminal command) and hub-up.cmd (the launcher for the scheduler, output goes to logs\\hub-up.log).
    cmd reads .cmd files in the console code page (OEM): we write in it too — otherwise a path with a non-Latin user name
    would not be found; a name outside it goes through a variable (cmd_text). The log is opened
    by hub-up itself (--log), not by cmd's `>>`: that does not open the file while someone else is writing to it — and hub-up did not start.
    app: both set the app's environment (env_lines); the launcher runs app.py launch before hub-up (its own log) —
    shell: start.py --prepare of the environment (the one-line install). The app's hub.cmd sets this hub's HUB_STATE_DIR
    over the caller's: from a session of another hub of the computer (a second hub's Codex session found it in the user's
    PATH, 2026-11-26) it refuses, as hub-<name>.cmd of an instance does (guard)."""
    encoding = encoding or _oem()
    for d in ("bin", "logs"):
        os.makedirs(os.path.join(state, d), exist_ok=True)
    hub_cmd = os.path.join(state, "bin", "hub.cmd")
    launcher = os.path.join(state, "hub-up.cmd")
    hub, hub_up = ntpath.join(repo, "bin", "hub"), ntpath.join(repo, "bin", "hub-up")   # Windows paths in .cmd
    log = ntpath.join(state, "logs", "hub-up.log")
    files = {hub_cmd: f'@"{python}" "{hub}" %*\r\n',
             launcher: (f'@echo off\r\ncd /d "{repo}"\r\n'
                        f'"{python}" "{hub_up}" --scheduled --log "{log}"\r\n')}
    if app:
        env = env_lines(app, state)
        # a hub stopped by the owner (core/restart.HELD) — the task's 5-minute repeat runs nothing of the app at all:
        # no watchdog resumed for a hub that is off on purpose, no program of the app held while Setup.exe replaces it
        held = ntpath.join(state, HELD)
        prepare = (f'"{python}" -I -B "{ntpath.join(app, "start.py")}" --prepare' if shell
                   else f'"{python}" "{ntpath.join(app, "app.py")}" launch')
        files = {hub_cmd: f'@echo off\r\nsetlocal\r\n{guard(state, unset=True)}{env}"{python}" "{hub}" %*\r\n',
                 launcher: (f'@echo off\r\nif exist "{held}" exit /b 0\r\n{env}'
                            f'{prepare}\r\ncd /d "{repo}"\r\n'
                            f'"{python}" "{hub_up}" --scheduled --log "{log}"\r\n')}
    if instance:
        env = instance_lines(state, config)
        held = ""
        prepare = ""
        if app and shell:   # a second hub of the one-line install: its variables, then the environment's, as the main's
            env += env_lines(app, state)
            held = f'if exist "{ntpath.join(state, HELD)}" exit /b 0\r\n'
            prepare = f'"{python}" -I -B "{ntpath.join(app, "start.py")}" --prepare\r\n'
        os.makedirs(os.path.join(state, "cmd"), exist_ok=True)
        files = {hub_cmd: f'@echo off\r\nsetlocal\r\n{env}"{python}" "{hub}" %*\r\n',
                 # from a session of another hub — refused: this hub's core would take it for the owner
                 os.path.join(state, "cmd", f"hub-{instance}.cmd"): (
                     f'@echo off\r\nsetlocal\r\n{guard(state)}{env}"{python}" "{hub}" %*\r\n'),
                 launcher: (f'@echo off\r\n{held}{env}{prepare}cd /d "{repo}"\r\n'
                            f'"{python}" "{hub_up}" --scheduled --log "{log}"\r\n')}
    for path, text in files.items():
        with open(path, "wb") as f:
            f.write(cmd_text(text, encoding, environ))
    return hub_cmd, launcher


def guard(state, unset=False):
    """The lines of a .cmd that end it with code 2 when a session (HUB_SESSION) of another hub runs it: the caller's
    HUB_STATE_DIR is not this hub's. unset: a session without HUB_STATE_DIR is this hub's (the main one, whose state
    folder is the default)."""
    return ('if not defined HUB_SESSION goto hub\r\n'
            + ('if not defined HUB_STATE_DIR goto hub\r\n' if unset else '')
            + f'if /i "%HUB_STATE_DIR%"=="{state}" goto hub\r\n'
            'echo this is another hub of this computer: its sessions are reached through the bus (hub bus) 1>&2\r\n'
            'exit /b 2\r\n:hub\r\n')


# the user's folders a .cmd may name through their variables (cmd expands them itself, in any letters)
FOLDERS = ("LOCALAPPDATA", "APPDATA", "USERPROFILE")


def cmd_text(text, encoding, environ=None):
    """The text of a .cmd in the console code page -> bytes. A path it cannot hold (a user named in letters outside it,
    a Cyrillic name on an English Windows) is written through %LOCALAPPDATA%, %APPDATA% or %USERPROFILE%; what is
    still outside it raises UnicodeEncodeError, as before."""
    try:
        return text.encode(encoding)
    except UnicodeEncodeError:
        pass
    env = os.environ if environ is None else environ
    for name in FOLDERS:
        value = (env.get(name) or "").rstrip("\\")
        if ntpath.isabs(value):   # where a path starts (a quote, set's =, PATH's ;); Windows paths: any case
            text = re.sub('(?<=["=;])' + re.escape(value + "\\"), lambda m, n=name: f"%{n}%\\", text,
                          flags=re.IGNORECASE)
    return text.encode(encoding)


def _oem():
    """The console code page (OEM): cp866 on Russian Windows. Not Windows — UTF-8 (tests)."""
    if os.name != "nt":
        return "utf-8"
    import ctypes
    return f"cp{ctypes.windll.kernel32.GetOEMCP()}"


if __name__ == "__main__":
    argv = sys.argv[1:]
    run = None
    if "--run" in argv[3:]:
        i = argv.index("--run", 3)
        run = argv[i + 1]
        del argv[i:i + 2]
    shell = "--shell" in argv[3:]
    if shell:
        argv.remove("--shell")
    repo, state, cfg = argv[0:3]
    rest = dict(zip(argv[3::2], argv[4::2]))   # --app <folder> and/or --instance <name> (both: the one-line install's)
    app, instance = rest.get("--app"), rest.get("--instance")
    hub_home(repo, state)
    if write_config(cfg, state, app, instance, run or repo):
        print("hub.json: written", file=sys.stderr)
    elif app and refresh_config(cfg, run or repo, app):
        print("hub.json: the paths of the app updated", file=sys.stderr)
    else:
        print("hub.json: already there — leaving it alone", file=sys.stderr)
    made = scripts(repo, state, sys.executable, app=app, instance=instance, config=os.path.dirname(cfg),
                   shell=shell and bool(app))
    # for a person to read: install.ps1 takes the paths from their place, and a folder outside the pipe's code page
    # (a non-Latin user name) must not fail the install after the files are written
    sys.stdout.reconfigure(errors="replace")
    print("\n".join(made))
