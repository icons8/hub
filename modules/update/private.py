"""Private hub modules (module extraction spec §4.2–4.5): session login through an accounts pool (accounts), voice reply
(voice-reply), the stats report (stats): the code is in a separate repository, and the hub installs them itself with a commit pin.

- `modules/private.json` in the core tree says which repository, tag and commit, and which modules (folder, capability). No file:
  nothing is done (a hub without private modules).
- The ensure step runs in the update loop (at start and once an hour while not everything is installed; the update module) and at hub start before
  hubd (hub-up, no longer than BOOT_WAIT, only when there is something to install). First `git ls-remote` of the modules repository
  with this machine's keys: this is the check "the invitation was accepted and the key works". It installs `hub module add <address>
  --ref <tag> --subdir <folder>` on behalf of the owner (as post_update calls hub migrate). What the owner removed (`hub module
  remove`, `modules.use.<capability> = "builtin"`) is a refusal (declined): not installed again, to bring it back use `hub module add` by hand.
  If it fails, nothing changes (the built-in code works), the owner gets one line (owner.notify id private-modules,
  cleared on success), and a fact update.modules. The result is `<state>/private-modules.json`.
- Hub readiness for a release without built-in code is `<git code folder>/hub-ready/<state folder key>.json`: every
  hub on this code folder writes its own; the update gate (needs-module) does not merge a target whose private.json is
  `required` while the function is needed somewhere and the module does not handle it (§4.4).
- Waiting for startup (§4.5): when there is no built-in login in the tree, the pool is needed and the module is being installed or started, a session
  start waits (core/daemon.wait_private) instead of going to the machine's login.

The "function is needed" detector reads files itself, without the built-in functions' code: in a release without them it is the same.
"""
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

PIN = os.path.join("modules", "private.json")
PIN_GIT = "modules/private.json"   # the same file inside a commit (`git show <sha>:…`): always «/», on Windows too
STATE = "private-modules.json"     # in the state folder: the step result
LOCK = "private-modules.lock"
READY_DIR = "hub-ready"            # in the git code folder (shared by hubs on this code folder)
NOTICE = "private-modules"         # id of the owner's line (owner.notify)
GATE_NOTICE = "needs-module"
EVERY = 3600          # the step runs no more often while everything is installed or installing failed
ACCESS_EVERY = 86400  # everything is installed: access (ls-remote) still once a day: a fresh access for readiness (§6.2)
HELLO_WAIT = 300      # installed: hello must arrive within this long, otherwise "hello did not arrive"
UNREAD_FAIL = 6 * 3600  # the state of modules is not read for this long: no longer "we do not know", the owner is told
HELLO_RETRY = 60      # while waiting for hello: the next step after this long
HELLO_FAILED_RETRY = 600  # failed only for hello: the module may come up later (restore, restart), look again sooner than in EVERY
BOOT_WAIT = 120       # the step at hub start before hubd: no longer than this (hub-up)
INSTALLING_TTL = 900  # "installing" older than this: the step died in the middle, do not wait for it
PRIVATE_WAIT = 600    # §4.5: a session start waits for the login module no longer than this (initial setup)
GIT_WAIT = 60
REF = re.compile(r"pm-v\d+\.\d+\.\d+")
COMMIT = re.compile(r"[0-9a-f]{40}")
SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
CAPS = ("profile", "speech", "stats")
# why a module is not ready, a short code without text (the module's `code` in the step result and in the stats report):
# git let us not in / there is no tag / the tag was moved / `hub module add` failed / the module is paused or stopped /
# hello did not arrive / the state of modules could not be read (the module itself may be fine)
CODES = ("no-access", "no-ref", "tag", "install", "stopped", "hello", "read")
ACCESS_CODE = {"no-access": "no-access", "no-ref": "no-ref", "tag-moved": "tag"}   # ls-remote result -> code
REASON_CODE = {**ACCESS_CODE, "install": "install", "stopped": "stopped", "hello": "hello", "read": "read"}   # reason of the step -> code
# the built-in implementation in the core tree: present means the function works without a module (release R1); absent means only through a module (R2)
BUILTIN = ("profile", "speech", "stats")   # their files and the check — core/cutout.py


# --- the pin file -----------------------------------------------------------------------------------------------------

def parse(text):
    """private.json -> {repo, url, ref, commit, modules: {name: {subdir, cap}}, required: [names]} or None: no file
    or it has the wrong shape (install nothing: a broken pin is no reason to pull in who knows what)."""
    from core import modules
    try:
        raw = json.loads(text) if text else None
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    repo, url, ref, commit = raw.get("repo"), raw.get("url"), raw.get("ref"), raw.get("commit")
    mods = raw.get("modules")
    if not (isinstance(repo, str) and SLUG.fullmatch(repo) and isinstance(url, str) and url
            and isinstance(ref, str) and REF.fullmatch(ref) and isinstance(commit, str) and COMMIT.fullmatch(commit)
            and isinstance(mods, dict) and mods):
        return None
    out = {}
    for name, m in mods.items():
        if not (isinstance(name, str) and modules.NAME.fullmatch(name) and isinstance(m, dict)
                and isinstance(m.get("subdir"), str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", m["subdir"])
                and m.get("cap") in CAPS):
            return None
        out[name] = {"subdir": m["subdir"], "cap": m["cap"]}
    req = raw.get("required", False)
    required = sorted(out) if req is True else [n for n in req if n in out] if isinstance(req, list) else []
    return {"repo": repo, "url": url, "ref": ref, "commit": commit, "modules": out, "required": required}


def tree():
    """The code folder of this hub (hub command tests: the folder of the fake pin HUB_TEST_PRIVATE_PIN)."""
    pin = os.environ.get("HUB_TEST_PRIVATE_PIN")
    return os.path.dirname(os.path.dirname(os.path.abspath(pin))) if pin else REPO


def _pin_text(repo):
    # hub command tests: the pin of their own fake tree instead of this code's tree
    path = os.environ.get("HUB_TEST_PRIVATE_PIN") if repo == REPO else None
    try:
        from core import embed   # the pin is a file of defaults an embed may bring (core/embed.py)
        with open(path or embed.file(PIN_GIT, repo) or os.path.join(repo, PIN), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def pin_at(git, ref):
    """The text of the pin in commit ref (git(*args) -> stdout, "" on a failure): the core's own modules/private.json,
    else an embed's (core/embed.py: embed/<name>/modules/private.json of the first embed by name that has one)."""
    if git("cat-file", "-t", f"{ref}:{PIN_GIT}").strip() == "blob":   # the core's own, empty or not
        return git("show", f"{ref}:{PIN_GIT}")
    names = sorted(x for x in (git("ls-tree", "--name-only", f"{ref}:embed") or "").split()
                   if re.fullmatch(r"[a-z0-9_-]{1,32}", x))   # core/embed.NAME
    for name in names:
        if git("cat-file", "-t", f"{ref}:embed/{name}/embed.json").strip() == "blob":
            got = git("show", f"{ref}:embed/{name}/{PIN_GIT}")
            if got:
                return got
    return ""


def _app():
    from core import packaged
    return packaged.is_app(packaged.info())


def load_pin(repo=REPO):
    """The pin to install by, or None. The app has none whatever the file says (app spec §3.3: the four functions are its
    built-in code)."""
    if _app():
        return None
    return parse(_pin_text(repo))


def parse_retired(text):
    """The pin of the release that returns hubs to the built-in code (app spec §3.3, decision "one copy in the core"):
    `{"repo", "url", "modules": {}, "retired": {name: {subdir, cap}}}` -> {repo, url, modules: the retired ones} or None.
    parse() sees no modules in it — no pin: older updaters and this ensure step install nothing by it."""
    try:
        raw = json.loads(text) if text else None
    except ValueError:
        return None
    if not isinstance(raw, dict) or raw.get("modules") != {}:
        return None
    probe = parse(json.dumps({**raw, "ref": "pm-v0.0.0", "commit": "0" * 40, "modules": raw.get("retired"),
                              "required": False}))
    return {"repo": probe["repo"], "url": probe["url"], "modules": probe["modules"]} if probe else None


def load_retired(repo=REPO):
    """What the retire step removes, or None: the retired pin; on the app also the modules of an active pin."""
    text = _pin_text(repo)
    got = parse_retired(text)
    if got is None and _app():
        got = parse(text)
    return got


def builtin_present(cap, repo=REPO):
    """The built-in code of function cap in the core tree (R1), or it is absent (a release without it, R2; the open flavor
    of the app) — core/cutout.py, the one check."""
    from core import cutout
    return cutout.present(cap, repo)


# --- address -----------------------------------------------------------------------------------------------------------

def _git(repo, *args):
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=30,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def address(pin, origin):
    """The modules repository address in the same way the hub reaches its own origin (ssh or https: its credential
    helper or key). The login and token are not taken from the origin address (no secret is written to hub.json). If there is no origin or
    it is a folder on disk (a developer hub, a test bench), the address from private.json as is."""
    repo = pin["repo"] + ".git"
    o = (origin or "").strip()
    m = re.fullmatch(r"(?:ssh|git\+ssh)://(?:([^@/]+)@)?([^/:]+)(?::(\d+))?/.+", o)
    if m:
        user, host, port = m.groups()
        return f"ssh://{user + '@' if user else ''}{host}{':' + port if port else ''}/{repo}"
    m = re.fullmatch(r"https?://(?:[^@/]+@)?([^/]+)/.+", o)
    if m:
        return f"{o.split('://', 1)[0]}://{m.group(1)}/{repo}"
    m = re.fullmatch(r"(?:([^@/:]+)@)?([^/:]+):(?!//).+", o)
    if m and not re.fullmatch(r"[A-Za-z]:[\\/].*", o):
        user, host = m.groups()
        return f"{user + '@' if user else ''}{host}:{repo}"
    return pin["url"]


def origin_of(repo, cfg):
    remote = ((cfg or {}).get("update") or {}).get("remote") or "origin"
    return _git(repo, "remote", "get-url", remote)


def _repo_key(url):
    """A key for comparing addresses: host/path without scheme, login, ".git" and case; for a folder, its full path."""
    from core import codestore
    u = str(url or "").strip()
    if codestore.is_local(u):
        p = u[len("file://"):] if u.lower().startswith("file://") else u
        return "path:" + os.path.normcase(os.path.realpath(os.path.expanduser(p))).rstrip("/\\")
    m = re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/]+@)?([^/:]+)(?::\d+)?(/.*)?", u)   # scheme: a port is not a path
    if m:
        host, path = m.group(1), m.group(2) or ""
    else:   # scp form host:path
        host, _, path = re.sub(r"^[^@/]+@", "", u).partition(":")
    path = re.sub(r"\.git$", "", path.strip("/"))
    return f"{host.lower()}/{path.lower()}"


def same_repo(a, b, pin=None):
    """a and b are one modules repository: one address in any form; with pin, also the same owner/name path on any host
    (an ssh host alias from ~/.ssh/config)."""
    ka, kb = _repo_key(a), _repo_key(b)
    if ka == kb:
        return True
    if pin and not ka.startswith("path:"):
        return ka.split("/", 1)[-1] == pin["repo"].lower()
    return False


# --- what is needed, what is installed ---------------------------------------------------------------------------------

def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError):
        return None
    return got


def _conduit_login(config_dir):
    """The conduit login of Claude Code is found in the login file (.credentials.json). We do not read the Mac keychain: a conduit
    that signed in earlier has already put a row into the pool, and the pool detector sees it."""
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    blob = _read_json(os.path.join(home, ".credentials.json")) or {}
    oauth = blob.get("mcpOAuth") if isinstance(blob, dict) else None
    def conduit(name):   # added by hand, or by a plugin: Claude Code names those "plugin:<plugin>:conduit"
        return name == "conduit" or (isinstance(name, str) and name.startswith("plugin:") and name.count(":") == 2
                                     and name.endswith(":conduit"))
    return any(isinstance(e, dict) and conduit(e.get("serverName")) and (e.get("accessToken") or e.get("refreshToken"))
               for e in (oauth or {}).values()) if isinstance(oauth, dict) else False


def needed(cfg, config_dir=None):
    """{module name: whether its function is needed on this hub} (§4.4): accounts, if the pool has a row (even paused), the gateway is on
    or conduit is not off and its login is found; voice-reply, if the voice reply is on; stats, if the report is on."""
    from core import catalog
    cfg = cfg or {}
    conf = config_dir or catalog.config_dir()
    pool = _read_json(os.path.join(conf, "accounts.json"))
    rows = pool.get("accounts") if isinstance(pool, dict) else pool if isinstance(pool, list) else None
    gate = cfg.get("gateway") if isinstance(cfg.get("gateway"), dict) else {}
    conduit = _read_json(os.path.join(conf, "conduit.json"))
    conduit_on = not (isinstance(conduit, dict) and conduit.get("off")) and _conduit_login(conf)

    def on(section):
        s = cfg.get(section)
        return isinstance(s, dict) and s.get("enabled") is True

    return {"accounts": bool(rows) or gate.get("enabled") is True or conduit_on,
            "voice-reply": on("voice_reply"), "stats": on("stats")}


def sources(cfg, pin, url=None):
    """{module name: hub.json source}: sources from the modules repository with this module's folder."""
    from core import modules
    out = {}
    for s in modules.settings(cfg or {}).get("sources") or []:
        if not (isinstance(s, dict) and isinstance(s.get("git"), str)):
            continue
        if not (same_repo(s["git"], url or pin["url"], pin) or same_repo(s["git"], pin["url"], pin)):
            continue
        for name, m in pin["modules"].items():
            if str(s.get("subdir") or "").replace("\\", "/").strip("/") == m["subdir"]:
                out[name] = s
    return out


def enabled(cfg, name):
    from core import modules
    return name in (modules.settings(cfg or {}).get("enabled") or [])


def read_state(state_dir):
    got = _read_json(os.path.join(state_dir, STATE))
    return got if isinstance(got, dict) else {}


def write_state(state_dir, st):
    from core import catalog
    catalog.write_private(os.path.join(state_dir, STATE), st)


def _state_lock(state_dir):
    from core import lock
    os.makedirs(state_dir, exist_ok=True)
    return lock.exclusive(os.path.join(state_dir, LOCK + ".state"))


def save_result(state_dir, st, began=None):
    """The ensure step result, under the file lock; the owner's refusals (the declined field) are those in the file now: `hub module
    remove` in the middle of a step is not lost. began is the file as the step read it at the start. -> what was written."""
    with _state_lock(state_dir):
        now = read_state(state_dir)
        st = {**st, "declined": list(now.get("declined") or [])}
        if now.get("next") == 0 and (began or {}).get("next") != 0:
            st["next"] = 0   # a refusal or return in the middle of a step asked for the next pass at once, not in an hour
        write_state(state_dir, st)
    return st


def declined(state_dir, cfg, pin):
    """Modules the hub owner removed (hub module remove) or keeps built-in (modules.use.<capability> = builtin)."""
    from core import providers
    got = {n for n in read_state(state_dir).get("declined") or [] if isinstance(n, str) and n in pin["modules"]}
    return got | {n for n, m in pin["modules"].items() if providers.named(cfg or {}, m["cap"]) == "builtin"}


def _edit_declined(state_dir, names, on):
    """-> the names this edit added (on) or removed, under the same lock as the write."""
    with _state_lock(state_dir):
        st = read_state(state_dir)
        have = set(st.get("declined") or [])
        st["declined"] = sorted(have | set(names) if on else have - set(names))
        if st["declined"] != sorted(have):
            st["next"] = 0   # the step runs on the nearest pass (a minute), not in an hour: "About the hub" and hub modules already show it
            write_state(state_dir, st)
    return sorted(set(names) - have) if on else sorted(set(names) & have)


def private_names(state_dir, cfg, which, repo=REPO):
    """Modules of the modules repository that `hub module add|remove <which>` touches (a module name or an address)."""
    pin = load_pin(repo)
    if not pin:
        return []
    have = sources(cfg, pin)
    if which in pin["modules"]:
        return [which] if which in have else []
    if same_repo(which, pin["url"], pin) or any(same_repo(which, s["git"], pin) for s in have.values()):
        return sorted(have)
    return []


def on_removed(state_dir, names):
    """`hub module remove` removes private modules: an owner's refusal, the ensure step no longer installs them. -> the names whose refusal
    is recorded now (earlier ones are not them: a failed removal clears only its own)."""
    return _edit_declined(state_dir, names, True) if names else []


def undecline(state_dir, names):
    """The refusal is lifted: the ensure step handles the module again (hub module add, a failed remove)."""
    if names and set(names) & set(read_state(state_dir).get("declined") or []):
        _edit_declined(state_dir, names, False)


def on_added(state_dir, url, subdir, repo=REPO):
    """`hub module add` of the modules repository: a module removed earlier is installed and updated by the ensure step again."""
    pin = load_pin(repo)
    if not pin or not same_repo(url, pin["url"], pin):
        return
    sub = str(subdir or "").replace("\\", "/").strip("/")
    undecline(state_dir, [n for n, m in pin["modules"].items() if not sub or m["subdir"] == sub])


# --- access ------------------------------------------------------------------------------------------------------------

def _t(name, /, **kw):
    """The catalog text in this process's language (lazy: this file also runs as a script, `private.py boot`)."""
    from core import i18n
    return i18n.t(name, **kw)


def ls_remote(url, ref, timeout=GIT_WAIT):
    """(result, commit, why): ok means the tag exists; no-access means git did not let us in (invitation not accepted, no key, network);
    no-ref means there is access but no tag. The commit is the one under the tag (for an annotated one, the commit it points to)."""
    from core import codestore
    try:
        r = codestore.git("ls-remote", "--", url, f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}", timeout=timeout,
                          remote=url)
    except RuntimeError as e:
        return "no-access", None, str(e)[:300]
    if r.returncode:
        return "no-access", None, codestore.said(r)[:300]
    refs = dict(reversed(line.split("\t", 1)) for line in r.stdout.splitlines() if "\t" in line)
    sha = refs.get(f"refs/tags/{ref}^{{}}") or refs.get(f"refs/tags/{ref}")
    return ("ok", sha, "") if sha else ("no-ref", None, _t("private.no_ref", ref=ref))


# --- module state (hello) in a separate process: the hub journal is large, the update process lives long -------------

def _answered(state_dir, names):
    """Modules that answered hello in their current launch, by the files hubd writes (the launch and the slice of hub state):
    no journal read. A module paused for a while still has the process and the hello: such a one counts as ready here."""
    from core import modpages, modrun
    wanted, out, static = modrun.wants(state_dir), {}, modpages.statics(state_dir)
    for n in names:
        run, got = modrun.run_of(state_dir, n), modrun.slice_of(state_dir, n)
        if n in static:   # a module without a process (Module API 2.10): nobody to say hello — it is in place when hubd keeps it
            out[n] = {"ready": True, "status": "ok"}
        elif n in wanted and run and got and got.get("ready") and got["ready"] == run.get("launch"):
            out[n] = {"ready": True, "status": "ok"}
    return out


def module_states(state_dir, names):
    """{name: {"ready": bool, "status": ok|suspended|error|blocked}} for each name. First from the files (_answered);
    the rest from the hub journal, in a separate process. If that read did not work (the journal is big, the machine is
    busy: a timeout), such a module is {"ready": False, "status": "unknown", "unread": True}: we do not know, which is
    not the same as "hello did not arrive"."""
    out = _answered(state_dir, names)
    rest = [n for n in names if n not in out]
    if not rest:
        return out
    try:
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "states", state_dir, *rest],
                           capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
                           env={**os.environ, "HUB_STATE_DIR": state_dir})
        got = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        got = None
    got = got if isinstance(got, dict) else {}
    for n in rest:
        out[n] = got[n] if isinstance(got.get(n), dict) else {"ready": False, "status": "unknown", "unread": True}
    return out


def _states_main(state_dir, names):
    from core import modpages, modrun
    mods = modrun.Modules(state_dir)
    wanted, static = modrun.wants(state_dir), modpages.statics(state_dir)
    print(json.dumps({n: {"ready": n in static or mods.ready(n, wanted) is not None, "status": mods.status(n)}
                      for n in names}))


# --- the ensure step ---------------------------------------------------------------------------------------------------

def hub_cli(state_dir, args):
    """A hub command on behalf of the owner, into this hub (as modules/update.hub_cli)."""
    env = {**{k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_MODULE", "HUB_EXTENSION")},
           "HUB_STATE_DIR": state_dir}
    try:
        r = subprocess.run([sys.executable, os.path.join(REPO, "bin", "hub"), *args], env=env, capture_output=True,
                           text=True, timeout=600, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, "no answer within 600 s"   # as the command's output: the words of codestore.git for a hung git
    return r.returncode, (r.stdout + r.stderr).strip()


class Ensure:
    """One "install private modules" step. hub(state_dir, args) -> (code, output) is the owner's hub command;
    access(url, ref) -> (result, commit, why) is ls-remote; states(state_dir, names) is module readiness (hello)."""

    def __init__(self, repo, state_dir, cfg, hub=hub_cli, access=ls_remote, states=module_states, clock=time.time,
                 emit=None, config_dir=None):
        from core import spool
        self.repo, self.state_dir, self.cfg, self.hub, self.access, self.states = repo, state_dir, cfg, hub, access, states
        self.clock, self.config_dir = clock, config_dir
        self.emit = emit or (lambda t, e, d: spool.put(os.path.join(state_dir, "spool"), t, e,
                                                        {**d, "channel": "cli", "owner": True, "source": "update"},
                                                        src="update"))

    def due(self, boot=False):
        """Whether it is time for the step: something is not at the pinned commit (and not removed), we wait for hello, access was not checked for long, or
        an hour has passed since a failure. boot is hub start: only when there is something to install and the failure is not fresh."""
        pin = load_pin(self.repo)
        if not pin:
            return False
        st, now = read_state(self.state_dir), self.clock()
        if boot:
            return bool(self.todo(pin)) and not (st.get("commit") == pin["commit"] and st.get("state") == "failed"
                                                 and now - (st.get("ts") or 0) < EVERY)
        return st.get("commit") != pin["commit"] or now >= (st.get("next") or 0) or self.came_up(st)

    def came_up(self, st):
        """A module the last step called stopped (or without hello) answers hello in its current launch now (hub modules reset,
        a late start): look at once, not at the hourly step. Only the files hubd writes are read, the journal is not."""
        names = [n for n, m in (st.get("modules") or {}).items()
                 if isinstance(m, dict) and m.get("state") == "failed" and m.get("code") in ("stopped", "hello")]
        return bool(names) and bool(_answered(self.state_dir, names))

    def todo(self, pin):
        """What to install: not removed and not at the pinned commit. One installed (at any commit) but switched off in "Modules" is
        the owner's word: not installed and not switched on again (hub module add would switch it on)."""
        have, no = sources(self.cfg, pin), declined(self.state_dir, self.cfg, pin)
        return [n for n in pin["modules"] if n not in no and not (n in have and (
            (have[n].get("commit") == pin["commit"] and not self.damaged(have[n])) or not enabled(self.cfg, n)))]

    def damaged(self, src):
        """The folder of the module's pinned commit is gone or its files were changed (a temp cleaner, a failed disk, an edit by
        hand): the module is blocked and the built-in code works, but nothing brings the files back by itself except `hub
        module add` of the same commit — so such a module is also to install."""
        from core import modsrc
        try:
            return modsrc.damage(self.state_dir, src["git"], src["commit"]) is not None
        except (OSError, ValueError, KeyError, TypeError):
            return False   # an unreadable list is not a reason to reinstall in a loop: the next step looks again

    def run(self, boot=False):
        """The step under the state folder lock (a second one, the start step or the update process, will not wait). -> the result
        (what is written to private-modules.json) or None: no pin or a step is already running."""
        from core import lock
        pin = load_pin(self.repo)
        if not pin:
            return None
        os.makedirs(self.state_dir, exist_ok=True)
        held = lock.acquire(os.path.join(self.state_dir, LOCK))
        if held is None:
            return None
        try:
            return self._run(pin, boot)
        finally:
            held.close()

    def _cfg(self):
        from core import catalog
        try:
            self.cfg = catalog.load_config(self.state_dir)
        except ValueError:
            pass   # broken hub.json: the previous settings
        return self.cfg

    def _run(self, pin, boot):
        now = self.clock()
        prev = read_state(self.state_dir)
        url = address(pin, origin_of(self.repo, self.cfg))
        have = sources(self.cfg, pin, url)
        url = next((s["git"] for s in have.values()), url)   # already installed with another form of the address: the same address
        no = declined(self.state_dir, self.cfg, pin)
        todo = self.todo(pin)
        asked = set(todo)
        # off is what the step does not touch: removed by command (declined, written by hub module remove) and built-in by the owner's word
        st = {"commit": pin["commit"], "ref": pin["ref"], "url": _shown(url), "off": sorted(no),
              "access": prev.get("access") if prev.get("commit") == pin["commit"] else None,
              "modules": {}, "told": prev.get("told"), "unready": dict(prev.get("unready") or {}),
              "unread": dict(prev.get("unread") or {})}
        reasons = []   # (code, text): why not everything is installed
        blocked = None   # the code of why the modules to install were not (access, tag): for their `code`
        fresh_access = (st["access"] or {}).get("ts") and now - st["access"]["ts"] < ACCESS_EVERY
        if todo or not fresh_access:
            if todo:
                save_result(self.state_dir, {**prev, "state": "installing", "ts": now, "commit": pin["commit"]}, prev)
            from core import gitaccess
            got, sha, why = self.access(url, pin["ref"])
            # access is whether git let us into the repository; no tag or it was moved: there is access, the release is not the right one
            st["access"] = {"ok": got != "no-access", "ts": round(now),
                            **({"why": why[:300]} if got == "no-access" and why else {}),
                            **({"code": gitaccess.code(why)} if got == "no-access" else {})}
            if got == "ok" and sha != pin["commit"]:
                got, why = "tag-moved", _t("private.tag_moved", ref=pin["ref"], sha=str(sha)[:12], pinned=pin["commit"][:12])
            # everything is installed and the daily check did not get through (network): only access; a tag that vanished or moved: always say so
            if got != "ok" and (todo or got != "no-access"):
                reasons.append((got, why))
            if got != "ok":
                blocked, todo = ACCESS_CODE.get(got), []
        for name in todo:
            if name in declined(self.state_dir, self.cfg, pin):
                continue   # removed in the middle of the step: the add command would remove the refusal as well
            m = pin["modules"][name]
            # by the pinned commit, not the tag: the tag could have been moved after ls-remote; git itself checks that this is what arrived
            code, out = self.hub(self.state_dir, ["module", "add", url, "--ref", pin["commit"], "--subdir", m["subdir"]])
            st["unready"].pop(name, None)   # installed anew: we wait for hello again for HELLO_WAIT
            self._cfg()
            src = sources(self.cfg, pin, url).get(name)
            if code != 0 or not src:
                reasons.append(("install", f"{name}: hub module add — " + (out[-300:] or _t("private.exit_code", code=code))))
                st["modules"][name] = {"state": "failed", "why": (out[-300:] or f"exit code {code}"), "code": "install"}
            elif src.get("commit") != pin["commit"]:
                reasons.append(("tag-moved", _t("private.installed_moved", name=name, installed=str(src.get("commit"))[:12],
                                                pinned=pin["commit"][:12])))
                st["modules"][name] = {"state": "failed", "why": "the commit is not the pinned one", "code": "tag"}
        have, no = sources(self.cfg, pin, url), declined(self.state_dir, self.cfg, pin)   # what was removed during the step too
        st["off"] = sorted(no)
        at_pin = [n for n in pin["modules"] if n in have and have[n].get("commit") == pin["commit"] and n not in no]
        placed = [n for n in at_pin if enabled(self.cfg, n)]
        switched_off = {n for n in have if n not in no and not enabled(self.cfg, n)}   # switched off in "Modules"
        states = self.states(self.state_dir, placed) if placed else {}
        waiting = unread = False
        for name in pin["modules"]:
            if name in st["modules"]:
                continue   # did not come up in this step (failed): a failure, not the owner's word
            if name in no or name in switched_off:   # removed or switched off in "Modules": its word
                st["modules"][name] = {"state": "declined"}
                continue
            if name not in placed:
                st["modules"][name] = {"state": "missing", **({"code": blocked} if blocked and name in asked else {})}
                continue
            got = (states or {}).get(name) or {}
            if got.get("ready"):
                st["unready"].pop(name, None)
                st["modules"][name] = {"state": "ready", "commit": pin["commit"]}
                continue
            # not ready: since which step in a row (after a hub restart the module comes up later: not a failure)
            since = st["unready"].setdefault(name, round(now))
            if got.get("status") in ("suspended", "error", "blocked"):
                st["modules"][name] = {"state": "failed", "why": f"module {got['status']}", "commit": pin["commit"],
                                       "code": "stopped"}
                reasons.append(("stopped", _t("private.module_status", name=name, status=got["status"])))
            elif now - since < HELLO_WAIT:
                st["modules"][name] = {"state": "installed", "commit": pin["commit"]}
                waiting = True
            elif states is None or got.get("unread"):
                # the state could not be read: we do not know whether hello came. Not a failure of the module and not a line to
                # the owner; the next look is in an hour, not in a minute (the read is heavy). But not for ever: a module that
                # never came up on a hub whose journal cannot be read would stay silent - count from the first unread step
                if now - st["unread"].setdefault(name, round(now)) < UNREAD_FAIL:
                    st["modules"][name] = {"state": "installed", "commit": pin["commit"], "code": "read"}
                    unread = True
                else:
                    st["modules"][name] = {"state": "failed", "why": "the modules' state is not readable",
                                           "commit": pin["commit"], "code": "read"}
                    reasons.append(("read", _t("private.unread_hours", name=name, hours=UNREAD_FAIL // 3600)))
            else:
                st["modules"][name] = {"state": "failed", "why": "hello did not arrive", "commit": pin["commit"],
                                       "code": "hello"}
                reasons.append(("hello", _t("private.hello_missing", name=name, minutes=HELLO_WAIT // 60)))
        st["unready"] = {n: t for n, t in st["unready"].items() if n in placed}
        st["unread"] = {n: t for n, t in st["unread"].items() if (st["modules"].get(n) or {}).get("code") == "read"}
        try:
            from core import providers
            st["local_edits"] = providers.local_edits(self.repo, self.cfg)
        except Exception:  # noqa: BLE001 — git in the code folder: the report without edits, the step goes on
            st["local_edits"] = prev.get("local_edits") or {}
        missing = sorted(n for n, m in st["modules"].items() if m["state"] in ("missing", "failed"))
        hello_only = bool(reasons) and all(code == "hello" for code, _ in reasons)
        ok = not reasons and not missing
        st.update(state="pending" if (waiting or unread) and not reasons else "done" if ok else "failed", ts=round(now),
                  missing=missing, why="; ".join(t for _, t in reasons)[:600],
                  **({"code": REASON_CODE[reasons[0][0]]} if reasons and reasons[0][0] in REASON_CODE else {}),
                  next=round(now + (HELLO_RETRY if waiting else HELLO_FAILED_RETRY if hello_only else EVERY)))
        self.tell(st, reasons, pin)
        return save_result(self.state_dir, st, prev)

    def tell(self, st, reasons, pin):
        """The update.modules fact and a line to the owner, only when the result changed (do not repeat the same thing once an hour)."""
        # own edits to the built-in code: on a colleague's hub (on a publishing hub without origin this is core development itself)
        edits = sorted(c for c in (st.get("local_edits") or {}) if c in {m["cap"] for m in pin["modules"].values()}) \
            if origin_of(self.repo, self.cfg) else []
        mark = [st["state"] == "done", st["missing"], [c for c, _ in reasons], st["off"], edits,
                bool((st.get("access") or {}).get("ok"))]
        # a changed cause of the refusal (the network came back, and the token turned out to be revoked) is told again
        from core import gitaccess
        mark += [k for k in [gitaccess.kind(w) for c, w in reasons if c == "no-access"] if k]
        if st["state"] == "pending" or st.get("told") == mark:
            return
        st["told"] = mark
        self.emit("update.modules", None, {"ok": st["state"] == "done", "missing": st["missing"], "why": st["why"],
                                           "access": bool((st.get("access") or {}).get("ok")),
                                           "declined": st["off"], "local_edits": edits, "ref": pin["ref"],
                                           "modules": {n: m["state"] for n, m in st["modules"].items()}})
        lines = [notice_line(code, why, pin) for code, why in reasons]
        if edits:
            lines.append(_t("private.local_edits", repo=pin["repo"], files=", ".join(
                p for c in edits for p in (st["local_edits"].get(c) or [])[:3])))
        if lines:
            if not builtin_present("profile", self.repo) and "accounts" in st["missing"]:
                lines.append(_t("private.pool_down"))
            self.emit("owner.notify", None, {"id": NOTICE, "title": _t("private.title"),
                                             "text": "\n".join(dict.fromkeys(f"• {x}" for x in lines))})
        else:
            self.emit("owner.notify_resolved", None, {"id": NOTICE})


RETIRED = "private-retired.json"   # in the state folder: the retire step's result


def read_retired(state_dir):
    got = _read_json(os.path.join(state_dir, RETIRED))
    return got if isinstance(got, dict) else {}


class Retire:
    """The step of the release that returns hubs to the built-in code (app spec §3.3, decision "one copy in the core"):
    the pin retires the modules, and the sources the ensure step installed itself go (`hub module remove` as the owner);
    the functions come back to the built-in by the usual "module removed" transition (core/providers.py). The ensure step
    installs by the pinned commit (`--ref <sha>`), so a source of the modules repository with its module's folder and a
    commit for its ref is its own; one the owner added by hand (a tag, no ref) stays, and so does a name another source
    or a folder of modules.paths also gives (removing by name would take that one too). Nothing is declined: a release that
    brings a module back installs it as usual. No retired pin (and not the app): None, nothing is done."""

    def __init__(self, repo, state_dir, cfg, hub=hub_cli, clock=time.time):
        self.repo, self.state_dir, self.cfg, self.hub, self.clock = repo, state_dir, cfg, hub, clock

    def own(self, pin):
        """{name: source} the ensure step installed."""
        return {n: s for n, s in sources(self.cfg, pin).items()
                if isinstance(s.get("ref"), str) and COMMIT.fullmatch(s["ref"]) and s.get("commit") == s["ref"]}

    def shared(self, name, src):
        """Another source or a modules.paths folder gives this name too."""
        from core import modsrc, modules
        st = modules.settings(self.cfg)
        for s in st.get("sources") or []:
            if isinstance(s, dict) and (s.get("git"), s.get("subdir")) != (src.get("git"), src.get("subdir")) \
                    and isinstance(s.get("git"), str) and s["git"] \
                    and name in modsrc._source_names(self.state_dir, s):
                return True
        return any(name in {f["name"] for f in modules._scan(os.path.expanduser(p), "path")}
                   for p in st.get("paths") or [] if isinstance(p, str))

    def _cfg(self):
        from core import catalog
        try:
            self.cfg = catalog.load_config(self.state_dir)
        except ValueError:
            pass
        return self.cfg

    def run(self, boot=False):
        """-> the result written to private-retired.json: {state: retired, removed: [this step's], gone: [all steps'],
        kept: [left installed], why, failed: {name: when its removal failed}} and changed (written now, not the same as
        before) or None (no retired pin, or the ensure/retire step is already running)."""
        from core import catalog, lock
        pin = load_retired(self.repo)
        if not pin:
            return None
        os.makedirs(self.state_dir, exist_ok=True)
        held = lock.acquire(os.path.join(self.state_dir, LOCK))
        if held is None:
            return None
        try:
            self._cfg()
            prev, now = read_retired(self.state_dir), self.clock()
            own = self.own(pin)
            # a failed removal is retried after EVERY, and at hub start at once (the owner may have fixed the cause);
            # what is not installed any more has no failure to carry (the file may be damaged: only the right shapes)
            failed = {n: t for n, t in (prev.get("failed") if isinstance(prev.get("failed"), dict) else {}).items()
                      if n in own and isinstance(t, (int, float)) and now - t < EVERY and not boot}
            reasons = prev.get("reasons") if isinstance(prev.get("reasons"), dict) else {}
            removed, why = [], {n: w for n, w in reasons.items() if n in failed and isinstance(w, str)}
            for name in sorted(own):
                src = self.own(pin).get(name)   # from the settings as they are now: each removal rewrites them
                if not src or name in failed:
                    continue
                if self.shared(name, src):
                    why[name] = _t("private.retire_shared", name=name)
                    continue
                code, out = self.hub(self.state_dir, ["module", "remove", name])
                self._cfg()
                if code == 0 and name not in sources(self.cfg, pin):
                    removed.append(name)
                else:
                    failed[name] = round(now)
                    why[name] = f"{name}: hub module remove — " + (out[-300:] or _t("private.exit_code", code=code))
            gone = (prev.get("gone") if isinstance(prev.get("gone"), list) else []) + removed   # what this hub had
            st = {"state": "retired", "retired": sorted(pin["modules"]), "removed": removed,
                  "gone": sorted({n for n in gone if isinstance(n, str)}), "kept": sorted(sources(self.cfg, pin)),
                  "why": "; ".join(why[n] for n in sorted(why))[:600], "reasons": why, "failed": failed}
            # the update loop runs this every minute: the same result is no news (no write, no log line)
            changed = bool(removed) or {k: v for k, v in prev.items() if k not in ("ts", "removed")} \
                != {k: v for k, v in st.items() if k != "removed"}
            if changed:
                catalog.write_private(os.path.join(self.state_dir, RETIRED), {**st, "ts": round(now)})
            return {**st, "ts": round(now), "changed": changed}
        finally:
            held.close()


def notice_line(code, why, pin):
    if code == "no-access":
        # the real cause, from what git said: a hint for its kind; an unrecognised one is shown as git worded it
        from core import gitaccess, i18n
        said = " ".join(str(why or "").split())[:200]
        cause = gitaccess.hint(why) or (i18n.t("web.updates.private_cause_other", why=said) if said else "")
        return " ".join(x for x in (i18n.t("core.private.no_access", repo=pin["repo"]), cause,
                                    i18n.t("core.private.no_access_tail")) if x)
    if code == "no-ref":
        return _t("private.line_no_ref", repo=pin["repo"], ref=pin["ref"])
    if code == "tag-moved":
        return _t("private.line_tag_moved", why=why)
    if code == "read":
        return _t("private.line_read", why=why)
    return _t("private.line_other", why=why)


def _shown(url):
    from core import codestore
    return codestore.shown(url)


# --- readiness of the shared code folder and the gate (§4.4) ------------------------------------------------------------

def _layer(repo):
    """The app's code layer (core/appcode.py): no git clone — no hub shares it, and git would find a repository it sits in."""
    from core import appwatch
    return bool(appwatch.layer(repo))


def ready_dir(repo):
    gd = None if _layer(repo) else _git(repo, "rev-parse", "--absolute-git-dir")
    return os.path.join(gd or os.path.join(repo, ".git"), READY_DIR)


def hub_key(state_dir):
    return hashlib.sha256(os.path.normcase(os.path.realpath(state_dir)).encode()).hexdigest()[:16]


def readiness(state_dir, cfg, pin, repo=REPO, config_dir=None):
    """{name: {needed, provider, proved?}} of this hub: provider is who handles the function now (builtin|shadow|warm|module) or
    why there is no module (missing|declined|local_edits)."""
    from core import providers
    recs, need = providers.current(state_dir), needed(cfg, config_dir)
    have, no, st = sources(cfg, pin), declined(state_dir, cfg, pin), read_state(state_dir)
    out = {}
    for name, m in pin["modules"].items():
        rec = recs.get(m["cap"]) or {}
        if name in no:
            prov = "declined"
        elif name not in have:
            prov = "missing"
        elif not enabled(cfg, name):
            prov = "declined"   # installed but switched off in "Modules": the owner's word
        elif m["cap"] in (st.get("local_edits") or {}):
            prov = "local_edits"
        elif rec.get("module") == name and providers.active(m["cap"], cfg, recs) == name:
            prov = "module"
        elif rec.get("module") == name and rec.get("state") in ("shadow", "warm"):
            prov = rec["state"]
        else:
            prov = "builtin"
        out[name] = {"needed": bool(need.get(name)), "provider": prov,
                     # the voice module's first voice message that reached the owner since it took over (core/providers.said): a
                     # sign of life where nothing else proves it, because voicing happens only when the owner gets a reply
                     **({"proved": True} if prov == "module" and rec.get("proved") else {})}
    return out


def write_ready(repo, state_dir, cfg, now=None):
    """Its own readiness record in hub-ready/, shared by the code folder (a second hub on this folder reads it before merging).
    No pin: nothing. Written when it changed, and once an hour (the write time)."""
    from core import catalog
    pin = load_pin(repo)
    if not pin or _layer(repo):   # a record in a layer is an edit of it (app.code_modified at every start)
        return None
    now = now if now is not None else time.time()
    rec = {"state": os.path.realpath(state_dir), "host": socket.gethostname()[:80],
           "modules": readiness(state_dir, cfg, pin, repo)}
    folder = ready_dir(repo)
    path = os.path.join(folder, hub_key(state_dir) + ".json")
    old = _read_json(path) or {}
    if isinstance(old, dict) and {k: old.get(k) for k in rec} == rec and now - (old.get("ts") or 0) < EVERY:
        return rec
    os.makedirs(folder, exist_ok=True)
    catalog.write_private(path, {**rec, "ts": round(now)})
    return rec


def others(repo, state_dir):
    """{key: record} of other hubs' readiness on this code folder (a corrupt one is {"bad": True})."""
    folder, mine, out = ready_dir(repo), hub_key(state_dir), {}
    for n in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if not n.endswith(".json") or n[:-5] == mine:
            continue
        got = _read_json(os.path.join(folder, n))
        out[n[:-5]] = got if isinstance(got, dict) and isinstance(got.get("modules"), dict) else {"bad": True}
    return out


def forget(repo, key):
    """`hub update forget-follower <key>`: the record of a hub removed for good no longer holds a release."""
    if not re.fullmatch(r"[0-9a-f]{16}", key or ""):
        raise ValueError(_t("private.forget_bad_key"))   # bin/hub prints it to the owner
    path = os.path.join(ready_dir(repo), key + ".json")
    if not os.path.exists(path):
        raise ValueError(_t("private.forget_missing", id=key))
    os.unlink(path)


def gate(repo, state_dir, cfg, target_text, config_dir=None):
    """Why not to merge the update target (needs-module): a list of lines, empty means it can be. A target with private.json `required`:
    on this hub and on every other hub of this code folder each needed module from required is with the module (provider module).
    An old or corrupt record of another hub holds just like a not-ready one (§4.4)."""
    target = parse(target_text)
    if not target or not target["required"]:
        return []
    out = []
    mine = readiness(state_dir, cfg, target, repo, config_dir)
    for name in target["required"]:
        r = mine.get(name) or {}
        if r.get("needed") and r.get("provider") != "module":
            out.append(f"{name}: {provider_text(r.get('provider'))}")
    for key, rec in others(repo, state_dir).items():
        who = _t("private.who_hub", state=rec.get("state") or "?", id=key) if not rec.get("bad") \
            else _t("private.who_record", id=key)
        if rec.get("bad"):
            out.append(_t("private.unreadable", who=who))
            continue
        for name in target["required"]:
            r = (rec.get("modules") or {}).get(name)
            if r is None:
                out.append(_t("private.no_data", who=who, name=name))
            elif r.get("needed") and r.get("provider") != "module":
                out.append(f"{who}: {name} — {provider_text(r.get('provider'))}")
    return out


# provider -> the catalog key of its text (translated where it is shown: a constant would be read in the language of the import)
PROVIDER_TEXT = {"builtin": "private.provider_builtin", "shadow": "private.provider_shadow", "warm": "private.provider_warm",
                 "missing": "private.provider_missing", "declined": "private.provider_declined",
                 "local_edits": "private.provider_local_edits"}


def provider_text(provider):
    """Who handles the function, in this process's language; an unknown provider is shown as it is."""
    return _t(PROVIDER_TEXT[provider]) if provider in PROVIDER_TEXT else provider


# --- waiting for startup (§4.5) ------------------------------------------------------------------------------------------

def waiting(state_dir, cfg, ready=None, status=None, repo=REPO, now=None, config_dir=None):
    """Why a session start should wait for the login module (a line) or None: do not wait. Only without a built-in login in the tree:
    the pool is needed, the module is not removed, and it is being installed (the ensure step is running) or installed and coming up (not ready, not stopped).
    ready(name) / status(name) are the module's readiness and state (modrun.Supervisor)."""
    if builtin_present("profile", repo):
        return None
    pin = load_pin(repo)
    if not pin:
        return None
    name = next((n for n, m in pin["modules"].items() if m["cap"] == "profile"), None)
    if not name or not needed(cfg, config_dir).get(name) or name in declined(state_dir, cfg, pin):
        return None
    now = now if now is not None else time.time()
    placed = name in sources(cfg, pin) and enabled(cfg, name)
    if placed and ready and ready(name):
        return None   # ready: the step may install other modules or repeat without access: this does not hinder starts
    st = read_state(state_dir)
    if st.get("state") == "installing" and now - (st.get("ts") or 0) < INSTALLING_TTL:
        return _t("private.installing", name=name)
    if placed:
        if status and status(name) in ("suspended", "error", "blocked"):
            return None
        return _t("private.coming_up", name=name)
    return None


def unavailable_line(state_dir, cfg, repo=REPO):
    """A line to the owner when a start went to the machine's login without the login module (§4.5)."""
    why = read_state(state_dir).get("why") or _t("private.login_not_ready")
    return _t("private.unavailable", why=why)


def lines(state_dir, repo=REPO):
    """The `hub modules` line: private modules are installed, being installed, or why not."""
    pin = load_pin(repo)
    st = read_state(state_dir)
    if not pin or not st.get("state"):   # the step has not run yet (only a refusal from hub module remove)
        return []
    by, mods = {}, st.get("modules") or {}
    for n, m in mods.items():
        by.setdefault(_t("private.unread_label") if m.get("code") == "read" and m.get("state") == "installed" else m.get("state"),
                      []).append(n)
    text = {"done": _t("private.state_done"), "pending": _t("private.state_pending"),
            "installing": _t("private.state_installing"), "failed": _t("private.state_failed")}
    if st.get("state") == "pending" and "installed" not in by:
        text["pending"] = _t("private.state_unread")
    out = _t("private.lines_head", ref=pin["ref"], url=st.get("url") or pin["repo"],
             state=text.get(st.get("state"), st.get("state")))
    out += "".join(f"; {k}: {', '.join(sorted(v))}" for k, v in sorted(by.items()) if k)
    if st.get("why"):
        out += f" — {st['why']}"
    return [out]


# --- stats report (§6.2) ---------------------------------------------------------------------------------------------------

def _code_of(rec):
    """{"code": ...} if the module has a reason code from the closed set (the file is not trusted: nothing else gets to the report)."""
    code = rec.get("code") if isinstance(rec, dict) else None
    return {"code": code} if code in CODES else {}


def _cause_of(data):
    """The cause code of a return to the built-in (module.provider): the recorded one; an event of a core before the field —
    by the hold it carries (a failure in service / a failed shadow); otherwise "unknown" (not counted as a failure)."""
    from core import providers
    c = data.get("cause")
    if c in providers.CAUSES:
        return c
    hold = data.get("hold")
    kind = hold.get("kind") if isinstance(hold, dict) else None
    return {"fail": "fail", "shadow": "shadow"}.get(kind, "unknown")


# failure kinds of a module call (core/modrun.post, Modules.fail, fold; hub-up: exit) — words of the core, not texts
ERROR_KINDS = ("connect", "closed", "timeout", "slow", "status", "size", "schema", "exit", "hello", "migrate",
               "rejected", "check")   # the last two — core.write (core/corewrite.py: a fold with edits refused or broken)
ERROR_HOOKS = ("process", "http")   # besides the points (modrun.POINTS): hub-up's exit, the page's proxy (modproxy)


def _error_key(data):
    """"<point>:<kind>" of a module.error for the report's errors count: which call failed and how, from the core's own
    vocabulary only (anything else is "other") — the reason text never goes."""
    from core import modrun
    hook, kind = data.get("hook"), data.get("kind")
    hook = hook if isinstance(hook, str) and (hook in modrun.POINTS or hook in ERROR_HOOKS) else "other"
    return f"{hook}:{kind if kind in ERROR_KINDS else 'other'}"


def report(state_dir, cfg, events=None, repo=REPO):
    """The private field of the stats report: the pin, access (the last ls-remote; when it failed, a code of why: git | auth | network | no-access), per module whether the function is needed,
    who handles it, the commit, why it is not ready (a code from CODES); period counters from journal events (module.error,
    suspensions, returns to the built-in one: all of them (fallback), by cause code (causes) and those caused by a failure
    (fault); the failures by call and kind (errors: {"profile.list:timeout": 2}) — which call fails tells a slow machine
    from a hub restart and a crash). Without reason texts. No pin: None."""
    pin = load_pin(repo)
    if not pin:
        return None
    st = read_state(state_dir)
    acc = st.get("access") or {}
    mods = readiness(state_dir, cfg, pin, repo)
    from core import gitaccess, modrun, providers
    counts = {n: {"failed": 0, "paused": 0, "fallback": 0, "fault": 0} for n in pin["modules"]}
    causes, errors = {n: {} for n in pin["modules"]}, {n: {} for n in pin["modules"]}
    caps = {m["cap"]: n for n, m in pin["modules"].items()}
    folded = {}   # a pause is not an event: the module's failures folded as the core does (modrun.fold), over the period
    for e in events or []:
        t, d = e.get("type"), e.get("data") or {}
        mod = d.get("module") if isinstance(d.get("module"), str) else None
        if t == "module.error" and mod in counts:
            if d.get("kind") not in ("shadow", "late"):
                counts[mod]["failed"] += 1
                key = _error_key(d)
                errors[mod][key] = errors[mod].get(key, 0) + 1
            was = (folded.get(mod) or {}).get("suspensions", 0)
            folded[mod] = modrun.fold(folded.get(mod), d, e.get("ts") or 0)
            counts[mod]["paused"] += folded[mod]["suspensions"] > was
        elif t == "module.reset" and mod in counts:
            folded.pop(mod, None)
        elif t == "module.provider" and d.get("state") == "builtin" and caps.get(d.get("cap")) == mod and mod:
            counts[mod]["fallback"] += 1
            c = _cause_of(d)
            causes[mod][c] = causes[mod].get(c, 0) + 1
            if providers.CAUSES.get(c) == "fault":
                counts[mod]["fault"] += 1
    return {"ref": pin["ref"], "commit": pin["commit"][:12], "state": st.get("state"), **_code_of(st),
            "access": {"ok": bool(acc.get("ok")), "ts": acc.get("ts"),
                       **({} if acc.get("ok") else {"code": acc.get("code") or gitaccess.code(acc.get("why"))})} if acc else None,
            "modules": {n: {**mods[n], "state": ((st.get("modules") or {}).get(n) or {}).get("state"),
                            **_code_of((st.get("modules") or {}).get(n)), **counts[n],
                            **({"causes": causes[n]} if causes[n] else {}),
                            **({"errors": errors[n]} if errors[n] else {})} for n in pin["modules"]},
            "local_edits": sorted(st.get("local_edits") or {})}


if __name__ == "__main__":
    if sys.argv[1:2] == ["states"] and len(sys.argv) >= 3:
        _states_main(sys.argv[2], sys.argv[3:])
    elif sys.argv[1:2] == ["boot"]:
        # hub-up before hubd: the step, only if there is something to install and the failure is not fresh
        from core import catalog
        from core.daemon import default_state_dir
        sd = default_state_dir()
        try:
            conf = catalog.load_config(sd)
        except ValueError:
            sys.exit(0)
        try:   # before the core: the functions start on the built-in code at once; a failure does not stop the step below
            got = Retire(REPO, sd, conf).run(boot=True)
        except Exception as e:  # noqa: BLE001 — the update loop repeats it
            got = None
            print(f"private modules: retiring failed — {e!r}", flush=True)
        if got and got["changed"] and (got["removed"] or got["why"]):
            print(f"private modules: retired — removed {', '.join(got['removed']) or '-'}"
                  + (f"; {got['why']}" if got["why"] else ""), flush=True)
        e = Ensure(REPO, sd, conf)
        if e.due(boot=True):
            got = e.run(boot=True)
            print(f"private modules: {(got or {}).get('state', 'the step is already running')}", flush=True)
    else:
        sys.exit("private.py states <state dir> <module…> | boot")
