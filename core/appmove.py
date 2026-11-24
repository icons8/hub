"""The move of a hub from a git clone into the app (app spec §8): the app's installer finds the hub its user runs from
a clone, asks "Move / Stay on the clone / Later", and on "Move" the hub goes on in the same state folder on the app's
code layer. Sessions, the journal, the settings, plugins and the hub's own modules stay; the clone stays on the disk as
it was, the hub's own edits of the core get a backup branch in it (modules/update/takeoff.keep_copy); `hub app undo`
(and "About the hub" for UNDO_DAYS) puts the service back on the clone.

This is the code of the release being installed: the shell (deploy/mac/appshell.py, tools/package/windows/app.py, the
.deb's first_run.py) runs its `bin/hub app …` and only shows the dialog and hands the service over the way it installs
itself. The steps of `hub app move <clone>` (prepare), with what the hub ran from saved before anything changes:
1. wait while sessions are in a turn or run work in the background (restart.blockers, the restart's ceiling);
2. stop the clone's hub (its service off; Windows: the soft stop, the task does not bring it back);
3. save what the app's install rewrites — the service's file, the terminal commands, the hub session's rules links,
   hub.json (and hub.json.bak-app next to it) — into <state>/app-move/;
4. the backup branch of the hub's own edits in the clone;
5. hub.json: the programs the app now gives (browser.mcp, voice.ffmpeg) when they name the clone, Homebrew, a
   program that is gone, or the clone installer's default; any other path to a file inside the clone that the code
   layer has too (an MCP server's bin/…, a hook) goes to <state>/code/…; the rules links of the hub session's folder when
   hub.json puts it outside <state>/hub-home (that folder the shell links) likewise; the rest stays;
6. Linux: the clone's user unit off (it would override the package's unit of the same name), the commands to the app.
Then the shell installs its service and waits for the page: `hub app move --done`, or `hub app undo --failed`.
A second hub of the machine from git (an installer run with HUB_INSTANCE: ~/.hub-<name>, its own settings folder in
its service) moves as itself: its anchor (core/instance.py) names its settings folder — written now from its service
when it stands from before anchors, as hub-up would adopt it — and the layer as its code; its service names, its
commands hub-<name>; the main hub's service, commands and settings are never touched.
Undo: the app's hub stopped, every saved file back as it was (what was absent goes), the clone's service on again.
The private modules need nothing here: the app's update step removes the ones the update module installed
(modules/update/private.py, the retired pin)."""
import json
import os
import re
import shutil
import subprocess
import sys
import time

DIR = "app-move"
RECORD = "record.json"
STAY = "stay.json"
UNDO_DAYS = 14
VERSION = 1
STOP_WAIT = 120       # s for a hub-up to be gone after its service is off (launchd and systemd kill it at 75 s)
POLL = 10             # s between looks at the busy sessions
CONFIG_BACKUP = "hub.json.bak-app"
LABEL = "com.hub-core"
UNIT = "agents-hub.service"
TASK = "Agents Hub"
DEB = "/opt/agents-hub"   # the .deb's folder (tools/package/deb): its programs, its python
BREW = ("/opt/homebrew/", "/usr/local/Cellar/", "/usr/local/opt/")   # and Linuxbrew: a path through /.linuxbrew/
COMMANDS = ("hub", "hub-telegram")
LAYER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # this release's code: what <state>/code will hold
RULES = ("CLAUDE.md", "ONBOARDING.md")   # the hub session's rules: links in its folder to the code's hub-home
ONELINE_ENV = ".agents-hub-shell.json"   # the mark of an environment of the one-line install (deploy/oneline/unpack.py)
# variables of a service that put the hub's settings elsewhere (or may: an environment file, a line not understood):
# such a hub is not offered — the move reads and the app's hub uses the standard settings folder. One exception: an
# instance's HUB_CONFIG_DIR that its anchor names (or would name: instance.adoptable) — the app's hub finds it there
OWN_SETTINGS = ("HUB_CONFIG", "HUB_CONFIG_DIR", "XDG_CONFIG_HOME", "EnvironmentFile", "Environment")


class Refused(Exception):
    """Nothing was changed: what is in the way, for a person."""


class NotBack(Refused):
    """The move failed and the clone's service did not start again: the record stays the move's ("moving", with
    "returning": `move --done` never takes it for a move that came up) — `hub app undo` starts it."""


def _os(osname=None):
    import platforms
    return osname or platforms.current.APP_OS


def home():
    return os.path.expanduser("~")


def folder(state):
    return os.path.join(state, DIR)


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write(path, data):
    from core import catalog
    os.makedirs(os.path.dirname(path), exist_ok=True)
    catalog.write_private(path, data)


def record(state):
    return _read(os.path.join(folder(state), RECORD))


# --- where the clone is: the service that runs this state folder's hub-up ---

def _a_clone(repo):
    """A git clone of the core: its own .git (a folder, or a file of a worktree) and bin/hub-up."""
    return bool(repo) and os.path.lexists(os.path.join(repo, ".git")) and os.path.isfile(os.path.join(repo, "bin", "hub-up"))


def _repo_of(script, sep="/"):
    """<repo>/bin/hub-up -> <repo>."""
    if not script.replace("\\", "/").endswith("/bin/hub-up"):
        return None
    return script[:-len("bin/hub-up") - 1]


def repo_from_plist(path):
    import plistlib
    from xml.parsers.expat import ExpatError
    try:
        with open(path, "rb") as f:
            agent = plistlib.load(f)
    except (OSError, ValueError, plistlib.InvalidFileException, ExpatError):
        return None
    args = [str(a) for a in agent.get("ProgramArguments") or []] if isinstance(agent, dict) else []
    return next((r for r in map(_repo_of, args) if r), None)


def repo_from_unit(path):
    import shlex
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        if line.startswith("ExecStart="):
            try:
                words = shlex.split(line[len("ExecStart="):])
            except ValueError:
                return None
            return next((r for r in map(_repo_of, words) if r), None)
    return None


def _encoding():
    import platforms
    return platforms.current.console_encoding()


def repo_from_cmd(path, encoding=None):
    """hub-up.cmd of a hub from git (deploy/windows/setup.py, the console code page): `"<python>" "<repo>\\bin\\hub-up"`;
    the app's launcher (it runs app.py launch) is not a clone's."""
    try:
        with open(path, "rb") as f:
            text = f.read(16384).decode(encoding or _encoding(), errors="replace")
    except OSError:
        return None
    if re.search(r'"[^"\r\n]*app\.py" launch', text):
        return None
    m = re.search(r'"([^"\r\n]+)[\\/]bin[\\/]hub-up"', text)
    # a folder the code page cannot hold is named through the user's variable (deploy/windows/setup.py cmd_text)
    return re.sub(r"%(LOCALAPPDATA|APPDATA|USERPROFILE)%", lambda v: os.environ.get(v.group(1)) or v.group(0),
                  m.group(1)) if m else None


def names(name=None):
    """The OS service names of the main hub, or of the instance `name` (core/instance.py service_ids)."""
    if name:
        from core import instance
        return instance.service_ids(name)
    return {"launchd": LABEL, "systemd": UNIT, "task": TASK}


def unit_of(state, osname=None, home_dir=None):
    """{"kind", "path"} of the service file this state folder's hub runs from, or None."""
    osname = _os(osname)
    if osname == "windows":
        from core import instance
        path = os.path.join(state, "hub-up.cmd")
        return {"kind": "task", "path": path, "id": names(instance.name(state))["task"]} if os.path.isfile(path) else None
    from platforms import posix
    unit = posix.service_unit(state, home=home_dir, platform="darwin" if osname == "mac" else "linux")
    return {"kind": unit["kind"], "path": unit["path"], "id": unit["id"]} if unit else None


def unit_env(unit):
    """The variables the service gives its hub (a plist's EnvironmentVariables, a unit's Environment= lines)."""
    if unit["kind"] == "launchd":
        import plistlib
        from xml.parsers.expat import ExpatError
        try:
            with open(unit["path"], "rb") as f:
                env = plistlib.load(f).get("EnvironmentVariables")
        except (OSError, ValueError, plistlib.InvalidFileException, ExpatError, AttributeError):
            return {}
        return {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}
    if unit["kind"] == "systemd":
        import shlex
        try:
            with open(unit["path"], encoding="utf-8") as f:
                lines = [x.strip() for x in f.read().splitlines()]
        except (OSError, UnicodeDecodeError):
            return {}
        env = {}
        for line in lines:
            if line.startswith("EnvironmentFile="):   # variables this file cannot see: some may move the settings
                env["EnvironmentFile"] = line.split("=", 1)[1]
            if not line.startswith("Environment="):
                continue
            try:   # Environment="A=1" B=2 — systemd's quoting, several assignments on a line
                words = shlex.split(line[len("Environment="):])
            except ValueError:
                env["Environment"] = line
                continue
            env.update(w.split("=", 1) for w in words if "=" in w)
        return env
    return {}


def _same(a, b):
    return bool(a) and bool(b) and os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _state_folders(osname=None, home_dir=None):
    """(the OS default state folder, the main hub's state folder: the installers' mark, else that one) — home_dir
    stands for the home of macOS and Linux (tests)."""
    import platforms
    from core import statemark
    default = os.path.join(home_dir, ".hub-core") if home_dir and _os(osname) != "windows" else \
        platforms.current.state_dir()
    return default, statemark.default_state() or default


def instance_of(state, unit, env, osname=None, home_dir=None):
    """{"instance", "config_dir"} of a second hub of the machine whose service this is: its anchor's name and settings
    folder — the service's HUB_CONFIG_DIR, when it names one, must be that folder — or, without an anchor, the name
    and folder hub-up would adopt it under (instance.adoptable: ~/.hub-<name> next to the default state folder, a
    settings folder of its own, not the main hub's). The service must have the instance's own name (the app's service
    is put under it). {} — the main hub (no anchor, no settings folder of its own, a folder not named as an
    instance's); None — not such a hub (a ~/.hub-<name> on the main hub's settings: splitting them is the person's)."""
    import platforms
    from core import instance
    anchor = instance.load(state)
    cfg = env.get("HUB_CONFIG_DIR") or ""
    default, main_state = _state_folders(osname, home_dir)
    if anchor:
        name, folder = anchor["name"], instance.config_dir(state)
        if not folder or (cfg and not _same(cfg, folder)):
            return None
    elif cfg:
        name = instance.adoptable(state, env, main_state, platforms.current.config_dir(), default_state=default)
        if not name:
            return None
        folder = cfg
    elif instance.name_of_folder(state, default) and not _same(state, main_state):
        return None
    else:
        return {}
    if unit["id"] != names(name)[unit["kind"]]:
        return None
    return {"instance": name, "config_dir": os.path.abspath(folder)}


def find(state, osname=None, home_dir=None):
    """The clone this state folder's hub runs from: {"repo", "unit", "version"} (and "instance", "config_dir" of a
    second hub of the machine, instance_of), or None (no hub, the app's own, a service whose code is not a git clone of
    the core). Not offered either: a hub whose service names a settings folder of its own that is not an instance's
    (HUB_CONFIG, HUB_CONFIG_DIR — the app's hub reads the standard one or its anchor's), and a clone inside the state
    folder but for <state>/code itself, where `hub instance new` puts a second hub's clone: that one is the app's place
    for its code, so the move takes the clone aside (put_aside) and undo puts it back."""
    osname = _os(osname)
    unit = unit_of(state, osname, home_dir)
    if not unit:
        return None
    repo = {"launchd": repo_from_plist, "systemd": repo_from_unit, "task": repo_from_cmd}[unit["kind"]](unit["path"])
    if not repo or not _a_clone(repo):
        return None
    env = unit_env(unit)
    own = instance_of(state, unit, env, osname, home_dir)
    if own is None or any(env.get(k) for k in OWN_SETTINGS if not (own and k == "HUB_CONFIG_DIR")):
        return None
    top = os.path.normcase(os.path.join(os.path.realpath(state), ""))
    if os.path.normcase(os.path.join(os.path.realpath(repo), "")).startswith(top) and not in_code_folder(state, repo):
        return None
    return {"repo": os.path.abspath(repo), "unit": unit, "version": version_of(repo), **own}


def in_code_folder(state, repo):
    """The clone is <state>/code itself, a real folder (not the app's pointer, which leads to a layer)."""
    from core import appcode
    code = os.path.join(state, "code")
    return _same(repo, code) and not appcode.is_pointer(code) and os.path.isdir(code)


def version_of(repo):
    try:
        r = subprocess.run(["git", "-C", repo, "describe", "--tags", "--always", "--match", "v[0-9]*.[0-9]*.[0-9]*"],
                           capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def texts(found):
    """The dialog's words in the owner's language (the process language: hub.json and the state of this hub)."""
    from core import i18n, instance
    command = instance.command_name(found.get("instance"))   # hub-<name> of a second hub
    return {"title": i18n.t("core.appmove.title"),
            "text": i18n.t("core.appmove.text", repo=found["repo"], version=found.get("version") or "?",
                           command=command),
            "move": i18n.t("core.appmove.move"), "stay": i18n.t("core.appmove.stay"),
            "later": i18n.t("core.appmove.later"), "waiting": i18n.t("core.appmove.waiting"),
            "failed": i18n.t("core.appmove.failed"), "undo_failed": i18n.t("core.appmove.undo_failed", command=command)}


# --- what the app's install rewrites: saved before, put back by undo ---

def artefacts(state, cfg, unit, osname=None, home_dir=None, name=None):
    """The paths the move and the app's install change, of this OS (hub.json apart: undo puts back only the keys the
    move changed, an edit of the owner's on the app stays). An instance (name): its anchor and registry entry, its
    commands hub-<name>, hub-telegram-<name> and <state>/bin/hub — never the main hub's commands."""
    osname = _os(osname)
    h = home_dir or home()
    paths = [os.path.join(cfg, "state-dir"),
             os.path.join(state, "hub-home", "CLAUDE.md"), os.path.join(state, "hub-home", "ONBOARDING.md"),
             os.path.join(state, "app.json"), os.path.join(state, "app"),
             # the one-line install's mark (core/uninstall MARK): after an undo the hub is the clone's again
             os.path.join(state, "layers", "oneline.json")]
    if name:
        from core import instance
        paths += [os.path.join(state, instance.FILE), os.path.join(instance.registry_dir(), name + ".json")]
    if unit and unit.get("path"):
        paths.append(unit["path"])
    if osname == "windows":
        paths.append(os.path.join(state, "bin", "hub.cmd"))
        if name:   # its terminal command (deploy/windows/setup.py), in the user's PATH through <state>\cmd
            paths.append(os.path.join(state, "cmd", f"hub-{name}.cmd"))
    else:
        own = (f"hub-{name}", f"hub-telegram-{name}") if name else COMMANDS
        paths += [os.path.join(h, ".local", "bin", c) for c in own]
        if name:
            paths.append(os.path.join(state, "bin", "hub"))
        paths.append(os.path.join(state, "bin", "python3"))
    if osname == "mac":
        from core import uninstall   # the clone's bundle: its agent runs through it (the team's or the open flavor's)
        paths += [os.path.join(state, n) for n in uninstall.BUNDLES]
    if osname == "linux" and unit:
        paths.append(os.path.join(os.path.dirname(unit["path"]), "default.target.wants", os.path.basename(unit["path"])))
    seen, out = set(), []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def save(paths, into, out=None):
    """Each path as it is now: a file copied, a link's target, a folder moved aside (the clone's bundle — the app's
    install deletes it), or absent. -> [{"path", "kind", ...}] — `out` filled as it goes: what was saved before a
    failure is known."""
    os.makedirs(into, exist_ok=True)
    out = [] if out is None else out
    for n, path in enumerate(paths):
        if os.path.islink(path):
            out.append({"path": path, "kind": "link", "to": os.readlink(path)})
        elif os.path.isfile(path):
            name = f"{n}-{os.path.basename(path)}"
            shutil.copy2(path, os.path.join(into, name))
            out.append({"path": path, "kind": "file", "copy": name})
        elif os.path.isdir(path):
            name = f"{n}-{os.path.basename(path)}"
            os.replace(path, os.path.join(into, name))
            out.append({"path": path, "kind": "dir", "copy": name})
        else:
            out.append({"path": path, "kind": "none"})
    return out


def _remove(path):
    from core import appcode
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif appcode.is_pointer(path):   # a junction (Windows: <state>\app) goes with rmdir, without what it leads to —
        os.rmdir(path)               # shutil.rmtree refuses it, and the pointer stayed
    elif os.path.isdir(path):   # (an emptied folder that stays — macOS, the app's bundle — takes the saved one: replace)
        import platforms
        platforms.current.rmtree(path)


def restore(saved, into):
    """Every saved path back as it was; -> the paths that did not go back (with why)."""
    left = []
    for item in saved:
        path = item.get("path")
        if not isinstance(path, str):
            continue
        try:
            kind = item.get("kind")
            if kind == "link" and os.path.islink(path) and os.readlink(path) == item.get("to"):
                continue
            src = os.path.join(into, item.get("copy") or "")
            if kind in ("file", "dir") and not os.path.lexists(src):
                if os.path.lexists(path):
                    continue   # put back already (an undo that ran before)
                raise OSError("the saved copy is gone")
            if os.path.lexists(path):
                _remove(path)
            if kind == "link":
                os.makedirs(os.path.dirname(path), exist_ok=True)
                os.symlink(item["to"], path)
            elif kind == "file":
                os.makedirs(os.path.dirname(path), exist_ok=True)
                shutil.copy2(src, path)
            elif kind == "dir":
                os.makedirs(os.path.dirname(path), exist_ok=True)
                os.replace(src, path)
        except (OSError, KeyError, TypeError) as e:
            left.append(f"{path}: {e}")
    return left


# --- hub.json: the programs the app gives ---

def app_programs(app, state, osname=None):
    """{"mcp": [...], "ffmpeg": path} of the app in `app` (its folder with paths.json — Mac, Windows; the .deb has fixed
    places). Mac names them through <state>/app, the link its shell keeps on the app it runs from."""
    osname = _os(osname)
    try:
        with open(os.path.join(app, "paths.json"), encoding="utf-8") as f:
            rel = json.load(f)
    except (OSError, ValueError):
        rel = None
    if not isinstance(rel, dict):
        if osname == "linux":
            return {"mcp": [os.path.join(app, "bin", "chrome-devtools-mcp")], "ffmpeg": os.path.join(app, "bin", "ffmpeg")}
        return {}
    import ntpath
    base = os.path.join(state, "app") if osname == "mac" else app
    join = (lambda r: ntpath.join(base, *r.split("/"))) if osname == "windows" else (lambda r: os.path.join(base, r))
    out = {}
    if isinstance(rel.get("node"), str) and isinstance(rel.get("mcp"), str):
        out["mcp"] = [join(rel["node"]), join(rel["mcp"])]
    if isinstance(rel.get("ffmpeg"), str):
        out["ffmpeg"] = join(rel["ffmpeg"])
    return out


def _inside(value, repo):
    top = os.path.normcase(os.path.join(os.path.abspath(repo), ""))
    return isinstance(value, str) and os.path.normcase(os.path.abspath(value)).startswith(top)


def _replaceable(value, repo, program):
    """A setting of the app's program that the move may set to the app's: unset, inside the clone, Homebrew's, a full
    path that is gone, or (the browser) the clone installer's default — chrome-devtools-mcp by any runner."""
    if value is None:
        return True
    items = value if isinstance(value, list) else [value]
    if not items or not all(isinstance(v, str) for v in items):
        return False
    if any(_inside(v, repo) or v.startswith(BREW) or "/.linuxbrew/" in v for v in items):
        return True
    if os.path.isabs(items[0]) and not os.path.exists(items[0]):
        return True
    return program == "mcp" and any("chrome-devtools-mcp" in v for v in items)


def _on_layer(value, repo, state, layer):
    """The place on the code layer (<state>/code/<the same path>) of a file inside the clone that the layer has too; else
    None. A relative value is no one's path; a layer inside the clone (the move run by the clone's own code) is no answer."""
    if not isinstance(value, str) or not os.path.isabs(value) or not _inside(value, repo) or _inside(layer, repo):
        return None
    rel = os.path.relpath(os.path.abspath(value), os.path.abspath(repo))
    if not os.path.isfile(os.path.join(layer, rel)):
        return None
    return os.path.join(state, "code", rel)


def rules_links(conf, state, repo, layer=LAYER):
    """[(link, its target on the layer)]: the hub session's rules links that name the clone, in the folder hub.json puts
    outside <state>/hub-home (the shell's install links the one inside). A file the person wrote, or a link elsewhere, stays."""
    cwd = ((_read(conf) or {}).get("hub") or {}).get("cwd")
    if not isinstance(cwd, str) or not os.path.isabs(os.path.expanduser(cwd)):
        return []
    cwd = os.path.expanduser(cwd)
    if os.path.realpath(cwd) == os.path.realpath(os.path.join(state, "hub-home")):
        return []
    out = []
    for name in RULES:
        link = os.path.join(cwd, name)
        if not os.path.islink(link):
            continue
        to = _on_layer(os.path.join(os.path.dirname(link), os.readlink(link)), repo, state, layer)
        if to:
            out.append((link, to))
    return out


def relink(pairs):
    """Each link to its new target, put over the old one in one step; -> the links that could not be made (a Windows user
    may not make a link): they stay as they were, on the clone."""
    left = []
    for link, to in pairs:
        aside = link + ".moving"
        try:
            if os.path.lexists(aside):
                os.unlink(aside)
            os.symlink(to, aside)
            os.replace(aside, link)
        except OSError as e:
            left.append(f"{link}: {e}")
            if os.path.islink(aside):
                os.unlink(aside)
    return left


def relink_config(path, repo, state, moved, layer=LAYER):
    """hub.json: every string that is a path to a file inside the clone that the layer has too goes to <state>/code/…
    (an MCP server's command or argument, a hook); -> the places changed ("mcp.browser.command"), and `moved` gets
    [[the value before, the value set]] for undo. Paths outside the clone stay."""
    from core import catalog
    changed = []

    class Unchanged(Exception):
        pass

    def walk(node, at):
        for key, value in (list(node.items()) if isinstance(node, dict) else list(enumerate(node))):
            if isinstance(value, (dict, list)):
                walk(value, at + [key])
            elif isinstance(value, str):
                to = _on_layer(value, repo, state, layer)
                if to and to != value:
                    node[key] = to
                    if [value, to] not in moved:
                        moved.append([value, to])
                    changed.append(".".join(str(k) for k in at + [key]))
    try:
        with catalog.edit_settings(path) as cfg:
            walk(cfg, [])
            if not changed:
                raise Unchanged
    except Unchanged:
        return []
    return changed


def rewrite_config(path, repo, programs, was=None):
    """hub.json: browser.mcp and voice.ffmpeg to the app's where _replaceable; -> the keys changed ("section.key"), and
    `was` gets {"section.key": [the value before or None, present, the value set, the section is new]} for undo. The
    rest stays."""
    from core import catalog
    changed = []
    was = {} if was is None else was

    class Unchanged(Exception):
        pass
    try:
        with catalog.edit_settings(path) as cfg:
            for section, key, program in (("browser", "mcp", "mcp"), ("voice", "ffmpeg", "ffmpeg")):
                want = programs.get(program)
                if not want:
                    continue
                fresh = section not in cfg
                have = cfg.setdefault(section, {})
                if not isinstance(have, dict):
                    continue
                if have.get(key) != want and _replaceable(have.get(key), repo, program):
                    was[f"{section}.{key}"] = [have.get(key), key in have, want, fresh]
                    have[key] = want
                    changed.append(f"{section}.{key}")
            if not changed:
                raise Unchanged
    except Unchanged:
        return []
    return changed


def _swap(node, table, at):
    """Every string in node that is a key of table becomes its value; -> the places changed."""
    done = []
    for key, value in (list(node.items()) if isinstance(node, dict) else list(enumerate(node))):
        if isinstance(value, (dict, list)):
            done += _swap(value, table, at + [key])
        elif isinstance(value, str) and value in table:
            node[key] = table[value]
            done.append(".".join(str(k) for k in at + [key]))
    return done


def unrewrite_config(path, was, moved=None):
    """Undo of rewrite_config and relink_config: each key the move set goes back to its value before — only while it
    still holds what the move set (the owner changed it since: it stays, as every other edit made on the app). -> the
    keys put back."""
    from core import catalog
    back = []

    class Unchanged(Exception):
        pass
    try:
        with catalog.edit_settings(path) as cfg:
            for name, item in (was or {}).items():
                if not (isinstance(item, list) and len(item) == 4 and name.count(".") == 1):
                    continue
                before, present, set_to, fresh = item
                section, key = name.split(".")
                have = cfg.get(section)
                if not isinstance(have, dict) or have.get(key) != set_to:
                    continue
                if present:
                    have[key] = before
                else:
                    del have[key]
                    if fresh and not have:
                        del cfg[section]
                back.append(name)
            if moved:   # a path the move set goes back wherever it stands now (a list reordered on the app too)
                back += _swap(cfg, {to: before for before, to in moved}, [])
            if not back:
                raise Unchanged
    except Unchanged:
        return []
    return back


# --- the steps ---

def busy(state, now=None):
    """Who holds a restart now (restart.blockers over the state of the journal)."""
    from core import journal, restart
    st = journal.load_state(state)
    return restart.blockers(st, now or time.time()) if isinstance(st, dict) and st.get("sessions") is not None else []


def wait_free(state, ceiling_minutes, say=print, clock=time.monotonic, sleep=time.sleep, look=busy):
    """Until no session is in a turn or runs work in the background, at most the ceiling (0 — no ceiling)."""
    end = clock() + ceiling_minutes * 60 if ceiling_minutes else None
    told = None
    while True:
        held = look(state)
        if not held:
            return []
        if end is not None and clock() >= end:
            return held
        if held != told:
            from core import i18n
            say(i18n.t("core.appmove.waiting_for", who="; ".join(held)))
            told = held
        sleep(POLL)


def _run(cmd, run=subprocess.run, timeout=120):
    import platforms
    try:   # bytes: schtasks writes in the console's OEM code page; text=True read it as UTF-8 and failed (byte 0x93)
        r = run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))
    text = lambda out: platforms.current.output_text(out) if isinstance(out, bytes) else out
    return subprocess.CompletedProcess(r.args, r.returncode, text(r.stdout), text(r.stderr))


def _uid():
    return os.getuid() if hasattr(os, "getuid") else 0


def _hub_up_gone(state, seconds=STOP_WAIT, clock=time.monotonic, sleep=time.sleep):
    """hub-up of this state folder is gone within `seconds`."""
    from core import restart
    end = clock() + seconds
    while restart.running(state):
        if clock() >= end:
            return False
        sleep(1)
    return True


def stop(state, unit, osname=None, run=subprocess.run, gone=None):
    """The hub of this state folder stopped through its service (launchd/systemd) or the soft stop (Windows: the task
    does not start it again while the stop mark lies). -> True when its hub-up is gone."""
    osname = _os(osname)
    gone = gone or _hub_up_gone
    from core import restart
    if unit and unit["kind"] == "launchd":
        _run(["launchctl", "bootout", f"gui/{_uid()}/{unit['id']}"], run)
    elif osname == "linux":
        _run(["systemctl", "--user", "stop", unit["id"] if unit else UNIT], run)
    else:
        if restart.running(state):
            restart.ask_stop(state)
        return gone(state)
    if gone(state, STOP_WAIT // 2):
        return True
    # still up: not under that service (the .deb starts it apart when there is no user bus) — the soft stop of hub-up
    # itself (a start by hand clears its marks)
    if restart.running(state):
        restart.ask_stop(state)
    return gone(state, STOP_WAIT // 2)


def _linux_handover(state, unit, run=subprocess.run, deb=DEB, home_dir=None, name=None):
    """The clone's user unit off and out of the way (the package's unit has the same name: a user one overrides it);
    the terminal commands to the package's python and the code folder of the app — of the main hub: an instance's
    hub-<name> is the installer's to write (the one-line setup.py), the main hub's `hub` is not this hub's."""
    _run(["systemctl", "--user", "disable", unit["id"]], run)
    for p in (unit["path"],):
        if os.path.lexists(p):
            os.unlink(p)
    _run(["systemctl", "--user", "daemon-reload"], run)
    if name:
        return
    from deploy import commands
    bin_dir = os.path.join(home_dir or home(), ".local", "bin")
    os.makedirs(bin_dir, exist_ok=True)
    py = os.path.join(deb, "runtime", "python", "bin", "python3")
    for cmd in COMMANDS:
        commands.put(bin_dir, cmd, commands.wrapper(os.path.join(state, "code"), py, cmd, os.path.join(deb, "bin")),
                     say=lambda dst: None)


def _base(repo):
    from core import codeedits
    for tags in ("refs/hub-update/tags/", "refs/tags/"):
        base = codeedits.release_base(repo, tags, "refs/remotes/origin/main")
        if base:
            return base
    return None


def edits_copy(repo, now=None):
    """The backup branch of the clone's own edits of the core: {"branch", "count"} | None (none, or no release known).
    A failure does not stop the move: the clone itself stays as it was."""
    from modules.update import takeoff
    base = _base(repo)
    if not base:
        return None
    has_name = subprocess.run(["git", "-C", repo, "config", "user.email"], capture_output=True, timeout=30,
                              stdin=subprocess.DEVNULL).returncode == 0
    identity = [] if has_name else ["-c", "user.name=hub", "-c", "user.email=hub@localhost"]   # as the updater's
    try:
        got = takeoff.keep_copy(repo, base, identity=identity, now=now)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        return {"error": str(e)[:300]}
    return {"branch": got["branch"], "count": got["count"]} if got["result"] == "kept" else None


def move(state, cfg, repo, app, osname=None, run=subprocess.run, say=print, wait=wait_free, now=time.time,
         home_dir=None, stopper=stop, layer=LAYER):
    """`hub app move <clone> --app <the app's folder>`: the steps 1–6 of the docstring. -> the record. Refused —
    nothing changed (or all of it put back and the clone's hub on again)."""
    osname = _os(osname)
    old = record(state)
    if old and old.get("status") in ("moving", "moved"):
        from core import instance
        raise Refused(i18n_t("core.appmove.already", repo=old.get("repo") or "?",
                             command=instance.command_name(old.get("instance"))))
    found = find(state, osname, home_dir)
    if not found or os.path.normcase(os.path.abspath(found["repo"])) != os.path.normcase(os.path.abspath(repo)):
        raise Refused(i18n_t("core.appmove.not_clone", repo=repo))
    unit, name = found["unit"], found.get("instance")
    if name:   # its settings folder is the one its anchor names (or will): not the main hub's
        if not os.path.isfile(os.path.join(app, ONELINE_ENV)):   # the .deb's and the .app's shells put the main hub
            raise Refused(i18n_t("core.appmove.instance_oneline", name=name))
        cfg = found.get("config_dir") or cfg
    from core import catalog, restart
    try:
        ceiling = restart.ceiling(catalog.load_config(state))
    except Exception:  # noqa: BLE001 — unreadable settings: the default ceiling
        ceiling = restart.WAIT_MINUTES
    held = wait(state, ceiling, say=say)
    if held:
        raise Refused(i18n_t("core.appmove.still_busy", who="; ".join(held)))
    # a folder of this attempt: the copies of an earlier one (a file an undo could not put back) are never deleted
    n, saved = 0, f"saved-{int(now())}"
    while os.path.lexists(os.path.join(folder(state), saved)):
        n += 1
        saved = f"saved-{int(now())}-{n}"
    into = os.path.join(folder(state), saved)
    rec = {"v": VERSION, "status": "moving", "repo": found["repo"], "version": found.get("version") or "",
           "os": osname, "unit": unit, "app": app, "at": now(), "saved": [], "into": saved, "was": {}}
    if found.get("instance"):
        rec["instance"] = found["instance"]
    say(i18n_t("core.appmove.stopping"))
    try:
        stopped = stopper(state, unit, osname, run)
    except BaseException:   # Ctrl+C while the clone's hub stops: its service on again, never left off
        down = _start_clone(state, rec, osname, run)
        if down:   # it did not come on: the record for `hub app undo` (and the line's next run) to start it
            _write(os.path.join(folder(state), RECORD), {**rec, "returning": True, "left": down})
        raise
    if not stopped:
        from core import restart
        down = _start_clone(state, rec, osname, run)
        if down and not restart.running(state):   # its service went off and does not come on again, nothing runs
            _not_back(state, rec, i18n_t("core.appmove.not_stopped"), down)
        raise Refused(i18n_t("core.appmove.not_stopped"))
    conf = os.path.join(cfg, "hub.json")
    rec["config_path"], rec["config_absent"] = conf, not os.path.lexists(conf)
    try:
        links = rules_links(conf, state, found["repo"], layer)   # before the settings change; saved with the rest
        save(artefacts(state, cfg, unit, osname, home_dir, rec.get("instance")) + [l for l, _ in links], into, rec["saved"])
        _write(os.path.join(folder(state), RECORD), rec)
        if rec.get("instance"):
            _anchor_layer(state, rec["instance"], unit, found, osname, home_dir)
        if os.path.isfile(conf):
            shutil.copy2(conf, os.path.join(cfg, CONFIG_BACKUP))
        rec["edits"] = edits_copy(found["repo"], now())
        if os.path.isfile(conf):
            rec["moved"] = []
            rec["config"] = rewrite_config(conf, found["repo"], app_programs(app, state, osname), rec["was"])
            rec["config"] += relink_config(conf, found["repo"], state, rec["moved"], layer)
        _write(os.path.join(folder(state), RECORD), rec)
        rec["unlinked"] = relink(links)
        if in_code_folder(state, found["repo"]):   # the app's code goes there
            put_aside(state, found["repo"], rec)
        if osname == "linux":
            _linux_handover(state, unit, run, deb=app, home_dir=home_dir, name=rec.get("instance"))
        _write(os.path.join(folder(state), RECORD), rec)
    except BaseException as e:
        placed, left = bring_back(rec)
        left += restore(rec["saved"], into)
        try:
            unrewrite_config(conf, rec["was"], rec.get("moved"))
        except Exception as again:  # noqa: BLE001 — said in the record; the copy hub.json.bak-app holds it
            left.append(f"{conf}: {again}")
        down = _start_clone(state, rec, osname, run) if placed else []   # no clone in its place: nothing to start
        rec.update(error=str(e)[:300], left=left + down)
        if down or not placed:
            _not_back(state, rec, str(e)[:300], left + down)
        rec.update(status="failed")
        _write(os.path.join(folder(state), RECORD), rec)
        if left:   # the clone runs; what did not go back is named (its copy stays in the saved folder)
            raise Refused(i18n_t("core.appmove.prepare_failed_left", error=str(e)[:300],
                                 what="; ".join(left)[:600])) from None
        raise Refused(i18n_t("core.appmove.prepare_failed", error=str(e)[:300])) from None
    return rec


def aside_path(state):
    """Where the clone of <state>/code goes: beside the state folder, its name without the dot and "-clone" after it
    (~/.hub-work -> ~/hub-work-clone); a name taken gets -2, -3…"""
    parent, base = os.path.split(os.path.realpath(state))   # a state folder that is a link: beside what it leads to
    stem = (base.lstrip(".") or "hub") + "-clone"
    path, n = os.path.join(parent, stem), 1
    while os.path.lexists(path):
        n += 1
        path = os.path.join(parent, f"{stem}-{n}")
    return path


def _worktrees(repo):
    """The folders of the clone's linked worktrees (git lists the clone itself first)."""
    r = _run(["git", "-C", repo, "worktree", "list", "--porcelain"], timeout=60)
    return [line[len("worktree "):] for line in r.stdout.splitlines() if line.startswith("worktree ")][1:] \
        if r.returncode == 0 else []


def _relocated(paths, old, new):
    """Worktree folders after the clone went from `old` to `new`: the ones inside it follow, the others stay."""
    out, top = [], os.path.realpath(old)
    for p in paths:
        real = os.path.realpath(p)
        try:
            rel = os.path.relpath(real, top)
        except ValueError:   # another drive
            rel = os.pardir
        out.append(real if rel == os.pardir or rel.startswith(os.pardir + os.sep) else os.path.join(new, rel))
    return out


def _repair(repo, paths):
    """git worktree repair from the clone's new place, naming the worktrees' new folders (git cannot know them: a
    worktree's .git file and the clone's record of it both hold absolute paths). -> [what did not work]."""
    here = [p for p in paths if os.path.isdir(p)]
    out = [f"{p}: the worktree's folder is not there, git still has its old path" for p in paths if p not in here]
    if not here:
        return out
    r = _run(["git", "-C", repo, "worktree", "repair", *here], timeout=120)
    return out + ([] if r.returncode == 0 else
                  [f"git worktree repair: {((r.stderr or '') + (r.stdout or '')).strip()[-300:]}"])


def put_aside(state, repo, rec):
    """The clone at <state>/code beside the state folder (the app's install puts its code layer there), its linked
    worktrees repaired; the record has it before the folder moves, so that an undo finds it after any failure."""
    _loaded()
    trees, to = _worktrees(repo), aside_path(state)
    rec["aside"] = {"from": repo, "to": to}
    _write(os.path.join(folder(state), RECORD), rec)
    os.replace(repo, to)
    rec["aside"]["repair"] = _repair(to, _relocated(trees, repo, to))
    _write(os.path.join(folder(state), RECORD), rec)


def _loaded():
    """What the steps after the clone's folder moves import lazily: the code that runs may be the clone's own (or reached
    through the app's pointer, which goes), so it is in memory before."""
    from core import appcode, catalog, codeedits, instance, restart  # noqa: F401
    import platforms  # noqa: F401
    try:
        from deploy import commands  # noqa: F401 — the Linux handover of the main hub
        from modules.update import takeoff  # noqa: F401
    except ImportError:   # a layer without them: the steps that use them do not run there either
        pass


def bring_back(rec):
    """Undo of put_aside: the clone to <state>/code again (the app's pointer there goes, an empty folder too).
    -> (the clone is in its place, what did not work: the paths with why)."""
    from core import appcode
    aside = rec.get("aside")
    if not (isinstance(aside, dict) and isinstance(aside.get("from"), str) and isinstance(aside.get("to"), str)):
        return True, []
    here, was = aside["from"], aside["to"]
    if not os.path.isdir(was):
        if _a_clone(here) and not appcode.is_pointer(here):
            return True, []   # back already (an undo that ran before)
        return False, [f"{here}: the clone is not at {was}"]
    _loaded()
    try:
        if appcode.is_pointer(here):
            _remove(here)
        elif os.path.isdir(here) and not os.listdir(here):
            os.rmdir(here)
        elif os.path.lexists(here):
            return False, [f"{here}: is in the way of the clone, which stays at {was}"]
        trees = _worktrees(was)
        os.replace(was, here)
    except OSError as e:
        return False, [f"{here}: {e}"]
    return True, _repair(here, _relocated(trees, was, here))


def _not_back(state, rec, error, what):
    """The clone's service did not start again, the hub may run nowhere: the record stays the move's ("moving") —
    `hub app undo` (and the line's next run) starts it — and the person is told so."""
    from core import instance
    rec.update(status="moving", returning=True, left=what)
    _write(os.path.join(folder(state), RECORD), rec)
    raise NotBack(i18n_t("core.appmove.prepare_not_back", error=error, what="; ".join(what)[:600],
                         command=instance.command_name(rec.get("instance"))))


def _anchor_layer(state, name, unit, found, osname=None, home_dir=None):
    """An instance's anchor and registry entry name the layer as its code (saved before: undo puts them back as they
    were, or away when there were none). A hub from before anchors gets them now, as hub-up would adopt it."""
    from core import instance
    data = instance.load(state)
    if not data:
        import platforms
        default, main_state = _state_folders(osname, home_dir)
        data = instance.adopt(state, {**unit_env(unit), "HUB_CONFIG_DIR": found["config_dir"]}, main_state,
                              platforms.current.config_dir(), default_state=default, service=unit, repo=found["repo"])
    if not data or data.get("name") != name:
        raise RuntimeError(f"the anchor of the hub {name} is not written")
    data = {**data, "code": {"kind": "layer", "path": os.path.join(state, "code")}}
    instance.register({**data, "state": state})
    instance.write(state, data)


def i18n_t(key, **kw):
    from core import i18n
    return i18n.t(key, **kw)


def done(state, now=time.time):
    """The shell saw the app's hub come up: the move is done (the line in "About the hub" for UNDO_DAYS)."""
    rec = record(state)
    if not rec or rec.get("status") != "moving" or rec.get("returning"):   # a return that did not finish: not a move
        return None
    rec.update(status="moved", done=now())
    _write(os.path.join(folder(state), RECORD), rec)
    return rec


def stay(state, now=time.time):
    """"Stay on the clone": nothing changes; the mark says the owner chose it (for the git hub's offer to move, app spec
    §8: the one line of a git hub that the hub is now an app). Opening the installer again is asking again: the dialog comes again."""
    _write(os.path.join(folder(state), STAY), {"at": now()})


def _start_clone(state, rec, osname, run=subprocess.run):
    """The clone's service on again (its files are back); a stop mark of the soft stop goes (the Windows task would
    not bring the hub up with it). -> what did not start, for the record and the person ([] — it started)."""
    from core import restart
    for mark in (restart.HELD, restart.STOP):
        try:
            os.unlink(os.path.join(state, mark))
        except FileNotFoundError:
            pass
    unit = rec.get("unit") or {}
    own = names(rec.get("instance"))
    if unit.get("kind") == "launchd":
        cmd = ["launchctl", "bootstrap", f"gui/{_uid()}", unit["path"]]
        r = _run(cmd, run)
        if r.returncode:   # launchd may still hold the app's agent of that label (bootout does not wait): once more
            target = f"gui/{_uid()}/{unit.get('id') or own['launchd']}"
            end = time.monotonic() + BOOT_WAIT
            while _run(["launchctl", "print", target], run, timeout=30).returncode == 0 and time.monotonic() < end:
                time.sleep(1)
            r = _run(cmd, run)
        return _not_started(r)
    if osname == "linux":
        name = unit.get("id") or own["systemd"]   # the clone's own unit, as find() named it
        _run(["systemctl", "--user", "daemon-reload"], run)
        return _not_started(_run(["systemctl", "--user", "enable", name], run)) + \
            _not_started(_run(["systemctl", "--user", "restart", name], run))
    return _not_started(_run(["schtasks", "/Run", "/TN", unit.get("id") or own["task"]], run))


BOOT_WAIT = 90   # s for launchd to let go of an agent booted out, its ExitTimeOut being 75 (too early: "5: Input/output error")


def _not_started(r):
    if not r.returncode:
        return []
    why = ((r.stderr or "") + (r.stdout or "")).strip()[-300:] or f"exit {r.returncode}"
    return [f"{' '.join(str(a) for a in r.args[:2])}: {why}"]


def undo(state, failed=False, osname=None, run=subprocess.run, now=time.time, stopper=stop):
    """`hub app undo`: the app's hub stopped, the saved files back, the clone's service on. -> the record."""
    rec = record(state)
    if not rec or rec.get("status") not in ("moving", "moved"):
        raise Refused(i18n_t("core.appmove.nothing_to_undo"))
    osname = rec.get("os") or _os(osname)
    if not failed and rec.get("status") == "moved" and now() - (rec.get("done") or rec.get("at") or 0) > UNDO_DAYS * 86400:
        raise Refused(i18n_t("core.appmove.too_late", days=UNDO_DAYS))
    app_unit = dict(rec.get("unit") or {})
    own = names(rec.get("instance"))   # the app's service of this hub: the main hub's names, or the instance's own
    if osname == "mac":
        app_unit = {"kind": "launchd", "id": own["launchd"]}
    if osname == "linux":
        app_unit = {"kind": "systemd", "id": own["systemd"]}   # the package's unit: the clone's own is not there now
    if not stopper(state, app_unit, osname, run):
        raise Refused(i18n_t("core.appmove.not_stopped"))
    placed, left = bring_back(rec)   # first: the files that come back may be inside it
    left += restore(rec.get("saved") or [], os.path.join(folder(state), rec.get("into") or "saved"))
    conf = rec.get("config_path")
    if isinstance(conf, str) and os.path.isfile(conf):
        copy = f"{conf}.bak-undo-{int(now())}"   # as it was on the app: an edit made there is never lost
        try:
            if rec.get("config_absent"):   # the app's install wrote it: the clone had none — aside, not deleted
                os.replace(conf, copy)
            else:
                shutil.copy2(conf, copy)
                rec["config_back"] = sorted(set(rec.get("config_back") or []) | set(unrewrite_config(conf, rec.get("was"), rec.get("moved"))))
        except Exception as e:  # noqa: BLE001 — said in the result; the app's settings stay, the copy is next to them
            left.append(f"{conf}: {e}")
    down = _start_clone(state, rec, osname, run) if placed else []   # the clone is not in its place: no start
    if down or not placed:   # the clone's service did not start, the hub may run nowhere: the move stays to undo — "moving" (no days'
        # limit; the line's next run undoes it too), `hub app undo` again, of this code or the clone's own, starts it
        rec.update(status="moving", returning=True, left=left + down, undo_tried=now())
    else:
        rec.update(status="failed" if failed else "undone", undone=now(), left=left)
    _write(os.path.join(folder(state), RECORD), rec)
    return rec


def undo_command(state, python, hub, log, env=None, which=shutil.which, osname=None):
    """argv of `hub app undo` started by the app's own hub (the page's button): on Linux under its systemd unit the
    service's stop takes its whole cgroup, the undo with it — there it runs as a unit of its own."""
    env = os.environ if env is None else env
    cmd = [python, hub, "app", "undo"]
    if _os(osname) == "linux" and env.get("INVOCATION_ID") and which("systemd-run"):
        keep = ["HUB_CONFIG", "HUB_CONFIG_DIR", "XDG_CONFIG_HOME", "PATH", "HUB_LANG"]
        sets = [f"--setenv={k}={env[k]}" for k in keep if env.get(k)]
        return ["systemd-run", "--user", "--collect", "--quiet", f"--setenv=HUB_STATE_DIR={state}", *sets,
                "-p", f"StandardOutput=append:{log}", "-p", f"StandardError=append:{log}", "--", *cmd]
    return cmd


def where(rec):
    """Where the clone of a move is: beside the state folder when the move took it out of <state>/code, else where it was."""
    aside = rec.get("aside")
    return str((aside.get("to") if isinstance(aside, dict) else None) or rec.get("repo") or "")


def snapshot(state, now=None):
    """For the page ("About the hub"): the move done, within UNDO_DAYS — {"repo", "at", "until", "edits"}; else None."""
    rec = record(state)
    if not rec or rec.get("status") != "moved":
        return None
    at = rec.get("done") or rec.get("at")
    if not isinstance(at, (int, float)):
        return None
    until = at + UNDO_DAYS * 86400
    if (now or time.time()) > until:
        return None
    edits = rec.get("edits") if isinstance(rec.get("edits"), dict) else {}
    return {"repo": where(rec), "at": at, "until": until,
            "branch": edits.get("branch") if isinstance(edits.get("branch"), str) else None}
