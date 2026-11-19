"""Several hubs on one machine: the main hub has no name (~/.hub-core, ~/.config/hub-core, the command `hub`, ports
8787/8788 — as always), every further hub is an instance with a name (~/.hub-<name>, ~/.config/hub-<name>, the command
`hub-<name>`, its own ports). The anchor of an instance is <state>/instance.json: any process that knows the state
folder knows the rest (the settings folder, the ports), so a process that lost HUB_CONFIG_DIR on the way does not drift
into the main hub's settings. The main hub has no instance.json and no registry entry — one hub, no new files.

The registry of the machine's hubs is a file per instance in ~/.config/agents-hub/hubs (%APPDATA%\\agents-hub\\hubs):
only for the list and for handing out ports past the ports of all hubs; each instance writes and removes its own file.

Only the stdlib and the names of `platforms` (config_dir, state_dir, replace; makedirs_private only where it writes),
imported where needed: the installers and the processes of the previous release load this new file lazily from the
tree on disk (core/statemark.py is the pattern)."""
import json
import os
import re
import socket
import tempfile
import time

# an instance name: a letter, then letters, digits and dashes, 2 to 16 in all
NAME = re.compile(r"[a-z][a-z0-9-]{1,15}")
# never a name: the main hub's words (its folders are hub-core, its cookie hub_main)
MAIN_WORDS = frozenset({"core", "main"})
# names a new instance cannot take: the commands of the hub and its parts (hub-<name> would collide with
# bin/hub-<part>), plus words other tools already use on machines (hub-open). An instance that already stands under
# such a name (a stand in ~/.hub-codex) is adopted under it: its command is its own
RESERVED = MAIN_WORDS | frozenset({
    "up", "hubd", "browser", "bus-relay", "cloudflare", "conduit", "files", "gateway", "stats", "tailnet", "telegram",
    "update", "watch", "open", "codex", "instance", "app",
})

FILE = "instance.json"
# the folders of a hub's live sessions (slice M2): the other hubs of the machine do not fast-forward a folder, take or
# remove an executor's copy one of them works in. Written only on a machine with more than one hub; rewritten at least
# every FOLDERS_EVERY s by a running hub, so a file older than FOLDERS_STALE is a stopped hub's and holds nothing
FOLDERS = "folders.json"
FOLDERS_EVERY, FOLDERS_STALE = 600, 1800
DEFAULT_PORTS = {"web": 8787, "files": 8788}
PORT_ENV = {"web": "HUB_WEB_PORT", "files": "HUB_FILES_PORT"}
# the page and "Files" of an instance: pairs from 8801 by 2 — past the ports below it that hubs already use: tailnet
# takes 8790/8792 for services of earlier versions and removes them, 8795–8798 are the lab's and an earlier second Mac
# hub's, 8781–8784 pilot stands
PAIR_FIRST, PAIR_LAST = 8801, 8999
# CDP of the agents' browsers: Chrome's own 9222, the Mac installer's 9223/9224, the lab's from 9225, the default 9230
CDP_FIRST, CDP_LAST = 9231, 9399
CDP_KNOWN = frozenset({9222, 9223, 9224, 9225, 9226, 9227, 9228, 9229, 9230})


def _format(name):
    return isinstance(name, str) and bool(NAME.fullmatch(name)) and not name.endswith("-") and "--" not in name \
        and name not in MAIN_WORDS


def valid_name(name):
    """True if a NEW instance may take `name`: the format, not a reserved word, and not a name whose command would be
    another instance's second command (hub-telegram-<name>: an instance "telegram-x" and the telegram of "x")."""
    return _format(name) and name not in RESERVED and not name.startswith("telegram-")


def cookie_name(name=None):
    """The cookie of a hub's page (sign-in by a link with a key): one per hub, not per port — cookies do not split by
    port, so the page and "Files" of one hub share it and two hubs on 127.0.0.1 do not. The main hub (no name) has
    hub_main; an instance — hub_<name>, the name as is (a dash is allowed in a cookie name)."""
    if not name:
        return "hub_main"
    if not _format(name):
        raise ValueError(f"not an instance name: {name!r}")
    return f"hub_{name}"


def command_name(name=None):
    """The terminal command of a hub: `hub` for the main one, `hub-<name>` for an instance."""
    return f"hub-{name}" if name else "hub"


def service_ids(name):
    """The OS service names of an instance (the main hub's are com.hub-core, agents-hub.service, "Agents Hub")."""
    return {"launchd": f"com.hub-{name}", "systemd": f"agents-hub-{name}.service", "task": f"Agents Hub ({name})"}


# --- folders ------------------------------------------------------------------------------------------------------

def _sibling(default_folder, name):
    """The instance's folder next to the OS default folder of the hub: ~/.hub-core -> ~/.hub-<name>,
    %LOCALAPPDATA%\\hub-core -> %LOCALAPPDATA%\\hub-<name> (the OS spelling is the default folder's)."""
    head, base = os.path.split(os.path.normpath(default_folder))
    if "hub-core" not in base:
        raise ValueError(f"not a default hub folder: {default_folder}")
    return os.path.join(head, base.replace("hub-core", f"hub-{name}", 1))


def folders(name, default_state=None, default_config=None):
    """{"state", "config"} of an instance: next to the OS default folders of the hub (platforms' state_dir and
    config_dir), wherever the main hub itself stands (a state mark may point it at another folder)."""
    import platforms
    return {"state": _sibling(default_state or platforms.current.state_dir(), name),
            "config": _sibling(default_config or platforms.current.config_dir(), name)}


def name_of_folder(state, default_state=None):
    """The instance name its state folder spells, or None: only a folder next to the OS default one and named like it
    (~/.hub-personal next to ~/.hub-core -> personal); any other (a copy of a hub, a test's temporary folder, a
    hand-made path) is not taken for an instance by its name."""
    if default_state is None:
        import platforms
        default_state = platforms.current.state_dir()
    default_state = os.path.normpath(default_state)
    if os.path.realpath(os.path.dirname(os.path.normpath(state))) != os.path.realpath(os.path.dirname(default_state)):
        return None   # the parents compared, not the folders: either may be a link to another disk
    base = os.path.basename(os.path.normpath(state))
    prefix = os.path.basename(default_state).replace("hub-core", "hub-", 1)
    name = base[len(prefix):] if "hub-core" in os.path.basename(default_state) and base.startswith(prefix) else ""
    return name if _format(name) else None


def registry_dir():
    """The folder of the machine's registry of hubs (this user's): next to the OS settings folder of the hub."""
    import platforms
    return os.path.join(os.path.dirname(os.path.normpath(platforms.current.config_dir())), "agents-hub", "hubs")


# --- the anchor ---------------------------------------------------------------------------------------------------

_cache = {}   # path -> (mtime_ns, size, data): config_dir() reads the anchor on every call


def _read(path):
    """A JSON object from a file, None if there is none or it is not an object (a broken file is not an anchor)."""
    try:
        st = os.stat(path)
    except OSError:
        _cache.pop(path, None)
        return None
    key = (st.st_mtime_ns, st.st_size)
    hit = _cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = None
    data = data if isinstance(data, dict) else None
    _cache[path] = (key, data)
    return data


def load(state):
    """<state>/instance.json of an instance, or None (the main hub, no such folder, a broken file)."""
    if not state:
        return None
    data = _read(os.path.join(state, FILE))
    return data if data and _format(data.get("name")) else None


def name(state=None):
    """This hub's instance name, None for the main hub; state defaults to HUB_STATE_DIR (a process without it belongs
    to the main hub)."""
    data = load(state if state is not None else os.environ.get("HUB_STATE_DIR"))
    return data["name"] if data else None


def config_dir(state):
    """The settings folder the anchor names, or None."""
    data = load(state)
    got = data.get("config_dir") if data else None
    return got if isinstance(got, str) and got else None


def _port_value(v):
    if isinstance(v, str) and v.isascii() and v.isdigit():
        v = int(v)
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 < v < 65536 else None


def anchored(kind, state):
    """The port ("web" or "files") the anchor of the hub in `state` names, or None (the main hub, no such port)."""
    ports = (load(state) or {}).get("ports")
    return _port_value(ports.get(kind)) if isinstance(ports, dict) else None


def port(kind, state=None, environ=None):
    """This hub's port of the page ("web") or "Files" ("files"): HUB_WEB_PORT / HUB_FILES_PORT (digits), else the
    anchor's, else 8787 / 8788. state defaults to HUB_STATE_DIR."""
    environ = os.environ if environ is None else environ
    got = _port_value(environ.get(PORT_ENV[kind], ""))
    if got:
        return got
    return anchored(kind, state if state is not None else environ.get("HUB_STATE_DIR")) or DEFAULT_PORTS[kind]


def anchor(environ, state):
    """What hub-up hands to all its children: HUB_STATE_DIR is always this folder (the same value for the main hub),
    and an instance's HUB_CONFIG_DIR and ports from its anchor where the environment lacks them (a service that lost
    them, a stand started by hand). -> the anchor or None."""
    environ["HUB_STATE_DIR"] = state
    data = load(state)
    if not data:
        return None
    if not environ.get("HUB_CONFIG_DIR") and config_dir(state):
        environ["HUB_CONFIG_DIR"] = config_dir(state)
    for kind, key in PORT_ENV.items():
        if not _port_value(environ.get(key, "")) and anchored(kind, state):
            environ[key] = str(anchored(kind, state))
    return data


# --- writing ------------------------------------------------------------------------------------------------------

def _write_private(path, data, indent=1):
    """Whole and for this user only (0600): a temp file next to it, then a replace."""
    import platforms
    folder = os.path.dirname(path)
    platforms.current.makedirs_private(folder)   # only the writers (new code) reach this name
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
        platforms.current.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _cache.pop(path, None)


def write(state, data):
    """Write the anchor of an instance (only the installer, `hub instance` and the adoption at hub-up's start)."""
    if not _format((data or {}).get("name")):
        raise ValueError("an anchor without an instance name")
    _write_private(os.path.join(state, FILE), data)


def entries(folder=None):
    """The registry: [entry] sorted by name, each with "gone": True when its state folder is no longer there (removed by
    hand: offer to drop the entry). Garbage is skipped, not an error."""
    folder = folder or registry_dir()
    try:
        files = sorted(os.listdir(folder))
    except OSError:
        return []
    out = []
    for f in files:
        if not f.endswith(".json"):
            continue
        data = _read(os.path.join(folder, f))
        if not data or data.get("name") != f[:-5] or not _format(data["name"]) \
                or not isinstance(data.get("state"), str):
            continue
        out.append({**data, "gone": not os.path.isdir(data["state"])})
    return out


def register(data, folder=None):
    """This instance's registry entry (its own file: no shared file two processes rewrite at once)."""
    if not _format((data or {}).get("name")):
        raise ValueError("a registry entry without an instance name")
    _write_private(os.path.join(folder or registry_dir(), data["name"] + ".json"), data)


def unregister(name, folder=None):
    """Remove this instance's registry entry; no entry is not an error."""
    if not _format(name):
        return False
    try:
        os.remove(os.path.join(folder or registry_dir(), name + ".json"))
        return True
    except FileNotFoundError:
        return False


# --- the other hubs of the machine (slice M2) ----------------------------------------------------------------------

def others(state, main_state=None, registry=None):
    """The other hubs of this machine (this user's), [{"name": instance name or None for the main hub, "state"}]: the
    registry's entries whose folder is there, and the main hub (which has no entry) when this hub is not it. []: the
    hub is alone on the machine."""
    if main_state is None:
        from core import statemark
        import platforms
        main_state = statemark.default_state() or platforms.current.state_dir()
    out = [{"name": e["name"], "state": e["state"]} for e in entries(registry)
           if not e["gone"] and not _same(e["state"], state)]
    if main_state and not _same(main_state, state) and os.path.isdir(main_state) \
            and not any(_same(h["state"], main_state) for h in out):
        out.append({"name": None, "state": main_state})
    return out


def machine(state, main_state=None, registry=None):
    """How the hub in `state` shares the machine: {"space": its instance name (None for the main hub) — the namespace
    of its executors' copies, "owner": {"state", "hub": its command} or None — set only when another hub stands here
    or this hub is an instance (then copies get an owner mark and git in a repo takes the repo's file lock), "others":
    others()}. A hub alone on the machine: no space, no owner — nothing new on disk."""
    space = name(state)
    hubs = others(state, main_state, registry)
    owner = {"state": os.path.realpath(state), "hub": command_name(space)} if space or hubs else None
    return {"space": space, "owner": owner, "others": hubs}


def write_folders(state, cwds):
    """<state>/folders.json: the folders of this hub's live sessions, for the other hubs of the machine."""
    _write_private(os.path.join(state, FOLDERS), {"cwds": list(cwds), "at": int(time.time())})


def live_folders(hubs, now=None):
    """[(command of the hub, folder)] of the live sessions of hubs (others()): from their folders.json, skipping a file
    older than FOLDERS_STALE (its hub is not running) and anything that is not a path."""
    now = time.time() if now is None else now
    out = []
    for h in hubs:
        path = os.path.join(h["state"], FOLDERS)
        try:
            if now - os.stat(path).st_mtime > FOLDERS_STALE:
                continue
        except OSError:
            continue
        cwds = (_read(path) or {}).get("cwds")
        if isinstance(cwds, list):
            out += [(command_name(h["name"]), c) for c in cwds if isinstance(c, str) and c]
    return out


def live_in(hubs, path, inside):
    """The command of another hub (hubs: others()) one of whose live sessions works in path — inside(cwd, path) is the
    comparison (hosts/worktree.in_tree: the folder itself or under it) — or None. Read now, not from a list read earlier."""
    if not hubs or not path:
        return None
    return next((hub for hub, cwd in live_folders(hubs) if inside(cwd, path)), None)


# --- ports --------------------------------------------------------------------------------------------------------

def free(port):
    """The OS lets this user bind the port on 127.0.0.1 and on all addresses: no program (of this user or another one,
    whom the registry does not see) listens on it."""
    for host in ("127.0.0.1", "0.0.0.0"):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((host, port))
        except OSError:
            return False
        finally:
            s.close()
    return True


def taken(entries_=None, main_browsers=()):
    """The ports the machine's hubs already have: the main hub's page and "Files", its agents' browsers
    (main_browsers), every registry entry's ports."""
    out = set(DEFAULT_PORTS.values()) | {_port_value(p) for p in main_browsers}
    for e in entries_ or []:
        ports = e.get("ports") if isinstance(e.get("ports"), dict) else {}
        out |= {_port_value(ports.get(kind)) for kind in DEFAULT_PORTS}
        browsers = ports.get("browsers") if isinstance(ports.get("browsers"), dict) else {}
        out |= {_port_value(p) for p in browsers.values()}
    return out - {None}


def allocate(taken_ports, browsers=(), is_free=free):
    """Ports of a new instance: {"web", "files", "browsers": {name: port}} — the first pair past taken_ports that the OS
    lets bind, the agents' browsers from 9231 likewise. Handed out once, at install: the ports then never change
    (links, the phone app, tailnet). No free pair -> ValueError."""
    taken_ports = set(taken_ports)
    pair = next(((p, p + 1) for p in range(PAIR_FIRST, PAIR_LAST, 2)
                 if not {p, p + 1} & taken_ports and is_free(p) and is_free(p + 1)), None)
    if not pair:
        raise ValueError(f"no free pair of ports in {PAIR_FIRST}–{PAIR_LAST}")
    taken_ports |= set(pair)
    out = {"web": pair[0], "files": pair[1], "browsers": {}}
    candidates = (p for p in range(CDP_FIRST, CDP_LAST + 1) if p not in taken_ports | CDP_KNOWN)
    for b in browsers:
        got = next((p for p in candidates if is_free(p)), None)
        if got is None:
            raise ValueError(f"no free port for a browser in {CDP_FIRST}–{CDP_LAST}")
        out["browsers"][b] = got
    return out


# --- adoption -----------------------------------------------------------------------------------------------------

def code_of(service_file):
    """The code folder a service file runs hub-up from (a launchd plist or a systemd unit), or None."""
    import shlex
    args = []
    try:
        if service_file.endswith(".plist"):
            import plistlib
            with open(service_file, "rb") as f:
                args = [str(a) for a in plistlib.load(f).get("ProgramArguments") or []]
        else:
            with open(service_file, encoding="utf-8", errors="replace") as f:
                lines = [x[len("ExecStart="):].strip().lstrip("-@+!:") for x in f if x.startswith("ExecStart=")]
            for line in lines:
                try:   # one line systemd reads otherwise than a shell (a lone quote) does not hide the next
                    args += shlex.split(line.replace("%h", shlex.quote(os.path.expanduser("~"))))
                except ValueError:
                    continue
    except Exception:  # noqa: BLE001 — an unreadable or hand-written service file names no code
        return None
    for a in args:
        head, tail = os.path.split(os.path.normpath(a))
        if tail == "hub-up" and os.path.basename(head) == "bin":
            return os.path.dirname(head)
    return None


def service_of(stop):
    """The OS service of a hub-up that no service file names (a unit of systemd-run, a plist whose folder is spelled
    otherwise), from the command that stops it (platforms' service()): {"kind", "id"} or None."""
    words = (stop or "").split()
    if words[:3] == ["systemctl", "--user", "stop"] and len(words) == 4:
        return {"kind": "transient", "id": words[3]}
    if words[:2] == ["launchctl", "bootout"] and len(words) >= 3:   # the Mac agent's: gui/$(id -u)/<label>
        return {"kind": "launchd", "id": " ".join(words[2:]).rsplit("/", 1)[-1]}
    return {"kind": "other", "stop": stop} if stop else None


def _same(a, b):
    return bool(a) and bool(b) and os.path.realpath(a) == os.path.realpath(b)


def adoptable(state, environ, main_state, main_config, default_state=None):
    """The instance name if this hub is a second hub that stands from before the anchor (a Mac HUB_INSTANCE, a stand
    run by systemd-run): its state folder is not the main hub's and spells an instance name next to the OS default
    folder (name_of_folder), it has no anchor, and the environment of its hub-up carries its own HUB_CONFIG_DIR, not
    the main hub's. Otherwise None: a second hub on the main hub's settings (Linux with only HUB_STATE_DIR) is not
    adopted — splitting the settings is the person's call."""
    cfg = environ.get("HUB_CONFIG_DIR") or ""
    if not state or _same(state, main_state) or not cfg or _same(cfg, main_config):
        return None
    if os.path.exists(os.path.join(state, FILE)):
        return None
    return name_of_folder(state, default_state)


def adopt(state, environ, main_state, main_config, default_state=None, service=None, repo=None, main_repo=None,
          registry=None, now=None):
    """Write the registry entry and the anchor of a second hub that stands from before them (adoptable), once, from
    the facts: the ports of its environment, its OS service (service: platforms' service_unit or service_of), its code —
    "shared" when it is the main hub's, "git" when another one. Nothing moves: hub.json, the service, `update` and the
    tmux server it shared with the main hub (HUB_HOST=tmux: its live sessions are there) stay as they were. The entry
    goes first: a failed anchor is tried again at the next start, and the name is this hub's then. -> the anchor
    written, or None (not adoptable, or the name is another hub's in the registry)."""
    name_ = adoptable(state, environ, main_state, main_config, default_state)
    if not name_:
        return None
    if any(e["name"] == name_ and not _same(e["state"], state) for e in entries(registry)):
        return None
    ports = {kind: _port_value(environ.get(key, "")) for kind, key in PORT_ENV.items()}
    ports = {k: v for k, v in ports.items() if v}
    if main_repo and repo:
        kind = "shared" if _same(repo, main_repo) else "git"
    elif repo and os.path.realpath(repo).startswith(os.path.realpath(state) + os.sep):
        kind = "git"
    else:
        kind = "unknown"
    svc = {k: service[k] for k in ("kind", "id", "stop") if isinstance(service, dict) and service.get(k)} \
        or {"kind": "none"}
    data = {"name": name_, "title": name_, "config_dir": environ["HUB_CONFIG_DIR"], "ports": ports, "service": svc,
            "command": command_name(name_), "code": {"kind": kind, "path": repo or ""},
            "tmux": {"socket": "hub", "prefix": "hub-"},
            "created": int(now if now is not None else time.time()), "by": "adopted"}
    register({**data, "state": state}, registry)
    write(state, data)
    return data


def tmux(state):
    """{"socket", "prefix"} of the tmux host of the hub in `state` (HUB_HOST=tmux): the main hub's `tmux -L hub` with
    sessions hub-<session>; a new instance its own server hub-<name> with <name>-<session>; an adopted one keeps the
    server it had (its anchor's "tmux")."""
    data = load(state)
    if not data:
        return {"socket": "hub", "prefix": "hub-"}
    own = data.get("tmux") if isinstance(data.get("tmux"), dict) else {}
    if isinstance(own.get("socket"), str) and own["socket"] and isinstance(own.get("prefix"), str) and own["prefix"]:
        return {"socket": own["socket"], "prefix": own["prefix"]}
    return {"socket": f"hub-{data['name']}", "prefix": f"{data['name']}-"}


# --- putting an instance on the machine (the installers, `hub instance new`) -----------------------------------------

ENGINES = ("claude", "codex")
BROWSER_DEFAULT_PORT = 9230   # modules/browser/browsers.py: a local browser without a port
BROWSER_DESCRIPTION = "The Chrome of this hub: its own profile, without the sign-ins of another hub's browser"


def main_state():
    """The state folder of the main hub: the installers' mark (core/statemark), else the OS default one."""
    from core import statemark
    import platforms
    return statemark.default_state() or platforms.current.state_dir()


def _settings(path):
    """hub.json as a dict, {} if it is missing or broken (read only: the installers' shape, not the hub's checks)."""
    data = _read(path) if path else None
    return data or {}


def local_browsers(cfg):
    """{name: port} of the local agents' browsers of a hub.json (the default port when none is set)."""
    out = {}
    for name, b in (cfg.get("browsers") if isinstance(cfg.get("browsers"), dict) else {}).items():
        if isinstance(b, dict) and b.get("kind") == "local":
            out[name] = _port_value(b.get("port")) or BROWSER_DEFAULT_PORT
    return out


def _browser_ports(cfg):
    """Every CDP port a hub.json names: local browsers and remote ones on this machine (127.0.0.1:<port>)."""
    out = set(local_browsers(cfg).values())
    for b in (cfg.get("browsers") if isinstance(cfg.get("browsers"), dict) else {}).values():
        url = b.get("url") if isinstance(b, dict) else None
        m = re.match(r"(?:https?|wss?)://(?:127\.0\.0\.1|localhost|\[::1\]):(\d+)", url) if isinstance(url, str) else None
        if m:
            out.add(_port_value(m.group(1)))
    return out - {None}


def service_of_os(name, platform=None):
    """{"kind", "id"} of the OS service the installer of this OS puts for the instance; platform: "mac", "linux",
    "windows" (platforms' APP_OS) — the installers name theirs."""
    if not platform:
        import platforms
        platform = platforms.current.APP_OS
    ids = service_ids(name)
    if platform == "mac":
        return {"kind": "launchd", "id": ids["launchd"]}
    if platform == "windows":
        return {"kind": "task", "id": ids["task"]}
    return {"kind": "systemd", "id": ids["systemd"]}


def code_kind(repo, state):
    """"git" — the instance's own clone inside its state folder (hub instance new); "shared" — the code of another
    hub (the installer run with HUB_INSTANCE from a clone that is not the instance's: it follows that clone)."""
    real, inside = os.path.realpath(repo or ""), os.path.realpath(state) + os.sep
    return "git" if repo and real.startswith(inside) else "shared"


def siblings(default_state=None):
    """The anchors in the folders next to the OS default state folder (~/.hub-*), as registry-like entries: an
    instance uninstalled without --purge leaves its registry entry but keeps its folder and ports — they are not handed
    to another one."""
    import platforms
    default_state = os.path.normpath(default_state or platforms.current.state_dir())
    parent = os.path.dirname(default_state)
    try:
        names = os.listdir(parent)
    except OSError:
        return []
    out = []
    for n in sorted(names):
        path = os.path.join(parent, n)
        data = load(path) if name_of_folder(path, default_state) else None
        if data:
            out.append({**data, "state": path})
    return out


def _clash(ports, others, main_browsers=()):
    """Ports of this anchor another hub of the machine has too (others: entries; the main hub's browsers), or
    missing ones."""
    if not isinstance(ports, dict) or not all(_port_value(ports.get(k)) for k in DEFAULT_PORTS):
        return True
    mine = {_port_value(ports.get(k)) for k in DEFAULT_PORTS} | {
        _port_value(p) for p in (ports.get("browsers") or {}).values()}
    return bool((mine - {None}) & taken(others, main_browsers))


def _move_ports(config, old, new):
    """New ports of a hub put on again: its kept hub.json follows them where they are the old ones (tailnet.expose, its
    local browsers) — the rest of the owner's settings stays."""
    path = os.path.join(config, "hub.json")
    cfg = _read(path)
    if not cfg:
        return
    old = old if isinstance(old, dict) else {}
    expose = (cfg.get("tailnet") or {}).get("expose") if isinstance(cfg.get("tailnet"), dict) else None
    if isinstance(expose, dict):
        for kind in DEFAULT_PORTS:
            if expose.get(kind) == old.get(kind) or not _port_value(expose.get(kind)):
                expose[kind] = new[kind]
    for name_, b in (cfg.get("browsers") if isinstance(cfg.get("browsers"), dict) else {}).items():
        if isinstance(b, dict) and b.get("kind") == "local" and name_ in new.get("browsers", {}):
            b["port"] = new["browsers"][name_]
    _write_private(path, cfg, indent=2)


def setup(name, repo, *, title=None, engine=None, claude=None, origin=None, browsers=(), main_config=None,
          default_state=None, default_config=None, registry=None, is_free=free, platform=None, now=None, kind=None):
    """What every installer and `hub instance new` share: the folders of the instance (next to the OS default ones),
    its ports — handed out once, past the ports of every hub of the machine and the ones the OS does not let bind — its
    OS service and command. The anchor and the registry entry are written the first time; a run again (a reinstall, a
    broken-off `hub instance new`) reuses them, so the ports never change. -> the anchor with "state".
    A new name must be free: no registry entry of another hub, no folder of something else under the name. kind: the
    code's kind when the folder cannot tell it yet ("layer" — the one-line install's own layer, put after this)."""
    where = folders(name, default_state, default_config)
    state, config = where["state"], where["config"]
    have = load(state)
    if have and have.get("name") != name:
        raise ValueError(f"{state} is the hub {have.get('name')}")
    if not have:
        if not valid_name(name):
            raise ValueError(f"not a name for a new hub: {name!r}")
        known = entries(registry)
        if any(e["name"] == name and not _same(e["state"], state) for e in known):
            raise ValueError(f"the name {name} is another hub's")
        if os.path.isfile(os.path.join(state, "journal.jsonl")):
            raise ValueError(f"{state} holds a hub without an anchor")
        if engine not in (None, *ENGINES) or claude not in (None, "shared", "own"):
            raise ValueError("engine: claude|codex, claude: shared|own")
        main_cfg = _settings(main_config or os.path.join(default_config or _default_config(), "hub.json"))
        names = sorted(set(local_browsers(main_cfg)) | set(browsers))
        known += [e for e in siblings(default_state) if not any(_same(e["state"], k["state"]) for k in known)]
        ports = allocate(taken(known, _browser_ports(main_cfg)), browsers=names, is_free=is_free)
        kind = kind or code_kind(repo, state)
        have = {"name": name, "title": title or name, "config_dir": config, "ports": ports,
                "service": service_of_os(name, platform), "command": command_name(name),
                "code": {"kind": kind, "path": repo or "", **({"origin": origin or ""} if kind == "git" else {})},
                "engine": engine or "claude", "claude": claude or "shared",
                "created": int(now if now is not None else time.time()), "by": "install"}
        _private_dir(state)
        _private_dir(config)
        register({**have, "state": state}, registry)
        write(state, have)
    else:
        for path in (state, config):
            _private_dir(path)
        known = entries(registry)
        if any(e["name"] == name and not _same(e["state"], state) and not e["gone"] for e in known):
            raise ValueError(f"the name {name} is another hub's")
        registered = any(e["name"] == name and _same(e["state"], state) for e in known)
        others = [e for e in known + siblings(default_state) if not _same(e["state"], state)]
        main_cfg = _settings(main_config or os.path.join(default_config or _default_config(), "hub.json"))
        # put on again after its registry entry went (its ports may have gone to another hub meanwhile), or adopted
        # without ports in its environment: new ones, past every other hub's
        if (not registered or not _port_value((have.get("ports") or {}).get("web"))) \
                and _clash(have.get("ports"), others, _browser_ports(main_cfg)):
            old = have.get("ports") if isinstance(have.get("ports"), dict) else {}
            names = sorted(set(old.get("browsers") or {}) | set(browsers))
            have = {**have, "ports": allocate(taken(others, _browser_ports(main_cfg)), browsers=names,
                                              is_free=is_free)}
            write(state, have)
            _move_ports(have.get("config_dir") or config, old, have["ports"])
            registered = False
        if not registered:
            register({**have, "state": state}, registry)
    return {**have, "state": state}


def _default_config():
    import platforms
    return platforms.current.config_dir()


def _private_dir(path):
    import platforms
    platforms.current.makedirs_private(path)


def settings(cfg, data, state, main_cfg=None):
    """The instance's part of a new hub.json (the installer wrote the rest as for any hub): its title, tailnet on its
    own ports, its agents' browsers on its own ports with their own profiles in its state folder (local ones only — a
    browser of the main hub's Chrome with its sign-ins is not taken over), the update of its own clone (the main hub's
    channel; no origin — off) or following the shared code, the engine, its own Claude Code folder. -> cfg."""
    main_cfg = main_cfg or {}
    ports = data.get("ports") or {}
    cfg["title"] = cfg.get("title") or data.get("title") or data["name"]
    cfg["tailnet"] = {**(cfg.get("tailnet") if isinstance(cfg.get("tailnet"), dict) else {}),
                      "expose": {"web": ports["web"], "files": ports["files"]}}
    have = cfg.get("browsers") if isinstance(cfg.get("browsers"), dict) else {}
    theirs = main_cfg.get("browsers") if isinstance(main_cfg.get("browsers"), dict) else {}
    out = {n: b for n, b in have.items() if not (isinstance(b, dict) and b.get("kind") == "local")}
    for name, port in sorted((ports.get("browsers") or {}).items()):
        base = have.get(name) if isinstance(have.get(name), dict) and have[name].get("kind") == "local" \
            else theirs.get(name) if isinstance(theirs.get(name), dict) else {}
        out[name] = {**{k: v for k, v in base.items() if k not in ("profile", "port", "risk", "description")},
                     "kind": "local", "port": port, "profile": os.path.join(state, "browsers", name), "risk": [],
                     "description": BROWSER_DESCRIPTION}
    if out or "browsers" in cfg:
        cfg["browsers"] = out
    code = data.get("code") or {}
    if code.get("kind") == "git":
        update = {k: v for k, v in (main_cfg.get("update") if isinstance(main_cfg.get("update"), dict) else {}).items()
                  if k != "follow"}
        cfg["update"] = update if code.get("origin") else {**update, "enabled": False}
    elif code.get("kind") == "shared":
        cfg["update"] = {"enabled": False, "follow": True}   # the hub of that code updates it; it changes — restart
    elif code.get("kind") == "layer":   # its own layer of the one-line install: updated by its own update, the main's channel
        cfg["update"] = {k: v for k, v in (main_cfg.get("update") if isinstance(main_cfg.get("update"), dict) else {}).items()
                         if k in ("channel", "enabled")}
    if data.get("engine") == "codex":
        cfg["host"] = "codex"
    if data.get("claude") == "own":
        cfg["claude_config_dir"] = os.path.join(state, "claude")
    return cfg


def write_settings(state, path, main_config=None):
    """Put the instance's part into the hub.json an installer has just written (settings())."""
    data = load(state)
    if not data:
        raise ValueError(f"no {FILE} in {state}")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    main_cfg = _settings(main_config or os.path.join(_default_config(), "hub.json"))
    _write_private(path, settings(cfg, data, state, main_cfg), indent=2)   # as the installers write hub.json


def bot_holder(token, mine, registry=None, default_config=None):
    """The command of another hub of this machine whose Telegram bot has this token, or None: one bot serves one hub
    (getUpdates of two hubs take each other's messages, 409). Each hub's telegram.json is read (the same user — this
    one may read them) and compared by sha256, the token itself not kept anywhere new. mine — this hub's telegram.json."""
    import hashlib
    import hmac
    want = hashlib.sha256(token.strip().encode()).digest()
    hubs = [(None, default_config or _default_config())] + [(e["name"], e.get("config_dir")) for e in entries(registry)
                                                           if not e["gone"]]
    for name_, folder in hubs:
        path = os.path.join(folder, "telegram.json") if isinstance(folder, str) and folder else None
        if not path or _same(path, mine) or not os.path.exists(path):
            continue
        got = (_read(path) or {}).get("token")
        if isinstance(got, str) and got and hmac.compare_digest(hashlib.sha256(got.strip().encode()).digest(), want):
            return command_name(name_)
    return None


def _codex_refresh(state):
    """sha256 of the refresh token of the hub's Codex sign-in (<state>/codex-home/auth.json), or None."""
    import hashlib
    try:   # not through _read: its cache would keep the token in this process
        with open(os.path.join(state, "codex-home", "auth.json"), encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError):
        return None
    got = got.get("tokens") if isinstance(got, dict) else None
    token = got.get("refresh_token") if isinstance(got, dict) else None
    return hashlib.sha256(token.encode()).digest() if isinstance(token, str) and token else None


def codex_copy_of(state, registry=None, default_state=None):
    """The command of another hub of this machine whose Codex sign-in is the same one as this hub's — a copied folder
    (a backup put back, a folder moved by hand): the refresh token is one-time, the first refresh of either breaks the
    other's (core/codexcli). Compared by sha256 of the refresh token, nothing kept. -> the command, or None."""
    import hmac
    mine = _codex_refresh(state)
    if mine is None:
        return None
    hubs = [(None, default_state or main_state())] + [(e["name"], e.get("state")) for e in entries(registry)
                                                        if not e["gone"]]
    for name_, folder in hubs:
        if not isinstance(folder, str) or not folder or _same(folder, state):
            continue
        theirs = _codex_refresh(folder)
        if theirs is not None and hmac.compare_digest(theirs, mine):
            return command_name(name_)
    return None


def _shell(value):
    import shlex
    return shlex.quote(str(value))


def main(argv):
    """The installers' entry (no imports of the hub but platforms):
      instance.py setup <name> --repo <code> [--os mac|linux|windows] [--browsers a,b] [--title T] [--engine E]
                        [--claude C] [--origin URL]
          the anchor (once) and shell assignments: INST_STATE, INST_CONFIG, INST_WEB, INST_FILES, INST_SERVICE,
          INST_COMMAND, INST_CODE (deploy/install.sh, deploy/mac/install.sh); --ports — only "<web> <files>" (ASCII:
          Windows PowerShell 5.1 reads a program's output in another code page, it spells the folders itself)
      instance.py settings <state> <hub.json>    the instance's part of a new hub.json"""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if argv[:1] == ["settings"] and len(argv) == 3:
        write_settings(argv[1], argv[2])
        return 0
    if argv[:1] != ["setup"] or len(argv) < 2:
        print(main.__doc__, file=sys.stderr)
        return 2
    opts, rest, i = {}, argv[2:], 0
    flags = ("--repo", "--browsers", "--title", "--engine", "--claude", "--origin", "--os")
    while i < len(rest):
        if rest[i] in flags and i + 1 < len(rest):
            opts[rest[i][2:]] = rest[i + 1]
            i += 2
        elif rest[i] == "--ports":
            opts["ports"] = True
            i += 1
        else:
            print(f"instance.py setup: unknown {rest[i]}", file=sys.stderr)
            return 2
    try:
        got = setup(argv[1], opts.get("repo"), title=opts.get("title"), engine=opts.get("engine"),
                    claude=opts.get("claude"), origin=opts.get("origin"), platform=opts.get("os"),
                    browsers=[b for b in (opts.get("browsers") or "").split(",") if b])
    except (ValueError, OSError) as e:
        print(f"instance {argv[1]}: {e}", file=sys.stderr)
        return 3
    if opts.get("ports"):
        print(got["ports"]["web"], got["ports"]["files"])
        return 0
    for key, value in (("STATE", got["state"]), ("CONFIG", got["config_dir"]), ("WEB", got["ports"]["web"]),
                       ("FILES", got["ports"]["files"]), ("SERVICE", got["service"]["id"]),
                       ("COMMAND", got["command"]), ("CODE", got["code"]["kind"])):
        print(f"INST_{key}={_shell(value)}")
    return 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main(_sys.argv[1:]))
