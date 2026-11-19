"""Embedded code (Embed API, spec d2144426): a company's own code that its build puts into the core's tree, and the only
places the core looks for it. The core is the tree without embed/; an embed is one folder embed/<name>/ with the core's
own paths in it (modules/accounts/accounts.py, tests/test_accounts.py, i18n/en.json) and its passport embed.json
{"embed": 1, "api": "MAJOR.MINOR"}. A build (tools/package) carries exactly the embeds of its profile.

The core finds an embed only here:
- a slot (SLOTS): a function the core calls when its code is in the tree — the core's own path or the same path in an
  embed (present(), asked of the disk every time, as core/cutout.py always did);
- the Python packages modules, modules.voice and tests: their __path__ also has embed/<name>/<the same folder> of every
  embed (extend()), so `from modules.accounts import accounts` and `tests.test_accounts` load from an embed unchanged;
- the parts of hub-up a slot runs: bin/hub-part <part> (PARTS);
- a file of defaults (file("modules/private.json"));
- the phrase catalogs: embed/<name>/i18n/<lang>.json is added to the core's (core/i18n.py);
- the Module API modules (since 1.1): embed/<name>/modules/<module>/module.json is a built-in module, as the core's own
  modules/<module>/module.json (module_folders(), core/modules.discover) — on by its manifest (default_off) without a
  command; a module named as one of the core's is refused, the core's stays (problems() refuses it at the build).

What the core promises an embed (API, the major number): the slots, their paths and NEEDS, the names the core calls of
each slot with their signatures (NAMES; check() holds an embed to them, tests/test_embed.py holds the core to them), the
folder, the packages above, the defaults file, the catalogs, the modules (core/version.EMBED_API). Nothing else: an embed may import any of the core (full
access), and the build's run of its tests on the built tree is what catches a change there. A new name in a minor number
is optional for an embed (OPTIONAL: the core works without it); a name becomes required, goes or changes meaning only with
a major one (and its old form through core/version.DEPRECATED). The stdlib only, nothing of the hub but the paths and the API number
(problems() reads an embed's module manifests with core/modules when it runs)."""
import importlib
import importlib.util
import inspect
import json
import os
import re
import sys

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# the API's number (core/version.EMBED_API names it too); kept here: a process started on the previous release keeps
# its older core.version in memory when it first imports this file
API = "1.1"   # 1.1: the slot codex_pool; the Module API modules of an embed are built-in ones (module_folders)
FOLDER = "embed"
PASSPORT = "embed.json"
PROFILE = "profile.json"
NAME = re.compile(r"[a-z0-9_-]{1,32}\Z")
# what an embed may carry: files under these folders, and these two at its top
ALLOWED = ("modules/", "i18n/", "tests/")
TOP = (PASSPORT, PROFILE)
# the folders whose files share one Python namespace with the core's: no path of them repeats the core's or another
# embed's (an embed adds, it never replaces)
SHARED = ("modules/", "tests/")
CATALOG = re.compile(r"i18n/[a-z]{2,3}(-[A-Za-z0-9]+)?\.json\Z")
# the packages that look into the embeds too (extend()); a new one is a minor number of the API
PACKAGES = ("modules", "modules/voice", "tests")

# a slot -> the file whose presence means its code is in the tree, and the module the core imports
SLOTS = {"profile": {"file": "modules/accounts/accounts.py", "module": "modules.accounts.accounts"},
         "gateway": {"file": "modules/gateway/gateway.py", "module": "modules.gateway.gateway"},
         "conduit": {"file": "modules/conduit/conduit.py", "module": "modules.conduit.conduit"},
         "speech": {"file": "modules/voice/speak.py", "module": "modules.voice.speak"},
         "voice_cloud": {"file": "modules/voice/chatgpt.py", "module": "modules.voice.chatgpt"},   # spec d2140189
         "stats": {"file": "modules/stats/stats.py", "module": "modules.stats.stats"},
         # Codex (experimental): the Codex account pool (core/codexslot.py) — the open flavor runs Codex on one sign-in
         "codex_pool": {"file": "modules/accounts/codexpool.py", "module": "modules.accounts.codexpool"}}
# the gateway and conduit serve only the pool of the profile slot: without it they are as good as absent
NEEDS = {"gateway": "profile", "conduit": "profile"}
# the parts of hub-up a slot runs (bin/hub-up, core/restart.PARTS): part -> (slot, the arguments of an empty command line)
PARTS = {"gateway": ("gateway", []), "conduit": ("conduit", ["watch"]), "stats": ("stats", ["watch"])}
# the slots whose main(argv) bin/hub-part runs: the parts' and the children their code starts of itself (the voice's
# cloud row); not profile — its main is `hub accounts`
MAINS = ("gateway", "conduit", "stats", "voice_cloud")
# the names the core calls of each slot's module: a function by its parameters ("a, b=" — b has a default), a value by
# its type. Built from the core's own calls (tests/test_embed.py Calls reads them out of the code)
NAMES = {
    "profile": {"COMMAND_RETRY": int, "REFRESH_EVERY": int,
                "decide": "acc, pool, on, now, refresh=, skip=", "enabled": "pool",
                "fetch_token": "name, file=, run=, why=, note=", "fingerprint": "token", "load": "file=",
                "main": "args, st, put, me, cfg_path, stdin=, now=, file=, state_dir=, measure=, mods=", "path": "",
                "probe": "names, st, pool, measure, report", "tally": "counts, name, outcome, now, why=",
                "use_check": "acc, pool, on, name, force, now"},
    "gateway": {"DOWN_WAIT": int, "STEADY": int, "alive": "status, now", "claude_key": "claude",
                "core_view": "acc, on, users", "folder": "state_dir", "identity": "state_dir",
                "limits_fact": "account, gen, lim", "measure": "token, upstream=, env=, timeout=",
                "probe": "claude, base_env, run=, first_party_ok=", "reachable": "port, timeout=",
                "session_env": "env, url, secret, stub, first_party=", "sessions_env": "", "settings": "cfg",
                "status_of": "state_dir", "summary": "cfg, status, core, now", "write": "state_dir, name, data, private=",
                "main": "argv"},
    "conduit": {"main": "argv"},
    "speech": {"SpeakError": type, "missing": "cfg, lang=", "reply": "text, cfg, voice=, account=", "settings": "cfg"},
    "voice_cloud": {"chosen": "state", "choose": "state", "check": "state, lang=", "note_failure": "state, why, by=",
                    "row": "lang, state, fallback=", "summary": "state, fallback=", "main": "argv"},
    "stats": {"main": "argv, state_dir=", "page_report": "state_dir, cfg, frm, to, hub=",
              "preview": "state_dir, cfg, now=, lang=, make=", "settings": "cfg", "show_log": "state_dir, n, lang="},
    "codex_pool": {"BadAnswer": type, "Pool": "state_dir, mods, config, clock, log=", "USAGE": str,
                   "check_list": "res, ctx", "check_decide": "res, ctx", "check_use": "res, ctx", "check_add": "res, ctx",
                   "check_remove": "res, ctx", "cli": "what, args, state_dir, me, put", "status": "state_dir"},
}
# for one release (the move of the team's code into its embed): a process started before it — hub-up, hubd, the page,
# until their restart — still runs or imports the code by its old paths; these files there hand it the moved code
# (redirect(), bin/hub-part). The core never counts them as its own code (where()); the open flavor cuts them
SHIMS = ("modules/accounts/accounts.py", "modules/gateway/gateway.py", "modules/conduit/conduit.py",
         "modules/stats/stats.py", "modules/voice/speak.py", "bin/hub-gateway", "bin/hub-conduit", "bin/hub-stats",
         "core/codexpool.py")   # the Codex pool's move (the slot codex_pool): an earlier hubd and page import it there
OPTIONAL = {}   # slot -> {name: since "MAJOR.MINOR"}: the names an embed of an earlier minor number may lack
_seen = set()   # the embeds a slot was found in by this process: its packages look into them (_reach)


def roots(repo=None):
    """[(name, folder)] of the embeds in the tree repo (this core's by default): embed/<name>/ with a passport, by
    name."""
    top = os.path.join(repo or CORE, FOLDER)
    try:
        names = sorted(os.listdir(top))
    except OSError:
        return []
    return [(n, os.path.join(top, n)) for n in names
            if NAME.match(n) and os.path.isfile(os.path.join(top, n, PASSPORT))]


def module_folders(repo=None):
    """[(embed name, folder)] of the Module API modules the embeds of the tree repo carry: embed/<name>/modules/<module>/
    with a module.json, by embed name, then by folder."""
    out = []
    for name, top in roots(repo):
        root = os.path.join(top, "modules")
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        out += [(name, os.path.join(root, d)) for d in names if os.path.isfile(os.path.join(root, d, "module.json"))]
    return out


def where(rel, repo=None):
    """The path of rel ('/'-separated) in the tree: the core's own, else the first embed that has it; None — nowhere."""
    parts = rel.split("/")
    own = os.path.join(repo or CORE, *parts)
    if rel not in SHIMS and os.path.isfile(own):   # the core's own first: one stat, the embeds only when it is not there
        return own
    for _, top in roots(repo):
        path = os.path.join(top, *parts)
        if os.path.isfile(path):
            return path
    return None


def core_path(rel):
    """A file of the tree by its place in the core's layout: embed/<name>/<path> -> <path>; any other as it is."""
    parts = rel.split("/", 2)
    return parts[2] if len(parts) == 3 and parts[0] == FOLDER and NAME.match(parts[1]) else rel


def file(rel, repo=None):
    """A file of defaults an embed may bring (modules/private.json): its path, or None."""
    return where(rel, repo)


def redirect(name, rel):
    """A shim (SHIMS) at the old path rel hands a process the moved code: imported as name, it is replaced in
    sys.modules by the embed's file loaded under the same name; run as a program ("__main__"), it runs the embed's file
    as one. No embed has it — ImportError, as for a module that is not there."""
    path = next((p for p in (os.path.join(top, *rel.split("/")) for _, top in roots()) if os.path.isfile(p)), None)
    if path is None:
        raise ImportError(f"{rel}: moved into an embed, and no embed of this tree has it", name=name)
    if name == "__main__":
        import runpy
        sys.argv[0] = path
        runpy.run_path(path, run_name="__main__")
        return
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module   # the import that runs the shim returns what sys.modules holds after it
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise


def present(slot, repo=None):
    """The code of slot (one of SLOTS) is in the tree repo, with what it needs (NEEDS). Tests of the hub command name
    slots to treat as absent in HUB_TEST_CUT (comma-separated): the command runs in its own process."""
    cut = (os.environ.get("HUB_TEST_CUT") or "").split(",")
    for s in [slot] + ([NEEDS[slot]] if slot in NEEDS else []):
        if s in cut:
            return False
        path = where(SLOTS[s]["file"], repo)
        if path is None:
            return False
        if repo in (None, CORE) and path != os.path.join(CORE, *SLOTS[s]["file"].split("/")):
            _reach(path)
    return True


def _reach(path):
    """The code of a slot found at path in an embed: the packages this process already imported look into that embed
    (one that came after their import too), and a folder the import system saw missing is looked at again."""
    top = os.path.join(CORE, FOLDER, os.path.relpath(path, os.path.join(CORE, FOLDER)).split(os.sep)[0])
    if top in _seen:
        return
    for package in PACKAGES:
        loaded = sys.modules.get(package.replace("/", "."))
        if loaded is not None and isinstance(getattr(loaded, "__path__", None), list):
            extend(loaded.__path__, package)
    _seen.add(top)
    importlib.invalidate_caches()


def parts(names, repo=None):
    """names without the parts of hub-up whose slot's code is not in the tree."""
    return [n for n in names if n not in PARTS or present(PARTS[n][0], repo)]


def extend(path, package, repo=None):
    """A package's __path__ with embed/<name>/<package> of every embed, after its own (the core's comes first). The
    folder need not exist yet: the code of a slot may come into an embed later (present() refreshes the caches)."""
    for _, top in roots(repo):
        folder = os.path.join(top, *package.split("/"))
        if folder not in path:
            path.append(folder)
    return path


def catalogs(lang, repo=None):
    """The paths of embed/<name>/i18n/<lang>.json, by embed name."""
    return [p for p in (os.path.join(top, "i18n", f"{lang}.json") for _, top in roots(repo)) if os.path.isfile(p)]


def _version(text):
    m = re.fullmatch(r"(\d+)\.(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def passport(top):
    """embed.json of an embed folder, checked: {"embed": 1, "api": "MAJOR.MINOR"}, or a reason (str)."""
    try:
        with open(os.path.join(top, PASSPORT), encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError) as e:
        return f"{PASSPORT}: not readable ({e})"
    if not (isinstance(got, dict) and got.get("embed") == 1 and isinstance(got.get("api"), str)
            and _version(got["api"])):
        return f'{PASSPORT}: not {{"embed": 1, "api": "MAJOR.MINOR"}}'
    return got


def _files(top):
    """The files of a folder, relative with '/', without bytecode; and what is wrong with any entry (a link, not a
    regular file)."""
    out, bad = [], []
    for d, dirs, names in os.walk(top):
        dirs[:] = sorted(x for x in dirs if x != "__pycache__")
        for n in sorted(dirs + names):
            path = os.path.join(d, n)
            rel = os.path.relpath(path, top).replace(os.sep, "/")
            if os.path.islink(path):
                bad.append(f"{rel}: a link")
            elif n in names and not os.path.isfile(path):
                bad.append(f"{rel}: not a regular file")
            elif n in names:
                out.append(rel)
        dirs[:] = [x for x in dirs if not os.path.islink(os.path.join(d, x))]
    return out, bad


def problems(repo=None, wanted=None):
    """What breaks the rules of embedding (spec d2144426 §2.4) in the tree repo: [reason], empty — none. wanted — the
    names of the profile's embeds (None — whatever is there)."""
    repo = repo or CORE
    bad = []
    top = os.path.join(repo, FOLDER)
    there = sorted(os.listdir(top)) if os.path.isdir(top) else []
    for n in there:
        if not (NAME.match(n) and os.path.isfile(os.path.join(top, n, PASSPORT))):
            bad.append(f"{FOLDER}/{n}: not an embed (a name [a-z0-9_-] with {PASSPORT})")
    found = [n for n, _ in roots(repo)]
    if wanted is not None:
        bad += [f"{FOLDER}/{n}: not in the profile" for n in found if n not in wanted]
        bad += [f"{FOLDER}/{n}: the profile's embed is not in the tree" for n in wanted if n not in found]
    seen, slots, keys = {}, {}, {}
    core_api = _version(API)
    for name, folder in roots(repo):
        got = passport(folder)
        if isinstance(got, str):
            bad.append(f"{FOLDER}/{name}/{got}")
        else:
            v = _version(got["api"])
            if v[0] != core_api[0] or v[1] > core_api[1]:
                bad.append(f"{FOLDER}/{name}: api {got['api']} — this core speaks {API}")
        files, wrong = _files(folder)
        bad += [f"{FOLDER}/{name}/{w}" for w in wrong]
        for rel in files:
            where_ = f"{FOLDER}/{name}/{rel}"
            if rel not in TOP and not rel.startswith(ALLOWED):
                bad.append(f"{where_}: an embed carries only {', '.join(ALLOWED)} and {', '.join(TOP)}")
                continue
            if rel.startswith(SHARED):
                if rel not in SHIMS and os.path.isfile(os.path.join(repo, *rel.split("/"))):
                    bad.append(f"{where_}: the core's own file — an embed adds, it does not replace")
                elif rel in seen:
                    bad.append(f"{where_}: {FOLDER}/{seen[rel]}/{rel} too")
                seen.setdefault(rel, name)
            if CATALOG.match(rel):
                lang = rel[len("i18n/"):-len(".json")]
                bad += _keys(os.path.join(folder, *rel.split("/")), where_, lang, name, keys, repo)
        for slot, spec in SLOTS.items():
            if spec["file"] in files:
                if slot in slots:
                    bad.append(f"{FOLDER}/{name}/{spec['file']}: the slot {slot} is {FOLDER}/{slots[slot]}'s already")
                slots.setdefault(slot, name)
    bad += _module_names(repo)
    return bad


def _module_names(repo):
    """An embed's modules as the hub will read them (core/modules.read, validate and the API range of the core that runs
    the check — the layer runs the tree's own, tools/package/layer.py): a manifest the hub would block fails the build;
    a name is none of the core's and of another embed's — the core's stays, an embed adds; the module's folder is not one
    of the core's modules/ (the package modules.<folder> is the core's: its catalog.py would never load)."""
    folders = module_folders(repo)
    if not folders:
        return []
    from core import modules, version
    root = os.path.join(repo, "modules")
    own = {}
    for d in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        if os.path.isfile(os.path.join(root, d, "module.json")):
            own.setdefault(modules.read(os.path.join(root, d), "builtin")["name"], None)
    bad = []
    for name, folder in folders:
        where_ = f"{FOLDER}/{name}/modules/{os.path.basename(folder)}/module.json"
        got = modules.read(folder, "builtin")
        fits_api, why = version.module_api(got["manifest"].get("api"), version.MODULE_API) if not got["errors"] \
            else (True, "")
        bad += [f"{where_}: {e}" for e in got["errors"]] + ([] if fits_api else [f"{where_}: {why}"])
        if os.path.isdir(os.path.join(root, os.path.basename(folder))):   # modules.<folder> would import the core's
            bad.append(f"{where_}: the folder modules/{os.path.basename(folder)} is the core's — name the module's own")
        if got["name"] in own:
            bad.append(f"{where_}: the module {got['name']} is " + (
                "the core's — an embed adds, it does not replace" if own[got["name"]] is None
                else f"{FOLDER}/{own[got['name']]}'s already"))
        else:
            own[got["name"]] = name
    return bad


def _keys(path, where_, lang, name, keys, repo):
    """A catalog of an embed: an object, none of its keys the core's or another embed's of the same language."""
    try:
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError) as e:
        return [f"{where_}: not readable ({e})"]
    if not isinstance(got, dict):
        return [f"{where_}: not an object"]
    if (repo, lang) not in keys:
        try:
            with open(os.path.join(repo, "i18n", f"{lang}.json"), encoding="utf-8") as f:
                core = json.load(f)
        except (OSError, ValueError):
            core = {}
        keys[(repo, lang)] = {k: None for k in (core if isinstance(core, dict) else {})}
    have, out = keys[(repo, lang)], []
    for k in got:
        if k in have:
            out.append(f"{where_}: the key {k} is " + ("the core's" if have[k] is None else f"{FOLDER}/{have[k]}'s"))
        else:
            have[k] = name
    return out


def signature(fn):
    """A function's parameters as NAMES writes them: "a, b=" (b has a default), with Python's own marks of the kinds
    NAMES never uses — "/" after the positional-only ones, "*" before the keyword-only ones, *args, **kw."""
    sig, out, star = inspect.signature(fn), [], False
    params = list(sig.parameters.values())
    for i, p in enumerate(params):
        if p.kind is p.KEYWORD_ONLY and not star:
            out.append("*")
            star = True
        if p.kind is p.VAR_POSITIONAL:
            out.append("*" + p.name)
            star = True
        elif p.kind is p.VAR_KEYWORD:
            out.append("**" + p.name)
        else:
            out.append(p.name + ("=" if p.default is not p.empty else ""))
        if p.kind is p.POSITIONAL_ONLY and (i + 1 == len(params) or params[i + 1].kind is not p.POSITIONAL_ONLY):
            out.append("/")
    return ", ".join(out)


def fits(have, want):
    """The parameters have (signature()) take every call want describes — the core passes them by position, by name or
    leaves the optional ones out: each of want's names in its place as an ordinary parameter (by position and by name),
    none of have's extra parameters required, none of want's optional ones required in have."""
    h = [x.strip() for x in have.split(",") if x.strip()]
    w = [x.strip() for x in want.split(",") if x.strip()]
    for i, x in enumerate(w):
        if i >= len(h) or h[i].startswith("*") or h[i] == "/" or h[i].rstrip("=") != x.rstrip("="):
            return False
        if "/" in h[i + 1:] or (x.endswith("=") and not h[i].endswith("=")):
            return False   # positional-only (the core may pass it by name), or required where the core may leave it out
    return all(x.endswith("=") or x == "/" or x.startswith("*") for x in h[len(w):])


def check(slot, module, api=API):
    """What of NAMES the module of slot does not have or takes otherwise: [reason], empty — it fits. api — the embed's
    number: a name OPTIONAL since a later minor one may be missing."""
    bad = []
    optional = {n for n, since in OPTIONAL.get(slot, {}).items() if (_version(since) or (0, 0)) > (_version(api) or (0, 0))}
    for name, want in NAMES[slot].items():
        if not hasattr(module, name):
            if name not in optional:
                bad.append(f"{slot}: no {name}")
            continue
        got = getattr(module, name)
        if want is type:
            if not (isinstance(got, type) and issubclass(got, Exception)):
                bad.append(f"{slot}: {name} is not an exception class")
        elif isinstance(want, type):
            if not isinstance(got, want) or isinstance(got, bool):
                bad.append(f"{slot}: {name} is not {want.__name__}")
        elif not callable(got):
            bad.append(f"{slot}: {name} is not a function")
        elif not fits(signature(got), want):
            bad.append(f"{slot}: {name}({signature(got)}) — the core calls {name}({want})")
    return bad


def check_tree(repo=None):
    """check() for every slot whose code is in an embed of this core's tree (the factory runs it on the built tree in
    its own process). -> [reason]."""
    bad = []
    for slot, spec in SLOTS.items():
        path = where(spec["file"], repo)
        if path is None or path == os.path.join(repo or CORE, *spec["file"].split("/")):
            continue
        name = next(n for n, top in roots(repo) if path.startswith(top + os.sep))
        got = passport(dict(roots(repo))[name])
        try:
            module = importlib.import_module(spec["module"])
        except Exception as e:  # noqa: BLE001 — an embed that does not import is a finding, not a crash of the check
            bad.append(f"{slot}: {spec['module']} does not import ({type(e).__name__}: {e})")
            continue
        bad += check(slot, module, got["api"] if isinstance(got, dict) else API)
    return bad

