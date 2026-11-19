"""`hub instance new|list|uninstall` — several hubs on one machine (core/instance.py; the spec of several hubs, 2.5–2.6).

new <name>: another hub next to the main one — its own clone of the main hub's code in <state>/code (a local `git
clone`: on one disk git links the object files, seconds and almost no space; the clone does not depend on the main
hub's repo), its folders, ports, service and command `hub-<name>`, all put by the installer of this OS with
HUB_INSTANCE (deploy/install.sh, deploy/mac/install.sh, deploy/windows/install.ps1). A page that does not answer in a
minute: the service and the commands go, the folders stay, the reason is the last lines of its hub-up log. The one-line
installer calls it the same way (`--instance <name>`): non-interactive, the exit code says.

list: the hubs of this machine (the main one and the registry) — command, page, running or not, engine, code.

uninstall <name>: that hub's own `hub uninstall` (its code, its folders): the service, the commands, the registry
entry; the data only with --purge. The main hub — `hub uninstall`."""
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import platforms
from core import i18n, instance, restart, uninstall
from core import net  # noqa: F401 — its page on 127.0.0.1 is asked past the machine's proxy (tests/test_net.py)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WAIT = 60   # s for the new hub's page to answer
# the environment of this hub that must not reach the installer of another one
OWN_ENV = ("HUB_STATE_DIR", "HUB_CONFIG_DIR", "HUB_CONFIG", "HUB_WEB_PORT", "HUB_FILES_PORT", "HUB_SESSION",
           "HUB_TELEGRAM_CONFIG", "HUB_BROWSER_PORT", "HUB_LABEL", "HUB_SERVICE", "HUB_SERVICE_INTERACTIVE",
           "HUB_CODEX_BIN", "CODEX_HOME", "HUB_ON_CODEX", "HUB_CORE_MODEL")


class Refused(Exception):
    """A refusal said in this process's language; key and params let a caller say it in another one (the page)."""

    def __init__(self, text, key=None, **params):
        super().__init__(text)
        self.key, self.params = key, params


def _refused(key, **params):
    return Refused(i18n.t(key, **params), key, **params)


def _parse(argv, values, switches):
    """-> (positional, {flag: value or True}); an unknown flag or a flag without its value — Refused."""
    pos, opts, i = [], {}, 0
    while i < len(argv):
        a = argv[i]
        if a in values:
            if i + 1 >= len(argv):
                raise Refused(i18n.t("cli.instance.needs_value", flag=a))
            opts[a], i = argv[i + 1], i + 2
        elif a in switches:
            opts[a], i = True, i + 1
        elif a.startswith("-"):
            raise Refused(i18n.t("cli.instance.unknown_flag", flag=a))
        else:
            pos.append(a)
            i += 1
    return pos, opts


def _git(*args, cwd=None):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       stdin=subprocess.DEVNULL, timeout=600)
    if r.returncode != 0:
        raise Refused((r.stderr or r.stdout).strip()[-300:] or f"git {args[0]}: {r.returncode}")
    return r.stdout.strip()


def code_source(repo, main_state=None):
    """Where the new hub's code comes from: ("git", repo) — a clone of this one; ("shared", repo) — a package without
    git (.deb): the instance runs on it, the package updates it; ("oneline", the layer) — the main hub is of the one-line
    install: the new one gets its own copy of the main hub's layer and the same environment (spec §7.3). The app's code
    layers otherwise — not yet."""
    mark = os.path.join(repo, "PACKAGED")
    if os.path.exists(mark):
        try:
            with open(mark, encoding="utf-8") as f:
                fmt = (json.load(f) or {}).get("format")
        except (OSError, ValueError, AttributeError):
            fmt = None
        if fmt == "app":
            from core import shellnext
            if os.path.isfile(os.path.join(repo, "MANIFEST.json")) and \
                    shellnext.folder_env(main_state or instance.main_state()):
                return "oneline", os.path.realpath(repo)
            raise _refused("cli.instance.app_not_yet")
        return "shared", repo
    if not os.path.isdir(os.path.join(repo, ".git")) and not os.path.isfile(os.path.join(repo, ".git")):
        raise _refused("cli.instance.no_git", path=repo)
    return "git", repo


def clone(repo, code, origin):
    """The main hub's commit (not its own branches or edits) in a local clone, then origin = the main hub's origin
    (none: no origin, no update). Partial clone gone on failure."""
    sha = _git("rev-parse", "HEAD", cwd=repo)
    try:
        branch = _git("symbolic-ref", "--short", "-q", "HEAD", cwd=repo)
    except Refused:
        branch = "main"   # a detached head (the updater keeps the hub on a tag)
    try:
        _git("clone", "--no-checkout", "--quiet", repo, code)
        _git("checkout", "--quiet", "-B", branch or "main", sha, cwd=code)
        if origin:
            _git("remote", "set-url", "origin", origin, cwd=code)
        else:
            _git("remote", "remove", "origin", cwd=code)
    except BaseException:
        platforms.current.rmtree(code)
        raise
    return sha


def origin_of(repo):
    """The main hub's origin as written in its config (not after insteadOf), or ""."""
    try:
        return _git("config", "--get", "remote.origin.url", cwd=repo)
    except Refused:
        return ""


def installer(code, name, platform):
    if platform == "mac":
        return ["sh", os.path.join(code, "deploy", "mac", "install.sh")]
    if platform == "windows":
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                os.path.join(code, "deploy", "windows", "install.ps1"), "-Yes", "-Instance", name]
    return ["sh", os.path.join(code, "deploy", "install.sh"), "-y"]


def answers(port, timeout=2):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=timeout):
            return True
    except Exception:  # noqa: BLE001 — not up yet, any way it says so
        return False


def log_tail(state, n=3):
    try:
        with open(os.path.join(state, "logs", "hub-up.log"), encoding="utf-8", errors="replace") as f:
            lines = [x.rstrip() for x in f.readlines()[-200:] if x.strip()]
    except OSError:
        return []
    return lines[-n:]


def _commands_py():
    import importlib.util
    spec = importlib.util.spec_from_file_location("hub_deploy_commands", os.path.join(REPO, "deploy", "commands.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _command_free(name, state):
    """hub-<name> is nobody's or this hub's own command: a wrapper of it (deploy/commands.py, the earlier Mac one too)
    that names its state folder, or its own <state>\\cmd\\hub-<name>.cmd on Windows. -> another program's path, or None."""
    commands = _commands_py()
    inside = os.path.normcase(os.path.realpath(state)) + os.sep
    for cmd in (f"hub-{name}", f"hub-telegram-{name}"):
        for p in {os.path.join(os.path.expanduser("~"), ".local", "bin", cmd), shutil.which(cmd)}:
            if not p or not os.path.lexists(p) or os.path.normcase(os.path.realpath(p)).startswith(inside):
                continue
            got = commands.leads_to(p)
            if not commands.ours(p, cmd) or got is None or not commands._same_dir(got, state):
                return p
    return None


def main_code():
    """The code the main hub runs (its service names it), else this one: a new hub is a clone of the main hub's."""
    unit = platforms.current.service_unit(instance.main_state()) or {}
    got = instance.code_of(unit["path"]) if unit.get("path") else None
    return got if got and os.path.isfile(os.path.join(got, "bin", "hub")) else REPO


def codex_for(state, say=None, dry=False):
    """The program codex of a new hub on Codex (core/codexinstall.for_instance) -> (kind, path); Refused when there is
    none and none can be put."""
    from core import codexinstall
    try:
        return codexinstall.for_instance(state, say=say, dry=dry)
    except (codexinstall.Failed, OSError) as e:
        raise _refused("cli.instance.codex_failed", why=str(e))


def codex_line(kind, path):
    from core import codexinstall
    pin = codexinstall.PIN
    size = (pin["assets"].get(codexinstall.target()) or (None, 0))[1]
    key = {"shared": "cli.instance.codex_shared", "linked": "cli.instance.codex_linked",
           "own": "cli.instance.codex_own", "install": "cli.instance.codex_install"}[kind]
    return i18n.t(key, path=path, version=pin["version"], mb=round(size / 1e6))


def _clean_env(name):
    env = {k: v for k, v in os.environ.items() if k not in OWN_ENV}
    return {**env, "HUB_INSTANCE": name, "HUB_INSTALL_NO_WAIT": "1"}


def new(argv, repo=None, run=subprocess.run, wait=WAIT, say=print, platform=None, sleep=time.sleep):
    platform = platform or platforms.current.APP_OS
    pos, opts = _parse(argv, ("--title", "--engine", "--claude", "--origin"), ("--dry-run", "--yes"))
    if len(pos) != 1:
        raise Refused(i18n.t("cli.instance.usage_new"))
    name = pos[0]
    engine, claude = opts.get("--engine") or "claude", opts.get("--claude")
    if engine not in instance.ENGINES or claude not in (None, "shared", "own"):
        raise Refused(i18n.t("cli.instance.usage_new"))
    if engine == "codex" and claude:
        raise Refused(i18n.t("cli.instance.claude_only"))
    if not uninstall.marked(instance.main_state()):
        raise Refused(i18n.t("cli.instance.no_main"))
    where = instance.folders(name)
    state, config = where["state"], where["config"]
    have = instance.load(state)
    if not have:   # a broken-off earlier `instance new` of this name goes on; anything else under the name is refused
        if not instance.valid_name(name):
            raise Refused(i18n.t("cli.instance.bad_name", name=name))
        if any(e["name"] == name for e in instance.entries()):
            raise Refused(i18n.t("cli.instance.taken", command=instance.command_name(name)))
        for path in (state, config):
            if os.path.lexists(path):
                raise Refused(i18n.t("cli.instance.folder_taken", path=path))
    elif have.get("name") != name:
        raise Refused(i18n.t("cli.instance.folder_taken", path=state))
    other = _command_free(name, state)
    if other:
        raise Refused(i18n.t("cli.instance.command_taken", path=other))
    kind, source = code_source(repo or main_code())
    src = oneline_source() if kind == "oneline" else None   # before anything is written
    code = os.path.join(state, "code") if kind in ("git", "oneline") else source
    origin = opts.get("--origin") if "--origin" in opts else (origin_of(source) if kind == "git" else "")
    say(i18n.t("cli.instance.plan", name=name, state=state, config=config, code=code,
               engine="Codex" if engine == "codex" else "Claude Code",
               claude=i18n.t("cli.instance.claude_none" if engine == "codex" else
                             "cli.instance.claude_own" if claude == "own" else "cli.instance.claude_shared")))
    if engine == "codex":   # the machine has no codex fit for the hub and none can be put: refused before anything
        say(codex_line(*codex_for(state, dry=True)))
    quoted = platforms.current.join_command([code])
    no_origin = i18n.t("cli.instance.no_origin", code=quoted, config=config) \
        if kind == "git" and not origin else ""
    if no_origin:   # said in the plan and again at the end: an install's output scrolls past
        say(no_origin)
    if opts.get("--dry-run"):
        return 0
    made, made_cfg = not os.path.lexists(state), not os.path.lexists(config)
    try:
        platforms.current.makedirs_private(state)
        if kind == "git" and not os.path.isdir(os.path.join(code, ".git")):
            if os.path.lexists(code):
                platforms.current.rmtree(code)
            say(i18n.t("cli.instance.cloning", repo=source, sha=clone(source, code, origin)[:12]))
        got = instance.setup(name, code, title=opts.get("--title"), engine=engine, claude=claude, origin=origin,
                             browsers=["mac"] if platform == "mac" else (), platform=platform,
                             kind="layer" if kind == "oneline" else None)
        own = oneline_layer(state, source) if kind == "oneline" else None
    except BaseException as e:
        if made and not instance.load(state):   # no anchor yet: nothing of the hub is left under the name
            platforms.current.rmtree(state)
            if made_cfg:
                platforms.current.rmtree(config)
            if any(e["name"] == name and e["gone"] for e in instance.entries()):
                instance.unregister(name)
        if isinstance(e, ValueError):
            raise Refused(str(e))
        raise
    lock = os.path.join(platforms.current.config_dir(), "test-lock.json")   # the windows of the machine's test lock
    if os.path.isfile(lock) and not os.path.exists(os.path.join(config, "test-lock.json")):
        shutil.copy2(lock, os.path.join(config, "test-lock.json"))
    web = got["ports"]["web"]
    if engine == "codex":   # before its service: the hub's sessions start on it
        try:
            say(codex_line(*codex_for(state, say=say)))
        except Refused as e:
            say(str(e))
            instance.unregister(name)   # as after a hub that did not come up: the folders stay, a download goes on
            say(i18n.t("cli.instance.codex_kept", state=state, name=name))
            return 1
    if kind == "oneline":
        r = oneline(state, name, src, own, run)
    else:
        r = run(installer(code, name, platform), env=_clean_env(name), stdin=subprocess.DEVNULL, cwd=code)
    up = r.returncode == 0
    end = time.time() + wait
    while up and not answers(web):
        if time.time() >= end:
            up = False
            break
        sleep(1)
    if up:
        say(i18n.t("cli.instance.up", command=instance.command_name(name), url=f"http://127.0.0.1:{web}/"))
        if no_origin:
            say(no_origin)
        if engine == "codex":
            say(i18n.t("cli.instance.codex_next", command=instance.command_name(name)))
        elif claude == "own":
            say(i18n.t("cli.instance.claude_next", command=instance.command_name(name)))
        return 0
    say(i18n.t("cli.instance.not_up", name=name, seconds=wait))
    for line in log_tail(state):
        say("  " + line)
    # the service and the commands go (its own `hub uninstall`), the folders stay: `hub instance new <name>` again — and
    # the working copies of its executors too (a name that comes back may have sessions with work in them)
    run(own_hub(state, got, ["uninstall", "--yes", *(["--keep-copies"] if keeps_copies(got) else [])]), env=own_env(state, got), stdin=subprocess.DEVNULL,
        cwd=os.path.expanduser("~"))
    say(i18n.t("cli.instance.kept", state=state, name=name))
    return 1


def oneline_source():
    """What a second hub of the one-line install takes from the main hub — its environment, PACKAGED, build key and
    channel — checked before anything is written. -> dict ("key" None: the open build, whose line has none); Refused
    when the main hub has no key (the open build: no address)."""
    from core import shellnext
    main = instance.main_state()
    env = shellnext.folder_env(main)
    if not env:
        raise Refused(i18n.t("cli.instance.no_env", path=os.path.join(main, "app")))
    path = os.path.join(main, "layers", "deliver.json")
    try:
        with open(os.path.join(env, "PACKAGED"), encoding="utf-8") as f:
            mark = json.load(f)
        with open(path, encoding="utf-8") as f:
            deliver = json.load(f)
    except (OSError, ValueError, TypeError):
        mark, deliver = None, None
    if not (isinstance(mark, dict) and isinstance(deliver, dict) and isinstance(deliver.get("url"), str)
            and (isinstance(deliver.get("key"), str) or mark.get("flavor") == "open")):
        raise Refused(i18n.t("cli.instance.no_key", path=path))
    cfg = instance._settings(os.path.join(instance._default_config(), "hub.json"))
    update = cfg.get("update") if isinstance(cfg.get("update"), dict) else {}
    return {"env": env, "mark": mark, "url": deliver["url"],
            "key": deliver["key"] if isinstance(deliver.get("key"), str) else None,
            "channel": "nightly" if update.get("channel") == "main" else "stable"}


def oneline_layer(state, layer):
    """The main hub's layer copied into the new hub's state folder (checked against its manifest). -> its folder."""
    from core import appcode
    layers = os.path.join(state, "layers")
    platforms.current.makedirs_private(layers)
    try:
        got, _ = appcode.install(layer, layers)
    except appcode.Bad as e:
        raise Refused(str(e)) from None
    return os.path.join(layers, got)


def oneline(state, name, src, own, run):
    """A second hub of the one-line install: its own layer's deploy/oneline/setup.py as the install line runs it
    (--as-instance: its service, commands and settings of its anchor), on the main hub's environment, with the main
    hub's build key and channel."""
    from core import shellnext
    key = os.path.join(state, f".install-key.{os.getpid()}")
    if src["key"] is not None:   # the open build has none
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(src["key"] + "\n")
    mark = src["mark"]
    try:
        return run([shellnext.python_of(src["env"]), "-I", "-B", os.path.join(own, "deploy", "oneline", "setup.py"),
                    "--contract", "1", "--env", src["env"], "--server", src["url"],
                    *(["--key-file", key] if src["key"] is not None else []),
                    "--platform", str(mark.get("platform")), "--flavor", str(mark.get("flavor") or "team"),
                    "--runtime", str(mark.get("runtime")), "--channel", src["channel"], "--state", state,
                    "--as-instance", name, "--handed", "--yes", "--no-open", "--no-input"],
                   env=_clean_env(name), stdin=subprocess.DEVNULL, cwd=os.path.expanduser("~"))
    finally:
        with contextlib.suppress(OSError):
            os.unlink(key)


def keeps_copies(anchor):
    """The hub's own code knows `hub uninstall --keep-copies` (a clone made by an earlier core does not: it would refuse
    the whole command). Without it such a core has no step that removes the copies either."""
    code = (anchor.get("code") or {}).get("path") or REPO
    try:
        with open(os.path.join(code, "core", "uninstall.py"), encoding="utf-8", errors="replace") as f:
            return "--keep-copies" in f.read()
    except OSError:
        return False


def own_env(state, anchor):
    env = {k: v for k, v in os.environ.items() if k not in OWN_ENV}
    return {**env, "HUB_STATE_DIR": state, "HUB_CONFIG_DIR": anchor.get("config_dir") or ""}


def own_hub(state, anchor, args):
    """The command line of that hub's own `hub` (its code, its Python): its uninstall knows its own commands."""
    code = (anchor.get("code") or {}).get("path") or REPO
    if not os.path.isfile(os.path.join(code, "bin", "hub")):
        code = REPO
    py = os.path.join(state, "bin", "python3")
    py = py if platforms.current.APP_OS != "windows" and os.path.exists(py) else sys.executable
    return [py, os.path.join(code, "bin", "hub"), *args]


def _engine(config_dir):
    cfg = instance._settings(os.path.join(config_dir, "hub.json")) if config_dir else {}
    return "codex" if cfg.get("host") == "codex" else "claude"


def rows():
    """The hubs of this machine: the main one (when it stands) and every registry entry."""
    out = []
    main = instance.main_state()
    if uninstall.marked(main):
        unit = platforms.current.service_unit(main) or {}
        code = main_code()
        out.append({"name": "", "command": "hub", "title": "", "state": main,
                    "config_dir": platforms.current.config_dir(),
                    "ports": {"web": unit.get("port") or 8787, "files": unit.get("files_port") or 8788},
                    "service": {"kind": unit.get("kind") or "none", "id": unit.get("id") or ""},
                    "code": {"kind": "layer" if os.path.isfile(os.path.join(code, "MANIFEST.json")) else
                             "git" if os.path.lexists(os.path.join(code, ".git")) else "shared",
                             "path": code}})
    for e in instance.entries():
        out.append({**{k: e.get(k) for k in ("name", "title", "state", "config_dir", "ports", "service", "code",
                                              "gone")}, "command": instance.command_name(e["name"])})
    for row in out:
        web = (row.get("ports") or {}).get("web")
        row["engine"] = _engine(row.get("config_dir"))
        row["running"] = bool(not row.get("gone") and restart.running(row["state"]))
        row["port_busy"] = bool(web and not row["running"] and not instance.free(web))
        row["url"] = f"http://127.0.0.1:{web}/" if web else ""
    return out


CODE = {"git": lambda: i18n.t("cli.instance.code_git"), "shared": lambda: i18n.t("cli.instance.code_shared"),
        "layer": lambda: i18n.t("cli.instance.code_layer"),
        "unknown": lambda: i18n.t("cli.instance.code_unknown")}


def show(argv, say=print):
    _, opts = _parse(argv, (), ("--json",))
    got = rows()
    if opts.get("--json"):
        say(json.dumps(got, ensure_ascii=False, indent=1))
        return 0
    if not got:
        say(i18n.t("cli.instance.none"))
        return 0
    for row in got:
        bits = [i18n.t("cli.instance.running" if row["running"] else "cli.instance.stopped"),
                "Codex" if row["engine"] == "codex" else "Claude Code",
                CODE.get((row.get("code") or {}).get("kind"), CODE["unknown"])()]
        if row.get("gone"):
            bits = [i18n.t("cli.instance.gone", name=row["name"])]
        elif row["port_busy"]:
            bits.insert(1, i18n.t("cli.instance.port_busy", port=row["ports"]["web"]))
        if (row.get("service") or {}).get("kind") == "transient":
            bits.append(i18n.t("cli.instance.transient"))
        title = row.get("title") if row.get("title") and row.get("title") != row.get("name") else ""
        say(f"{row['command']:<18} {title + '  ' if title else ''}{row['url']}  " + " · ".join(bits))
    return 0


def remove(argv, run=subprocess.run, say=print):
    pos, opts = _parse(argv, (), ("--purge", "--yes", "--dry-run", "--keep-copies"))
    if len(pos) != 1:
        raise Refused(i18n.t("cli.instance.usage_uninstall"))
    name = pos[0]
    if name in ("", "hub") or name in instance.MAIN_WORDS:
        raise Refused(i18n.t("cli.instance.main_uninstall"))
    entry = next((e for e in instance.entries() if e["name"] == name), None)
    state = entry["state"] if entry else instance.folders(name)["state"]
    anchor = instance.load(state)
    if not entry and not anchor:
        raise Refused(i18n.t("cli.instance.unknown", name=name))
    if not anchor:   # the folder was removed by hand: only its registry entry is left
        if not opts.get("--dry-run"):
            instance.unregister(name)
        say(i18n.t("cli.instance.forgot", name=name))
        return 0
    args = ["uninstall", *[f for f in ("--purge", "--yes", "--dry-run", "--keep-copies") if opts.get(f)]]
    return run(own_hub(state, anchor, args), env=own_env(state, anchor), cwd=os.path.expanduser("~")).returncode


def _say(*args, **kw):
    print(*args, **kw, flush=True)   # before the installer's own lines, in the order they happen


def main(argv, say=_say):
    sub, rest = (argv[0], argv[1:]) if argv else ("", [])
    try:
        if sub == "new":
            return new(rest, say=say)
        if sub == "list":
            return show(rest, say=say)
        if sub == "uninstall":
            return remove(rest, say=say)
        raise Refused(i18n.t("cli.instance.usage"))
    except Refused as e:
        print(str(e), file=sys.stderr)
        return 2
