"""Core modules (Module API, spec §2–3): a folder with module.json and its own process — here they are found, checked and
ordered; the module process and calls to it live in core/modrun.py.

Where they come from (one manifest and one check for all):
- built-in: `modules/<name>/module.json` in this repository — the same schema, no privileges; an embed's
  `embed/<embed>/modules/<name>/module.json` too (core/embed.py), unless the core has a module of that name;
- folders: `"modules": {"paths": ["~/hub-modules"]}` in hub.json — a module folder or a folder of folders.

An external module loads only if it is named in `modules.enabled`: a module is code with the hub's rights, a person turns
it on. The same goes for a built-in one with `"default_off": true`. A module that cannot load is `blocked` with a reason a
person can fix (missing dependency, wrong version, cycle, API); its dependents are blocked too, the rest of the hub works.
Module code is not run here: the search and the graph are safe in any process. A hub without a `modules` section and
without built-in module.json files sees no modules — and behaves as without this file.
"""
import json
import os
import re

from core import i18n, version

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = re.compile(r"[a-z][a-z0-9-]{0,39}")
PROVIDE = re.compile(r"[a-z][a-z0-9.-]{0,60}")
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,80}")
# names that change which code claude and its tools will run, or which session this is: they cannot be passed to a session
# (launch.env), and in the manifest's env field they are refused
ENV_DENIED = {"PATH", "ENV", "BASH_ENV", "SHELL", "SHELLOPTS", "BASHOPTS", "PS4", "PROMPT_COMMAND", "IFS", "HOME",
              "ZDOTDIR", "NODE_OPTIONS", "NODE_PATH", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS",
              "PERL5OPT", "PERL5LIB", "PERLLIB", "RUBYOPT", "RUBYLIB",
              # claude itself: the file before each command, its settings folder, its bash
              "CLAUDE_CODE_SHELL", "CLAUDE_CODE_SHELL_PREFIX", "CLAUDE_ENV_FILE", "CLAUDE_CONFIG_DIR",
              "CLAUDE_CODE_GIT_BASH_PATH",
              # the loader and the session's programs: their settings name code (git's core.hooksPath in XDG_CONFIG_HOME, the
              # gconv module, the OpenSSL engine, the less input filter, the editor, the browser)
              "XDG_CONFIG_HOME", "XDG_CONFIG_DIRS", "GCONV_PATH", "GLIBC_TUNABLES", "OPENSSL_CONF", "OPENSSL_ENGINES",
              "OPENSSL_MODULES", "LESSOPEN", "LESSCLOSE", "PAGER", "MANPAGER", "EDITOR", "VISUAL", "BROWSER",
              "SSH_ASKPASS", "SUDO_ASKPASS",
              # session network without the ENV_NETWORK words: TLS keys to a file, curl and wget settings (proxy, no verification), docker
              "SSLKEYLOGFILE", "CURL_HOME", "WGETRC", "DOCKER_HOST"}
# whole families; HUB_* are the core's internal names (the session label, the nested claude…): only the module's own
# HUB_MOD_<NAME>_* are open to it
ENV_DENIED_PREFIXES = ("LD_", "DYLD_", "PYTHON", "GIT_", "NPM_CONFIG_", "BASH_FUNC_", "HUB_")
# session network — words of the name (HTTPS_PROXY, NODE_EXTRA_CA_CERTS, SSL_CERT_FILE, NODE_TLS_REJECT_UNAUTHORIZED,
# CLAUDE_CODE_CLIENT_CERT…): a proxy with its own CA or without TLS verification reads everything the session sends over
# HTTPS — claude's login (checked with a real claude) and the tokens of its tools (gh, git, curl); removing the login keys does not close this
ENV_NETWORK = {"PROXY", "CA", "CERT", "CERTS", "TLS", "SSL"}
# choosing claude's provider (Bedrock, Vertex, Foundry…): the login is then cloud-based, from machine files — the core cannot strip it
ENV_PROVIDER = "CLAUDE_CODE_USE_"
# names of the closed families that only sign a commit, set a program's output, or are a claude tool
ENV_HARMLESS = {"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
                "GIT_COMMITTER_DATE", "PYTHONUNBUFFERED", "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONDONTWRITEBYTECODE",
                "CLAUDE_CODE_USE_POWERSHELL_TOOL", "CLAUDE_CODE_USE_NATIVE_FILE_SEARCH"}
# capabilities: a list for a person before enabling (not a sandbox); a point with a capability is called only if it is declared.
# Module API slices add their own here
CAPABILITIES = {"launch.env", "cli", "doctor", "exec", "fs", "net", "http", "prompt",
                "core.write",   # writes core events and intercepts their handling (core/corewrite.py)
                "voice.transcribe",   # audio transcription by the hub's voice provider (hubmod.transcribe, 2.4)
                "sessions",   # reads hub session state from its own slice (core/modstate.session_rows, hubmod.session)
                "accounts",   # limits of the hub's Claude accounts in its own slice, no tokens (modstate.account_view, 2.6)
                "messages",   # to whom the owner's messages are addressed and what counted as an answer, no text (2.6)
                "model",   # a model call through the core on the hub's accounts (hubmod.model, 2.7); the owner allows it: modules.model
                # session login (core/profiles.py, 2.8): named in modules.use.profile or after the shadow (core/providers.py)
                "profile",
                "speech",   # the reply voice (modules/voice/speech.py, 2.8): named in modules.use.speech or chosen by hubd
                "stats",   # sends the hub stats report instead of the built-in (core/providers.py, 2.8): no points
                "bus.transport",   # carries envelopes of the hub bus: bus.offered, bus.handed (core/busstate.py, 2.9)
                "task.admit",   # may hold an executor's launch: the task.admit point (core/daemon.admit_pass, 2.10)
                "queue.pause",   # pauses the executor queue with a pause of its own and lifts only that (core/executors.py, 2.10)
                "message.rush"}   # an urgent message (now): typed into the session mid-turn, as the hub's --now (2.10)
# capabilities only one module may provide: the one named in modules.use wins, otherwise all claimants are blocked
EXCLUSIVE = set()
# module.json fields of this version; slices add their own. A field the hub does not know is refused: a module relying on it
# would otherwise silently not work
FIELDS = {"name", "version", "api", "description", "requires", "optional", "provides", "capabilities", "env", "run",
          "default_off", "budget", "commands", "http",
          "web",   # a page plugin from the module (core/modstate.py): the main .js relative to the module folder
          "css",   # its stylesheets, in the page's <head> from the first byte (2.11; vetted and dropped by core/modstate.py)
          "fold", "fold_state", "actions",   # intercepting core event handling (core/corewrite.py)
          "snapshot"}   # its own keys and flags in the snapshot (core/modclaims.py)
# the http field — the module's routes on the page (modules/web/modproxy.py): limits on the request body and the duration of one reply, its own paths
# /api/… (core/modclaims.py)
HTTP_MAX_BODY, HTTP_MAX_STREAM = 1 << 30, 86400
# fields of the old line (a module inside the core process): nobody calls them here
OTHER_LINE = {"entry", "webhook", "remote", "mode"}
MAX_RUN = 32


def settings(cfg):
    """The `modules` section of hub.json with lists of the right shape; a wrong one (a typo) is read as empty instead of
    crashing every process that looks at modules."""
    s = (cfg or {}).get("modules")
    if not isinstance(s, dict):
        return {}
    shapes = {"paths": (list, str), "sources": (list, dict), "enabled": (list, str), "use": (dict, None),
              "core_write": (list, str),   # the owner's consent for modules with core.write (core/corewrite.py)
              "model": (list, str),        # modules the owner allowed a model (core/modelcall.py)
              "dev": (list, str),          # developer mode for modules from a folder (core/modrun.py: Supervisor.watch)
              "off": (list, str)}          # an embed's modules the owner turned off (they are on by their manifest)
    out = dict(s)
    for key, (outer, item) in shapes.items():
        if key in out and not (isinstance(out[key], outer) and (item is None or all(isinstance(x, item)
                                                                                    for x in out[key]))):
            out[key] = outer()
    return out


def dev_of(cfg, found):
    """{name: folder} of modules in developer mode: named in modules.dev and found in a folder (modules.paths) or in an
    embed (its own folder there) — the core's built-in and git ones do not work this way (core/modrun.py:
    Supervisor.watch); nor do those without a process. A copy with the name of an embed's module does not take it."""
    named = set(settings(cfg).get("dev") or [])
    out = {}
    for f in found:
        # without a process there is nothing to restart: the page serves the plugin's files from the disk as they are
        if (f["origin"] == "path" or f.get("embed")) and f["name"] in named and not processless(f.get("manifest")):
            out.setdefault(f["name"], f["dir"])   # an embed's module comes before the folders of modules.paths
    return out


def embed_on(cfg, name, man):
    """An embed's module (core/embed.py) is on: not in modules.off, and named in modules.enabled if its manifest says
    default_off."""
    s = settings(cfg)
    return name not in (s.get("off") or []) and (man.get("default_off") is not True or name in (s.get("enabled") or []))


def switch(cfg, name, on, embedded):
    """hub.json's modules section after the owner's switch of a module (the page's "Enable"/"Disable"): enabled for any
    module; an embed's one (embedded) is on by its manifest, so turning it off also names it in modules.off. The lists
    are kept as they are, foreign entries included; cfg is changed in place. -> [(the key, before, after)] of the lists
    it set: modules.enabled, and modules.off for an embed's module."""
    mods = cfg.get("modules") if isinstance(cfg.get("modules"), dict) else {}
    cfg["modules"] = mods
    was = list(mods.get("enabled")) if isinstance(mods.get("enabled"), list) else []
    mods["enabled"] = was if on == (name in was) else [n for n in was if n != name] + ([name] if on else [])
    out = [("modules.enabled", was, mods["enabled"])]
    if embedded:
        off = list(mods.get("off")) if isinstance(mods.get("off"), list) else []
        mods["off"] = [n for n in off if n != name] + ([] if on else [name])
        out.append(("modules.off", off, mods["off"]))
    return out


def env_prefix(name):
    """The module's own environment names: HUB_MOD_<NAME>_ (a hyphen becomes an underscore)."""
    return "HUB_MOD_" + name.upper().replace("-", "_") + "_"


def env_denied(key, module=None):
    """A name the module cannot set for a session; module is its name (its own HUB_MOD_<NAME>_* are open)."""
    key = key.upper()   # Windows: Path is PATH
    if module and key.startswith(env_prefix(module)) and len(key) > len(env_prefix(module)):
        return False
    return key not in ENV_HARMLESS and (key in ENV_DENIED or key.startswith(ENV_DENIED_PREFIXES) or provider_name(key)
                                        or bool(set(key.split("_")) & ENV_NETWORK))


def provider_name(key):
    """The name chooses claude's provider (CLAUDE_CODE_USE_BEDROCK…), not one of its tools."""
    key = key.upper()
    return key.startswith(ENV_PROVIDER) and key not in ENV_HARMLESS


# --- manifest -------------------------------------------------------------------------------------------------------

def validate(man, path=None):
    """What is wrong with the manifest — in short lines a person can fix; [] — all is well."""
    if not isinstance(man, dict):
        return ["module.json is not a JSON object"]
    errs = []
    name = str(man.get("name") or "")
    if not NAME.fullmatch(name):
        errs.append("name: lowercase Latin letters, digits and \"-\", starting with a letter, up to 40 characters")
    if version.parse_version(man.get("version")) is None:
        errs.append("version: like 1.2.0")
    if not version.valid_range(man.get("api")) or not version.has_floor(man.get("api")):
        # without a lower bound ("<3") the module would be read as written for API 0.x and would load nowhere
        errs.append(f"api: a module API range with a lower bound, e.g. ^{version.MODULE_API}")
    run = man.get("run")
    if "run" not in man:
        errs += _noproc_errors(man)
    elif not (isinstance(run, list) and run and len(run) <= MAX_RUN and all(isinstance(x, str) and x for x in run)):
        errs.append('run: the module process command as a list of words, e.g. ["python3", "main.py"]')
    for section in ("requires", "optional"):
        block = man.get(section, {})
        if not isinstance(block, dict):
            errs.append(f"{section}: an object with modules" + (" and plugins" if section == "requires" else ""))
            continue
        mods = block.get("modules", {})
        if not isinstance(mods, dict):
            errs.append(f"{section}.modules: an object name -> version range")
        else:
            errs += [f"{section}.modules.{n}: bad name or range \"{r}\"" for n, r in mods.items()
                     if not NAME.fullmatch(str(n)) or not version.valid_range(r)]
        if section == "requires" and "plugins" in block and not (
                isinstance(block["plugins"], list) and all(isinstance(p, str) and p for p in block["plugins"])):
            errs.append("requires.plugins: a list of tool ids from the catalog (hub tools list)")
    for field, pattern in (("provides", PROVIDE), ("env", ENV_NAME)):
        items = man.get(field, [])
        if not isinstance(items, list) or not all(isinstance(x, str) and pattern.fullmatch(x) for x in items):
            errs.append(f"{field}: a list of names" + (" in UPPERCASE" if field == "env" else ""))
        elif field == "env" and any(env_denied(x, name) for x in items):
            errs.append("env: the core does not pass these to sessions: " + ", ".join(x for x in items if env_denied(x, name))
                        + f" (they change which code runs; the module's own names are {env_prefix(name or 'name')}*)")
    caps = man.get("capabilities", [])
    if not isinstance(caps, list):
        errs.append("capabilities: a list")
    else:
        errs += [f"capabilities: unknown capability \"{c}\"" for c in caps
                 if not (isinstance(c, str) and (c in CAPABILITIES or re.fullmatch(r"net:[A-Za-z0-9.*-]+", c)))]
    if not isinstance(man.get("default_off", False), bool):
        errs.append("default_off: true or false")
    errs += _budget_errors(man.get("budget", {}))
    errs += _commands_errors(man)
    if "http" in man:
        errs += _http_errors(man["http"], caps if isinstance(caps, list) else [])
    if "snapshot" in man:
        from core import modclaims
        errs += modclaims.snapshot_errors(man["snapshot"])
    if "web" in man and web_error(man["web"]):
        errs.append(web_error(man["web"]))
    from core import corewrite
    errs += corewrite.manifest_errors(man)
    for field in sorted(set(man) & OTHER_LINE):
        errs.append(f"{field}: not supported by this hub — a module here is its own process (the run field), module API "
                    f"{version.MODULE_API}")
    for field in sorted(set(man) - FIELDS - OTHER_LINE):
        errs.append(f"{field}: unknown field for module API {version.MODULE_API}")
    if version.valid_range(man.get("api")):
        # a field or capability newer than the version the module was written for (the lower bound of api): otherwise api lies,
        # and a hub of that version would refuse with "no such field" instead of "update the hub"
        own = version.parse_version(version.module_floor(man["api"]))
        names = (set(man) & FIELDS) | {c for c in (caps if isinstance(caps, list) else []) if isinstance(c, str)}
        if isinstance(man.get("http"), dict):
            names |= {f"http.{k}" for k in man["http"]}
        for x in sorted(names):
            since = version.manifest_since(x)
            if since and version.parse_version(since) > own:
                errs.append(f"{x}: exists since module API {since}, but the module's api is \"{man['api']}\" — set ^{since}")
    return errs


def processless(man):
    """A module without its own process: no run in module.json (module API 2.10) — the core starts nothing for it and
    serves its page plugin itself."""
    return isinstance(man, dict) and "run" not in man


# fields and rights of a module that exist only with a process: it answers the points, the commands, the http handles
NEEDS_PROCESS = ("capabilities", "commands", "http", "fold", "fold_state", "actions", "snapshot", "budget", "env")


def _noproc_errors(man):
    """No run: only a page plugin and the graph fields (requires, provides…) — everything that needs a process is refused
    with the reason, so the author sees the choice: add run, or drop the field."""
    errs = []
    if "web" not in man:
        errs.append('run: a module without a process carries a page plugin (the web field, e.g. "web/main.js"), otherwise '
                    'it does nothing; for a process add run, e.g. ["python3", "main.py"]')
    for field in NEEDS_PROCESS:
        if field in man and man[field] not in ([], {}):   # an empty value asks for nothing
            errs.append(f"{field}: needs the module process (the run field) — without run a module only gives a page plugin")
    if version.valid_range(man.get("api")) and version.parse_version(version.module_floor(man["api"])) \
            < version.parse_version(version.NOPROC_SINCE):
        errs.append(f"run: a module without a process exists since module API {version.NOPROC_SINCE}, but the module's "
                    f"api is \"{man['api']}\" — set ^{version.NOPROC_SINCE}")
    return errs


def _http_errors(http, caps):
    """http: {max_body: bytes, max_stream: seconds, paths, views} — route limits and own /api/… paths; routes exist only
    with the http capability."""
    from core import modclaims
    known = ("max_body", "max_stream", "paths", "views")
    if not isinstance(http, dict):
        return ["http: an object {" + ", ".join(known) + "}"]
    errs = [f"http.{k}: unknown field (known: {', '.join(known)})" for k in sorted(set(http) - set(known))]
    errs += modclaims.http_errors(http)
    for key, top, unit in (("max_body", HTTP_MAX_BODY, "bytes"), ("max_stream", HTTP_MAX_STREAM, "seconds")):
        v = http.get(key)
        if key in http and not (isinstance(v, int) and not isinstance(v, bool) and 0 < v <= top):
            errs.append(f"http.{key}: an integer number of {unit}, above 0 and at most {top}")
    if "http" not in caps:
        errs.append("http: module handles need the http capability in capabilities")
    return errs


def web_error(web):
    """The web field: the main file of the page plugin, a .js inside the module folder (the page serves files only from its folder)."""
    parts = web.replace("\\", "/").split("/") if isinstance(web, str) else []
    if not (parts and web.endswith(".js") and "\\" not in web and not web.startswith("/") and ":" not in web
            and all(p and p not in (".", "..") for p in parts)):
        return 'web: a path to the page plugin .js inside the module folder, e.g. "web/main.js"'
    return None


def _budget_errors(budget):
    """budget: {point: seconds} — only less than the core's (the core will not agree to wait for a module longer)."""
    from core import modrun
    if not isinstance(budget, dict):
        return ["budget: an object point -> seconds"]
    errs = []
    for point, sec in budget.items():
        if point not in version.module_points():
            errs.append(f"budget.{point}: no such point in module API {version.MODULE_API}")
        elif not (isinstance(sec, (int, float)) and not isinstance(sec, bool) and 0 < sec <= modrun.budget_of(point)):
            errs.append(f"budget.{point}: seconds, above 0 and at most {modrun.budget_of(point):g}")
    return errs


MAX_COMMANDS = 32


def _commands_errors(man):
    """commands: {command: help} — the `hub <command>` commands the module answers (the cli point)."""
    if "commands" not in man:
        return []
    cmds = man["commands"]
    if not (isinstance(cmds, dict) and len(cmds) <= MAX_COMMANDS and all(
            isinstance(k, str) and NAME.fullmatch(k) and isinstance(v, str) and len(v) <= 200 for k, v in cmds.items())):
        return [f"commands: an object command -> help (a name is lowercase Latin letters, digits and \"-\", up to "
                f"{MAX_COMMANDS} commands)"]
    if cmds and "cli" not in (man.get("capabilities") or []):
        return ["commands: needs the cli capability in capabilities"]
    return []


def read(path, origin, **extra):
    """A module folder -> {name, version, dir, origin, manifest, errors, …extra}."""
    try:
        with open(os.path.join(path, "module.json"), encoding="utf-8-sig") as f:
            man = json.load(f)
        errs = validate(man, path)
    except (OSError, ValueError) as e:
        man, errs = {}, [i18n.t("core.modules.json_unreadable", error=e)]
    if not isinstance(man, dict):
        man = {}
    name = man.get("name") if isinstance(man.get("name"), str) and man.get("name") else os.path.basename(path)
    return {"name": name, "version": str(man.get("version") or "?"), "dir": path, "origin": origin, "manifest": man,
            "errors": errs, **extra}


def _scan(root, origin, **extra):
    """A module folder or a folder of module folders."""
    if not os.path.isdir(root):
        return [{"name": os.path.basename(root.rstrip("/\\")) or root, "version": "?", "dir": root, "origin": origin,
                 "manifest": {}, "errors": [i18n.t("core.modules.no_folder", path=root)], **extra}]
    if os.path.isfile(os.path.join(root, "module.json")):
        return [read(root, origin, **extra)]
    try:
        names = sorted(os.listdir(root))
    except OSError as e:
        return [{"name": os.path.basename(root.rstrip("/\\")) or root, "version": "?", "dir": root, "origin": origin,
                 "manifest": {}, "errors": [i18n.t("core.modules.folder_unreadable", error=e)], **extra}]
    return [read(os.path.join(root, d), origin, **extra) for d in names
            if os.path.isfile(os.path.join(root, d, "module.json"))]


def discover(cfg, state_dir, builtin=True):
    """All modules the hub sees, in a stable order: built-in (the core's, then the embeds'), folders from modules.paths,
    git sources. A folder of modules.paths that is a built-in module's own folder is that module, not a second one."""
    paths = settings(cfg).get("paths") or []
    folders = []
    if builtin or paths:
        root = os.path.join(REPO, "modules")
        folders = [(None, os.path.join(root, d)) for d in sorted(os.listdir(root))
                   if os.path.isfile(os.path.join(root, d, "module.json"))] if os.path.isdir(root) else []
        folders += _embedded()
    found = [read(folder, "builtin", **({"embed": name} if name else {})) for name, folder in folders] if builtin else []
    same = lambda d: os.path.normcase(os.path.realpath(d))  # noqa: E731
    own = {same(folder) for _, folder in folders}
    for p in paths:
        found += [f for f in _scan(os.path.expanduser(str(p)), "path") if same(f["dir"]) not in own]
    from core import modsrc
    return found + modsrc.discover(cfg, state_dir)   # git sources: folders of approved commits


def _embedded():
    """[(embed name, module folder)] of the embeds of this tree (core/embed.py, Embed API 1.1)."""
    from core import embed
    if not hasattr(embed, "module_folders"):   # a process of the previous release keeps its core.embed of 1.0: none until its restart
        return []
    return embed.module_folders(REPO)


# --- dependency graph -------------------------------------------------------------------------------------------------

def _plugin_present(want, catalog):
    """A plugin by catalog id or by short name: "hub-base" is also "core:hub-base", "jira" is also "jira@set"."""
    return want in catalog or any(c.split(":", 1)[-1] == want or c.split("@", 1)[0] == want for c in catalog)


def resolve(found, cfg, catalog=None, blocked=None, api=version.MODULE_API):
    """What loads: [{…found, state: ok|blocked|disabled, reason, order}] — ok ones in load order (dependencies first,
    ties by name), then the rest by name. catalog is the catalog's tool ids; None — the catalog is unknown to this process:
    a module with requires.plugins is then blocked (do not load blindly). blocked is {name: reason} from the module
    process (did not start, did not answer hello): these are blocked as by the graph, and their dependents with them."""
    s = settings(cfg)
    enabled, use = set(s.get("enabled") or []), s.get("use") or {}
    res = [{**f, "state": None, "reason": "", "order": None} for f in found]

    def block(r, why):
        r["state"], r["reason"] = "blocked", why

    count = {}
    for r in res:
        count.setdefault(r["name"], []).append(r)
    kept = {}   # name -> the one built-in module of several with its name: its copies are blocked, it is the name's
    for r in res:
        man = r["manifest"]
        same = count[r["name"]]
        # the core's own built-in module, else an embed's (core/embed.py): an embed adds, it never replaces the core's
        builtin = [x for x in same if x["origin"] == "builtin" and not x.get("embed")] \
            or [x for x in same if x["origin"] == "builtin"]
        if len(builtin) == 1 and r is not builtin[0]:
            # a copy does not disable the built-in module: only the copy is blocked
            block(r, i18n.t("core.modules.copy_of_builtin", name=r["name"], dir=r["dir"]))
            kept[r["name"]] = builtin[0]
        elif len(same) > 1 and len(builtin) != 1:   # none of them built-in, or two built-in ones (two embeds)
            block(r, i18n.t("core.modules.same_name", name=r["name"], dirs=", ".join(x["dir"] for x in same)))
        elif r["errors"]:
            block(r, "; ".join(r["errors"]))
        elif r["origin"] != "builtin" and r["name"] not in enabled:
            r["state"], r["reason"] = "disabled", i18n.t("core.modules.not_enabled")
        elif r.get("embed") and r["name"] in (s.get("off") or []):
            r["state"], r["reason"] = "disabled", i18n.t("core.modules.turned_off")
        elif man.get("default_off") is True and r["name"] not in enabled:
            r["state"], r["reason"] = "disabled", i18n.t("core.modules.off_by_default")
        elif not version.module_api(man.get("api"), api)[0]:
            block(r, version.module_api(man.get("api"), api)[1])
        elif (man.get("requires") or {}).get("plugins") and catalog is None:
            block(r, i18n.t("core.modules.needs_catalog"))
        elif catalog is not None:
            missing = [p for p in (man.get("requires") or {}).get("plugins") or [] if not _plugin_present(p, catalog)]
            if missing:
                block(r, i18n.t("core.modules.plugin_missing", plugins=", ".join(missing)))
        if r["state"] is None and (blocked or {}).get(r["name"]):
            block(r, str(blocked[r["name"]]))

    for cap in sorted(EXCLUSIVE):
        claim = [r for r in res if r["state"] is None and cap in (r["manifest"].get("provides") or [])]
        if len(claim) < 2:
            continue
        chosen = use.get(cap)
        for r in claim:
            if chosen in {c["name"] for c in claim}:
                if r["name"] != chosen:
                    block(r, i18n.t("core.modules.cap_given_to", cap=cap, chosen=chosen))
            else:
                block(r, i18n.t("core.modules.cap_pick", cap=cap, names=", ".join(c["name"] for c in claim)))

    from core import corewrite
    corewrite.resolve_rules(res, s, block)   # core.write without the owner's consent, two interceptors of one type

    named = {r["name"]: r for r in res if len(count[r["name"]]) == 1}
    named.update(kept)   # a blocked copy does not take the built-in module away from those who need it

    def requires(r):
        return ((r["manifest"].get("requires") or {}).get("modules") or {}).items()

    changed = True
    while changed:   # the block spreads to those who need a blocked module, until nothing changes
        changed = False
        for r in res:
            if r["state"] is not None:
                continue
            for dep, rng in requires(r):
                d = named.get(dep)
                why = None
                if d is None:
                    why = i18n.t("core.modules.dep_missing", dep=dep, range=rng)
                elif d["state"] == "disabled":
                    why = i18n.t("core.modules.dep_off", dep=dep, range=rng)
                elif not version.satisfies(d["version"], rng):
                    why = i18n.t("core.modules.dep_version", dep=dep, range=rng, found=d["version"])
                elif d["state"] == "blocked":
                    why = i18n.t("core.modules.dep_blocked", dep=dep)
                if why:
                    block(r, why)
                    changed = True
                    break

    live = {r["name"]: r for r in res if r["state"] is None}

    def cycle_from(start):
        """A path start -> … -> start along required dependencies among the live ones, or None."""
        stack, seen = [(start, [start])], set()
        while stack:
            node, path = stack.pop()
            for dep, _ in sorted(requires(live[node])):
                if dep == start:
                    return path + [start]
                if dep in live and dep not in seen:
                    seen.add(dep)
                    stack.append((dep, path + [dep]))
        return None

    for n in sorted(live):
        loop = cycle_from(n)
        if loop:
            block(live[n], i18n.t("core.modules.cycle", loop=" → ".join(loop)))
    changed = True
    while changed:
        changed = False
        for r in res:
            if r["state"] is None and any(named.get(dep, {}).get("state") == "blocked" for dep, _ in requires(r)):
                block(r, i18n.t("core.modules.dep_blocked",
                                dep=next(dep for dep, _ in requires(r) if named[dep]["state"] == "blocked")))
                changed = True

    live = {r["name"]: r for r in res if r["state"] is None}
    after = {n: set() for n in live}   # name -> what loads before it
    for n, r in live.items():
        after[n] |= {dep for dep, _ in requires(r)}
        for dep, rng in ((r["manifest"].get("optional") or {}).get("modules") or {}).items():
            if dep in live and version.satisfies(live[dep]["version"], rng):
                after[n].add(dep)
    order = _topo(after)
    if len(order) < len(live):   # optional dependencies closed a loop: they do not block — order without them
        order = _topo({n: {dep for dep, _ in requires(live[n])} for n in live})
    for i, n in enumerate(order):
        live[n]["state"], live[n]["order"] = "ok", i
    return [live[n] for n in order] + sorted((r for r in res if r["state"] != "ok"),
                                             key=lambda r: (r["name"], r["dir"]))


def _topo(after):
    """Kahn's order; among the ready ones by name, so the order does not depend on the file system."""
    import heapq
    left = {n: set(d) for n, d in after.items()}
    ready = [n for n, d in left.items() if not d]
    heapq.heapify(ready)
    out = []
    while ready:
        n = heapq.heappop(ready)
        out.append(n)
        for m, d in left.items():
            if n in d:
                d.discard(n)
                if not d and m not in out and m not in ready:
                    heapq.heappush(ready, m)
    return out


# --- the "a module wrote to the core state" mark ----------------------------------------------------------------------

# A module with the core.write capability writes records to the journal that a fold without its support would fold in its own way (spec §2.5).
# Before the first such write the core puts a CORE_WRITTEN file {"format": N, …} and a core_written mark into the state. Code
# that understands such records up to format CORE_WRITE_FORMAT inclusive starts; older code refuses
# (rolling the core back to a version without support for them is not supported). This code understands format 1 — records with the fields by and fold
# (core/corewrite.py).
CORE_WRITTEN = "core-written"
CORE_WRITE_FORMAT = 1


def core_write_refusal(state_dir, state=None):
    """The reason not to start, or None: the state folder has a CORE_WRITTEN file or the state has a core_written mark
    of a newer format than this code understands. An unreadable file is a refusal too: the effects of the edits do not age."""
    marks = []
    path = os.path.join(state_dir, CORE_WRITTEN)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                marks.append(json.load(f))
        except (OSError, ValueError):
            marks.append({})
    if isinstance(state, dict) and state.get("core_written"):
        marks.append(state["core_written"])
    for m in marks:
        fmt = m.get("format") if isinstance(m, dict) else None
        if not (isinstance(fmt, int) and not isinstance(fmt, bool) and fmt <= CORE_WRITE_FORMAT):
            names = ", ".join(sorted(k for k in (m.get("modules") or {}) if isinstance(k, str))) \
                if isinstance(m, dict) and isinstance(m.get("modules"), dict) else ""
            return ("this hub's journal has records of a module with the core.write right" + (f" ({names})" if names else "")
                    + f" — this core code does not understand them and would fold the journal its own way ({path}); "
                    "rolling back to this version is not supported — return to a core version that understands them")
    return None


def state_file_refusal(state_dir):
    """core_write_refusal by the file and by state.json (without folding the journal: a check before the core starts)."""
    from core import journal
    try:
        st = (journal.read_state_file(state_dir) or {}).get("state")
    except (OSError, ValueError, AttributeError):
        st = None
    return core_write_refusal(state_dir, st)


# --- hub modules ----------------------------------------------------------------------------------------------------

LABEL = {"ready": "cli.modules.state_ready", "starting": "cli.modules.state_starting",   # catalog keys
         "suspended": "cli.modules.state_suspended", "stopped": "cli.modules.state_stopped",
         "blocked": "cli.modules.state_blocked", "disabled": "cli.modules.state_disabled",
         "static": "cli.modules.state_static"}


def rows(cfg, state_dir, st, now=None):
    """What `hub modules` and the page show: the graph + the process state from the journal + the current launch."""
    import time
    from core import modrun
    now = time.time() if now is None else now
    recs = (st or {}).get("modules") or {}
    catalog = set(st["catalog"]) if (st or {}).get("catalog") is not None else None
    blocked = {n: r.get("reason") or "" for n, r in recs.items() if r.get("state") in modrun.STOPPED}
    named_dev = set(settings(cfg).get("dev") or [])
    found = discover(cfg, state_dir)
    devs = dev_of(cfg, found)
    out = []
    plan = resolve(found, cfg, catalog=catalog, blocked=blocked)
    from core import modclaims, modsrc
    lost = modclaims.resolve(plan, cfg)[1]   # its own paths and snapshot keys the module did not get (2.4)
    for r in plan:
        man, rec = r["manifest"], recs.get(r["name"]) or {}
        state, reason = r["state"], r["reason"]
        if rec.get("state") == "error":
            state = "stopped"
            reason = i18n.t("core.modules.stopped_by_failures", hook=rec["last"]["hook"], error=rec["last"]["error"],
                            name=r["name"])
        elif state == "ok" and modrun.status(rec, now) == "suspended":
            state = "suspended"
            reason = i18n.t("core.modules.paused_until", time=time.strftime("%H:%M", time.localtime(rec["until"])),
                            hook=rec["last"]["hook"], error=rec["last"]["error"])
        elif state == "ok" and processless(man):
            state = "static"   # no process to wait for: the page plugin is served by the core
        elif state == "ok":
            run = modrun.run_of(state_dir, r["name"]) or {}
            state = "ready" if rec.get("ready") and rec["ready"] == run.get("launch") else "starting"
        dev = devs.get(r["name"]) == r["dir"]
        last = rec.get("last") or {}
        out.append({"name": r["name"], "version": r["version"], "origin": r["origin"], "state": state,
                    "reason": reason, "dir": r["dir"], "order": r["order"], "dev": dev,
                    **({"log": modrun.log_of(state_dir, r["name"]),
                        "last": f"{last.get('hook')}: {last.get('error')}" if last else ""} if dev else {}),
                    **({"note": i18n.t("core.modules.dev_note")}
                       if r["name"] in named_dev and not dev else {}),
                    **({"trusted": True} if r.get("trusted") else {}),
                    **({"source": modsrc.shown(r.get("source")), "commit": r.get("commit"), "ref": r.get("ref")}
                       if r["origin"] == "git" else {}),
                    **({"embed": r["embed"], "on": embed_on(cfg, r["name"], man)} if r.get("embed") else {}),
                    "api": str(man.get("api") or ""), "description": man.get("description") or "", "capabilities": man.get("capabilities") or [],
                    "requires": (man.get("requires") or {}).get("modules") or {}, "lost": lost.get(r["name"], []),
                    "points": rec.get("points") or [] if state == "ready" else []})
    return out


def text(table):
    """`hub modules` for a person."""
    from core import i18n
    label = lambda s: i18n.t(LABEL[s]) if s in LABEL else s  # noqa: E731
    count = {}
    for r in table:
        count[r["state"]] = count.get(r["state"], 0) + 1
    head = f"module API {version.MODULE_API}"
    if not table:
        return i18n.t("cli.modules.none", head=head)
    lines = [head + " · " + ", ".join(f"{label(s)}: {n}" for s, n in sorted(count.items()))]
    for r in table:
        where = i18n.t("cli.modules.builtin") if r["origin"] == "builtin" else r["dir"]
        lines.append(f"  {label(r['state']):<10} {r['name']} {r['version']} ({where})"
                     + (i18n.t("cli.modules.trusted") if r.get("trusted") else "")
                     + (f" — {r['reason']}" if r["reason"] else "")
                     + (i18n.t("cli.modules.points", points=", ".join(r["points"])) if r["points"] else "")
                     + (f"; {r['note']}" if r.get("note") else ""))
        for x in r.get("lost") or []:
            lines.append(i18n.t("cli.modules.lost", what=x))
        if r.get("dev"):
            lines.append(i18n.t("cli.modules.dev", log=r["log"])
                         + (i18n.t("cli.modules.dev_last", last=r["last"]) if r.get("last") and r["state"] != "blocked" else ""))
    return "\n".join(lines)
