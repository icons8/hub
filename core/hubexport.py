"""`hub export` / `hub import` — a hub into one archive and back, on this or another machine (spec d2789272).

`hub export [<file>] [--secrets] [--full]` — the live hub is not stopped. The archive (`.tar.gz`): the journal (the agents,
their profiles' folders, the schedule, deputies, tasks), the agents' profiles, directions with their boards, tasks,
specs, module and plugin data, contacts, hub.json without secrets, the Claude memory of the current sessions; last —
manifest.json (format, release, the old machine's folders, the repos, what was left out, sha256 of every member).
- A white list of folders (TAKEN): a folder the core does not know may hold a module's sign-ins — it is only listed,
  `--full` takes it. In the agents' folders a working file over CAP is left out (their profiles never); `--full` takes
  it, the attachments and the agents' conversations.
- Files first, the journal after, then what the journal knows and the files lacked (an agent born meanwhile): no agent
  without its profile. A file that keeps changing while read is left out and named (a torn file is never archived).
- `--secrets` adds secrets.bin: hub.json whole, the Claude account pool, the bot, the bus keys, the vault and its key —
  sealed with a password (scrypt -> AES-256-GCM of `cryptography`, core/cryptolib.py; no cryptographic code of our own).

`hub import <file> [--replace] [--no-secrets] [--no-clone]` — on a hub right after its install. Everything is checked
before anything changes (sha256, the code folds the archive's journal, the hub is fresh, the password); then the hub is
stopped, what it had goes aside (`<state>.before-import-<stamp>`, every step in its steps.json), the archive goes in,
hub.json and the directions' folders get this machine's paths, the repos are cloned, the mark import.json is left for
hubd (core.imported: the state's paths, the executors without their copy) and the hub is started. A failure undoes the
steps; `hub import --undo` undoes the last import. The journal is never rewritten (core/pathmap.py)."""
import datetime
import hashlib
import io
import json
import os
import re
import secrets as _random
import shutil
import subprocess
import sys
import tarfile
import time

import platforms
from core import pathmap

FORMAT = 1
CAP = 1 << 20              # an agent's working file over this stays out without --full (its profile never)
STREAM = 64 << 20          # a file over this is copied into the archive as a stream, not read whole
MANIFEST = "manifest.json"
SECRETS = "secrets.bin"
MARK = "import.json"       # the import's facts for hubd: core.imported (core/daemon.Hub.take_import)
STEPS = "steps.json"
LOCK = "export.lock"
CUT = {"hub-export": "cut"}              # a value of hub.json left in secrets.bin
CUT_ENTRY = {"hub-export": "cut-entry"}  # a whole entry (an MCP server…) with a secret inside a string
PROFILE = re.compile(r"^CLAUDE(\.[A-Za-z0-9_-]+)?\.(md|json)$")
# the state folder: what goes into the archive whole
TAKEN = ("agents", "directions", "tasks", "specs", "comments", "moddata", "ext", "plugins", "my-skills", "marketplaces",
         "stops-audit", "agent-dev")
TOP_FILES = ("journal.jsonl", "migrated")
LOGINS = ("moddata/accounts",)   # a module's sign-ins: never in the open
HOME = "hub-home"                # the hub session's folder: its files, not the rules links the install makes
RULES = ("CLAUDE.md", "ONBOARDING.md")
# this machine's: never archived, never put aside by an import
MACHINE = ("code", "layers", "bin", "pylib", "claude-code", "browsers", "logs", "update", "npx-empty", "modules",
           "instance.json", HOME)
# the core's own folders that are not needed elsewhere (caches, queues, runs, the archive of the journal…)
NOT_NEEDED = ("cache", "sessions", "spool", "backup", "journal-archive", "sandbox", "market", "workspaces", "secrets",
              "bus", "telegram", "net", "net-inbox", "modrun", "jobs", "locks", "gateway", "mcp-login", "page-local",
              "setup", "proof", "voice-failed", "stats", "attachments")
CONFIG_FILES = ("contacts.json", "stats-id")
CONFIG_DIRS = ("themes",)
SECRET_CONFIG = ("accounts.json", "telegram.json")
SECRET_CONFIG_DIRS = ("bus", "bus-relay")
# hub.json keys of this machine: an import keeps the new machine's values
MACHINE_KEYS = (("claude_config_dir",), ("browser", "chrome"), ("browser", "mcp"), ("voice", "ffmpeg"), ("tailnet",))
SCRYPT = {"n": 1 << 17, "r": 8, "p": 1}
MAGIC = b"HUBSEC1\0"
PASSWORD_MIN = 12
UNKNOWN = "unknown event type"   # core/reducer: the fold's refusal of a type it does not know


class Refused(Exception):
    """Nothing was changed: the reason for a person (an i18n key and its parameters)."""

    def __init__(self, key, **params):
        super().__init__(key)
        self.key, self.params = key, params


def _t(key, **kw):
    from core import i18n
    return i18n.t(key, **kw)


def _stamp(now):
    return datetime.datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")


def _windows(osname=None):
    """Whether the machine of that OS name (platforms APP_OS: mac, linux, windows) writes Windows paths."""
    return str(osname or platforms.current.APP_OS).startswith("win")


def claude_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def _inside(child, parent):
    child, parent = os.path.realpath(child), os.path.realpath(parent)
    return os.path.normcase(child) == os.path.normcase(parent) or \
        os.path.normcase(child).startswith(os.path.normcase(parent.rstrip(os.sep) + os.sep))


def _size(path):
    total = 0
    for root, dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _read_stable(path, tries=4):
    """The file's bytes when its size and mtime did not change while it was read; None — it kept changing (or is gone)."""
    for _ in range(tries):
        try:
            before = os.stat(path)
            with open(path, "rb") as f:
                data = f.read()
            after = os.stat(path)
        except OSError:
            return None
        if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns) == (len(data), after.st_mtime_ns):
            return data
        time.sleep(0.05)
    return None


# --- hub.json without secrets (spec §5.2) -----------------------------------------------------------------------------

def cut_settings(cfg):
    """hub.json for the open part -> (settings, [path of a cut]): a value under a key-like name, env or headers becomes
    CUT; a secret inside a string or a list (`--token=…`, a URL with a key) takes its whole entry (the second level:
    mcp.<name>, the top level otherwise) as CUT_ENTRY — a half command is never left."""
    from core import redact, supportbundle
    cuts = []

    def walk(v, path, under):
        """v with its secret values cut; _Entry — a secret inside a string or a command line. under: True — every value
        is a secret (headers, a key-like name); "env" — a variable is one by its name or its value (PATH, DISPLAY, a
        URL stay: without them the server would not start on the new machine)."""
        if isinstance(v, dict):
            out = {}
            for k, x in v.items():
                if under == "env":
                    secret = redact.secret_name(k) or not isinstance(x, str) or redact.mask(x) != x
                else:
                    secret = under or supportbundle._secret(k, x)
                if secret and not isinstance(x, dict):
                    if x is None or x == "" or isinstance(x, bool):
                        out[k] = x
                    else:
                        out[k] = dict(CUT)
                        cuts.append({"path": path + [k], "kind": "value"})
                else:
                    out[k] = walk(x, path + [k], True if secret else "env" if k == "env" and isinstance(x, dict)
                                  else k in supportbundle.SECRET_PARTS)
            return out
        if isinstance(v, list):
            hide = False
            for x in v:   # `--token x`, `--token=x`: the rules of hub support-bundle (core/supportbundle.masked_settings)
                if hide and isinstance(x, str) or isinstance(x, str) and x.startswith("-") and "=" in x \
                        and supportbundle._flag_secret(x.split("=", 1)[0]):
                    raise _Entry()
                hide = supportbundle._flag_secret(x) and "=" not in x
            return [walk(x, path + [i], under) for i, x in enumerate(v)]
        if isinstance(v, str) and redact.mask(v) != v:
            raise _Entry()
        return v

    out = {}
    for top, value in (cfg or {}).items():
        if isinstance(value, dict) and not supportbundle._secret(top, value) and top not in supportbundle.SECRET_PARTS:
            inner = {}
            for k, x in value.items():   # an entry: an MCP server, a project, a module
                try:
                    inner[k] = walk({k: x}, [top], False)[k]
                except _Entry:
                    inner[k] = dict(CUT_ENTRY)
                    cuts.append({"path": [top, k], "kind": "entry"})
            out[top] = inner
            continue
        try:
            out[top] = walk({top: value}, [], False)[top]
        except _Entry:
            out[top] = dict(CUT_ENTRY)
            cuts.append({"path": [top], "kind": "entry"})
    return out, cuts


class _Entry(Exception):
    pass


def uncut_settings(cfg):
    """The open part's hub.json without what was cut -> (settings, [path]): a cut value is removed with its key, a cut
    entry with its name."""
    gone = []

    def walk(v, path):
        if isinstance(v, dict):
            out = {}
            for k, x in v.items():
                if x == CUT or x == CUT_ENTRY:
                    gone.append({"path": path + [k], "kind": "entry" if x == CUT_ENTRY else "value"})
                    continue
                out[k] = walk(x, path + [k])
            return out
        if isinstance(v, list):
            return [walk(x, path + [i]) for i, x in enumerate(v) if x != CUT and x != CUT_ENTRY]
        return v
    return walk(cfg or {}, []), gone


def _dig(cfg, keys):
    for k in keys:
        if not isinstance(cfg, dict) or k not in cfg:
            return None, False
        cfg = cfg[k]
    return cfg, True


def _drop(cfg, keys):
    holder, there = _dig(cfg, keys[:-1])
    if there and isinstance(holder, dict):
        holder.pop(keys[-1], None)


def _put(cfg, keys, value):
    for k in keys[:-1]:
        cfg = cfg.setdefault(k, {}) if isinstance(cfg.get(k), dict) or k not in cfg else cfg[k]
        if not isinstance(cfg, dict):
            return
    cfg[keys[-1]] = value


# --- what goes into the archive --------------------------------------------------------------------------------------

class Plan:
    def __init__(self):
        self.files = []      # (arcname, path)
        self.skipped = []    # {"path", "bytes", "why"}
        self.unknown = []    # {"name", "bytes"}
        self.changing = []   # paths that kept changing while read

    def walk(self, folder, arc, cap=None, keep=lambda rel: False, skip=lambda rel: False):
        for root, dirs, files in os.walk(folder):
            dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(root, d)))
            for name in sorted(files):
                path = os.path.join(root, name)
                rel = os.path.relpath(path, folder).replace(os.sep, "/")
                if os.path.islink(path) or skip(rel):
                    continue
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                if cap is not None and size > cap and not keep(rel):
                    self.skipped.append({"path": f"{arc}/{rel}", "bytes": size, "why": "big"})
                    continue
                self.files.append((f"{arc}/{rel}", path))


def plan_state(state, full=False):
    """The state folder's part of the archive (without the journal)."""
    p = Plan()
    for name in TAKEN:
        folder = os.path.join(state, name)
        if not os.path.isdir(folder):
            continue
        if name == "agents":
            p.walk(folder, "state/agents", cap=None if full else CAP,
                   keep=lambda rel: rel.count("/") == 1 and bool(PROFILE.match(rel.split("/")[1])))
        elif name == "moddata":
            p.walk(folder, "state/moddata", skip=lambda rel: any(f"moddata/{rel}".startswith(x + "/") for x in LOGINS))
        else:
            p.walk(folder, f"state/{name}")
    home = os.path.join(state, HOME)
    if os.path.isdir(home):
        p.walk(home, f"state/{HOME}", skip=lambda rel: rel in RULES)
    for name in TOP_FILES[1:]:
        if os.path.isfile(os.path.join(state, name)):
            p.files.append((f"state/{name}", os.path.join(state, name)))
    attach = os.path.join(state, "attachments")
    if full and os.path.isdir(attach):
        p.walk(attach, "state/attachments")
    elif os.path.isdir(attach):
        p.skipped.append({"path": "state/attachments", "bytes": _size(attach), "why": "full"})
    known = set(TAKEN) | set(MACHINE) | set(NOT_NEEDED)
    for name in sorted(os.listdir(state)) if os.path.isdir(state) else []:
        path = os.path.join(state, name)
        if name in known or not os.path.isdir(path) or os.path.islink(path):
            continue
        if full:
            p.walk(path, f"state/{name}")
            p.unknown.append({"name": name, "bytes": _size(path), "taken": True})
        else:
            p.unknown.append({"name": name, "bytes": _size(path), "taken": False})
    return p


def plan_config(conf, p):
    for name in CONFIG_FILES:
        if os.path.isfile(os.path.join(conf, name)):
            p.files.append((f"config/{name}", os.path.join(conf, name)))
    for name in CONFIG_DIRS:
        if os.path.isdir(os.path.join(conf, name)):
            p.walk(os.path.join(conf, name), f"config/{name}")


def read_journal(state):
    """The journal's bytes up to its last whole line: an open file read to its end (a rotation meanwhile — the old file
    is read whole by the same descriptor)."""
    path = os.path.join(state, "journal.jsonl")
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return b""
    end = data.rfind(b"\n")
    return data[:end + 1] if end >= 0 else b""


def journal_state(data):
    """The core's state at the journal's cut (reducer.replay of its lines) and (first seq, last seq)."""
    from core import reducer
    events = [json.loads(x) for x in data.split(b"\n") if x.strip()]
    if not events:
        return reducer.empty(), (None, None)
    return reducer.replay(events), (events[0].get("seq"), events[-1].get("seq"))


def live_sessions(st):
    return {n: s for n, s in (st.get("sessions") or {}).items()
            if isinstance(s, dict) and s.get("state") not in ("removed", "closed", "failed")}


def profile_of(sess):
    return sess.get("profile") or (sess.get("session_id") if sess.get("role") == "agent" else None)


def _git(path, *args, timeout=30):
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"}
    try:
        r = platforms.current.run(["git", "-C", path, *args], timeout, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", env=env)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def repo_info(path):
    """{path, remotes, branch, commit, dirty, unpushed} of the repository the folder is in, or None (not a repository)."""
    top = _git(path, "rev-parse", "--show-toplevel")
    if not top:
        return None
    top = os.path.normpath(top)
    remotes = {}
    for line in (_git(top, "remote", "-v") or "").splitlines():
        bits = line.split()
        if len(bits) >= 2 and (len(bits) < 3 or bits[2] == "(fetch)"):
            remotes.setdefault(bits[0], bits[1])
    commit = _git(top, "rev-parse", "HEAD")
    unpushed = _git(top, "rev-list", "--count", "HEAD", "--not", "--remotes") if commit else None
    return {"path": top, "remotes": remotes, "branch": _git(top, "symbolic-ref", "--short", "-q", "HEAD") or None,
            "commit": commit, "dirty": bool(_git(top, "status", "--porcelain")),
            "unpushed": int(unpushed) if unpushed and unpushed.isdigit() else 0}


def repos(state, cfg, st):
    """The repositories the hub works in: hub.json projects, plugins and sets outside the hub, the folders of live
    sessions, the directions' repos -> ([repo info with used_by], [executor copies with work only there])."""
    seen, out, copies = {}, [], []

    def add(path, who):
        if not isinstance(path, str) or not os.path.isabs(path) or not os.path.isdir(path) or _inside(path, state):
            return
        for info in out:
            if _inside(path, info["path"]):
                info["used_by"] = sorted(set(info["used_by"]) | {who})
                return
        info = repo_info(path)
        if info is None or info["path"] in seen:
            if info:
                seen[info["path"]]["used_by"] = sorted(set(seen[info["path"]]["used_by"]) | {who})
            return
        info["used_by"] = [who]
        seen[info["path"]] = info
        out.append(info)
    for name, proj in sorted(((cfg or {}).get("projects") or {}).items()):
        add((proj or {}).get("path") if isinstance(proj, dict) else proj, f"project:{name}")
    for name, ext in sorted(((cfg or {}).get("extensions") or {}).items()):
        add((ext or {}).get("path") if isinstance(ext, dict) else None, f"plugin:{name}")
    for s in (cfg or {}).get("sets") or []:
        add(s.get("path") if isinstance(s, dict) else None, "set")
    for name, sess in sorted(live_sessions(st).items()):
        wt = sess.get("worktree") if isinstance(sess.get("worktree"), dict) else None
        if wt and wt.get("path"):
            info = repo_info(wt["path"]) if os.path.isdir(wt["path"]) else None
            if info and (info["dirty"] or info["unpushed"]):
                copies.append({"session": name, "path": info["path"], "dirty": info["dirty"],
                               "unpushed": info["unpushed"], "branch": info["branch"]})
            add(wt.get("repo"), name)
            continue
        add(sess.get("cwd"), name)
    root = os.path.join(state, "directions")
    for slug in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        try:
            with open(os.path.join(root, slug, "direction.json"), encoding="utf-8") as f:
                add(json.load(f).get("repo"), f"direction:{slug}")
        except (OSError, ValueError, AttributeError):
            pass
    return out, copies


def claude_files(st, full=False, root=None):
    """The Claude Code files of the live agents and the hub: their memory; with full — their conversations."""
    from hosts import worktree
    root = root or claude_dir()
    projects = os.path.join(root, "projects")
    p, conv, found_by_id = Plan(), {}, {}
    if full and os.path.isdir(projects):   # one pass over the project folders, not one per agent
        for d in os.listdir(projects):
            folder = os.path.join(projects, d)
            for f in os.listdir(folder) if os.path.isdir(folder) else []:
                if f.endswith(".jsonl"):
                    found_by_id.setdefault(f[:-len(".jsonl")], []).append(os.path.join(folder, f))
    for name, sess in sorted(live_sessions(st).items()):
        if sess.get("role") != "agent" and sess.get("kind") != "hub":
            continue
        cwd = sess.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            continue
        slug = worktree.slug(cwd)
        memory = os.path.join(projects, slug, "memory")
        if os.path.isdir(memory):
            p.walk(memory, f"claude/{slug}/memory")
        sid = sess.get("session_id")
        if not full or not sid:
            continue
        found = found_by_id.get(sid) or []
        if not found:
            continue
        best = max(found, key=os.path.getmtime)
        p.files.append((f"claude/{slug}/{sid}.jsonl", best))
        sub = best[:-len(".jsonl")]
        if os.path.isdir(sub):
            p.walk(sub, f"claude/{slug}/{sid}")
        conv[name] = True
    return p, conv


def release_of(state):
    """(release, commit) of the code this hub runs."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rel = commit = None
    try:
        from core import appwatch
        man = appwatch.manifest(repo) if appwatch.layer(repo) else None
        if man:
            rel, commit = man.get("version"), man.get("commit")
    except Exception:  # noqa: BLE001 — a release that cannot be told is no reason to refuse an export
        pass
    if not rel:
        try:
            from core import version
            rel = version.release(repo, state)
        except Exception:  # noqa: BLE001
            rel = None
    if not rel:
        try:
            from core import packaged
            rel = (packaged.info() or {}).get("version")
        except Exception:  # noqa: BLE001
            rel = None
    if not commit:
        commit = _git(repo, "rev-parse", "HEAD")
    return rel, commit


# --- secrets.bin ----------------------------------------------------------------------------------------------------

def _crypto(state):
    from core import cryptolib
    try:
        lib = cryptolib.aesgcm(state)
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except (cryptolib.Unavailable, ImportError):
        raise Refused("cli.export.no_lib", command=" ".join(cryptolib.install_command(state))) from None
    return lib, Scrypt


def seal(state, files, password, archive_id):
    """{name: bytes} -> secrets.bin: MAGIC, the scrypt parameters and salt, the nonce, AES-256-GCM over a tar of the
    files; the header and the archive's id are its associated data (a secrets.bin moved into another archive does not
    open)."""
    lib, Scrypt = _crypto(state)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o600, int(time.time())
            tar.addfile(info, io.BytesIO(data))
    salt, nonce = os.urandom(16), os.urandom(12)
    params = json.dumps({**SCRYPT, "salt": salt.hex()}, sort_keys=True).encode()
    head = MAGIC + len(params).to_bytes(2, "big") + params
    key = Scrypt(salt=salt, length=32, **SCRYPT).derive(password.encode("utf-8"))
    sealed = lib.AESGCM(key).encrypt(nonce, buf.getvalue(), head + archive_id.encode())
    return head + nonce + sealed


def unseal(state, blob, password, archive_id):
    """secrets.bin -> {name: bytes}; Refused on a wrong password or a blob of another archive."""
    lib, Scrypt = _crypto(state)
    if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + 2:
        raise Refused("cli.import.secrets_broken")
    n = int.from_bytes(blob[len(MAGIC):len(MAGIC) + 2], "big")
    head = blob[:len(MAGIC) + 2 + n]
    try:
        params = json.loads(head[len(MAGIC) + 2:])
        salt = bytes.fromhex(params["salt"])
        kdf = Scrypt(salt=salt, length=32, n=int(params["n"]), r=int(params["r"]), p=int(params["p"]))
    except (ValueError, KeyError, TypeError):
        raise Refused("cli.import.secrets_broken") from None
    nonce, sealed = blob[len(head):len(head) + 12], blob[len(head) + 12:]
    try:
        plain = lib.AESGCM(kdf.derive(password.encode("utf-8"))).decrypt(nonce, sealed, head + archive_id.encode())
    except lib.InvalidTag:
        raise Refused("cli.import.password_wrong") from None
    out = {}
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r") as tar:
        for m in tar.getmembers():
            if m.isfile():
                out[m.name] = tar.extractfile(m).read()
    return out


def secret_files(state, conf, cfg_raw):
    """What --secrets carries: {name in secrets.bin: bytes} and the counts for the report."""
    from modules.secrets import vault
    files, counts = {}, {}
    if cfg_raw is not None:
        files["config/hub.json"] = cfg_raw
    for name in SECRET_CONFIG:
        data = _read_stable(os.path.join(conf, name)) if os.path.isfile(os.path.join(conf, name)) else None
        if data is not None:
            files[f"config/{name}"] = data
    for name in SECRET_CONFIG_DIRS:
        folder = os.path.join(conf, name)
        for root, dirs, names in os.walk(folder):
            for n in names:
                path = os.path.join(root, n)
                if os.path.islink(path):
                    continue
                data = _read_stable(path)
                if data is not None:
                    files[f"config/{name}/" + os.path.relpath(path, folder).replace(os.sep, "/")] = data
    vpath = vault.path(state)
    if os.path.isfile(vpath):
        data = _read_stable(vpath)
        if data is not None:
            files["state/secrets/vault.json"] = data
            try:
                counts["vault"] = len(json.loads(data).get("items") or {})
            except ValueError:
                counts["vault"] = 0
        try:
            key = vault.key(state)
        except vault.Error:
            key = None
        if key:
            files["vault.key"] = key.hex().encode()
    try:
        counts["accounts"] = len(json.loads(files.get("config/accounts.json", b"{}")).get("accounts") or [])
    except (ValueError, AttributeError):
        counts["accounts"] = 0
    counts["telegram"] = "config/telegram.json" in files
    counts["bus"] = any(n.startswith("config/bus/") for n in files)
    return files, counts


# --- export ---------------------------------------------------------------------------------------------------------

def default_path(state, now):
    from core import instance
    name = instance.name(state) or "main"
    base = os.path.join(os.path.expanduser("~"), f"hub-{name}-{datetime.date.fromtimestamp(now).isoformat()}")
    path, n = base + ".tar.gz", 1
    while os.path.exists(path):
        n += 1
        path = f"{base}-{n}.tar.gz"
    return path


def is_archive(path):
    """The file is a hub archive: an explicit path may be replaced only then. Its first member tells (an export starts
    with the journal): the whole archive is not read for it — a nightly run over last week's file would read it all."""
    try:
        with tarfile.open(path, "r:gz") as tar:
            first = tar.next()
    except (OSError, tarfile.TarError, EOFError, zlib_error()):
        return False
    return bool(first) and first.name == "state/journal.jsonl"


def export(state, conf, out=None, full=False, password=None, now=time.time, home=None):
    """The archive -> its summary (for the report). password: the --secrets password (None — no secrets)."""
    from core import catalog, lock
    now_ts = now()
    explicit = out is not None
    out = os.path.abspath(out) if out else default_path(state, now_ts)
    if _inside(out, state) or _inside(out, conf):
        raise Refused("cli.export.inside", path=out)
    if explicit and os.path.exists(out) and (os.path.isdir(out) or not is_archive(out)):
        raise Refused("cli.export.not_archive", path=out)
    held = lock.acquire(os.path.join(state, LOCK))
    if not held:
        raise Refused("cli.export.busy")
    try:
        return _export(state, conf, out, full, password, now_ts, home, catalog)
    finally:
        try:
            held.close()
        except (OSError, AttributeError):
            pass


def _export(state, conf, out, full, password, now_ts, home, catalog):
    cfg_path = catalog.config_path(state)
    cfg_raw = _read_stable(cfg_path) if os.path.isfile(cfg_path) else None
    if cfg_raw is None and os.path.isfile(cfg_path):   # kept changing: an archive without the settings is no backup
        raise Refused("cli.export.config_busy", path=cfg_path)
    try:
        cfg = json.loads(cfg_raw) if cfg_raw else {}
    except ValueError:
        raise Refused("cli.export.config_broken", path=cfg_path) from None
    p = plan_state(state, full)
    plan_config(conf, p)
    journal = read_journal(state)
    st, (first, last) = journal_state(journal)
    taken = {arc.split("/")[2] for arc, _ in p.files if arc.startswith("state/agents/") and arc.count("/") >= 3}
    for name, sess in live_sessions(st).items():   # born while the files were read: its profile now
        prof = profile_of(sess)
        folder = os.path.join(state, "agents", str(prof)) if prof else None
        if folder and os.path.isdir(folder) and str(prof) not in taken:
            p.walk(folder, f"state/agents/{prof}", cap=None if full else CAP,
                   keep=lambda rel: bool(PROFILE.match(rel)) and "/" not in rel)
    cp, conversations = claude_files(st, full)
    found, copies = repos(state, cfg, st)
    archive_id = _random.token_hex(16)
    tmp = f"{out}.{os.getpid()}.tmp"
    for stale in _stale_tmps(out):
        try:
            os.unlink(stale)
        except OSError:
            pass
    files, total = {}, 0
    changing = []
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz", compresslevel=6) as tar:
            def add_bytes(arc, data, mtime=None):
                nonlocal total
                info = tarfile.TarInfo(arc)
                info.size, info.mode, info.mtime = len(data), 0o600, int(mtime or now_ts)
                tar.addfile(info, io.BytesIO(data))
                files[arc] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                total += len(data)

            def add_file(arc, path):
                try:
                    size = os.path.getsize(path)
                    mtime = os.path.getmtime(path)
                except OSError:
                    return
                if size <= STREAM:
                    data = _read_stable(path)
                    if data is None:
                        changing.append(arc)
                        return
                    add_bytes(arc, data, mtime)
                    return
                _add_stream(tar, arc, path, size, mtime, files, changing)

            add_bytes("state/journal.jsonl", journal)
            masked, cuts = cut_settings(cfg)
            add_bytes("config/hub.json", json.dumps(masked, ensure_ascii=False, indent=1).encode())
            for arc, path in p.files + cp.files:
                if arc not in files:
                    add_file(arc, path)
            counts = {}
            if password is not None:
                sfiles, counts = secret_files(state, conf, cfg_raw)
                add_bytes(SECRETS, seal(state, sfiles, password, archive_id))
            rel, commit = release_of(state)
            from hosts import worktree
            from core import minihub
            sessions = {n: {"cwd": s.get("cwd"), "role": s.get("role"), "kind": s.get("kind"),
                            "runner": minihub.runner(s),   # an executor for a task: the core can stop it
                            "slug": worktree.slug(s["cwd"]) if isinstance(s.get("cwd"), str) and s["cwd"] else None,
                            "worktree": bool(isinstance(s.get("worktree"), dict) and s["worktree"].get("path")),
                            "profile": profile_of(s), "conversation": bool(conversations.get(n))}
                        for n, s in live_sessions(st).items()}
            manifest = {
                "format": FORMAT, "id": archive_id,
                "made": datetime.datetime.fromtimestamp(now_ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "hub": {"release": rel, "commit": commit},
                "from": {"os": platforms.current.APP_OS, "home": home or os.path.expanduser("~"), "state": os.path.abspath(state),
                         "config": os.path.abspath(conf), "claude": os.path.abspath(claude_dir())},
                "journal": {"first": first, "last": last, "bytes": len(journal)},
                "counts": _counts(st, state, conf),
                "files": files, "skipped": p.skipped + cp.skipped, "unknown": p.unknown, "changing": changing,
                "repos": found, "copies": copies, "sessions": sessions, "cuts": cuts,
                "had": {"telegram": os.path.isfile(os.path.join(conf, "telegram.json")),
                        "accounts": os.path.isfile(os.path.join(conf, "accounts.json")),
                        "bus": os.path.isdir(os.path.join(conf, "bus")),
                        "vault": os.path.isfile(os.path.join(state, "secrets", "vault.json"))},
                "commands": [{"id": jid, "command": j.get("command")} for jid, j in sorted((st.get("schedule")
                                                                                            or {}).items())
                             if isinstance(j, dict) and j.get("state") == "active" and j.get("command")],
                "secrets": password is not None, "secret_counts": counts, "full": bool(full),
            }
            info = tarfile.TarInfo(MANIFEST)
            data = json.dumps(manifest, ensure_ascii=False, indent=1).encode()
            info.size, info.mode, info.mtime = len(data), 0o600, int(now_ts)
            tar.addfile(info, io.BytesIO(data))
        with open(tmp, "rb+") as f:
            platforms.current.sync(f.fileno())
        os.replace(tmp, out)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return {"path": out, "bytes": os.path.getsize(out), "manifest": manifest}


def _stale_tmps(out):
    folder, base = os.path.dirname(out) or ".", os.path.basename(out)
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return [os.path.join(folder, n) for n in names if n.startswith(base + ".") and n.endswith(".tmp")
            and re.fullmatch(r"\d+", n[len(base) + 1:-4])]


def _add_stream(tar, arc, path, size, mtime, files, changing):
    """A big file (--full) copied as a stream of the size it had; it changed meanwhile — named in the report."""
    h = hashlib.sha256()

    class Hashing:
        def __init__(self, f):
            self.f, self.left = f, size

        def read(self, n=-1):
            n = self.left if n is None or n < 0 else min(n, self.left)
            data = self.f.read(n)
            if len(data) < n:
                data += b"\0" * (n - len(data))
            self.left -= len(data)
            h.update(data)
            return data
    info = tarfile.TarInfo(arc)
    info.size, info.mode, info.mtime = size, 0o600, int(mtime)
    with open(path, "rb") as f:
        tar.addfile(info, Hashing(f))
    files[arc] = {"size": size, "sha256": h.hexdigest()}
    try:
        if os.path.getsize(path) != size or os.path.getmtime(path) != mtime:
            changing.append(arc)
    except OSError:
        changing.append(arc)


def _counts(st, state, conf):
    agents = sum(1 for s in live_sessions(st).values() if s.get("role") == "agent")
    root = os.path.join(state, "directions")
    directions = sum(1 for d in (os.listdir(root) if os.path.isdir(root) else [])
                     if os.path.isfile(os.path.join(root, d, "direction.json")))
    jobs = sum(1 for j in (st.get("schedule") or {}).values() if isinstance(j, dict) and j.get("state") == "active")
    contacts = len(_contacts(conf))
    tasks = len(st.get("delegations") or {})
    return {"agents": agents, "directions": directions, "schedule": jobs, "contacts": contacts, "tasks": tasks}


def _contacts(conf):
    """The people of contacts.json (core/deputies — a list), [] when there is none or it is unreadable."""
    try:
        with open(os.path.join(conf, "contacts.json"), encoding="utf-8") as f:
            people = json.load(f)
    except (OSError, ValueError):
        return []
    return people if isinstance(people, list) else list((people or {}).get("contacts") or [])


def ask_password(twice=True):
    """The password from the terminal (asked twice for an export): never from a pipe — a password written into a
    script or a file next to the archive would seal nothing (spec d2789272 §5.3)."""
    if not sys.stdin.isatty():
        raise Refused("cli.export.password_terminal")
    import getpass
    first = getpass.getpass(_t("cli.export.password_ask"))
    again = getpass.getpass(_t("cli.export.password_again")) if twice else first
    if first != again:
        raise Refused("cli.export.password_differ")
    return first


# --- import ---------------------------------------------------------------------------------------------------------

def _member_ok(name):
    if not isinstance(name, str) or not name or name.startswith(("/", "\\")) or ":" in name.split("/")[0]:
        return False
    parts = name.replace("\\", "/").split("/")
    if ".." in parts or "" in parts[:-1]:
        return False
    if parts[0] == "state" and len(parts) > 1 and (parts[1] in MACHINE and parts[1] != HOME
                                                   or parts[1].endswith(".lock") or parts[1] == MARK):
        return False   # this machine's own entries (its code, its anchor) never come from an archive
    return name in (MANIFEST, SECRETS) or parts[0] in ("state", "config", "claude") and len(parts) > 1


def unpack(path, staging):
    """The archive into a fresh staging folder, every member checked (a name inside the staging, a plain file) and
    hashed -> (manifest, {name: sha256}). Refused: not an archive, a member outside, a member that is not a file."""
    hashes = {}
    try:
        tar = tarfile.open(path, "r:gz")
    except (OSError, tarfile.TarError, EOFError):
        raise Refused("cli.import.not_archive", path=path) from None
    with tar:
        try:
            for m in tar:
                if m.isdir():
                    continue
                if not m.isfile() or not _member_ok(m.name):
                    raise Refused("cli.import.bad_member", name=m.name)
                dest = os.path.join(staging, *m.name.split("/"))
                if not _inside(dest, staging):
                    raise Refused("cli.import.bad_member", name=m.name)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                h = hashlib.sha256()
                src = tar.extractfile(m)
                fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as f:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        h.update(chunk)
                        f.write(chunk)
                os.utime(dest, (m.mtime, m.mtime))
                hashes[m.name] = h.hexdigest()
        except (tarfile.TarError, EOFError, zlib_error()) as e:
            raise Refused("cli.import.damaged", name=type(e).__name__) from None
    try:
        with open(os.path.join(staging, MANIFEST), encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        raise Refused("cli.import.no_manifest", path=path) from None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("format"), int):
        raise Refused("cli.import.no_manifest", path=path)
    if manifest["format"] > FORMAT:
        raise Refused("cli.import.format_newer", format=manifest["format"], mine=FORMAT)
    listed = manifest.get("files") or {}
    for name, meta in listed.items():
        if hashes.get(name) != (meta or {}).get("sha256"):
            raise Refused("cli.import.damaged", name=name)
    extra = sorted(set(hashes) - set(listed) - {MANIFEST})
    if extra:
        raise Refused("cli.import.damaged", name=extra[0])
    return manifest, hashes


def zlib_error():
    import zlib
    return zlib.error


def unknown_types(journal_path):
    """The types of the journal's events after its last snapshot that this code's fold does not know (an archive of a
    newer core) — folded by this code as hubd would."""
    from core import reducer
    with open(journal_path, "rb") as f:
        events = [json.loads(x) for x in f.read().split(b"\n") if x.strip()]
    start = 0
    for i in range(len(events) - 1, -1, -1):
        if events[i]["type"] == "core.snapshot" and (events[i].get("data") or {}).get("state"):
            start = i
            break
    state, found = reducer.empty(), set()
    for e, known in reducer.verdicts(events[start:]):
        if e["type"] == "core.snapshot" and (e.get("data") or {}).get("state"):
            state = reducer.restore(e)
            continue
        state, _, why = reducer.reduce(state, e, known)
        if why == UNKNOWN and known != "rejected":
            found.add(e["type"])
    return sorted(found)


def _release_number(name):
    from core import version
    return version.release_number(name or "")


def fresh(state, conf):
    """What the person (or their sessions) made in this hub, [] — none: the import may go without --replace."""
    from core import catalog, journal, system
    from modules.secrets import vault
    made = []
    try:
        st = journal.load_state(state)
    except (OSError, ValueError, KeyError):
        st = {}
    raw = {}
    try:   # the file as the person left it: load_config would add the defaults
        with open(catalog.config_path(state), encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        made.append(("settings", "hub.json"))
    cfg = raw if isinstance(raw, dict) else {}
    system_names = set()
    try:
        system_names = {n for n, _ in system.specs(catalog.load_config(state), state, st)}
    except Exception:  # noqa: BLE001 — without the names every session counts as the person's: a refusal, not a loss
        pass
    from core import liaison
    own = sorted(n for n, s in (st.get("sessions") or {}).items()
                 if isinstance(s, dict) and s.get("kind") != "hub" and n not in system_names and n != liaison.NAME)
    if own:
        made.append(("sessions", ", ".join(own[:5])))
    root = os.path.join(state, "directions")
    dirs = [d for d in (os.listdir(root) if os.path.isdir(root) else []) if not d.startswith((".", "_"))]
    if dirs:
        made.append(("directions", ", ".join(sorted(dirs)[:5])))
    if st.get("delegations"):
        made.append(("tasks", str(len(st["delegations"]))))
    jobs = [j for j, x in (st.get("schedule") or {}).items() if isinstance(x, dict) and not x.get("system")
            and x.get("state") == "active"]
    if jobs:
        made.append(("schedule", ", ".join(sorted(jobs)[:5])))
    if (st.get("deputies") or {}):
        made.append(("deputies", str(len(st["deputies"]))))
    if _contacts(conf):
        made.append(("contacts", str(len(_contacts(conf)))))
    try:
        if (vault.load(state).get("items") or {}):
            made.append(("secrets", ""))
    except Exception:  # noqa: BLE001
        pass
    for name in SECRET_CONFIG:
        if os.path.isfile(os.path.join(conf, name)):
            made.append(("sign_ins", name))
    for key in ("projects", "mcp", "rules", "extensions"):
        if cfg.get(key):
            made.append(("settings", key))
    return made


class Steps:
    """The import's journal of steps (<aside>/steps.json, rewritten whole and synced on every step): what to undo."""

    def __init__(self, aside, data=None):
        self.path = os.path.join(aside, STEPS)
        self.data = data or {"steps": [], "undone": False}

    def add(self, **step):
        step["done"] = False
        self.data["steps"].append(step)
        self.save()
        return step

    def done(self, step):
        step["done"] = True
        self.save()

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
            f.flush()
            platforms.current.sync(f.fileno())
        os.replace(tmp, self.path)

    @classmethod
    def load(cls, aside):
        with open(os.path.join(aside, STEPS), encoding="utf-8") as f:
            return cls(aside, json.load(f))


def _move(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.replace(src, dst) if not os.path.isdir(src) else os.rename(src, dst)


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        platforms.current.sync(f.fileno())
    os.replace(tmp, path)


def _vault_account(state):
    from modules.secrets import vault
    return vault.account(state)


def put_key(state, conf, key, steps, stamp):
    """The vault's key into this OS's store; the key there before goes aside (a file renamed in the settings folder, a
    Keychain item under another account) — never into the state folder."""
    account = _vault_account(state)
    keyfile = platforms.current.secret_key_file(conf)
    old = platforms.current.secret_key(account, conf)
    # each stage is written down once it happened: an undo after a failure at any point touches only what was done
    step = steps.add(do="key", account=account, file=keyfile, aside=None, set=False)
    if old is not None:
        if keyfile:
            aside = f"{keyfile}.before-import-{stamp}"
            os.replace(keyfile, aside)
        else:
            aside = f"{account}.before-import-{stamp}"
            platforms.current.secret_key(aside, conf, True, value=old)
        step["aside"] = aside
        steps.save()
        if not keyfile:
            platforms.current.drop_secret_key(account, conf)
    platforms.current.secret_key(account, conf, True, value=key)
    step["set"] = True
    steps.save()
    if platforms.current.secret_key(account, conf) != key:
        raise OSError("the vault key does not read back")
    steps.done(step)


def _undo_key(step, conf):
    """The key store as before the import: the imported key goes (only if it was put in), the one put aside comes back
    (only if it was put aside) — a failure before either leaves the store as it is."""
    account = step["account"]
    if step.get("file"):
        if step.get("set"):
            try:
                os.unlink(step["file"])
            except FileNotFoundError:
                pass
        if step.get("aside") and os.path.exists(step["aside"]) and not os.path.exists(step["file"]):
            os.replace(step["aside"], step["file"])
        return
    if step.get("set"):
        platforms.current.drop_secret_key(account, conf)
    if step.get("aside"):
        old = platforms.current.secret_key(step["aside"], conf)
        if old is not None:
            platforms.current.secret_key(account, conf, True, value=old)   # never over a key that is there
            if platforms.current.secret_key(account, conf) == old:
                platforms.current.drop_secret_key(step["aside"], conf)


def _undo_claude(step, undone):
    """The Claude files the import put in: one it left as it was goes (the archive has it); one a session changed since
    goes to `undone` under a short name (a project folder's path is long: Windows refuses past 260 characters)."""
    keep = os.path.join(undone, "claude")
    for n, (path, size, mtime) in enumerate(step.get("files") or []):
        try:
            st = os.stat(path)
        except FileNotFoundError:
            continue
        if (st.st_size, st.st_mtime_ns) == (size, mtime):
            os.unlink(path)
        else:
            _move(path, os.path.join(keep, f"{n}-{os.path.basename(path)}"))
    for folder in sorted(step.get("dirs") or [], key=len, reverse=True):
        try:
            os.rmdir(folder)   # only an empty one: what a session wrote there since stays
        except OSError:
            pass


def _clear(folder, names, into):
    """These entries of the folder (that are there) moved into `into` — what the imported hub had and made."""
    for name in names:
        here = os.path.join(folder, name)
        if os.path.lexists(here):
            dest = os.path.join(into, name)
            n = 1
            while os.path.lexists(dest):
                n += 1
                dest = os.path.join(into, f"{name}.{n}")
            _move(here, dest)


def undo_steps(steps, state, conf, undone):
    """This hub as before the import. Everything the imported hub has in the state folder (but this machine's own
    entries) and in the settings the import manages goes to `undone` — what it brought and what it made since (never
    deleted: work done meanwhile is kept); the Claude files the import created are removed (the archive has them); what
    was put aside comes back; the key store as it was. A step is marked once undone: a second `hub import --undo` after
    a failure goes on from where it stopped."""
    if not steps.data.get("cleared"):   # once: a second run would take away what the first one already put back
        _clear(state, [n for n in sorted(os.listdir(state)) if n not in MACHINE and not n.endswith(".lock")]
               if os.path.isdir(state) else [], os.path.join(undone, "state"))
        managed = {os.path.basename(s["from"]) for s in steps.data["steps"]
                   if s["do"] == "aside" and s.get("kind") == "config"} | \
                  {os.path.relpath(s["to"], conf).split(os.sep)[0] for s in steps.data["steps"]
                   if s["do"] == "placed" and s.get("kind") == "config" and _inside(s["to"], conf)}
        _clear(conf, sorted(managed), os.path.join(undone, "config"))   # only what the import moved: never more
        steps.data["cleared"] = True
        steps.save()
    for step in reversed(steps.data["steps"]):
        if step.get("reverted"):
            continue
        do = step["do"]
        if do == "claude":
            _undo_claude(step, undone)
        elif do == "placed" and os.path.lexists(step["to"]):
            _move(step["to"], os.path.join(undone, step["kind"], os.path.relpath(step["to"], step["root"])))
        elif do == "aside" and os.path.lexists(step["to"]):
            if os.path.lexists(step["from"]):
                _move(step["from"], os.path.join(undone, step["kind"], os.path.relpath(step["from"], step["root"])))
            _move(step["to"], step["from"])
        elif do == "key":
            _undo_key(step, conf)
        step["reverted"] = True
        steps.save()
    steps.data["undone"] = True
    steps.save()


def _apply_tree_paths(table, root):
    """direction.json `repo` of every direction through the table."""
    n = 0
    for slug in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        path = os.path.join(root, slug, "direction.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        moved = table.move(data.get("repo")) if isinstance(data, dict) else None
        if moved is not None and moved != data.get("repo"):
            data["repo"] = moved
            _write(path, json.dumps(data, ensure_ascii=False, indent=1).encode())
            n += 1
    return n


def compose_settings(arch, full_cfg, fresh_cfg, table, secrets_in):
    """hub.json of the imported hub: the archive's (whole from secrets.bin, or without what was cut), this machine's
    MACHINE_KEYS (and page_key without secrets), this machine's paths -> (settings, [cut that is gone], moved)."""
    if secrets_in and full_cfg is not None:
        cfg, gone = full_cfg, []
    else:
        cfg, gone = uncut_settings(arch)
    keys = MACHINE_KEYS + ((("page_key",),) if not secrets_in else ())
    for keys_ in keys:   # this machine's value, or none — never the old machine's (its tailnet name, its programs)
        value, there = _dig(fresh_cfg or {}, keys_)
        if there:
            _put(cfg, list(keys_), value)
        else:
            _drop(cfg, keys_)
    cfg, moved = table.tree(cfg, pathlists=True)
    return cfg, gone, moved


def missing_programs(cfg):
    """The programs hub.json names that this machine lacks: [(where, program)]."""
    out = []

    def check(where, prog):
        if not isinstance(prog, str) or not prog.strip():
            return
        first = prog.split()[0] if not os.path.isabs(prog) else prog
        first = os.path.expanduser(first)
        if os.path.isabs(first) and not os.path.exists(first) or not os.path.isabs(first) and not shutil.which(first):
            out.append((where, prog))
    for name, srv in sorted(((cfg or {}).get("mcp") or {}).items()):
        server = (srv or {}).get("server") if isinstance(srv, dict) else None
        command = (server or srv or {}).get("command") if isinstance(server or srv, dict) else None
        check(f"MCP {name}", command)
    check("voice.ffmpeg", ((cfg or {}).get("voice") or {}).get("ffmpeg") if isinstance((cfg or {}).get("voice"), dict)
          else None)
    check("browser.chrome", ((cfg or {}).get("browser") or {}).get("chrome")
          if isinstance((cfg or {}).get("browser"), dict) else None)
    return out


def clone(repo, path, run_git=None):
    """A repository of the archive at its new path -> (outcome, detail): "here" (the same repo is there), "busy" (the
    folder holds something else), "cloned", "moved" (the branch is elsewhere now), "detached", "default", "no_remote",
    "failed" (git's reason)."""
    run_git = run_git or _git_run
    remote = (repo.get("remotes") or {}).get("origin") or next(iter((repo.get("remotes") or {}).values()), None)
    if os.path.exists(path):
        there = repo_info(path) if os.path.isdir(path) else None
        if there and remote and remote in (there.get("remotes") or {}).values():
            return "here", ""
        return "busy", ""
    if not remote:
        return "no_remote", ""
    ok, err = run_git(["clone", "--quiet", remote, path], None)
    if not ok:
        return "failed", err
    branch, commit = repo.get("branch"), repo.get("commit")
    has = commit and run_git(["cat-file", "-e", f"{commit}^{{commit}}"], path)[0]
    if branch:
        if run_git(["checkout", "--quiet", branch], path)[0]:
            head = _git(path, "rev-parse", "HEAD")
            return ("cloned", "") if not commit or head == commit else ("moved", f"{(commit or '')[:9]} → {(head or '')[:9]}")
        if has and run_git(["checkout", "--quiet", "--detach", commit], path)[0]:
            return "detached", commit[:9]
        return "default", branch
    if has and run_git(["checkout", "--quiet", "--detach", commit], path)[0]:
        return "cloned", ""
    return "default", (commit or "")[:9]


def _git_run(args, cwd):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    try:
        r = platforms.current.run(["git", *(["-C", cwd] if cwd else []), *args], 600, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", env=env)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return False, type(e).__name__
    said = (r.stderr or r.stdout or "").strip().splitlines()
    return r.returncode == 0, said[-1][:200] if said else ""


def import_archive(state, conf, path, *, replace=False, no_secrets=False, no_clone=False, password=None, stop=None,
                   start=None, now=time.time, home=None, claude=None, osname=None):
    """The archive into this hub -> the report's data. password() asks it (secrets.bin and no --no-secrets); stop() —
    the hub stopped (True) or not; start() — it is up again."""
    from core import catalog
    now_ts = now()
    stamp = _stamp(now_ts)
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise Refused("cli.import.no_file", path=path)
    state = os.path.abspath(state)
    staging = f"{state}.importing-{stamp}"
    platforms.current.makedirs_private(staging)
    try:
        manifest, _ = unpack(path, staging)
        mine_rel = release_of(state)[0]
        theirs_rel = (manifest.get("hub") or {}).get("release")
        mine, theirs = _release_number(mine_rel), _release_number(theirs_rel)
        if mine and theirs and theirs > mine:
            raise Refused("cli.import.newer", theirs=theirs_rel, mine=mine_rel)
        jpath = os.path.join(staging, "state", "journal.jsonl")
        if not os.path.isfile(jpath):
            raise Refused("cli.import.no_manifest", path=path)
        unknown = unknown_types(jpath)
        if unknown:
            raise Refused("cli.import.unknown", types=", ".join(unknown[:5]), theirs=theirs_rel or "?",
                          mine=mine_rel or "?")
        if not replace:
            made = fresh(state, conf)
            if made:
                raise Refused("cli.import.not_fresh", what=_made_text(made), file=path,
                              aside=f"{state}.before-import-{stamp}")
        secret = None
        if os.path.isfile(os.path.join(staging, SECRETS)) and not no_secrets:
            with open(os.path.join(staging, SECRETS), "rb") as f:
                blob = f.read()
            word = password() if password else None
            if word is None:
                raise Refused("cli.import.password_needed", file=path)
            secret = unseal(state, blob, word, manifest.get("id") or "")
        src = manifest.get("from") or {}
        table = pathmap.Table(_pairs(src, state, conf, home, claude), _windows(src.get("os")), _windows(osname))
        cfg_path = catalog.config_path(state)
        try:
            with open(cfg_path, encoding="utf-8") as f:
                fresh_cfg = json.load(f)
        except (OSError, ValueError):
            fresh_cfg = {}
        with open(os.path.join(staging, "config", "hub.json"), encoding="utf-8") as f:
            arch_cfg = json.load(f)
        full_cfg = json.loads(secret["config/hub.json"]) if secret and "config/hub.json" in secret else None
        cfg, gone, moved_cfg = compose_settings(arch_cfg, full_cfg, fresh_cfg, table, secret is not None)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if stop and not stop():
        shutil.rmtree(staging, ignore_errors=True)
        if start:   # `hub off` may have turned the service off before hubd let go: the hub as it was, on
            start()
        raise Refused("cli.import.not_stopped")
    aside = f"{state}.before-import-{stamp}"
    platforms.current.makedirs_private(aside)
    steps = Steps(aside, {"steps": [], "undone": False, "archive": path, "made": now_ts, "state": state, "config": conf})
    steps.save()
    pairs = _pairs(src, state, conf, home, claude)
    report = {"manifest": manifest, "aside": aside, "gone": gone, "moved_cfg": moved_cfg, "paths": pairs,
              "clones": [], "claude_skipped": [], "programs": [], "missing": [], "secrets": secret is not None,
              "profile_mentions": _mentions(staging, src.get("home")) if any(
                  p[0] == src.get("home") for p in pairs) else 0}
    try:
        _swap(state, conf, staging, aside, steps, secret)
        if os.path.lexists(cfg_path):   # hub.json where the core reads it (catalog.config_path), wherever that is
            step = steps.add(do="aside", **{"from": cfg_path}, to=os.path.join(aside, "settings", "hub.json"),
                             root=os.path.dirname(cfg_path), kind="settings")
            _move(cfg_path, step["to"])
            steps.done(step)
        _write_settings(cfg_path, cfg, aside, steps)
        report["directions_moved"] = _apply_tree_paths(table, os.path.join(state, "directions"))
        if secret:
            _put_secrets(state, conf, secret, aside, steps, stamp)
        report["programs"] = missing_programs(cfg)
        if not no_clone:
            for repo in manifest.get("repos") or []:
                target = table.keep(repo["path"])
                outcome, detail = clone(repo, target)
                report["clones"].append({"path": target, "outcome": outcome, "detail": detail,
                                         "used_by": repo.get("used_by") or []})
        else:
            report["clones"] = [{"path": table.keep(r["path"]), "outcome": "listed", "detail": "",
                                 "used_by": r.get("used_by") or []} for r in manifest.get("repos") or []]
        report["claude_skipped"] = _put_claude(staging, table, manifest, steps, claude or claude_dir())
        missing = []
        for name, sess in sorted((manifest.get("sessions") or {}).items()):
            cwd = table.keep(sess.get("cwd")) if sess.get("cwd") else None
            if cwd and not os.path.isdir(cwd):
                # an executor's copy did not come along: the core stops it (only an executor for a task can be)
                missing.append({"name": name, "cwd": cwd, "role": sess.get("role"),
                                "copy": bool(sess.get("worktree")) and bool(sess.get("runner"))})
        report["missing"] = missing
        mark = {"archive": os.path.basename(path), "made": manifest.get("made"), "from_os": src.get("os"),
                "to_os": osname or platforms.current.APP_OS, "release": (manifest.get("hub") or {}).get("release"),
                "paths": pairs,
                "missing": [m["name"] for m in missing if m["copy"]], "secrets": secret is not None}
        step = steps.add(do="placed", to=os.path.join(state, MARK), root=state, kind="state")
        _write(os.path.join(state, MARK), json.dumps(mark, ensure_ascii=False, indent=1).encode())
        steps.done(step)
    except BaseException as e:
        failed = f"{state}.failed-import-{stamp}"
        try:
            undo_steps(steps, state, conf, failed)
        except Exception as again:  # noqa: BLE001 — a half-undone hub is not started: the steps say what is left
            shutil.rmtree(staging, ignore_errors=True)
            raise Refused("cli.import.undo_failed", error=f"{type(e).__name__}: {e}",
                          again=f"{type(again).__name__}: {again}", aside=aside) from e
        shutil.rmtree(staging, ignore_errors=True)
        if start:
            start()
        if isinstance(e, Refused):
            raise
        raise Refused("cli.import.failed", error=f"{type(e).__name__}: {e}", failed=failed) from e
    shutil.rmtree(staging, ignore_errors=True)
    if start:
        start()
    report["aside_bytes"] = _size(aside)
    return report


def _pairs(src, state, conf, home, claude):
    """[old, new] of the move: the state, settings and Claude folders, then the home folder (the longest first)."""
    pairs = []
    for key, new in (("state", state), ("config", conf), ("claude", claude or claude_dir()),
                     ("home", home or os.path.expanduser("~"))):
        old = src.get(key)
        if isinstance(old, str) and old and os.path.abspath(new) != old:
            pairs.append([old, os.path.abspath(new)])
    return pairs


def _mentions(staging, old_home):
    """How many times the archive's agent profiles name the old home folder: they are not rewritten (the agent's own
    knowledge), the report says how many."""
    n, root = 0, os.path.join(staging, "state", "agents")
    if not old_home:
        return 0
    for r, dirs, files in os.walk(root):
        for f in files:
            if PROFILE.match(f) and f.endswith(".md"):
                try:
                    with open(os.path.join(r, f), encoding="utf-8", errors="replace") as fh:
                        n += fh.read().count(old_home)
                except OSError:
                    pass
    return n


MADE = {"sessions": "cli.import.made_sessions", "directions": "cli.import.made_directions",
        "tasks": "cli.import.made_tasks", "schedule": "cli.import.made_schedule", "deputies": "cli.import.made_deputies",
        "contacts": "cli.import.made_contacts", "secrets": "cli.import.made_secrets",
        "sign_ins": "cli.import.made_sign_ins", "settings": "cli.import.made_settings"}
REPO = {"moved": "cli.import.repo_moved", "detached": "cli.import.repo_detached", "default": "cli.import.repo_default",
        "failed": "cli.import.repo_failed", "busy": "cli.import.repo_busy", "no_remote": "cli.import.repo_no_remote",
        "listed": "cli.import.repo_listed"}


def _made_text(made):
    return "; ".join(_t(MADE[kind], what=what) for kind, what in made)


def _swap(state, conf, staging, aside, steps, secret):
    """What this hub had goes aside, the archive's state and settings come in."""
    os.makedirs(state, exist_ok=True)
    for name in sorted(os.listdir(state)):
        if name in MACHINE or name.endswith(".lock"):
            continue
        step = steps.add(do="aside", **{"from": os.path.join(state, name)}, to=os.path.join(aside, "state", name),
                         root=state, kind="state")
        _move(step["from"], step["to"])
        steps.done(step)
    src = os.path.join(staging, "state")
    for name in sorted(os.listdir(src)) if os.path.isdir(src) else []:
        if name == HOME:
            continue
        step = steps.add(do="placed", to=os.path.join(state, name), root=state, kind="state")
        _move(os.path.join(src, name), step["to"])
        steps.done(step)
    home_src = os.path.join(src, HOME)
    for root, dirs, files in os.walk(home_src):
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), home_src)
            dest = os.path.join(state, HOME, rel)
            if os.path.lexists(dest):
                continue
            step = steps.add(do="placed", to=dest, root=state, kind="state")
            _move(os.path.join(root, f), dest)
            steps.done(step)
    names = list(CONFIG_FILES) + list(CONFIG_DIRS)
    if secret:
        names += list(SECRET_CONFIG) + list(SECRET_CONFIG_DIRS)
    os.makedirs(conf, exist_ok=True)
    for name in names:
        here = os.path.join(conf, name)
        if os.path.lexists(here):
            step = steps.add(do="aside", **{"from": here}, to=os.path.join(aside, "config", name), root=conf,
                             kind="config")
            _move(here, step["to"])
            steps.done(step)
    csrc = os.path.join(staging, "config")
    for name in list(CONFIG_FILES) + list(CONFIG_DIRS):
        if os.path.lexists(os.path.join(csrc, name)):
            step = steps.add(do="placed", to=os.path.join(conf, name), root=conf, kind="config")
            _move(os.path.join(csrc, name), step["to"])
            steps.done(step)


def _write_settings(cfg_path, cfg, aside, steps):
    step = steps.add(do="placed", to=cfg_path, root=os.path.dirname(cfg_path), kind="config")
    _write(cfg_path, json.dumps(cfg, ensure_ascii=False, indent=1).encode())
    steps.done(step)


def _put_secrets(state, conf, secret, aside, steps, stamp):
    for name, data in sorted(secret.items()):
        if name in ("config/hub.json", "vault.key"):
            continue
        if name.startswith("config/"):
            dest = os.path.join(conf, *name.split("/")[1:])
            root, kind = conf, "config"
        elif name.startswith("state/"):
            dest = os.path.join(state, *name.split("/")[1:])
            root, kind = state, "state"
        else:
            continue
        if not _inside(dest, root):
            continue
        top = os.path.join(root, name.split("/")[1])
        if not os.path.lexists(top):   # a folder the import makes is undone whole
            steps.done(steps.add(do="placed", to=top, root=root, kind=kind))
        _write(dest, data)
    if "vault.key" in secret:
        key = bytes.fromhex(secret["vault.key"].decode())
        put_key(state, conf, key, steps, stamp)
        from modules.secrets import vault
        if vault.status(state) != "ok":
            raise OSError("the vault does not open with the imported key")


def _put_claude(staging, table, manifest, steps, root):
    """Memory and conversations into the project folders of the sessions' new folders: a file that is there is never
    replaced (-> the names left out)."""
    from hosts import worktree
    src = os.path.join(staging, "claude")
    if not os.path.isdir(src):
        return []
    slugs = {sess["slug"]: table.keep(sess["cwd"]) for sess in (manifest.get("sessions") or {}).values()
             if isinstance(sess, dict) and sess.get("slug") and isinstance(sess.get("cwd"), str)}
    skipped = []
    for old in sorted(os.listdir(src)):   # one step per project folder (thousands of files would be thousands of syncs)
        new_cwd = slugs.get(old)
        new = worktree.slug(new_cwd) if new_cwd else old
        base = os.path.join(src, old)
        step = steps.add(do="claude", files=[], dirs=[])
        for r, dirs, files in os.walk(base):
            for f in files:
                rel = os.path.relpath(os.path.join(r, f), base)
                dest = os.path.join(root, "projects", new, rel)
                if os.path.lexists(dest):
                    skipped.append(os.path.join(new, rel))
                    continue
                parent = os.path.dirname(dest)
                while not os.path.isdir(parent) and parent not in step["dirs"]:
                    step["dirs"].append(parent)
                    parent = os.path.dirname(parent)
                _move(os.path.join(r, f), dest)
                st = os.stat(dest)
                step["files"].append([dest, st.st_size, st.st_mtime_ns])
        steps.done(step)
    return skipped


def last_aside(state):
    """The newest `<state>.before-import-*` whose import was not undone, or None."""
    parent, base = os.path.dirname(os.path.abspath(state)), os.path.basename(os.path.abspath(state))
    found = []
    for name in os.listdir(parent) if os.path.isdir(parent) else []:
        if name.startswith(base + ".before-import-") and os.path.isfile(os.path.join(parent, name, STEPS)):
            found.append(os.path.join(parent, name))
    for aside in sorted(found, reverse=True):
        try:
            if not Steps.load(aside).data.get("undone"):
                return aside
        except (OSError, ValueError):
            continue
    return None


def undo(state, conf, stop=None, start=None, now=time.time):
    """`hub import --undo` -> (aside, undone folder)."""
    aside = last_aside(state)
    if not aside:
        raise Refused("cli.import.nothing_to_undo")
    steps = Steps.load(aside)
    if stop and not stop():
        if start:
            start()
        raise Refused("cli.import.not_stopped")
    undone = f"{os.path.abspath(state)}.undone-import-{_stamp(now())}"
    try:
        undo_steps(steps, os.path.abspath(state), conf, undone)
    except Exception as e:  # noqa: BLE001 — not started half-undone; the same command goes on from the marked steps
        raise Refused("cli.import.undo_failed", error="-", again=f"{type(e).__name__}: {e}", aside=aside) from e
    if start:
        start()
    return aside, undone


# --- the commands ---------------------------------------------------------------------------------------------------

def size_text(n):
    n = int(n or 0)
    if n >= 1 << 30:
        return _t("cli.export.size_gb", n=f"{n / (1 << 30):.1f}")
    return _t("cli.export.size_mb", n=max(1, round(n / (1 << 20))) if n else 0)


def export_lines(got):
    """The report of `hub export` (spec §4.3)."""
    m = got["manifest"]
    c = m["counts"]
    lines = [_t("cli.export.done", path=got["path"], size=size_text(got["bytes"])),
             _t("cli.export.counts", agents=c["agents"], directions=c["directions"], schedule=c["schedule"],
                contacts=c["contacts"], tasks=c["tasks"])]
    if m["secrets"]:
        s = m.get("secret_counts") or {}
        lines.append(_t("cli.export.with_secrets", vault=s.get("vault", 0), accounts=s.get("accounts", 0)))
    else:
        lines.append(_t("cli.export.no_secrets"))
    left = []
    attach = sum(x["bytes"] for x in m["skipped"] if x["why"] == "full")
    big = [x for x in m["skipped"] if x["why"] == "big"]
    if attach:
        left.append(_t("cli.export.left_attachments", size=size_text(attach)))
    if big:
        top = max(big, key=lambda x: x["bytes"])["path"].split("/")
        names = {s.get("profile"): n for n, s in m["sessions"].items() if s.get("profile")}
        if len(top) > 2 and top[2] in names:   # state/agents/<profile>/… -> the agent's name, not its folder's
            top = ["agents", names[top[2]], *top[3:]]
        where = "/".join(top[:-1]) if len(top) > 2 else "/".join(top)
        left.append(_t("cli.export.left_big", size=size_text(sum(x["bytes"] for x in big)), count=len(big), path=where))
    if not m["full"] and any(s.get("role") == "agent" for s in m["sessions"].values()):
        left.append(_t("cli.export.left_conversations"))
    if left:
        lines.append(_t("cli.export.left_head"))
        lines.append("  " + " · ".join(left))
    unknown = [u for u in m["unknown"] if not u.get("taken")]
    if unknown:
        lines.append(_t("cli.export.unknown", list=", ".join(f"{u['name']} {size_text(u['bytes'])}" for u in unknown)))
    elif any(u.get("taken") for u in m["unknown"]):
        lines.append(_t("cli.export.unknown_taken", list=", ".join(u["name"] for u in m["unknown"])))
    if m["changing"]:
        lines.append(_t("cli.export.changing", list=", ".join(m["changing"][:5])))
    lost = []
    for r in m["repos"]:
        if not r.get("remotes"):
            lost.append(_t("cli.export.lost_no_remote", path=r["path"]))
            continue
        if r.get("unpushed"):
            lost.append(_t("cli.export.lost_unpushed", path=r["path"], count=r["unpushed"], branch=r.get("branch") or "HEAD"))
        if r.get("dirty"):
            lost.append(_t("cli.export.lost_dirty", path=r["path"]))
    for cp in m["copies"]:
        lost.append(_t("cli.export.lost_copy", name=cp["session"], path=cp["path"]))
    if lost:
        lines.append(_t("cli.export.lost_head"))
        lines += ["  " + x for x in lost]
    lines.append(_t("cli.export.private"))
    if m["secrets"]:
        lines.append(_t("cli.export.two_hubs"))
    return lines


def import_lines(r, state):
    """The report of `hub import` (spec §6.5)."""
    m = r["manifest"]
    c = m.get("counts") or {}
    src = m.get("from") or {}
    hub = m.get("hub") or {}
    lines = [_t("cli.import.done", archive=os.path.basename(m.get("_path") or "") or "-", made=m.get("made") or "?",
                os=src.get("os") or "?", release=hub.get("release") or (hub.get("commit") or "?")[:9]),
             _t("cli.import.counts", agents=c.get("agents", 0), directions=c.get("directions", 0),
                schedule=c.get("schedule", 0), contacts=c.get("contacts", 0))]
    if r["paths"]:
        lines.append(_t("cli.import.paths", pairs=", ".join(f"{o} → {n}" for o, n in r["paths"])))
        if r.get("profile_mentions"):
            lines.append(_t("cli.import.mentions", count=r["profile_mentions"]))
    clones = r["clones"]
    if clones:
        done = sum(1 for x in clones if x["outcome"] in ("cloned", "moved", "detached", "default"))
        here = sum(1 for x in clones if x["outcome"] == "here")
        bad = [x for x in clones if x["outcome"] in ("failed", "busy", "no_remote")]
        lines.append(_t("cli.import.repos", cloned=done, here=here, failed=len(bad)))
        for x in clones:
            if x["outcome"] in REPO:
                lines.append("  " + _t(REPO[x["outcome"]], path=x["path"], detail=x["detail"],
                                       who=", ".join(x["used_by"][:3])))
    stopped = [x["name"] for x in r["missing"] if x["copy"]]
    if stopped:
        lines.append(_t("cli.import.executors", names=", ".join(stopped)))
    for x in r["missing"]:
        if not x["copy"]:
            lines.append(_t("cli.import.no_folder", name=x["name"], path=x["cwd"]))
    cmds = m.get("commands") or []
    if cmds:
        table = pathmap.Table(r["paths"], _windows(src.get("os")), _windows())
        lines.append(_t("cli.import.commands", list="; ".join(f"{x['id']} → {table.words(x['command'])[0]}"
                                                             for x in cmds[:5])))
    todo = [_t("cli.import.todo_claude")]
    had = m.get("had") or {}
    if not r["secrets"]:
        if had.get("telegram"):
            todo.append(_t("cli.import.todo_telegram"))
        if had.get("accounts"):
            todo.append(_t("cli.import.todo_accounts"))
        if had.get("vault"):
            todo.append(_t("cli.import.todo_vault"))
    for g in r["gone"]:
        p = g["path"]
        todo.append(_t("cli.import.todo_cut_entry" if g["kind"] == "entry" else "cli.import.todo_cut",
                       where=".".join(str(x) for x in p[:-1]) or str(p[0]), name=str(p[-1])))
    for where, prog in r["programs"]:
        todo.append(_t("cli.import.todo_program", where=where, program=prog))
    lines.append(_t("cli.import.todo_head"))
    lines += [f"  {i}. {x}" for i, x in enumerate(todo, 1)]
    if r["secrets"]:
        lines.append(_t("cli.import.secrets_moved"))
    lines.append(_t("cli.import.old_hub"))
    if r["claude_skipped"]:
        lines.append(_t("cli.import.claude_kept", count=len(r["claude_skipped"])))
    lines.append(_t("cli.import.aside", path=r["aside"], size=size_text(r.get("aside_bytes", 0))))
    return lines


def main_export(argv, state, conf):
    """hub export [<file>] [--secrets] [--full] -> the exit code."""
    flags = {"--secrets", "--full"}
    args = [a for a in argv if a not in flags]
    if len(args) > 1 or any(a.startswith("-") for a in args):
        print(_t("cli.export.usage"), file=sys.stderr)
        return 2
    password = None
    try:
        if "--secrets" in argv:
            _crypto(state)   # the library first: a password asked for nothing is worse than the refusal
            password = ask_password()
            if len(password) < PASSWORD_MIN:
                raise Refused("cli.export.password_short", min=PASSWORD_MIN)
        got = export(state, conf, args[0] if args else None, full="--full" in argv, password=password)
    except Refused as e:
        print(_t(e.key, **e.params), file=sys.stderr)
        return 1
    print("\n".join(export_lines(got)))
    return 0


def main_import(argv, state, conf, stop, start):
    """hub import <file> [--replace] [--no-secrets] [--no-clone] | hub import --undo -> the exit code."""
    if argv == ["--undo"]:
        try:
            aside, undone = undo(state, conf, stop=stop, start=start)
        except Refused as e:
            print(_t(e.key, **e.params), file=sys.stderr)
            return 1
        print(_t("cli.import.undone", aside=aside, undone=undone))
        return 0
    flags = {"--replace", "--no-secrets", "--no-clone"}
    args = [a for a in argv if a not in flags]
    if len(args) != 1 or args[0].startswith("-"):
        print(_t("cli.import.usage"), file=sys.stderr)
        return 2
    try:
        got = import_archive(state, conf, args[0], replace="--replace" in argv, no_secrets="--no-secrets" in argv,
                             no_clone="--no-clone" in argv, password=lambda: ask_password(twice=False),
                             stop=stop, start=start)
    except Refused as e:
        print(_t(e.key, **e.params), file=sys.stderr)
        return 1
    got["manifest"]["_path"] = args[0]
    print("\n".join(import_lines(got, state)))
    return 0
