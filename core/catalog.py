"""The tool catalog and the hub settings (the hub.json file). I/O lives here; only `catalog.loaded` goes into the fold.

A tool is a Claude Code plugin (skills, agents) or an MCP server:
- `core:<name>` — plugins of the core modules from this repo's `plugins/`: the version always matches the code;
- `<plugin>@<set>` — plugins from skill sets: git repos in the Claude Code marketplace format
  (`.claude-plugin/marketplace.json`), the risk is in `keywords` like `risk:money`;
- `<name>` from the `mcp` section of the settings — an MCP server (config as in `.mcp.json`) with a risk and a description;
- `browser:<name>` from the `browsers` section — a browser for a session (modules/browser).
"""
import contextlib
import json
import locale
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

import platforms

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_PLUGINS = os.path.join(REPO, "plugins")

DEFAULTS = {
    "hub": {"enabled": False, "name": "hub", "cwd": None},
    "sets": [],
    "base": ["core:hub-base"],           # every session has it
    "hub_tools": ["core:hub-coordinator"],  # only the hub session has it
    "mcp": {},
    "mcp_deny": [],   # plugin servers (`plugin:<plugin>:<server>`) not handed to sessions on this machine
    # MCP servers (by name) a sign-in to which from the page waits for the owner's confirmation — money, production
    # (core/mcpwatch, owner decision 2026-10-28); empty by default
    "mcp_login_confirm": [],
    "connectors": False,                  # claude.ai account connectors (owner decision 2026-09-29: off by default)
    "projects": {},                       # name -> {"path", "about"}: where the hub raises sessions (owner decision 2026-09-29)
    "timezone": "UTC",                    # the owner's time zone: quiet hours for reminders
    # non-working hours (core/owner.py, review_*): on a working day 19:00-10:00 and the whole day off (Saturday and
    # Sunday) cards and tool requests do not go to Telegram and push; at 10:00 of a working day they come as one batch. true is these hours, {"from": "HH:MM", "to": "HH:MM"} your own; urgent ("🚨") and a turn on
    # the owner's own word go at once
    "quiet_review": False,
    # journal.rotate_mb — the rotation threshold; journal.compact_on_rotate — rotation rewrites the archive before the
    # newest without what it repeats (its intermediate state snapshots go too); off: archives stay whole, as they were
    "journal": {"compact_on_rotate": False},
    "browsers": {},                       # name -> a local or remote browser (modules/browser)
    # which Claude Code settings a hub session loads: without user, neither the global CLAUDE.md nor the owner's
    # skills, hooks and plugins (checked 2026-09-30: 154 skills -> 13 built-in); the project one, yes
    "setting_sources": "project,local",
    # reasoning level of sessions (`claude --effort`): effortLevel from the owner's settings does not reach them
    # without user. Medium by default, higher for one session with the `hub session effort` command (owner decision
    # 2026-10-05: keep medium and add a command to set effort, so one can ask for higher when medium does not cope)
    "effort": "medium",
    "browser": {},                        # what to connect and launch with: mcp, chrome, env
    # system agents (core/system.py, owner decision 2026-10-08: everyone has them, always): Hub Developer maintains
    # this hub's code, Agent Developer creates new agents via evals (modules/agent_dev); cannot be turned off, the
    # name can be changed
    "dev": {"name": "hub-dev"},
    "agent_dev": {"name": "agent-dev"},
    # update from the shared repository (modules/update): every `every` seconds when set (by default an hour from git,
    # 6 h from a code layer's server: update.every()); with no remote it stays silent.
    # a hub from git follows the head of branch `branch` (one channel, main; core/version.channel)
    "update": {"enabled": True, "remote": "origin", "branch": "main"},
    # hub statistics (modules/stats): a daily summary to the server; enabled by a human himself (owner decision 2026-10-08);
    # the server's address is the module's (stats.URL), unless "url" names another
    "stats": {"enabled": False},
    # extensions outside the repo (EXTENDING.md): name -> {"path", "run": [...], "web": "main.js"}
    "extensions": {},
    # the owner's rules for all sessions (2026-10-08): strings; the core puts them to every session next to the agent list
    "rules": [],
    # a plugin was updated (claude plugin update, git pull of the set): sessions with it restart themselves when
    # free; free ones with no task are parked and come up with the new version at the first message. Off by default
    "plugin_update_restart": False,
    # forks of helper agents (Claude Code 2.1.302 enabled them in `claude -p`): a fork gets the session's whole conversation.
    # false sets CLAUDE_CODE_FORK_SUBAGENT=0 for sessions; true is as Claude Code decided (the core leaves the variable alone)
    "fork_subagents": True,
    # the Claude Code settings folder (login, MCP, skills) for the whole hub, even if the owner's environment has
    # another one (CLAUDE_CONFIG_DIR of his Claude outside the hub): two logins on one machine, signing in to one does
    # not touch the other. "" means as is
    "claude_config_dir": "",
}


def config_dir():
    """Where the OS recommends keeping settings: XDG on Linux/Mac, %APPDATA% on Windows. HUB_CONFIG_DIR is the own
    directory of a second hub on the same machine (owner decision 2026-10-03: a personal hub, not a work one):
    hub.json, bot, contacts, watcher. Without it, the anchor of the hub's state folder names the folder of an instance
    (core/instance.py): a process that lost HUB_CONFIG_DIR on the way stays with its hub's settings, not the main
    hub's. A process without HUB_STATE_DIR belongs to the main hub, which has no anchor."""
    if os.environ.get("HUB_CONFIG_DIR"):
        return os.environ["HUB_CONFIG_DIR"]
    if os.environ.get("HUB_STATE_DIR"):
        from core import instance
        got = instance.config_dir(os.environ["HUB_STATE_DIR"])
        if got:
            return got
    return platforms.current.config_dir()


def config_path(state_dir):
    """HUB_CONFIG -> hub.json in the OS settings directory -> hub.json in the state directory (the old place)."""
    if os.environ.get("HUB_CONFIG"):
        return os.environ["HUB_CONFIG"]
    standard = os.path.join(config_dir(), "hub.json")
    former = os.path.join(state_dir, "hub.json")
    return standard if os.path.exists(standard) or not os.path.exists(former) else former


def read_settings(path):
    """A settings file edited by a human (hub.json) or written by the hub: UTF-8 under any locale, a BOM from
    PowerShell 5.1 is not an error; one written by earlier code in the locale encoding (KOI8-R, cp1251) is read in it
    too (PR review), the next write will be UTF-8; no file gives {}; an unreadable one (UTF-16 from `Out-File` and `>`
    in PowerShell 5.1) or non-JSON raises ValueError, not {}: whoever appends to the settings must not overwrite
    what was not read."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return {}
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return json.loads(raw.decode(locale.getencoding()))
        except (UnicodeDecodeError, ValueError):
            from core import i18n
            raise ValueError(i18n.t("cli.catalog.not_utf8", path=path)) from None
    return json.loads(text)


@contextlib.contextmanager
def edit_settings(path, private=False):
    """hub.json: read it, edit in the `with` block and write under the file lock `<path>.lock`; a writer that also
    takes the lock will neither overwrite this edit nor lose its own (`hub module`). An exception in the block means
    nothing is written. private is as in write_settings."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)   # the lock is next to the file: the settings folder may not exist
    with platforms.current.exclusive(path + ".lock"):
        raw = read_settings(path)
        yield raw
        write_settings(path, raw, private=private)


def write_settings(path, raw, private=False):
    """Atomically, indented by 1 space (as the page and the marketplace write); the old file's permissions, a new one
    gets 0600: hub.json may hold tokens; private means 0600 for the old one too (the edit contains keys: not a moment
    with the old permissions). Each writer has its own temp file (mkstemp): the page writes from threads of one
    process, and a shared "<path>.<pid>.tmp" could no longer be found by the second replace. On failure the temp
    file is removed."""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=os.path.basename(path) + ".", suffix=".tmp")   # 0600 at once
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=1)
            f.flush()
            platforms.current.sync(f.fileno())   # a power cut leaves the old file or the new one, never an empty one
        if os.path.exists(path) and not private:
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        platforms.current.replace(tmp, path)   # Windows: waits while another process reads the file
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    platforms.current.sync_dir(folder)


# sections absent from DEFAULTS whose shape readers nevertheless rely on
SHAPES = {"modules": dict, "executors": dict, "compact_idle": dict, "session_providers": dict,
          "session_env_values": dict}


def defaults():
    """Hub settings without hub.json."""
    from core import packaged
    return packaged.apply(json.loads(json.dumps(DEFAULTS)))


def load_config(state_dir):
    """DEFAULTS with hub.json on top. A section of the wrong shape ({"hub": null}, "sets": "x") stays at its default
    with a log line: a typo in one section must not stop the hub. A file that is not JSON or not an object raises
    ValueError, as in read_settings (hubd keeps the previous settings, daemon.read_config)."""
    cfg = json.loads(json.dumps(DEFAULTS))
    path = config_path(state_dir)
    user = read_settings(path)
    if not isinstance(user, dict):
        from core import i18n
        raise ValueError(i18n.t("cli.catalog.not_object", path=path))
    for key, value in user.items():
        shape = type(DEFAULTS[key]) if isinstance(DEFAULTS.get(key), (dict, list)) else SHAPES.get(key)
        if shape and not isinstance(value, shape):
            print(f"catalog: {path}: {key} — not {'an object' if shape is dict else 'a list'}, the default is taken",
                  file=sys.stderr, flush=True)
            continue
        cfg[key] = {**cfg[key], **value} if isinstance(cfg.get(key), dict) and key in ("hub", "dev", "agent_dev", "update", "stats") else value
    from core import packaged
    return packaged.apply(cfg)   # hub from a package: no update from git and no Hub Developer


_LAST = {}   # hub.json path -> settings (as a JSON string) it last read in this process (config_or_last)
_TOLD = {}   # hub.json path -> text of the error the process already wrote to stderr
_HELD = threading.Lock()   # used by the page threads


def config_or_last(state_dir):
    """load_config for those who must not fall over on an unreadable hub.json: a session launch and the page, where
    the owner fixes this very file. The earlier settings of this process; if none were read yet, the defaults. For an
    error, one line to stderr per error text; the core tells the owner about the file (owner.notify), `hub doctor`
    shows it as the "settings" line. Not for those who ACT on settings (access, gateway, self-update, where to write
    data) or append to them: a default there is not what the owner chose; they use load_config/edit_settings, which
    refuse."""
    path = config_path(state_dir)
    try:
        cfg = load_config(state_dir)
    except (OSError, ValueError) as e:
        with _HELD:
            told, held = _TOLD.get(path) == str(e), _LAST.get(path)
            _TOLD[path] = str(e)
        if not told:
            print(f"catalog: {path}: the settings cannot be read ({e}) — "
                  f"{'the previous ones, read earlier' if held else 'the defaults'}", file=sys.stderr, flush=True)
        return json.loads(held) if held else defaults()
    with _HELD:
        _TOLD.pop(path, None)
        _LAST[path] = json.dumps(cfg)
    return cfg


OWNER_CLAUDE_DIR = "HUB_OWNER_CLAUDE_CONFIG_DIR"   # CLAUDE_CONFIG_DIR the hub was started with ("" means none)


def pin_claude_dir(state_dir, env=None):
    """claude_config_dir from hub.json goes into the process environment (changes env, os.environ by default): the
    core, modules, sessions and their hooks inherit it. Called by bin/hub-up and bin/hub before everything else. The
    default folder (~/.claude) means unsetting CLAUDE_CONFIG_DIR, not setting it: with it Claude Code looks for
    .claude.json inside the folder, not in the home directory (MCP and projects would vanish). If there is no
    setting, it is not a full path (with ~ or from the root) or hub.json is unreadable, CLAUDE_CONFIG_DIR is the one
    the hub was started with: hub-up restarts in place (exec) with the environment already pinned, and a removed
    setting would otherwise hold until the service restarts."""
    env = os.environ if env is None else env
    if OWNER_CLAUDE_DIR not in env:
        env[OWNER_CLAUDE_DIR] = env.get("CLAUDE_CONFIG_DIR", "")
    try:   # only this key: the full load_config would print its hub.json warnings on every hub command
        cfg = read_settings(config_path(state_dir))
    except (OSError, ValueError):
        cfg = {}
    want = cfg.get("claude_config_dir") if isinstance(cfg, dict) else None
    want = os.path.expanduser(want.strip()) if isinstance(want, str) else ""
    if want and not os.path.isabs(want):
        print(f"catalog: claude_config_dir {want!r} — not a full path, the launch environment is taken", file=sys.stderr)
        want = ""
    if not want:   # as at launch, no adjustments
        if env[OWNER_CLAUDE_DIR]:
            env["CLAUDE_CONFIG_DIR"] = env[OWNER_CLAUDE_DIR]
        else:
            env.pop("CLAUDE_CONFIG_DIR", None)
        return env
    want = os.path.abspath(want)
    default = os.path.expanduser("~/.claude")
    try:
        same = os.path.samefile(want, default)   # a link, letter case on macOS and Windows
    except OSError:
        same = os.path.normcase(os.path.realpath(want)) == os.path.normcase(os.path.realpath(default))
    if same:
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = want
    return env


def _git_version(path):
    try:
        # timeout: rereading the catalog is a step of the core's tick, and a hung git would keep the core from
        # stopping (PR review)
        # platforms.run: on Windows the timeout kills the real git, not only the Git\cmd\git.exe launcher
        out = platforms.current.run(["git", "-C", path, "rev-parse", "--short", "HEAD"], 10, capture_output=True,
                                    text=True)
        return out.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_trees(root, rels):
    """{plugin path in the set: hash of its folder in HEAD} with one git call per set; a plugin outside the commit has no hash.
    Paths are relative to the set's folder: a set can be a folder of a big repository."""
    out = {}
    try:
        if "." in rels:   # the plugin is the whole set: "HEAD:./" is the set's folder, not the repository root (HEAD^{tree})
            r = platforms.current.run(["git", "-C", root, "rev-parse", "HEAD:./"], 10, capture_output=True,
                                      text=True, encoding="utf-8", errors="replace")
            if r.returncode == 0 and r.stdout.strip():
                out["."] = r.stdout.strip()
        rest = [x for x in rels if x != "."]
        if rest:
            # -z: paths as they are; without -z non-Latin names come quoted with \ooo and do not match the set's path
            r = platforms.current.run(["git", "-C", root, "ls-tree", "-z", "HEAD", "--", *rest], 10,
                                      capture_output=True, text=True, encoding="utf-8", errors="replace")
            for entry in r.stdout.split("\0") if r.returncode == 0 else []:
                meta, _, rel = entry.partition("\t")
                if rel:
                    out[os.path.normpath(rel)] = meta.split()[-1]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return out


def _risk(keywords):
    return sorted(k.split(":", 1)[1] for k in keywords or [] if k.startswith("risk:"))


RUNS_SHOWN = 5   # lines of "what runs" a tool carries for the owner's card; the rest is a count
_runs_cache = {}  # (folder, version, tree) -> what of the plugin runs code: the catalog is built every CATALOG_EVERY
RUNS_TTL = 300    # a working copy (no commit pins its files) is read again after this many seconds


def _runs(path, name, entry, version=None, tree=None):
    """{"risk": ["code"], "runs": [...lines], "runs_more": N} for a plugin that runs code (core/plugrights.runs), {} for
    text only. By the plugin's files, not the author's words: a plugin that brings a hook, a server or a script is given
    to a session on the owner's word (owner 2026-11-17). Cached while the version or the folder tree is the same."""
    from core import plugrights
    key = (os.path.realpath(path), version, tree)
    at, got = _runs_cache.get(key) or (0, None)
    if got is not None and not (version or tree) and time.monotonic() - at > RUNS_TTL:
        got = None   # a working copy changes without a commit: a hook added there is seen within RUNS_TTL
    if got is None:
        try:
            got = plugrights.runs(path, name, entry)
        except (OSError, ValueError, TypeError, AttributeError) as e:   # unreadable — unknown is risky, not safe
            got = [["unknown", f"{name}: {e!r:.120}"]]
        _runs_cache[key] = (time.monotonic(), got)
    if not got:
        return {}
    lines = [plugrights.shown(r) for r in got]
    return {"risk": ["code"], "runs": lines[:RUNS_SHOWN], **({"runs_more": len(lines) - RUNS_SHOWN}
                                                             if len(lines) > RUNS_SHOWN else {})}


def build(cfg):
    """{id: tool description} — everything that can be connected."""
    tools = {}
    if os.path.isdir(CORE_PLUGINS):
        version = _git_version(REPO)
        for name in sorted(os.listdir(CORE_PLUGINS)):
            manifest = os.path.join(CORE_PLUGINS, name, ".claude-plugin", "plugin.json")
            if os.path.exists(manifest):
                with open(manifest, encoding="utf-8") as f:
                    m = json.load(f)
                tools[f"core:{name}"] = {"kind": "plugin", "path": os.path.join(CORE_PLUGINS, name),
                                         "description": m.get("description", ""), "risk": _risk(m.get("keywords")),
                                         "source": "core", "version": version,
                                         **({"plugin_name": m["name"]} if isinstance(m.get("name"), str) else {})}
    for s in cfg.get("sets") or []:
        try:
            tools.update(set_tools(s))
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as e:   # one broken set, not the whole catalog
            print(f"catalog: skill set {s!r:.80}: {e!r:.200}", file=sys.stderr, flush=True)
    for name, m in (cfg.get("mcp") or {}).items():
        if not isinstance(m, dict):
            print(f"catalog: mcp {name!r:.60} — not an object, skipped", file=sys.stderr, flush=True)
            continue
        tools[name] = {"kind": "mcp", "server": m.get("server") or {}, "description": m.get("description", ""),
                       "risk": sorted(m.get("risk") or []), "source": "config",
                       # one for everyone (a personal Telegram: one process at a time): granting it to another
                       # session removes it from the previous one
                       **({"exclusive": True} if m.get("exclusive") else {}),
                       # the server's name in the session when it is not the id: the owner's sign-in (OAuth) is
                       # stored by Claude Code under that name, and the plain id may be taken by another server
                       **({"as": m["as"]} if "as" in m else {}),
                       # granted only to the named sessions; the core answers the rest with only_note
                       **({"only": list(m["only"]), "only_note": m.get("only_note") or ""} if m.get("only") else {}),
                       # only together with a skill (2026-10-01): a session with the tool is launched with them too
                       **({"with": list(m["with"])} if m.get("with") else {})}
    tools.update(module_tools(cfg))
    _drop_clashing_as(tools)
    # base ones (every session has them): the core does not ask the owner's "yes" for them, the session has them anyway (2026-10-07)
    for tid in cfg.get("base") or []:
        if tid in tools:
            tools[tid] = {**tools[tid], "base": True}
    return tools


def as_name_ok(name):
    """An `as` the core takes: a name as it is (no spaces around it), printable, up to 200 characters. One rule for the
    catalog and mcpwatch.confirm_list, so the owner's confirmation gate holds the very name the session reports."""
    return isinstance(name, str) and bool(name) and name == name.strip() and len(name) <= 200 and name.isprintable()


def _drop_clashing_as(tools):
    """`as` must be a clean name (as_name_ok) of a server nobody else has: not another tool's id and not another entry's
    `as` (the first in the settings keeps it). Any other `as` is dropped with a log line and the entry goes to sessions
    under its id; a clash with a plugin's server name is known only at launch (launch_flags)."""
    taken = set()
    for tid, t in list(tools.items()):
        if "as" not in t:
            continue
        name = t["as"]
        if not as_name_ok(name):
            print(f"catalog: mcp {tid!r:.60}: as={name!r:.80} is not a server name — connected under its id",
                  file=sys.stderr, flush=True)
            tools[tid] = {k: v for k, v in t.items() if k != "as"}
        elif name != tid and (name in tools or name in taken):
            print(f"catalog: mcp {tid!r:.60}: as={name!r:.80} is taken (another tool's id or another entry's as) — "
                  "connected under its id", file=sys.stderr, flush=True)
            tools[tid] = {k: v for k, v in t.items() if k != "as"}
        else:
            taken.add(name)


def set_tools(s):
    """Plugins of one skill set ({"name", "path"}): a plugin with no name or with a source outside the set's folder
    is skipped with a log line."""
    tools = {}
    if not (isinstance(s, dict) and isinstance(s.get("path"), str) and isinstance(s.get("name"), str)):
        raise ValueError("a set is {name, path}")
    if s.get("plugin"):
        return _one_plugin(s)
    root = s["path"]
    try:
        market = read_settings(os.path.join(root, ".claude-plugin", "marketplace.json"))
    except (OSError, ValueError):
        return tools
    if not market:   # no marketplace.json: read_settings gives {}; the set is skipped, as before, without git
        return tools
    plugins = market.get("plugins") if isinstance(market, dict) else None
    plugins = plugins if isinstance(plugins, list) else []
    version = _git_version(root)
    # each plugin in the set has its own version, its folder in the commit: editing the README or a neighbour does not touch it
    trees = _git_trees(root, [os.path.normpath(p["source"]) for p in plugins
                              if isinstance(p, dict) and isinstance(p.get("source"), str)])
    inside = os.path.realpath(root) + os.sep
    for p in plugins:
        if not (isinstance(p, dict) and isinstance(p.get("name"), str) and p["name"]):
            print(f"catalog: set {s['name']}: a plugin without a name — skipped", file=sys.stderr, flush=True)
            continue
        src, ver = p.get("source"), version
        if s.get("market") and not isinstance(src, str):
            # a set from the hub's marketplace is loaded only from its own pinned commit: a copy of an external
            # source installed by Claude Code itself is not pinned by the hub (marketplaces spec 3.6)
            print(f"catalog: set {s['name']}: {p['name']} — an external source of the set, not pinned — skipped",
                  file=sys.stderr, flush=True)
            continue
        if isinstance(src, str):
            path = os.path.normpath(os.path.join(root, src))
            if not (os.path.realpath(path) + os.sep).startswith(inside):
                print(f"catalog: set {s['name']}: {p['name']} leads outside the set's folder — skipped",
                      file=sys.stderr, flush=True)
                continue
            copy = installed_path(f"{p['name']}@{s['name']}") if _managed_clone(root) else None
            if copy:
                # Claude Code does not run the marketplace clone, it has no dependencies (node_modules): the plugin's
                # server crashed at start; an installed plugin is its copy, as in the owner's terminal. A new version
                # is a new copy (another folder): the folder hash in the clone changes before Claude Code updates the
                # copy, and a restart on it would raise the session on the same old copy. The version is the one that
                # runs: Claude Code names the copy's folder after the version
                path, src, ver = copy, None, os.path.basename(os.path.normpath(copy))
        else:
            path = installed_path(f"{p['name']}@{s['name']}")  # git source: the copy Claude Code installed
        if not path:
            continue
        tree = trees.get(os.path.normpath(src)) if isinstance(src, str) else None
        # cached only where the files are a commit's (a set from a catalog, Claude Code's copy): a working copy changes
        # without a new commit, and a hook added there must make the plugin risky at once
        pinned = bool(s.get("market")) or not isinstance(src, str)
        runs = _runs(path, p["name"], p, ver if pinned else None, tree if pinned else None)
        tools[f"{p['name']}@{s['name']}"] = {
            "kind": "plugin", "path": path,
            "description": p.get("description", ""), **runs,
            "risk": sorted(set(_risk(p.get("keywords"))) | set(runs.get("risk") or [])),
            "source": s["name"], "version": ver, **_plugin_name(path), **({"tree": tree} if tree else {})}
    return tools


def _one_plugin(s):
    """A set record of one plugin (`plugin: true`, a skill set entry of a Claude Code or Codex catalog, core/mpinstall):
    the folder is the plugin itself, the tool is <entry>@<catalog>."""
    entry, path = s.get("entry"), s["path"]
    if not isinstance(entry, str) or not entry:
        raise ValueError("a one-plugin set has an entry")
    if not os.path.isdir(path):
        print(f"catalog: set {s['name']}: {entry} — no folder {path!r:.200}, skipped", file=sys.stderr, flush=True)
        return {}
    man = _manifest_of(path)
    commit = s.get("commit") if isinstance(s.get("commit"), str) else None
    ver = s.get("version") if isinstance(s.get("version"), str) else (commit or "")[:12] or None
    runs = _runs(path, entry, {}, ver, commit)
    about = man.get("description") if isinstance(man.get("description"), str) else ""
    return {f"{entry}@{s['name']}": {"kind": "plugin", "path": path, "description": about, **runs,
                                     "risk": runs.get("risk") or [], "source": s["name"], "version": ver,
                                     "plugin_name": _plugin_name(path).get("plugin_name") or os.path.basename(path)}}


def _manifest_of(path):
    """The plugin's manifest: Claude Code's, else Codex's, else {}."""
    for rel in ((".claude-plugin", "plugin.json"), (".codex-plugin", "plugin.json")):
        try:
            with open(os.path.join(path, *rel), encoding="utf-8-sig") as f:
                man = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(man, dict):
            return man
    return {}


def _plugin_name(path):
    """{"plugin_name": <name in .claude-plugin/plugin.json>} or {}: its MCP servers run as plugin:<that name>:<server>
    (plugin_servers), and it may differ from the catalog id, so the core finds a plugin's servers through it."""
    try:
        with open(os.path.join(path, ".claude-plugin", "plugin.json"), encoding="utf-8-sig") as f:
            name = json.load(f).get("name")
    except (OSError, ValueError, AttributeError):
        return {}
    return {"plugin_name": name} if isinstance(name, str) and name else {}


def installed_path(key):
    """The folder of plugin `<name>@<set>` that the owner installed in Claude Code (installed_plugins.json); None if absent."""
    path = os.path.join(_claude_dir(), "plugins", "installed_plugins.json")
    try:
        with open(path, encoding="utf-8") as f:
            copies = (json.load(f).get("plugins") or {}).get(key) or []
    except (OSError, ValueError, AttributeError):
        return None
    # the user-scope copy first: Claude Code runs it everywhere; project/local copies are for their own folders
    copies = [c for c in copies if isinstance(c, dict)] if isinstance(copies, list) else []
    copies = sorted(copies, key=lambda c: c.get("scope") != "user")
    return next((c["installPath"] for c in copies if isinstance(c.get("installPath"), str) and os.path.isdir(c["installPath"])),
                None)


def _claude_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def _managed_clone(root):
    """The set is a clone maintained by Claude Code (<claude settings>/plugins/marketplaces/…), not its own checkout."""
    managed = os.path.normcase(os.path.realpath(os.path.join(_claude_dir(), "plugins", "marketplaces")))
    return os.path.normcase(os.path.realpath(root)).startswith(managed + os.sep)


def plugin_options(key):
    """Settings of plugin `<name>@<set>` that the owner entered in Claude Code (pluginConfigs in settings.json); Claude
    Code keeps secret fields elsewhere, so they are not here."""
    try:
        with open(os.path.join(_claude_dir(), "settings.json"), encoding="utf-8-sig") as f:
            options = ((json.load(f).get("pluginConfigs") or {}).get(key) or {}).get("options")
    except (OSError, ValueError, AttributeError):
        return {}
    return options if isinstance(options, dict) else {}


def module_tools(cfg):
    """Module tools: a module has modules/<name>/catalog.py with entries(cfg), in the core or in an embed's module
    folder (embed/<embed>/modules/<name>/catalog.py). The core does not know what is there.
    We look for a module, not a file: an older .deb has its code compiled to C (Nuitka, until 2026-11-01), there are no .py files."""
    import importlib
    import importlib.util
    from core import embed, modules
    out = {}
    root = os.path.join(REPO, "modules")
    names = sorted(os.listdir(root)) if os.path.isdir(root) else []
    if hasattr(embed, "module_folders") and hasattr(modules, "embed_on"):   # an embed's modules too (Embed API 1.1):
        for _, d in embed.module_folders(REPO):                               # modules/ looks into the embeds
            got = modules.read(d, "builtin")   # one the owner turned off (or off by default) brings no tools
            if os.path.basename(d) not in names and not got["errors"] and modules.embed_on(cfg, got["name"], got["manifest"]):
                names.append(os.path.basename(d))
    for name in names:
        if not name.isidentifier():
            continue
        try:
            found = importlib.util.find_spec(f"modules.{name}.catalog")
        except ImportError:   # a folder without a module (data, __pycache__)
            found = None
        if found:
            out.update(importlib.import_module(f"modules.{name}.catalog").entries(cfg))
    return out


NO_TOOLS = ("SendMessage", "ListAgents")
# a core plugin that became another: a session whose set still names the old one starts with the new one (the Hub
# Developer of a packaged hub had its own reduced plugin until the one Hub Developer for every hub, core/dev.py)
RENAMED = {"core:hub-app-developer": "core:hub-developer"}
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def effort_flags(cfg, level=None):
    """`--effort <level>`: the session's own level, otherwise from the settings; an unknown one means no flag (claude
    would not start with it), with a log line."""
    level = level or cfg.get("effort") or "medium"
    if level in EFFORTS:
        return ["--effort", level]
    print(f"catalog: effort {level!r} is not one of {'|'.join(EFFORTS)} — sessions without --effort", file=sys.stderr, flush=True)
    return []


# session models (owner decision 2026-10-05: the ability to change the model for anyone): Claude Code itself
# understands a short name as the latest model of the family; a full id `claude-…` is used as is
MODELS = ("opus", "sonnet", "fable", "haiku")


def known_model(model, codex=False):
    # Codex: codex/<model> — a session on Codex CLI, only where the hub has the Codex host
    # (codex_host below; the fold — by the core's state, codex.host); elsewhere refused as in main
    return model in MODELS or str(model or "").startswith(("claude-", "codex/") if codex else "claude-")


def codex_host(cfg):
    """Codex: the hub has the Codex host — hub.json "host": "codex", or HUB_CODEX=1 (sessions on a
    codex/… model next to claude; core/daemon.make_host)."""
    return (cfg or {}).get("host") == "codex" or os.environ.get("HUB_CODEX") == "1"


def codex_state(st):
    """The same for the fold: the daemon's fact codex.host (core/daemon.codex_tick) — never on a Claude hub."""
    return bool((st.get("codex") or {}).get("host"))


def model_flags(cfg, model=None):
    """`--model <id>`: the session's own model, otherwise from the settings (`model` in hub.json), otherwise no flag,
    meaning Claude Code's default model; an unknown one means no flag, with a log line."""
    model = model or cfg.get("model")
    if not model:
        return []
    if str(model).startswith("codex/"):   # the Codex host's model: claude runs on its default one
        print(f"catalog: {model!r} — a Codex host model, claude starts without --model", file=sys.stderr, flush=True)
        return []
    if known_model(model):
        return ["--model", model]
    print(f"catalog: model {model!r} is not one of {'|'.join(MODELS)} nor claude-… — sessions without --model", file=sys.stderr,
          flush=True)
    return []


def plugin_servers(path, key=None):
    """The MCP a plugin brings: --strict-mcp-config drops such servers, and the session is left without the tools
    (Atlassian). We pass them ourselves under the name `plugin:<plugin>:<server>`: Claude Code stores the login (OAuth)
    under it, so the server connects without a new login. Where a plugin declares them follows Claude Code
    (plugins-reference, `mcpServers`): first `.mcp.json` in the root, then the manifest's `mcpServers` in order, a
    dict of servers, a `./…json` path to such a dict or an array of them; a server with the same name declared later
    replaces the earlier one (as with chrome-devtools-mcp: the server only in plugin.json, and the session got the
    plugin without the browser). Files only inside the plugin folder. The hub does not unpack `.mcpb`/`.dxt` bundles
    and skips a path that gave no servers; both get a line in the hub log. key (`<name>@<set>`) is whose settings to
    substitute into ${user_config.*}."""
    root = os.path.realpath(path)

    def read(rel):
        """Servers from JSON at a path relative to the plugin folder (with the `mcpServers` wrapper or without); None
        if unreadable. The file only inside the plugin and after resolving links: otherwise a plugin (from git it
        brings links too) could point at ~/.claude.json and pull someone else's servers with keys into the session."""
        full = os.path.realpath(os.path.join(path, rel))
        try:
            if os.path.commonpath([full, root]) != root:
                return None
        except ValueError:   # Windows: another drive
            return None
        try:
            with open(full, encoding="utf-8-sig") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        data = data.get("mcpServers", data) if isinstance(data, dict) else None
        return data if isinstance(data, dict) else None

    try:
        with open(os.path.join(path, ".claude-plugin", "plugin.json"), encoding="utf-8-sig") as f:
            manifest = json.load(f)
    except FileNotFoundError:
        # no Claude Code manifest: Claude Code names the plugin after its folder and still takes .mcp.json; a Codex
        # plugin declares its servers in .codex-plugin/plugin.json (a skill set entry, core/mpinstall)
        codex = _manifest_of(path) if os.path.exists(os.path.join(path, ".codex-plugin", "plugin.json")) else {}
        manifest = {"name": os.path.basename(os.path.normpath(path)),
                    **({"mcpServers": codex["mcpServers"]} if codex.get("mcpServers") else {})}
    except (OSError, ValueError):
        return {}
    plugin = manifest.get("name") if isinstance(manifest, dict) else None
    if not plugin:
        return {}
    found = read(".mcp.json")
    if found is None:
        if os.path.lexists(os.path.join(path, ".mcp.json")):
            sys.stderr.write(f"catalog: plugin {plugin}: .mcp.json could not be read (not JSON, no servers or a link "
                             "outside the plugin folder), the session will be without these servers\n")
        found = {}
    declared = manifest.get("mcpServers")
    for item in declared if isinstance(declared, list) else [declared] if declared else []:
        if isinstance(item, dict):
            found.update(item)
        elif isinstance(item, str) and item.lower().endswith((".mcpb", ".dxt")):
            sys.stderr.write(f"catalog: plugin {plugin}: the hub does not connect MCP from the bundle {item} — the session "
                             "will be without this server\n")
        elif isinstance(item, str):
            got = read(item)
            if not got:
                sys.stderr.write(f"catalog: plugin {plugin}: mcpServers \"{item}\" could not be read (no file inside the "
                                 "plugin, not JSON or no servers in it), the session will be without them\n")
            found.update(got or {})
    # Claude Code substitutes ${CLAUDE_PLUGIN_ROOT} only for the plugin's own servers; from --mcp-config it would
    # arrive as is. ${user_config.*} and ${VAR} / ${VAR:-default} (the session's environment, expand_env) too: the
    # string as is would be passed to the server. With no value (a secret field is not in settings.json) the server
    # is not started, see `server` below
    options = plugin_options(key) if key else {}
    for name, spec in (manifest.get("userConfig") or {}).items() if isinstance(manifest.get("userConfig"), dict) else ():
        if isinstance(spec, dict) and options.get(name) is None and spec.get("default") is not None:
            options = {**options, name: spec["default"]}   # an option never entered takes the default of plugin.json
    session_vars = {**platforms.current.owner_env(os.environ), **user_env()}
    ref = re.compile(r"\$\{(?:user_config\.([^}]*)|([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?)\}")
    # a non-string option goes as JSON; null in the settings is the same as no value
    text = lambda x: x if isinstance(x, str) else json.dumps(x)  # noqa: E731

    def fill(v, lost):
        """v with the references filled in in one pass (a value never gets expanded again); a reference without a value
        and a default becomes an empty string, its name (user_config.<key> or the variable's) goes into lost."""
        if isinstance(v, str):
            def one(m):
                if m.group(1) is not None:
                    got = options.get(m.group(1))
                    if got is None:
                        lost.append("user_config." + m.group(1))
                    return "" if got is None else text(got)
                if m.group(2) == "CLAUDE_PLUGIN_ROOT":
                    return path
                got = expand_env(m.group(2), m.group(3), session_vars)
                if got is None:
                    lost.append(m.group(2))
                return got or ""
            return ref.sub(one, v)
        if isinstance(v, list):
            return [fill(x, lost) for x in v]
        return {k: fill(x, lost) for k, x in v.items()} if isinstance(v, dict) else v

    def server(srv):
        """(the server, the names it lacks). A reference without a value and a default in the address, command,
        arguments or a header means the server is not started: it would connect with an empty value and be refused (or
        exit at once). An env key is the one exception: with the same name already in the session environment the key
        is left out and the server reads that variable itself (its fallback); without it the server is not started."""
        out, lost = {}, []
        for k, x in srv.items():
            if k == "env" and isinstance(x, dict):
                out[k] = {}
                for a, b in x.items():
                    gone = []
                    val = fill(b, gone)
                    if not gone:
                        out[k][a] = val
                    elif not session_vars.get(a):
                        lost += gone
            else:
                out[k] = fill(x, lost)
        return out, lost

    servers = {}
    for n, srv in found.items():
        if not isinstance(srv, dict):
            continue
        got, lost = server(srv)
        if lost:
            names = ", ".join(sorted(set(lost)))
            if _plugin_skipped.get((plugin, n)) != names:   # one line per set of names, not per spawn
                _plugin_skipped[(plugin, n)] = names
                sys.stderr.write(f"catalog: plugin {plugin}: server {n} is not started — {names} used in its address, "
                                 "command, headers or env have no value (the plugin's settings, the session environment) "
                                 "and no default; it comes up at a launch after they are set\n")
            continue
        _plugin_skipped.pop((plugin, n), None)
        servers[f"plugin:{plugin}:{n}"] = got
    return servers


_plugin_skipped = {}   # (plugin, server) -> the missing variables already reported (names only, never values)


def expand_env(name, default, env):
    """The value of ${NAME} / ${NAME:-default} from env, as in a shell and in Claude Code: a variable that is set and
    not empty; otherwise the default; None if there is none (the caller decides what a missing value means)."""
    got = env.get(name)
    return got if isinstance(got, str) and got else default


def expand_self_ref(env, base):
    """A variable's reference to itself ("PATH": "/my/bin:$PATH" or "${PATH}") is expanded with the value from `base`
    (the hub environment), as in a shell. If it is absent there or empty, the value is read as a list of paths: an
    element that is the reference as a whole is dropped (an empty path element would mean "and the current
    folder"), a reference inside an element becomes empty; if nothing is left, the variable is not set. Other $NAME
    (and longer ones: $PATH_EXTRA) and non-strings are left alone. Shared by the MCP env from the settings and browser.env."""
    out = {}
    for k, v in env.items():
        if isinstance(v, str):
            ref = re.compile(r"\$(?:\{%s\}|%s(?![A-Za-z0-9_]))" % (re.escape(k), re.escape(k)))
            val = base.get(k, "")
            if val:
                v = ref.sub(lambda _: val, v)
            elif ref.search(v):
                kept = [ref.sub("", p) for p in v.split(os.pathsep) if not ref.fullmatch(p)]
                if not kept:
                    continue
                v = os.pathsep.join(kept)
        out[k] = v
    return out


def write_private(path, data):
    """JSON for the owner only (0600) and whole: into a temp file next to it (mkstemp gives 0600 at once) and then a
    replace, so a reader sees neither half of it nor the old permissions; on failure the temp file is removed (MCP
    server env may hold keys and tokens)."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        platforms.current.replace(tmp, path)   # Windows: waits while another process reads the file
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def expand_server_env(server):
    """The env of an MCP server from the settings (hub.json -> mcp) may refer to itself from the hub environment, like
    browser.env (modules/browser/browsers.py:env()): "$PATH" is expanded to the real PATH rather than going into the
    session's mcp.json as a literal string (2026-10-13: an MCP server without system paths in PATH failed with
    CONNECTION_CLOSED on a real action even though the handshake passed)."""
    env = server.get("env")
    if not isinstance(env, dict):
        return server
    return {**server, "env": expand_self_ref(env, os.environ)}


def project_mcp_file(path, cwd, state_dir, name):
    """The project's .mcp.json for --mcp-config. claude takes that file whole, so the servers the project switched off
    (`disabledMcpjsonServers`, core/mcplogin.rejected) would come up all the same: without them it is a copy in the
    session's folder (in the state folder, outside the project's git tree, 0600: the env of the servers may hold keys; a copy that cannot be written fails the launch, as
    the session's own mcp.json does). Nothing switched off, or a file that cannot be read or is not
    {"mcpServers": {...}} (claude reports that itself) - the file as it is."""
    from core import mcplogin
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return path
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    off = mcplogin.rejected(cwd) & set(servers) if isinstance(servers, dict) else None
    d = os.path.join(state_dir, "sessions", name)
    copy = os.path.join(d, "project-mcp.json")
    if not off:
        with contextlib.suppress(OSError):
            os.unlink(copy)   # a switch-off that was lifted: no copy of the file (with the servers' env) is left behind
        return path
    os.makedirs(d, exist_ok=True)
    write_private(copy, {**data, "mcpServers": {k: v for k, v in servers.items() if k not in off}})
    return copy


def project_mcp_path(cfg, cwd, project):
    """The project's .mcp.json that a session in cwd is launched with (launch_flags), or None: not for the owner's
    connectors mode, not for a session of a narrowed project, not without the file."""
    path = os.path.join(cwd or "", ".mcp.json")
    return path if not cfg.get("connectors") and project == "all" and cwd and os.path.exists(path) else None


def _endpoint(server):
    """What an MCP server is: its address, or its command with the arguments, and the headers and env it is started with
    (the same address with other credentials is another access). None: neither address nor command (a malformed entry)."""
    if not isinstance(server, dict):
        return None
    keys = tuple(json.dumps(server.get(k) or {}, sort_keys=True, default=str) for k in ("headers", "env"))
    if isinstance(server.get("url"), str):
        return ("url", server["url"].rstrip("/"), *keys)
    args = server.get("args")
    if isinstance(server.get("command"), str) and (args is None or isinstance(args, list)):
        return ("command", server["command"], tuple(map(str, args or ())), *keys)
    return None


def repo_tools(tools, catalog, cfg, cwd, project):
    """Which of the catalog tools `tools` the session in cwd gets from its own project anyway: an MCP server of the
    catalog whose name is in the project's .mcp.json at the same address (the same command, headers and env), and not
    switched off there.
    Asking the owner for those is a question about what the session has: the project passes its file with the launch
    (launch_flags). Same name at another address is another server — not this tool."""
    from core import mcplogin
    if not project_mcp_path(cfg, cwd, project):
        return []
    theirs = mcplogin.project_servers(cwd)
    have = []
    for tid in tools:
        t = catalog.get(tid) or {}
        mine = _endpoint(t.get("server")) if t.get("kind") == "mcp" else None
        if mine is not None and _endpoint(theirs.get(t.get("as") or tid)) == mine:
            have.append(tid)
    return have


def launch_flags(tools, cfg, catalog, kind, cwd, project, state_dir, name, effort=None, model=None, deny=()):
    """`claude` flags for a session's set: plugins (--plugin-dir), MCP (--mcp-config), connector isolation; deny is
    further session prohibitions (mini-hub: edits in its own folder, core/minihub.deny)."""
    flags = ["--setting-sources", cfg.get("setting_sources") or "project,local"]
    # sessions write to each other only through the hub, `hub send` (owner decision 2026-10-01): the built-in
    # SendMessage/ListAgents send through Claude Code sockets bypassing the core: not in the journal, no queue and no
    # receipts (32 of them in a day)
    flags += ["--disallowedTools", *NO_TOOLS, *deny] + effort_flags(cfg, effort) + model_flags(cfg, model)
    dirs, servers = _set_of(tools, cfg, catalog, kind)
    flags += dirs
    if not cfg.get("connectors"):
        flags.append("--strict-mcp-config")  # only what the core passed
        project_mcp = project_mcp_path(cfg, cwd, project)
        if project_mcp:
            # project MCP (owner decision 2026-09-29: load everything a project brings) - except what its owner switched off
            flags += ["--mcp-config", project_mcp_file(project_mcp, cwd, state_dir, name)]
    if servers:
        d = os.path.join(state_dir, "sessions", name)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "mcp.json")
        write_private(path, {"mcpServers": servers})   # server env may hold keys and tokens
        flags += ["--mcp-config", path]
    return flags


def session_servers(tools, cfg, catalog, kind, **_):
    """The MCP servers of a session's set, as launch_flags puts them in the session's own --mcp-config (the project's
    .mcp.json aside), with nothing written: a tool granted mid-turn is connected to the running process with them
    (hosts/streamjson.mcp_set)."""
    return _set_of(tools, cfg, catalog, kind)[1]


def _set_of(tools, cfg, catalog, kind):
    """(--plugin-dir flags, MCP servers) of a session's set: base, the hub's own, granted, and what each brings."""
    ids = list(cfg.get("base") or []) + (list(cfg.get("hub_tools") or []) if kind == "hub" else []) + list(tools)
    ids = [RENAMED.get(tid, tid) if tid not in catalog else tid for tid in ids]
    ids += [w for tid in ids for w in (catalog.get(tid) or {}).get("with") or []]  # a tool comes with its skill
    flags, servers = [], {}
    # catalog MCP after plugins: an `as` that is a plugin's server name is a clash, found only here
    plugins_own = set()   # server names a plugin brought: the twin rule below drops only those, never a catalog one
    for tid in sorted(dict.fromkeys(ids), key=lambda i: (catalog.get(i) or {}).get("kind") == "mcp"):
        t = catalog.get(tid)
        if not t:
            continue
        if t["kind"] == "plugin":
            flags += ["--plugin-dir", t["path"]]
            own = plugin_servers(t["path"], tid)
            servers.update(own)
            plugins_own.update(own)
        elif t["kind"] == "mcp":
            key = t.get("as") or tid
            if key != tid and key in servers:
                sys.stderr.write(f"catalog: mcp {tid!r:.60}: as={key!r:.80} is a plugin's server name — connected "
                                 "under its id\n")
                key = tid
            servers[key] = expand_server_env(t["server"])
            plugins_own.discard(key)   # a catalog id that is itself plugin:<p>:<s> replaced the plugin's server
    for n in cfg.get("mcp_deny") or ():   # a plugin server the owner switched off on this machine
        # only what a plugin brought (not a catalog server); a stray entry (a list, a number) is skipped
        if isinstance(n, str) and n in plugins_own:
            plugins_own.discard(n)
            servers.pop(n, None)
    # a server that is granted to this session and also from the catalog is one connection, the catalog one: the
    # plugin's twin waited for its own login next to the working one. If not granted to the session, nothing hinders
    # the plugin's server, it stays
    for key in [k for k in servers if k in plugins_own and k.startswith("plugin:") and k.rsplit(":", 1)[-1] in ids and
                (catalog.get(k.rsplit(":", 1)[-1]) or {}).get("kind") == "mcp"]:
        sys.stderr.write(f"catalog: {key} — the same server is granted from the catalog ({key.rsplit(':', 1)[-1]}), one "
                         "connection comes up: the catalog's\n")
        del servers[key]
    return flags, servers


# names that hub.json `session_env_values` may set for sessions (an object name -> string). A short list on purpose: the
# section is for settings of the hub's sessions that differ from the owner's terminal, not a way to carry secrets
SESSION_ENV_VALUES = ("CLAUDE_CODE_SUBAGENT_MODEL",)
_session_env_logged = set()   # names already reported as not allowed: one stderr line per name, not per spawn


def session_env(cfg):
    """The session's claude variables from the hub settings: forks disabled means CLAUDE_CODE_FORK_SUBAGENT=0;
    `session_env_values` (name -> string, names from SESSION_ENV_VALUES) are set stripped (blank — skipped) and, coming
    after user_env, win over the same name it brought from the owner's settings.json; a module's launch.env comes later
    and may override them. Other names are ignored (logged, no value); a section that is not an object — load_config."""
    env = {"CLAUDE_CODE_FORK_SUBAGENT": "0"} if cfg.get("fork_subagents") is False else {}
    values = cfg.get("session_env_values")
    for name, value in (values.items() if isinstance(values, dict) else ()):
        if name not in SESSION_ENV_VALUES:
            if name not in _session_env_logged:
                _session_env_logged.add(name)
                shown = repr(name)   # escapes control and bidi characters; cut after escaping, so the line is bounded
                shown = shown[:80] + "…" if len(shown) > 80 else shown
                sys.stderr.write(f"catalog: session_env_values: the name {shown} is not in the allowed list "
                                 f"({', '.join(SESSION_ENV_VALUES)}) — skipped\n")
        elif isinstance(value, str) and value.strip():
            env[name] = value.strip()
    return env


# how long claude waits for an MCP server to connect, ms: when the whole fleet came up, the default 30 s was not
# enough for ms365 and the browser (2026-10-01 and 10-02, CONNECT_TIMEOUT); 60 s was not either: ms365 via npm exec
# failed to come up for one session 4 times in two days at load ~13 (2026-10-07), so 3 minutes; a value of its own
# in the environment takes precedence
MCP_TIMEOUT = "180000"

# what from the owner's settings `env` the hub's claude needs: login and network (owner decision 2026-10-02: only
# this; the rest of env is modes and experiments of his terminal, e.g. AGENT_TEAMS, hub sessions do not need them)
USER_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
            "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy", "NODE_EXTRA_CA_CERTS")


def user_env():
    """Login and network from the `env` of the owner's Claude Code settings (~/.claude/settings.json or
    $CLAUDE_CONFIG_DIR) go into the hub claude's environment. The hub loads settings without user (setting_sources),
    and claude lost the login from there: under launchd every turn gave "Not logged in · Please run /login" (a
    contributor's PR of 2026-10-02: the login token is in the settings env). Plugins, hooks, permissions and the
    rest of the owner's env still do not reach sessions, except the names he listed himself in hub.json `user_env`
    (credentials of work MCP and skills; the values come only from settings.json and are not copied into hub.json)."""
    path = os.path.join(_claude_dir(), "settings.json")
    try:
        with open(path, encoding="utf-8-sig") as f:
            env = json.load(f).get("env")
    except (OSError, ValueError, AttributeError):
        return {}
    names = set(USER_ENV) | set(_more_user_env())
    return {k: str(v) for k, v in env.items() if k in names} if isinstance(env, dict) else {}


def model_remap(name):
    """The name remaps the models Claude Code takes: ANTHROPIC_MODEL, ANTHROPIC_DEFAULT_<alias>_MODEL,
    ANTHROPIC_SMALL_FAST_MODEL, CLAUDE_CODE_SUBAGENT_MODEL."""
    return (name in ("ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL") or
            name.startswith("ANTHROPIC_DEFAULT_") and name.endswith("_MODEL"))


def service_env(base=None):
    """The environment of the core's own model calls (the watchdog and its "working on" line, hubmod.model through
    core/modelcall, the critic's claude, the stops audit): base (the caller's environment; none — user_env alone, and
    the provider is judged by it alone) and user_env, without the names that remap models (model_remap) — from user_env and from base alike: a remap given to
    sessions sent them all to another model (2026-10-27: `modelcall model=haiku` answered from opus). Sessions and
    executors keep user_env with it. A person's own provider in this environment (own_provider) keeps the names: there
    they are how the person names the provider's models."""
    env = {**(base or {}), **user_env()}
    return env if own_provider(env) else {k: v for k, v in env.items() if not model_remap(k)}


def own_provider(env):
    """The environment sends claude to a provider of the person's own: Bedrock, Vertex and the like, or an own
    ANTHROPIC_BASE_URL that is not the hub's account gateway (core/modelrule.own_endpoint)."""
    from core import modelrule, modlaunch
    return modlaunch.provider(env) or modelrule.own_endpoint(env)


def _more_user_env():
    """Names from hub.json `user_env` (a list of strings); no file, not JSON, not a list gives nothing. hub.json is
    the same as load_config's: if it is not in the OS settings folder, the old place, the state folder."""
    path = os.environ.get("HUB_CONFIG") or os.path.join(config_dir(), "hub.json")
    if not os.environ.get("HUB_CONFIG") and not os.path.exists(path):
        from core.daemon import default_state_dir   # not on every call: most hubs have the file in the new place
        path = config_path(default_state_dir())
    try:
        cfg = read_settings(path)
    except (OSError, ValueError):
        return []
    names = cfg.get("user_env") if isinstance(cfg, dict) else None
    return [n for n in names if isinstance(n, str)] if isinstance(names, list) else []
