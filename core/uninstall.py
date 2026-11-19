"""`hub uninstall [--purge] [--yes] [--dry-run]` (installer spec §10): takes this hub off the machine the way the
install scripts put it on (deploy/): stops it, removes its OS service (a launchd agent, a systemd --user unit, the
Windows scheduler task and its PATH entry) and the terminal commands that lead to it. The data — the state folder
(journal, conversations of sessions, settings) — stays unless `--purge` is given, and then only after a second,
typed confirmation (or `--yes`).

Never touched: Claude Code and its sign-in (~/.claude — Anthropic's, the person's own), the code folder (a git copy may
hold the person's own work), another hub on the same machine (its service, its commands, its folders). The plan is
built first and shown; `--dry-run` stops there."""
import contextlib
import os
import re
import shutil
import stat
import subprocess
import sys
import time

import platforms
from core import appcode, i18n, instance, langdata, restart, shortcut, statemark

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMMANDS = ("hub", "hub-telegram")
# a hub's state folder: its journal (every line starts with the event's seq) or the folder the install made (hub-home
# with the hub's CLAUDE.md) — --purge refuses anything else (a wrong HUB_STATE_DIR that points at a project with a logs/
# folder, a state.json or a journal.jsonl of another program); the core start closes nothing else (core/perms.secure)
JOURNAL, JOURNAL_START = "journal.jsonl", b'{"seq":'
HUB_HOME = os.path.join("hub-home", "CLAUDE.md")


def _read(path, limit=4096):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def _same(a, b):
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except (OSError, ValueError):
        return False


def default_state(home):
    """The state folder a bare `hub` command leads to (the install wrote it), or None."""
    return statemark.default_state(home)


def _is_default(state, home):
    """This state folder is the one a bare `hub` leads to (the install's pointer; without one — the platform's default)."""
    default = default_state(home)
    return default is None and _same(state, platforms.current.state_dir()) or bool(default and _same(default, state))


def commands(state, repo, home):
    """The terminal commands of this hub in ~/.local/bin: a `hub-<name>` wrapper that names this state folder; the
    bare `hub`/`hub-telegram` only when this hub is the one they lead to (the default one) — a second hub on the same
    code must not take the first one's commands."""
    folder = os.path.join(home, ".local", "bin")
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    out = []
    # the code of the hub: this one, and a one-line hub's <state>/code (its wrapper names that, `hub uninstall` may run
    # through the layer's own path)
    ours = [os.path.join(p, "bin") + sep for p in (repo, os.path.join(state, "code")) for sep in {os.sep, "/"}]
    is_default = _is_default(state, home)
    # this state folder exactly: followed by a quote, a space, a ";" or the line's end (`set "HUB_STATE_DIR=…"` of a .cmd
    # too), not the start of a longer path
    named = re.compile(r"""HUB_STATE_DIR=["']?""" + re.escape(state) + r"""(?=["'\s;]|$)""", re.M)
    for name in names:
        if not (name in COMMANDS or name.startswith("hub-")):
            continue
        path = os.path.join(folder, name)
        if not os.path.isfile(path) or os.path.islink(path):
            continue
        text = _read(path)
        if not any(o in text for o in ours):
            continue   # someone else's program with a similar name
        names_state = "HUB_STATE_DIR=" in text
        if names_state and named.search(text):
            out.append(path)
        elif not names_state and name in COMMANDS and is_default:
            out.append(path)
    return out


def _wide(path, home):
    """The root, the home folder or a folder above it, the shared settings root (~/.config): never a hub's own."""
    real, top = os.path.realpath(path), os.path.realpath(home)
    shared = {os.path.realpath(p) for p in (os.sep, home, os.path.join(home, ".config"),
                                            os.path.dirname(platforms.current.config_dir()))}
    return real in shared or top.startswith(real + os.sep)


def marked(state):
    """The folder holds what only a hub leaves there (JOURNAL, HUB_HOME)."""
    path = os.path.join(state, JOURNAL)
    try:
        if stat.S_ISREG(os.stat(path).st_mode):   # not a pipe: open() would wait for its writer
            with open(path, "rb") as f:
                if f.read(len(JOURNAL_START)) == JOURNAL_START:
                    return True
    except OSError:
        pass
    return os.path.lexists(os.path.join(state, HUB_HOME))


def looks_like_hub(state, home):
    return not _wide(state, home) and marked(state)


# the first line of the `hub` left in place of the main hub's when it is removed and other hubs of the machine stay
# (decision 9.3 of the spec of several hubs: an instance keeps its name; `hub` only says which hubs there are); the
# main hub's install puts its own `hub` over it (deploy/commands.py STUB)
STUB = "agents-hub: the main hub of this computer is removed"


def WINDOWS():
    return platforms.current.APP_OS == "windows"


def stub_path(home, main_state):
    """Where the main hub's `hub` is, and so its stub: ~/.local/bin/hub; Windows — hub.cmd in the main hub's
    <state>\\bin (the folder install.ps1 put into PATH)."""
    if WINDOWS():
        return os.path.join(main_state, "bin", "hub.cmd")
    return os.path.join(home, ".local", "bin", "hub")


def is_stub(path):
    return STUB in _read(path, 512)


def stub_text(names, windows=None):
    """The stub: names the hubs that stay (their commands) and exits with code 1."""
    windows = WINDOWS() if windows is None else windows
    text = i18n.t("cli.uninstall.stub_says", hubs=", ".join(names))
    if windows:
        text = re.sub(r"([&|<>^])", r"^\1", text)
        return f"@echo off\r\nrem {STUB}\r\necho {text} 1>&2\r\nexit /b 1\r\n"
    return "#!/bin/sh\n# " + STUB + "\necho '" + text.replace("'", "'\\''") + "' >&2\nexit 1\n"


def others(state, registry=None):
    """The commands of the other hubs of the machine that stand (the registry, without this one and the gone)."""
    mine = instance.name(state)
    return [instance.command_name(e["name"]) for e in instance.entries(registry)
            if not e["gone"] and e["name"] != mine and not _same(e["state"], state)]


# --- the one-line install (its spec §8): what it adds besides a service and the commands ------------------------------
# <state>/layers/oneline.json — the install's mark, the one fact every place reads (its scripts, its setup.py, this):
# {"data": <data>}; it stays with the data, so the line takes such a hub as its own again. <state>/app — the hub's
# pointer to its environment; the environments are shared by the hubs of this user, in <data>/shells/
# (platforms.oneline_data), read-only; ~/.local/bin got into PATH by one marked line of the shell's profile
# (deploy/oneline/setup.py writes PATH_MARK and PATH_LINE; deploy/mac/link-commands.sh the same text)
MARK = os.path.join("layers", "oneline.json")
APP = "app"
PATH_MARK = "hub-core: ~/.local/bin"
PATH_LINE = 'export PATH="$HOME/.local/bin:$PATH"'
PROFILES = (".zshrc", ".bash_profile", ".bash_login", ".profile", ".bashrc")
SHELLS, DATA = "shells", "agents-hub"
LINGER = "linger.json"   # in <data>: the install turned linger on itself (deploy/oneline/setup.py)
DATA_NAMES = frozenset({SHELLS, "downloads", "install.log", LINGER})   # all <data> holds of its own, and install.lock*
GIT_WRAPPER = "git of the hub's environment"   # the Mac's <state>/bin/git (deploy/oneline/setup.py git_wrapper)
BUNDLE = "Agents Hub.app"   # the Mac's launcher copied from the layer into <state>
BUNDLES = (BUNDLE, "Hub by Icons8.app")   # ...the team's or the open flavor's (deploy/mac/app.py NAMES, tests/test_mac_app.py)
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/"
              "lsregister")


def _norm(path):
    return os.path.normcase(os.path.realpath(path))


def oneline(state):
    """The hub was put on by the one-line install: its mark, or (a hub the install put before it wrote one) its pointer
    into an environment of the install — a packaged app's hub has a code layer and a pointer too, into its app."""
    if os.path.isfile(os.path.join(state, MARK)):
        return True
    env = os.path.realpath(os.path.join(state, APP))
    return os.path.isdir(env) and os.path.basename(os.path.dirname(env)) == SHELLS \
        and os.path.basename(os.path.dirname(os.path.dirname(env))) == DATA


def mark_data(state):
    """The <data> folder the install's mark names, or None."""
    try:
        import json
        with open(os.path.join(state, MARK), encoding="utf-8") as f:
            got = json.load(f).get("data")
    except (OSError, ValueError, AttributeError):
        return None
    return got if isinstance(got, str) else None


def write_mark(state, data):
    """The install's mark (deploy/oneline/setup.py, each time it points the hub at its environment): written when it is
    missing or names another <data>."""
    import json
    if mark_data(state) == data:
        return
    path = os.path.join(state, MARK)
    tmp = f"{path}.{os.getpid()}.part"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps({"data": data}) + "\n")
    platforms.current.replace(tmp, path)


def data_dirs(state, home):
    """The install's <data> folders this hub may have used: the one its mark names, the one its pointer leads into
    (…/agents-hub/shells/<env>) and the OS's default one — each only by its own name."""
    out = []
    env = os.path.realpath(os.path.join(state, APP))
    for d in (mark_data(state), os.path.dirname(os.path.dirname(env))
              if os.path.basename(os.path.dirname(env)) == SHELLS else None):
        if d and os.path.basename(os.path.normpath(d)) == DATA and not any(_same(d, o) for o in out):
            out.append(d)
    default = platforms.current.oneline_data(home)
    if not any(_same(default, d) for d in out):
        out.append(default)
    return [d for d in out if os.path.isdir(d)]


def _service_states(home):
    """The state folders the hubs' services of this user name (launchd agents, systemd --user units: HUB_STATE_DIR —
    core/appmove.unit_env reads them as launchd and systemd do)."""
    import glob
    from core import appmove
    units = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "systemd", "user")
    found = [{"kind": "launchd", "path": p} for p in glob.glob(os.path.join(home, "Library", "LaunchAgents", "*.plist"))]
    found += [{"kind": "systemd", "path": p} for p in glob.glob(os.path.join(units, "*.service"))]
    return [s for s in (appmove.unit_env(u).get("HUB_STATE_DIR") for u in found) if s]


def other_states(state, home, registry=None):
    """The state folders of the other hubs of this user that may stand on an environment: the registry, the main hub,
    the instances next to it, the ones the services name."""
    found = [e["state"] for e in instance.entries(registry) if not e["gone"]]
    found += [statemark.default_state(home) or platforms.current.state_dir()]
    found += [e["state"] for e in instance.siblings()]
    found += _service_states(home)
    out = []
    for s in found:
        if s and os.path.isdir(s) and not _same(s, state) and not any(_same(s, o) for o in out):
            out.append(s)
    return out


def _paths_in(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _paths_in(v)
    elif isinstance(value, list):
        for v in value:
            yield from _paths_in(v)


def shells_in_use(state, home, registry=None):
    """The environments (real paths, normcase) the other hubs stand on: their pointer <state>/app, and any path their
    record of an environment update names (layers/shell-update.json: the next one, not switched to yet)."""
    out = set()
    for s in other_states(state, home, registry):
        link = os.path.join(s, APP)
        if os.path.lexists(link):
            out.add(_norm(link))
        try:
            import json
            with open(os.path.join(s, "layers", "shell-update.json"), encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        out |= {_norm(p) for p in _paths_in(rec) if os.path.isabs(p)}
    return out


def _install_running(data):
    """Another install line works on <data> now (its lock: a folder with the holder's pid on macOS and Linux, a file
    held open with no sharing on Windows — reading it is refused then). Changes nothing (--dry-run)."""
    lock = os.path.join(data, "install.lock")
    if not os.path.lexists(lock):
        return False
    if os.path.isdir(lock):
        m = re.match(r"pid=(\d+)", _read(os.path.join(lock, "owner"), 256))
        if not m:   # made a moment ago, its owner not written yet — or left by a run cut short long since
            try:
                return time.time() - os.lstat(lock).st_mtime < 600
            except OSError:
                return False
        return platforms.current.alive(int(m.group(1)))
    try:
        with open(lock, "rb"):
            return False
    except PermissionError:
        return True
    except OSError:
        return False


def shell_steps(state, home, registry=None):
    """The environments nobody else stands on: each, or the whole <data> when no environment there is in use and it
    holds only the install's own files (the downloads, the log)."""
    steps, used = [], shells_in_use(state, home, registry)
    for data in data_dirs(state, home):
        if _install_running(data):
            steps.append({"do": "keep", "what": i18n.t("cli.uninstall.shells_busy", path=data)})
            continue
        folder = os.path.join(data, SHELLS)
        try:
            shells = sorted(os.listdir(folder))
        except OSError:
            shells = []
        free = [os.path.join(folder, n) for n in shells if _norm(os.path.join(folder, n)) not in used]
        try:
            names = set(os.listdir(data))
        except OSError:
            continue
        if len(free) == len(shells) and all(n in DATA_NAMES or n.startswith("install.lock") for n in names):
            linger = os.path.join(data, LINGER)
            if os.path.isfile(linger) and platforms.current.APP_OS == "linux" \
                    and not [s for s in _service_states(home) if not _same(s, state)]:
                user = os.environ.get("USER") or os.path.basename(home)
                steps.append({"do": "run", "argv": ["loginctl", "disable-linger", user],
                              "what": i18n.t("cli.uninstall.linger", user=user)})
            steps.append({"do": "shell", "path": data, "state": state, "what": i18n.t("cli.uninstall.shells", path=data)})
            continue
        for path in free:
            steps.append({"do": "shell", "path": path, "state": state, "what": i18n.t("cli.uninstall.shell", path=path)})
        if len(free) < len(shells):
            steps.append({"do": "keep", "what": i18n.t("cli.uninstall.shells_kept", path=folder)})
    return steps


def profile_steps(home, gone):
    """The marked PATH line of the shell's profiles, when ~/.local/bin holds nothing more once `gone` (the commands this
    uninstall removes) are away: Claude Code's `claude` and the person's own programs there keep the line."""
    folder = os.path.join(home, ".local", "bin")
    try:
        left = [n for n in os.listdir(folder) if not any(_same(os.path.join(folder, n), g) for g in gone)]
    except OSError:
        left = []
    marked = [os.path.join(home, n) for n in PROFILES if PATH_MARK in _read(os.path.join(home, n), 1 << 20)]
    if not marked:
        return []
    if left:
        return [{"do": "keep", "what": i18n.t("cli.uninstall.profile_kept", path=folder, names=", ".join(sorted(left)[:5]))}]
    return [{"do": "profile", "path": p, "what": i18n.t("cli.uninstall.profile", path=p)} for p in marked]


def drop_profile_line(path):
    """The install's two lines (the mark's comment and the export after it, with the empty line before them) out of
    this profile — through a link into the file it leads to; the rest as it was (a last line the install's newline
    ended keeps it). A profile left empty was the install's own: it goes (an empty ~/.bash_profile would hide
    ~/.profile from bash). -> True when changed."""
    real = os.path.realpath(path)
    with open(real, encoding="utf-8", errors="surrogateescape", newline="") as f:
        lines = f.read().splitlines(keepends=True)
    out, i, changed = [], 0, False
    while i < len(lines):
        if lines[i].lstrip().startswith("#") and PATH_MARK in lines[i] and i + 1 < len(lines) \
                and lines[i + 1].strip() == PATH_LINE:
            if out and not out[-1].strip():
                out.pop()
            i, changed = i + 2, True
            continue
        out.append(lines[i])
        i += 1
    if not changed:
        return False
    text = "".join(out)
    if not text.strip():
        os.remove(real)
        return True
    tmp = f"{real}.{os.getpid()}.hub-tmp"
    with open(tmp, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write(text)
    os.chmod(tmp, stat.S_IMODE(os.stat(real).st_mode))
    os.replace(tmp, real)
    return True


LATER = ".agents-hub-removing"   # in a folder left to platforms.remove_later: it deletes only a folder that holds it


def waits_for():
    """The processes a folder left for later waits for: this one, and the cmd.exe that runs it through bin\\hub.cmd (it
    reads that file once more at the end) — not an interactive console it runs in."""
    pids, parent = [os.getpid()], os.getppid()
    if re.search(r"\\bin\\hub[^\\]*\.cmd", platforms.current.command_line(parent) or "", re.I):
        pids.append(parent)
    return pids


def defer(paths, pids=None):
    """The folders that go once this command has ended (platforms.remove_later): each marked first, so a folder a new
    install puts at the same path meanwhile is not taken."""
    for path in paths:
        with open(os.path.join(path, LATER), "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()}\n")
    platforms.current.remove_later(paths, waits_for() if pids is None else pids)


def oneline_steps(state, home, purge):
    """(before, after): the Mac launcher's registration and the menu shortcut (core/shortcut.py: this hub's own, a
    .desktop file of ours or the Windows .lnk, and its icon) go before the folders; after the rest — the hub's own
    links through its pointer (without --purge: --purge takes the state folder whole), the pointer, then the
    environments."""
    before, after = [], []
    anchor = instance.load(state)
    files = shortcut.candidates(platforms.current.APP_OS, anchor["name"] if anchor else "", home)
    for p in files:
        if os.path.lexists(p) and shortcut.ours(p, os.path.join(state, "bin", "hub.cmd")):
            before.append({"do": "remove", "path": p, "what": i18n.t("cli.uninstall.file", path=p)})
    if files and files[0].endswith(".lnk"):   # the icon the install made from the picture of the code layer
        p = shortcut.icon_path(state)
        if os.path.lexists(p):
            before.append({"do": "remove", "path": p, "what": i18n.t("cli.uninstall.file", path=p)})
    for bundle in (os.path.join(state, n) for n in BUNDLES):
        if platforms.current.APP_OS == "mac" and os.path.isdir(bundle):
            before.append({"do": "run", "argv": [LSREGISTER, "-u", bundle], "ok": tuple(range(1, 256)),
                           "what": i18n.t("cli.uninstall.bundle", path=bundle)})
            if not purge:
                after.append({"do": "tree", "path": bundle, "what": i18n.t("cli.uninstall.file", path=bundle)})
    if not purge:
        app = os.path.join(state, APP)
        for name in ("python3", "git"):
            p = os.path.join(state, "bin", name)
            if os.path.islink(p) and os.readlink(p).startswith(os.path.join(app, "")) or \
                    name == "git" and os.path.isfile(p) and GIT_WRAPPER in _read(p, 512):
                after.append({"do": "remove", "path": p, "what": i18n.t("cli.uninstall.file", path=p)})
        if os.path.lexists(app):
            after.append({"do": "pointer", "path": app, "what": i18n.t("cli.uninstall.pointer", path=app)})
    return before, after


def plan(state, config_dir, home=None, repo=REPO, unit=None, purge=False, registry=None, copies=True):
    """[step]: {"do": run | remove | path | tree | stub | unstub | unregister | halt | profile | pointer | shell | keep,
    "argv"/"path", "what"}; unit — platforms.service_unit(state). A hub of the one-line install also loses its pointer
    to the environment and the environments no other hub stands on (oneline_steps, shell_steps)."""
    home = home or os.path.expanduser("~")
    steps = []
    hub = [sys.executable, os.path.join(repo, "bin", "hub")]
    if unit:
        steps += platforms.current.uninstall_service(unit, hub)
    # whatever the service did: a hub-up of this state folder that still runs (no service — a container, a terminal; a
    # unit no service manager runs; the Windows task, which only starts it) is stopped and waited for, so its data is
    # not deleted while it writes its snapshot; one that is already down is no failure
    steps.append({"do": "halt", "path": state, "service_off": bool(unit), "what": i18n.t("cli.uninstall.stop")})
    line = oneline(state)
    before, after = oneline_steps(state, home, purge) if line else ([], [])
    steps += before
    anchor, rest = instance.load(state), others(state, registry)
    if anchor and copies:   # its executors' working copies in the repos: while its state still tells where they are
        steps.append({"do": "copies", "state": state, "name": anchor["name"], "what": i18n.t("cli.uninstall.copies")})
    if anchor:   # before the folders go: with --purge the code that runs this lies in them
        steps.append({"do": "unregister", "name": anchor["name"], "folder": registry,
                      "what": i18n.t("cli.uninstall.registry", name=anchor["name"])})
    main = not anchor and _is_default(state, home)
    stub = stub_path(home, state if main else instance.main_state())
    # the main hub goes and other hubs stay: its `hub` becomes the stub that names them (on Windows its <state>\bin
    # stays in PATH for it); the last instance takes a stub away, another one rewrites it without itself
    keep_stub = bool(main and rest)
    for s in platforms.current.uninstall_path(state):
        if not (keep_stub and _same(s["path"], os.path.dirname(stub))):
            steps.append(s)
    cmds = commands(state, repo, home)
    for path in cmds:
        if not (keep_stub and _same(path, stub)):
            steps.append({"do": "remove", "path": path, "what": i18n.t("cli.uninstall.command", path=path)})
    pointers = statemark.pointers(home)   # the install wrote one of them; a hub moved by hand may have both
    if purge:
        # its Codex sign-ins first (the spec of several hubs, 2.6): the sign-out Codex itself does ("Sign out" in
        # "Settings -> Codex"), then the folder that holds them goes; a failed sign-out does not keep the data
        for folder in codex_homes(state):
            steps.append({"do": "codex_logout", "path": state, "home": folder,
                          "what": i18n.t("cli.uninstall.codex_logout", path=folder)})
        if os.path.isdir(state):
            # Windows: cmd reads bin\hub.cmd, which runs this, again when the command ends ("The batch file cannot be
            # found") — that folder goes after it (platforms.remove_later)
            keep = "bin" if WINDOWS() and os.path.isfile(os.path.join(state, "bin", "hub.cmd")) else None
            steps.append({"do": "tree", "path": state, "keep": keep, "what": i18n.t("cli.uninstall.state", path=state)})
        # the default settings folder belongs to the default hub: a second hub run without HUB_CONFIG_DIR names it too
        standard = any(_same(config_dir, os.path.dirname(p)) for p in pointers) \
            or _same(config_dir, platforms.current.config_dir())
        theirs = standard and not _is_default(state, home)
        # another folder (HUB_CONFIG_DIR) is the hub's by its hub.json in it, not by its name
        if os.path.isdir(config_dir) and not _same(config_dir, state) and not theirs and not _wide(config_dir, home) \
                and (standard or os.path.exists(os.path.join(config_dir, "hub.json"))):
            steps.append({"do": "tree", "path": config_dir, "what": i18n.t("cli.uninstall.config", path=config_dir)})
        for pointer in pointers:
            if _same(_read(pointer).strip() or "/nonexistent", state) and not _same(os.path.dirname(pointer), config_dir):
                steps.append({"do": "remove", "path": pointer, "what": i18n.t("cli.uninstall.command", path=pointer)})
    # another program's `hub` stays as it is (deploy/commands.py)
    if keep_stub and (WINDOWS() or not os.path.lexists(stub) or is_stub(stub) or stub in cmds):
        steps.append({"do": "stub", "path": stub, "text": stub_text(rest, stub.lower().endswith(".cmd")),
                      "what": i18n.t("cli.uninstall.stub", path=stub)})
    elif anchor and is_stub(stub):
        if rest:
            steps.append({"do": "stub", "path": stub, "text": stub_text(rest, stub.lower().endswith(".cmd")),
                          "what": i18n.t("cli.uninstall.stub", path=stub)})
        else:
            steps.append({"do": "unstub", "path": stub, "what": i18n.t("cli.uninstall.unstub", path=stub)})
    if main and not keep_stub:   # the PATH line was written for this hub's commands
        steps += profile_steps(home, cmds)
    if line:   # last: the Python this runs on lies there (its modules are loaded by now)
        steps += after + shell_steps(state, home, registry)
        # the folders the install made for its places, when nothing else is left in them (not said: nothing of the hub)
        steps.append({"do": "prune", "paths": [os.path.join(home, *p) for p in (
            (".local", "bin"), (".local", "share", "applications"), (".local", "share"), (".local",),
            (".config", "systemd", "user"), (".config", "systemd"))]})
    return steps


def codex_homes(state, depth=4):
    """The Codex folders of this hub with a sign-in in them: <state>/codex-home (a hub on Codex, or a Claude hub's own
    Codex sign-in for its critic and voice) and the Codex account pool's folders below <state>/moddata (a folder with
    the core's config.toml and an auth.json); links are not followed."""
    out = []
    hub = os.path.join(state, "codex-home")
    if os.path.isfile(os.path.join(hub, "auth.json")):
        out.append(hub)
    base = os.path.join(state, "moddata")
    for root, dirs, files in os.walk(base, onerror=lambda e: None):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        if root[len(base):].count(os.sep) >= depth:
            dirs[:] = []
        if "auth.json" in files and "config.toml" in files:
            out.append(root)
    return out


def drop_copies(state, name):
    """An instance's executors' working copies and merged branches in the repos (hosts/worktree.drop_space), under its own
    mark and the repos' file locks; what is not removed (uncommitted changes, commits merged nowhere, a repo another
    hub's git holds) stays and is named. -> (the summary, the lines of what stayed)."""
    from core import journal
    from hosts import worktree
    sessions = journal.load_state(state).get("sessions") or {}
    infos = [s["worktree"] for s in sessions.values() if isinstance(s, dict) and isinstance(s.get("worktree"), dict)]
    owner = {"state": os.path.realpath(state), "hub": instance.command_name(name)}
    got = worktree.drop_space(name, infos, owner)
    # the reason of a refusal usually names the path itself
    kept = [i18n.t("cli.uninstall.copy_kept", why=why if path in why else f"{path}: {why}") for path, why in got["kept"]]
    kept += [i18n.t("cli.uninstall.branch_kept", branch=branch, repo=repo) for repo, branch in got["branches"]]
    kept += [i18n.t("cli.uninstall.repo_busy", repo=repo) for repo in got["busy"]]
    summary = i18n.t("cli.uninstall.copies_done", count=len(got["removed"])) if got["removed"] \
        else i18n.t("cli.uninstall.copies_none")
    return summary, kept


def stop_codex_login(state):
    """The page's Codex sign-in still waiting for its code runs apart from hub-up (it outlives the hub's stop): stopped
    with the hub — on Windows its codex.exe holds the hub's folder. Whatever goes wrong here does not stop the rest."""
    try:
        from core import codexcli
        codexcli.stop_login(state)
    except Exception:  # noqa: BLE001 — at worst the sign-in's process ends by itself when its code expires
        pass


def stop_runs(state):
    """The hub's `hub run` commands live apart from it (core/runjobs.py): they stop with the hub that goes, not at
    their term. Whatever goes wrong here does not stop the rest (a run ends at its term by itself)."""
    try:
        from core import runjobs
        runjobs.stop_all(state)
    except Exception:  # noqa: BLE001
        pass


def codex_logout(state, home):
    """account/logout of Codex in that folder (core/codexcli, the same as "Sign out" in "Settings -> Codex")."""
    from core import codexcli
    try:
        codexcli.logout_home(state, home)
    except FileNotFoundError as e:   # no codex program: not "already gone" — the sign-out did not happen
        raise RuntimeError(str(e)) from None


def _zombie(pid):
    """Linux: the process has ended and waits for its parent to read its exit (a hub-up the install line started in a
    container without an init: nobody ever does) — it is gone for this. Elsewhere: no."""
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as f:
            return f.read().rsplit(")", 1)[1].split()[0] == "Z"
    except (OSError, ValueError, IndexError):
        return False


HALT_WAIT = 90   # s: longer than hub-up's own stop (the core's snapshot; platforms stop_grace, the unit's TimeoutStopSec)


def halt(state, service_off=False, wait=None):
    """Stop this hub's hub-up as `hub stop` does (core/restart: the marks, so the scheduler's retry does not bring it up
    again) and wait until it has gone, and the supervisor that started it (Windows: it holds the log open). Not running:
    nothing to do. A hub-up its service holds (launchd, systemd would start it again) stops only with the service:
    service_off — the plan's earlier step took it off, so it is only waited for."""
    up = restart.find_up(state, platforms.current.restart_gap())
    if not up:
        return
    if up.get("service") and not service_off:
        raise OSError(i18n.t("cli.uninstall.service_holds", service=up["service"]))
    restart.ask_stop(state)
    procs = [(p, platforms.current.process_start(p)) for p in (up["pid"], up.get("parent"))
             if isinstance(p, int) and platforms.current.alive(p)]

    def left():   # by pid and start time: no command line on every poll (PowerShell on Windows takes seconds)
        return [p for p, born in procs if platforms.current.alive(p) and platforms.current.process_start(p) == born
                and not _zombie(p)]
    until = time.time() + (HALT_WAIT if wait is None else wait)
    while left() and time.time() < until:
        time.sleep(0.2)
    if left():
        raise OSError(i18n.t("cli.uninstall.still_up", pid=up["pid"]))


def _rmtree(path, tries=5):
    """platforms.rmtree, again for a few seconds: on Windows a file a process that has just exited held open (the log)
    may still be shared for a moment. -> True when something is left."""
    for i in range(tries):
        if not platforms.current.rmtree(path):
            return False
        if i < tries - 1:
            time.sleep(1)
    return True


def write_stub(path, text):
    """The stub at the main hub's `hub` (stub_text): whole, a temp file next to it renamed over it."""
    windows = path.lower().endswith(".cmd")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.hub-tmp"
    encoding = platforms.current.console_encoding() if windows and hasattr(platforms.current, "console_encoding") \
        else "utf-8"
    with open(tmp, "w", encoding=encoding, errors="replace", newline="") as f:
        f.write(text)
    if not windows:
        os.chmod(tmp, 0o755)
    platforms.current.replace(tmp, path)


_WORDS = {}   # the words execute() may need, rendered before the steps: --purge takes the catalog away with the code


def _word(key, **params):
    text = _WORDS.get(key)
    if text is None:
        return i18n.t("cli.uninstall." + key, **params)
    for k, v in params.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def execute(steps, run=subprocess.run, say=print):
    """Each step on its own: a failure is said and the rest goes on — except after a hub that would not stop: nothing
    more is done, no data is deleted under it and its `hub` command stays to stop it with. -> the number of failed
    steps."""
    bad, still_up, service_failed, after, later = 0, False, False, [], []
    for key, params in (("left", ()), ("kept_running", ()), ("shell_busy", ("pid",)), ("later", ("path",)),
                        ("shells_busy", ("path",))):
        _WORDS[key] = i18n.t("cli.uninstall." + key, **{p: "{" + p + "}" for p in params})
    # the program this runs on, while its path still resolves: on Windows it goes through the junction <state>\app,
    # which an earlier step takes away — and with it the standard library: what a later step loads lazily, now
    runs_on = _norm(sys.executable)
    pids = None
    if WINDOWS() and any(s["do"] == "shell" or s.get("keep") for s in steps):
        import base64  # noqa: F401 — platforms.windows.remove_later
        for codec in ("utf-16-le", "oem"):   # remove_later's script, command_line's answer
            with contextlib.suppress(LookupError):
                "".encode(codec)
        pids = waits_for()
    for s in steps:
        if s["do"] == "keep":
            continue
        if s["do"] == "prune":
            for p in s["paths"] if not still_up else ():
                with contextlib.suppress(OSError):
                    os.rmdir(p)
            continue
        try:
            if still_up:
                raise OSError(_word("kept_running"))
            if s["do"] == "run":
                r = run(s["argv"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                        stdin=subprocess.DEVNULL, timeout=120)
                if r.returncode not in (0, *s.get("ok", ())):
                    raise OSError(((r.stderr or "") + (r.stdout or "")).strip()[-300:] or f"exit {r.returncode}")
            elif s["do"] == "remove":
                os.remove(s["path"])
            elif s["do"] == "tree" and s.get("keep"):
                left = [os.path.join(s["path"], n) for n in os.listdir(s["path"]) if n != s["keep"]]
                if [p for p in left if (_rmtree(p) if os.path.isdir(p) and not appcode.is_pointer(p) else _remove(p))]:
                    raise OSError(_word("left"))
                later.append(s["path"])
            elif s["do"] == "tree":
                if _rmtree(s["path"]):
                    raise OSError(_word("left"))
            elif s["do"] == "path":
                platforms.current.drop_user_path(s["path"])
            elif s["do"] == "codex_logout":
                codex_logout(s["path"], s["home"])
            elif s["do"] == "copies":
                done, after = drop_copies(s["state"], s["name"])
                s = {**s, "what": done}
            elif s["do"] == "unregister":
                instance.unregister(s["name"], s.get("folder"))
            elif s["do"] == "stub":
                write_stub(s["path"], s["text"])
            elif s["do"] == "unstub":
                if is_stub(s["path"]):
                    os.remove(s["path"])
                    if WINDOWS():   # the main hub's <state>\bin went into PATH for the stub only
                        platforms.current.drop_user_path(os.path.dirname(s["path"]))
                        for d in (os.path.dirname(s["path"]), os.path.dirname(os.path.dirname(s["path"]))):
                            try:
                                os.rmdir(d)
                            except OSError:
                                break
            elif s["do"] == "profile":
                drop_profile_line(s["path"])
            elif s["do"] == "pointer":
                if _remove(s["path"]):
                    raise OSError(_word("left"))
            elif s["do"] == "shell":
                rest = remove_shell(s["path"], s.get("state"), runs_on)
                if rest:
                    later.append(rest)
                    s = {**s, "what": _word("later", path=s["path"])}
            elif s["do"] == "halt":
                try:
                    # a service step that failed may have left the service on: its hub-up is not stopped behind it
                    halt(s["path"], service_off=s.get("service_off", False) and not service_failed)
                except OSError:
                    still_up = True
                    raise
                stop_codex_login(s["path"])
                stop_runs(s["path"])
            say("  ✓ " + s["what"])
            for line in after:
                say("      " + line)
            after = []
        except FileNotFoundError:
            say("  ✓ " + s["what"])
        except (OSError, subprocess.SubprocessError, RuntimeError) as e:   # RuntimeError: codex app-server refused
            bad += 1
            service_failed = service_failed or bool(s.get("service"))
            say("  ✗ " + s["what"] + f": {e}")
    if later:
        try:
            defer(later, pids)
        except (OSError, LookupError, ImportError) as e:
            bad += 1
            say("  ✗ " + _word("later", path=", ".join(later)) + f": {e}")
    return bad


def _writable_dirs(path):
    """Write rights back on the folders of a read-only tree (the environment), so its files can go; links are not
    followed. Windows has no such right on a folder: its files' read-only attribute is platforms.rmtree's."""
    for d, dirs, _ in os.walk(path):
        for x in [d] + [os.path.join(d, n) for n in dirs]:
            try:
                st = os.lstat(x)
                if stat.S_ISDIR(st.st_mode) and not st.st_mode & stat.S_IWUSR:
                    os.chmod(x, stat.S_IMODE(st.st_mode) | stat.S_IRWXU)
            except OSError:
                pass


def _ours(pid, state):
    """The process is of the hub in state: its command line names a path inside the state folder (a setup step of the
    hub, `…/.hub-core/layers/<v>/modules/setup/job.py <state> login`, outlives hub-up by design)."""
    line = platforms.current.command_line(pid)
    return any(os.path.join(p, "") in line for p in {state, os.path.realpath(state)})


def _remove(path):
    """A file or a link (a junction too: removed without what it leads to) away. -> True when it is still there."""
    try:
        (os.rmdir if getattr(os.path, "isjunction", lambda p: False)(path) else os.remove)(path)
    except FileNotFoundError:
        pass
    except OSError:
        return True
    return False


def remove_shell(path, state=None, runs_on=None):
    """An environment (or the whole <data>) away. What of this hub still runs from it is stopped; one a process of
    another hub runs from stays (macOS and Linux look for one). Windows: the one this command runs on (runs_on: its real
    path) goes after it — its python.exe is held open; another one is renamed aside first (a folder another program
    holds is refused) and what of it a just-ended program still holds goes after this command too.
    -> the folder left to platforms.remove_later, or None."""
    if not os.path.lexists(path):
        return None
    data = path if os.path.basename(path) == DATA else os.path.dirname(os.path.dirname(path))
    if _install_running(data):   # an install line began while the person read the plan
        raise OSError(_word("shells_busy", path=data))
    if not WINDOWS():
        pids = platforms.current.processes_in(path)
        mine = [p for p in pids if state and _ours(p, state)]
        if mine:
            platforms.current.stop_processes(mine)
            pids = platforms.current.processes_in(path)
        for _ in range(10):   # the hub's own parts (its MCP's node) may take a moment after hub-up has gone
            if not pids:
                break
            time.sleep(1)
            pids = platforms.current.processes_in(path)
        if pids:
            raise OSError(_word("shell_busy", pid=pids[0]))
        _writable_dirs(path)
        if _rmtree(path, tries=2):
            raise OSError(_word("left"))
        return None
    if (runs_on or _norm(sys.executable)).startswith(_norm(path) + os.sep):
        return path
    aside = f"{path}.removing-{os.getpid()}"
    try:
        os.rename(path, aside)
    except OSError as e:
        raise OSError(_word("shell_busy", pid="?")) from e
    return aside if _rmtree(aside, tries=3) else None


def main(argv, state, config_dir, unit, ask=input, tty=None):
    purge, yes, dry = "--purge" in argv, "--yes" in argv, "--dry-run" in argv
    keep = "--keep-copies" in argv
    rest = [a for a in argv if a not in ("--purge", "--yes", "--dry-run", "--keep-copies")]
    if rest:
        print(i18n.t("cli.uninstall.usage"), file=sys.stderr)
        return 2
    home = os.path.expanduser("~")
    if purge and not looks_like_hub(state, home):
        print(i18n.t("cli.uninstall.not_a_hub", path=state), file=sys.stderr)
        return 2
    steps = plan(state, config_dir, home=home, unit=unit, purge=purge, copies=not keep)
    print(i18n.t("cli.uninstall.plan", path=state))
    for s in steps:
        if s.get("what"):
            print("  · " + s["what"])
    inside = os.path.realpath(REPO).startswith(os.path.realpath(state) + os.sep)   # an instance's own clone, a layer
    line = oneline(state)
    print(i18n.t("cli.uninstall.keeps_purge_own") if purge and inside else
          i18n.t("cli.uninstall.keeps_oneline", path=state) if line and not purge else
          i18n.t("cli.uninstall.keeps" if not purge else "cli.uninstall.keeps_purge", path=state, repo=REPO))
    if dry:
        return 0
    tty = sys.stdin.isatty() if tty is None else tty
    if not yes:
        if not tty:
            print(i18n.t("cli.uninstall.need_yes"), file=sys.stderr)
            return 2
        if ask(i18n.t("cli.uninstall.confirm")).strip().lower() not in ("y", "yes", *langdata.words("words", "yes")):
            print(i18n.t("cli.uninstall.cancelled"))
            return 1
        if purge and ask(i18n.t("cli.uninstall.confirm_purge", name=os.path.basename(os.path.normpath(state)))) \
                .strip() != os.path.basename(os.path.normpath(state)):
            print(i18n.t("cli.uninstall.cancelled"))
            return 1
    # the words of the end before the steps: a one-line hub's Python and its code go with them
    done = i18n.t("cli.uninstall.done_oneline" if line else "cli.uninstall.done")
    partly = i18n.t("cli.uninstall.partly", n="{n}")
    bad = execute(steps)
    print(done if not bad else partly.replace("{n}", str(bad)))
    return 1 if bad else 0
