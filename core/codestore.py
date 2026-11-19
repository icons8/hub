"""Storage of code from git — shared by core modules (`<state>/modules/`, core/modsrc.py) and marketplace entries
(`<state>/plugins/`, core/marketplace.py): one code, the root is a parameter.

`<root>/<repo>-<address hash>/`:
- `repo.git` — a bare clone of the source (only the needed ref with one commit is fetched), git with the machine's
  keys, without prompts;
- `<commit>/` — the files of this commit (`git archive` into a file, not memory), the process works from it;
  `<commit>.files.json` — the list of its files with sizes and hashes, written last: a folder without it does not
  exist. A new version is a new folder, not a rename of a busy one (Windows: a process holds the folder).
- `damage` — whether the folder is intact: a file from the list is changed or gone. In a strict root (plugins) an extra
  file is a change too (a planted `json.py` next to `main.py` would otherwise run); `__pycache__` bytecode is not.
- `cleanup` — commit folders that nothing refers to (hub.json: modules, extensions, skill sets; module want files;
  working folders of live plugin processes in hub-up.json) are removed.

The roots differ so that a cleanup by an earlier core after a rollback (it knows only modules) does not delete a
plugin folder of the same repo. Limits (limits) belong to the marketplace entries root; modules have none, as before.
"""
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile

import platforms
from core import i18n, lock

MODULES = "modules"     # the core modules root (core/modsrc.py); plugins have their own (core/marketplace.py)
REPO_DIR = "repo.git"
LISTING = ".files.json"
REF = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]{0,200}")
COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
HEX = re.compile(r"[0-9a-f]{7,64}")
GIT_WAIT = 300          # clone and fetch of a big repo over a slow network
# variables that lead git into another repository (a command from a git hook or from a session's working copy)
GIT_PLACE = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
             "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_PREFIX", "GIT_CEILING_DIRECTORIES"}
MB = 1 << 20


def shown(url):
    """An address for the screen: without the login and token in it (https://user:token@host/…)."""
    return re.sub(r"(?<=://)[^/@]*@", "***@", str(url))


def is_local(url):
    """The address is a folder on this machine (a path, file://), not a server: that is how git understands it (the scp
    form is host:path, a colon before the first slash)."""
    u = str(url)
    if u.lower().startswith("file:"):
        return True
    if re.match(r"[A-Za-z][A-Za-z0-9+.-]*://", u):
        return False
    if re.match(r"[A-Za-z]:[\\/]", u) or u.startswith("\\\\"):
        return True   # Windows: a drive or a network folder
    head = re.split(r"[/\\]", u, maxsplit=1)[0]
    return ":" not in head


# --- git ------------------------------------------------------------------------------------------------------------

def quiet_env():
    """The environment of a git that goes to the network in the background (hub-up, modules, the update): no password or
    key-passphrase prompt (an error instead of a hung command) and no sign-in window. GCM_INTERACTIVE: Git Credential Manager
    opens no login window without a login."""
    env = {k: v for k, v in os.environ.items() if k.upper() not in GIT_PLACE}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="", SSH_ASKPASS="", SSH_ASKPASS_REQUIRE="never", GCM_INTERACTIVE="never")
    try:   # the hub's own GitHub sign-in («Setup → Team»): a credential helper for github.com, for this git only
        from core import ghauth
        env.update(ghauth.git_env(ghauth.state_dir(), base=env))
    except Exception:  # noqa: BLE001 — a broken sign-in folder must not stop git: without it, the machine's own access
        pass
    return env


def git_cwd():
    """{"cwd": a neutral folder} when the process stands in a layer of the install line, else {}: the layer carries a
    stop file `.git` (appcode.GIT_STOP), and a git that needs no repository of its own (ls-remote, config) looks for
    one from the current folder up, finds the stop and fails — the official catalog was never downloaded. The search
    is git's own: the nearest `.git` up the tree decides; a working copy of a person's own keeps its cwd (a relative
    address works). For `subprocess` calls of the core that ask the network or the machine's settings."""
    from core import appcode
    try:
        path = os.getcwd()
    except OSError:   # the folder the process stood in is gone: nothing to search from, and none to keep
        return {"cwd": tempfile.gettempdir()}
    while True:
        mark = os.path.join(path, appcode.GIT_STOP[0])
        if os.path.lexists(mark):
            try:
                with open(mark, "rb") as f:
                    stop = f.read(len(appcode.GIT_STOP[1]) + 1) == appcode.GIT_STOP[1]
            except OSError:   # a folder `.git` or unreadable: a repository
                stop = False
            return {"cwd": tempfile.gettempdir()} if stop else {}
        up = os.path.dirname(path)
        if up == path:
            return {}
        path = up


def _placed(args):
    """The command names its repository (`--git-dir=`, `-C`): git does not search for one, the cwd is not its business."""
    return any(a == "-C" or str(a).startswith("--git-dir=") for a in args[:2])


def git(*args, timeout=60, binary=False, remote=None):
    """git without prompts: a password, an ssh window or askpass is an error, not a hung command (the credential helper
    and ssh-agent work). Its own OS session without a controlling terminal: ssh asks for a key passphrase and an unknown
    host via /dev/tty, not stdin (on POSIX; a timeout kills the whole group, the transport helper too). remote — the
    address the command goes to: for a server the file transport is closed (a source cannot slip in a path on this
    machine)."""
    env = quiet_env()
    pre = ["-c", "protocol.file.allow=never"] if remote is not None and not is_local(remote) else []
    text = {} if binary else {"text": True, "encoding": "utf-8", "errors": "replace"}
    # getattr (here and in _planted): a page server of v1.4.0 reaches this file through the catalogs during the
    # update's self-check with its own platforms in memory (tests/test_old_page_server); _planted then sees no X bit,
    # as the Windows adapter does: "planted" files are wiped by _heal, and on Windows os.access(X_OK) holds for every file
    try:
        return platforms.current.run(["git", *pre, "-c", "core.askPass=", *args], timeout, capture_output=True,
                                     stdin=subprocess.DEVNULL, env=env, **({} if _placed(args) else git_cwd()),
                                     **getattr(platforms.current, "no_terminal", lambda: {})(), **text)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git: no answer within {timeout} s") from None   # core/gitaccess reads both: network, git
    except OSError as e:
        raise RuntimeError(f"git does not start: {e}") from None


def said(r):
    out = r.stderr if isinstance(r.stderr, str) else (r.stderr or b"").decode("utf-8", "replace")
    return out.strip()[-400:] or f"code {r.returncode}"


def ls_remote(url, *patterns, symref=False):
    """Lines of the source's `git ls-remote` by patterns: "<commit>\t<name>" (with symref also "ref: <target>\tHEAD").
    """
    r = git("ls-remote", *(["--symref"] if symref else []), "--", url, *patterns, timeout=GIT_WAIT, remote=url)
    if r.returncode:
        raise RuntimeError(f"git: {said(r)}")
    return r.stdout.splitlines()


def _pull(repo, url, *args, refspec, depth=True):
    depth = ["--depth=1"] if depth else []
    r = git(f"--git-dir={repo}", "fetch", "-q", "--force", *args, *depth, "--", url, *refspec, timeout=GIT_WAIT,
            remote=url)
    if r.returncode:
        raise RuntimeError(f"git: {said(r)}")


def fetch_into(repo, url, ref=None):
    """The source into the bare clone repo -> (commit, pinned) for ref: a branch moves (pinned False), a tag and a
    commit do not. Only the needed ref with one commit is fetched: the repo may hold other branches, and the whole
    core, next to it. An error is a RuntimeError with git's words."""
    if not isinstance(url, str) or not url.strip() or url.startswith("-"):
        raise RuntimeError("a git repository address is expected")
    if ref is not None and not (REF.fullmatch(str(ref)) and ".." not in str(ref)):
        raise RuntimeError(f"ref “{ref}”: a branch, a tag or a commit is expected")
    if not os.path.isfile(os.path.join(repo, "HEAD")):
        shutil.rmtree(repo, ignore_errors=True)   # an interrupted clone
        os.makedirs(os.path.dirname(repo), exist_ok=True)
        r = git("init", "--bare", "-q", repo)
        if not r.returncode:   # origin with an all-branches rule — so an older hub can update this clone
            r = git(f"--git-dir={repo}", "remote", "add", "origin", url)
        if not r.returncode:
            r = git(f"--git-dir={repo}", "config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*")
        if r.returncode:
            raise RuntimeError(f"git: {said(r)}")
    # "has no branch, tag or commit": core/marketplace.pin tells this error by these words - change both together
    missing = RuntimeError(f"{shown(url)} has no branch, tag or commit “{ref or 'HEAD'}”")
    if ref is None:
        heads = [x[5:].split("\t")[0].strip() for x in ls_remote(url, "HEAD", symref=True) if x.startswith("ref: ")]
        point = heads[0] if heads else ""
        if not point.startswith("refs/heads/"):
            raise missing
        _pull(repo, url, "--no-tags", refspec=[f"+{point}:{point}"])
        git(f"--git-dir={repo}", "symbolic-ref", "HEAD", point)
        tries = [(point, False)]
    else:
        names = {x.split("\t", 1)[1] for x in ls_remote(url, f"refs/heads/{ref}", f"refs/tags/{ref}") if "\t" in x}
        if f"refs/heads/{ref}" in names:
            _pull(repo, url, "--no-tags", refspec=[f"+refs/heads/{ref}:refs/heads/{ref}"])
        elif f"refs/tags/{ref}" in names:
            _pull(repo, url, "--no-tags", refspec=[f"+refs/tags/{ref}:refs/tags/{ref}"])
        elif HEX.fullmatch(ref):
            try:   # the server serves a commit by full name if allowed; else as before, all branches and tags
                if not COMMIT.fullmatch(ref):
                    raise RuntimeError("a short commit name")
                _pull(repo, url, "--no-tags", refspec=[f"+{ref}:refs/pins/{ref}"])
            except RuntimeError:
                _pull(repo, url, "--tags", refspec=["+refs/heads/*:refs/heads/*"], depth=False)
        else:
            raise missing
        tries = [(f"refs/heads/{ref}", False), (f"refs/tags/{ref}", True)] + ([(ref, True)] if HEX.fullmatch(ref) else [])
    for name, pinned in tries:
        r = git(f"--git-dir={repo}", "rev-parse", "-q", "--verify", f"{name}^{{commit}}")
        if not r.returncode and COMMIT.fullmatch(r.stdout.strip()):
            return r.stdout.strip(), pinned
    raise missing


def _refs(repo):
    r = git(f"--git-dir={repo}", "for-each-ref", "--format=%(refname) %(objectname)")
    return dict(x.split(" ", 1) for x in r.stdout.splitlines() if " " in x) if not r.returncode else {}


def fetch_capped(repo, url, ref=None, cap=None):
    """fetch_into with a limit on one download (cap, bytes; None — none): a clone growth above it is a refusal. A new
    clone is deleted; an earlier one stays as it was (refs go back, what was downloaded goes away): it holds the commit
    in use, and a repeat of the same download would hit the limit again. The growth is counted, not the whole clone:
    objects of earlier commits accumulate in it with every update."""
    had = cap and os.path.isfile(os.path.join(repo, "HEAD"))
    before, refs = (_size(repo), _refs(repo)) if had else (0, {})
    out = fetch_into(repo, url, ref)
    if cap and _size(repo) - before > cap:
        if had:
            for name, sha in _refs(repo).items():
                if refs.get(name) != sha:
                    git(f"--git-dir={repo}", "update-ref", *([name, refs[name]] if name in refs else ["-d", name]))
            git(f"--git-dir={repo}", "gc", "-q", "--prune=now", timeout=GIT_WAIT)
        else:
            remove(repo)
        raise RuntimeError(f"the clone of {shown(url)} is larger than {cap / MB:g} MB — such a source is not installed")
    return out


# --- files ------------------------------------------------------------------------------------------------------------

def _hash(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _walk(root):
    """(folder, names) — files and links, including links to folders (os.walk puts them with folders and does not
    descend into them); without Python bytecode."""
    for d, dirs, names in os.walk(root):
        links = [x for x in dirs if os.path.islink(os.path.join(d, x))]
        dirs[:] = sorted(x for x in dirs if x != "__pycache__" and x not in links)
        yield d, sorted(names + links)


def files(root):
    """{path/from/folder: [size, mtime_ns, sha1]} — files and links of the folder, without Python bytecode."""
    out = {}
    for d, names in _walk(root):
        for n in names:
            p = os.path.join(d, n)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            st = os.lstat(p)
            out[rel] = [-1, 0, os.readlink(p)] if os.path.islink(p) else [st.st_size, st.st_mtime_ns, _hash(p)]
    return out


NOISE = {".DS_Store", "Thumbs.db", "desktop.ini"}   # traces of file managers, not code
# files that are executed or picked up next to plugin code (import, launch, Claude Code loading): a planted one is a
# change; any other extra file in the commit folder is plugin data (cache, database, json in its process cwd), so
# it is left alone
CODE = {".py", ".pyc", ".pyo", ".pyw", ".pyd", ".so", ".dylib", ".dll", ".pth", ".js", ".mjs", ".cjs", ".node", ".sh",
        ".bash", ".zsh", ".bat", ".cmd", ".ps1", ".exe", ".com", ".vbs", ".jar", ".rb", ".pl", ".php"}
# folders and files of a skill set (a Claude Code plugin): anything extra there is planted, no plugin data lives in them
LOADED_DIRS = {"hooks", "commands", "agents", "skills", "monitors", "bin", "output-styles", "themes", ".claude-plugin"}
LOADED_FILES = {".mcp.json", ".lsp.json"}


def _planted(rel, path):
    """An extra file/link looks planted: a link, an executable, code or what Claude Code picks up."""
    parts = rel.split("/")
    return (os.path.islink(path) or os.path.splitext(parts[-1])[1].lower() in CODE or parts[-1] in LOADED_FILES
            or bool(LOADED_DIRS & set(parts[:-1]))
            or getattr(platforms.current, "has_exec_bit", lambda p: False)(path))


def _extra(root, listing):
    """Planted under the code: extra files and links (except `__pycache__` folders and file manager traces) that are
    not in the list and that are `_planted`. Any other extra is plugin data and does not count as a change."""
    out = []
    for d, names in _walk(root):
        for n in names:
            p = os.path.join(d, n)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            if n not in NOISE and rel not in listing and _planted(rel, p):
                out.append(rel)
    return out


def _intact(p, size, mtime, digest):
    """The file (or link) is in place and is as recorded in the commit list."""
    try:
        st = os.lstat(p)
        if size == -1:
            return os.path.islink(p) and os.readlink(p) == digest
        return not os.path.islink(p) and st.st_size == size and (st.st_mtime_ns == mtime or _hash(p) == digest)
    except OSError:
        return False


def _clear(path):
    """Remove what stands in the way (a file, a link, a folder), without following a link."""
    if os.path.islink(path) or os.path.isfile(path):
        try:
            os.unlink(path)
        except OSError as e:
            _writable(path, e)   # Windows: "read-only"
            os.unlink(path)
    elif os.path.isdir(path):
        platforms.current.rmtree(path)


def _heal(folder, fresh, listing, planted):
    """The commit folder exists but is changed: put the commit files back from the fresh unpacking `fresh` and remove
    the planted ones (planted), but leave everything else in the folder (plugin data). We do not delete the whole
    folder: two entries of the same commit live in one folder, and on Windows a folder held by a process would not be
    fully deleted."""
    for rel, (size, mtime, digest) in sorted(listing.items()):
        dst, src = os.path.join(folder, *rel.split("/")), os.path.join(fresh, *rel.split("/"))
        if _intact(dst, size, mtime, digest):
            if size != -1:   # the file's real time goes into the list: damage compares it and hashes only if it differs
                listing[rel][1] = os.lstat(dst).st_mtime_ns
            continue
        parts = rel.split("/")[:-1]
        for i in range(len(parts)):   # real folders along the path to the file, not planted links or files
            here = os.path.join(folder, *parts[:i + 1])
            if os.path.islink(here) or (os.path.lexists(here) and not os.path.isdir(here)):
                _clear(here)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        _clear(dst)
        if size == -1:
            os.symlink(digest, dst)
        else:
            shutil.copy2(src, dst)
            listing[rel][1] = os.lstat(dst).st_mtime_ns
    for rel in planted:
        _clear(os.path.join(folder, *rel.split("/")))


def older(repo, a, b):
    """Commit a is older than commit b by commit date (both in the bare clone repo): a move from b to a is a rollback.
    Clones are shallow (`--depth 1`), the history between them is not downloaded — hence the date, not ancestry; if
    unknown — False."""
    r = git(f"--git-dir={repo}", "show", "-s", "--format=%ct", a, b)
    t = r.stdout.split()
    return not r.returncode and len(t) == 2 and all(x.isdigit() for x in t) and int(t[0]) < int(t[1])


def _writable(path, exc):
    """Make a read-only entry deletable: a link itself, never the file it leads to (someone else's); where a link's own
    mode cannot be set (Linux) - the error as it was."""
    if not (os.path.islink(path) or os.path.isjunction(path)):
        os.chmod(path, 0o700)
    elif os.chmod in os.supports_follow_symlinks:
        os.chmod(path, 0o700, follow_symlinks=False)
    else:
        raise exc


def remove(path):
    if os.path.isdir(path) and not os.path.islink(path):
        def writable(fn, p, _exc):   # Windows: read-only files (from git) cannot be deleted otherwise
            _writable(p, _exc)
            fn(p)
        shutil.rmtree(path, onexc=writable)


def drop(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def write_json(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    platforms.current.replace(tmp, path)


def _size(root):
    total = 0
    for d, _, names in os.walk(root):
        for n in names:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(d, n)).st_size
    return total


def _forms(path):
    """How a path may be written: as is and without symlinks (/tmp and /private/tmp on macOS)."""
    p = os.path.abspath(os.path.expanduser(str(path)))
    return {os.path.normcase(p), os.path.normcase(os.path.realpath(p))}


def references(state_dir, cfg):
    """Folders that something refers to now: module commits from hub.json, extension and skill set folders, working
    folders of module processes (want files) and plugins (hub-up.json). Only so that nothing needed is deleted: an
    extra reference merely keeps a folder until the next cleanup."""
    from core import modpages, modules
    out = set()
    for s in modules.settings(cfg).get("sources") or []:
        if isinstance(s, dict) and isinstance(s.get("git"), str) and s["git"] and COMMIT.fullmatch(str(s.get("commit"))):
            out |= _forms(Store(state_dir, MODULES).commit_dir(s["git"], s["commit"]))
    exts = cfg.get("extensions")
    for x in exts.values() if isinstance(exts, dict) else []:
        if isinstance(x, dict) and isinstance(x.get("path"), str) and x["path"]:
            out |= _forms(x["path"])
    sets = cfg.get("sets")
    for x in sets if isinstance(sets, list) else []:
        if isinstance(x, dict) and isinstance(x.get("path"), str) and x["path"]:
            out |= _forms(x["path"])
    for w in modpages.pages(state_dir).values():
        if w.get("cwd"):
            out |= _forms(w["cwd"])
    try:
        from core.restart import UP
        with open(os.path.join(state_dir, UP), encoding="utf-8") as f:
            up = json.load(f).get("exts")
    except (OSError, ValueError, AttributeError):
        up = None
    for cwd in up.values() if isinstance(up, dict) else []:
        if isinstance(cwd, str) and cwd:
            out |= _forms(cwd)
    return out


def _inside(folder, refs):
    """The folder (or something inside it) is referenced."""
    for f in _forms(folder):
        if any(r == f or r.startswith(f.rstrip(os.sep) + os.sep) for r in refs):
            return True
    return False


# --- storage ----------------------------------------------------------------------------------------------------------

class Store:
    """A storage root: `<state>/<root>/`. strict — an extra file in the commit folder is a change too; limits —
    {"clone", "unpacked": bytes, "files": count}: above it — refusal (None — no limits); fix — what restores the files
    (for the text)."""

    def __init__(self, state_dir, root, strict=False, limits=None, fix="hub module update"):
        self.state_dir, self.root, self.strict, self.limits, self.fix = state_dir, root, strict, limits, fix
        self.base = os.path.join(state_dir, root)

    def source_dir(self, url):
        """The source folder: the repo name for a human and the address hash (two repos with one name)."""
        base = re.sub(r"\.git$", "", str(url).rstrip("/\\").replace("\\", "/").split("/")[-1].split(":")[-1])
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-.")[:40] or "repo"
        return os.path.join(self.base, f"{slug}-{hashlib.sha1(str(url).encode()).hexdigest()[:8]}")

    def git_dir(self, url):
        return os.path.join(self.source_dir(url), REPO_DIR)

    def commit_dir(self, url, commit, part=None):
        """The commit folder; part (subdir, name) — only that folder of the commit, unpacked as <name>/ into its own
        folder <commit>~<name>-<hash>: one plugin of a big catalog (limits count the plugin, not the whole repo), and the
        last folder of its path is the plugin's name (Claude Code names a plugin without a manifest after its folder)."""
        if not part:
            return os.path.join(self.source_dir(url), commit)
        sub, name = part
        tag = hashlib.sha1(f"{sub or ''}\0{name}".encode()).hexdigest()[:8]
        return os.path.join(self.source_dir(url), f"{commit}~{name[:40]}-{tag}")

    def listing_path(self, url, commit, part=None):
        return self.commit_dir(url, commit, part) + LISTING

    @contextlib.contextmanager
    def busy(self):
        """One command over the root's sources at a time: two do not download or delete each other's folders."""
        os.makedirs(self.base, exist_ok=True)
        with lock.exclusive(os.path.join(self.base, ".lock")):
            yield

    def fetch(self, url, ref=None):
        """The source into a bare clone -> (commit, pinned). One download above the limit is a refusal, the clone is
        deleted."""
        return fetch_capped(self.git_dir(url), url, ref, (self.limits or {}).get("clone"))

    def damage(self, url, commit, part=None):
        """None — the commit folder is intact; otherwise what is wrong with it: not unpacked or files changed."""
        folder = self.commit_dir(url, commit, part)
        try:
            with open(self.listing_path(url, commit, part), encoding="utf-8") as f:
                listing = json.load(f)["files"]
        except (OSError, ValueError, KeyError, TypeError):
            listing = None
        if not isinstance(listing, dict) or not os.path.isdir(folder):
            return i18n.t("core.codestore.not_downloaded", source=shown(url), commit=commit[:12], fix=self.fix)
        bad = [rel for rel, (size, mtime, digest) in sorted(listing.items())
               if not _intact(os.path.join(folder, *rel.split("/")), size, mtime, digest)]
        if self.strict:
            bad += _extra(folder, listing)
        if bad:
            if len(bad) > 3:
                return i18n.t("core.codestore.damaged_more", commit=commit[:12], files=", ".join(bad[:3]),
                              more=len(bad) - 3, fix=self.fix)
            return i18n.t("core.codestore.damaged", commit=commit[:12], files=", ".join(bad), fix=self.fix)
        return None

    def _check(self, url, commit, tree=None):
        """Limits before the archive: the number of files and their size, by the commit tree (git ls-tree), not by the
        written archive: a small clone can be huge unpacked. tree — only this tree of the commit (`<commit>:<subdir>`)."""
        if not (self.limits or {}).get("files") and not (self.limits or {}).get("unpacked"):
            return
        r = git(f"--git-dir={self.git_dir(url)}", "ls-tree", "-r", "-l", "-z", tree or commit, timeout=GIT_WAIT)
        if r.returncode:
            raise RuntimeError(f"git ls-tree {commit[:12]}: {said(r)}")
        count = size = 0
        for rec in r.stdout.split("\0"):
            meta = rec.split("\t", 1)[0].split()
            if len(meta) == 4 and meta[1] == "blob":   # <mode> blob <object> <size>; submodules (commit) are not files
                count, size = count + 1, size + (int(meta[3]) if meta[3].isdigit() else 0)
        if self.limits.get("files") and count > self.limits["files"]:
            raise RuntimeError(f"commit {commit[:12]} has {count} files — more than {self.limits['files']}")
        if self.limits.get("unpacked") and size > self.limits["unpacked"]:
            raise RuntimeError(f"commit {commit[:12]} is {size / MB:.1f} MB — more than {self.limits['unpacked'] / MB:g} MB")

    def unpack(self, url, commit, part=None):
        """The commit folder from the bare clone (git archive into a file), if it is absent or changed; -> the path.
        part (subdir, name) — only that folder, as <path>/<name> (commit_dir)."""
        folder = self.commit_dir(url, commit, part)
        if self.damage(url, commit, part) is None:
            return folder
        tmp, archive = f"{folder}.tmp-{os.getpid()}", f"{folder}.tmp-{os.getpid()}.tar"
        remove(tmp)
        drop(archive)
        tree = f"{commit}:{part[0] or ''}" if part else None
        self._check(url, commit, tree)
        try:
            r = git(f"--git-dir={self.git_dir(url)}", "archive", "--format=tar", f"--output={archive}",
                    *([f"--prefix={part[1]}/", tree] if part else [commit]), timeout=GIT_WAIT)
            if r.returncode:
                raise RuntimeError(f"git archive {commit[:12]}: {said(r)}")
            try:
                with tarfile.open(archive) as tar:
                    os.makedirs(tmp)
                    tar.extractall(tmp, filter="data")   # no paths outside the folder and no foreign permissions
                listing = files(tmp)
            except (tarfile.TarError, OSError) as e:
                raise RuntimeError(f"unpacking {commit[:12]}: {e}") from None
        except RuntimeError:
            remove(tmp)
            raise
        finally:
            drop(archive)
        drop(self.listing_path(url, commit, part))
        if self.strict and os.path.isdir(folder) and not os.path.islink(folder):
            # a plugin writes into its folder (the process cwd) — a reinstall repairs the code, not erases its data
            try:
                _heal(folder, tmp, listing, _extra(folder, listing))
            except OSError as e:   # the list is already gone: the next unpacking starts with a fresh check
                raise RuntimeError(f"unpacking {commit[:12]}: {e}") from None
            finally:
                remove(tmp)
        else:
            remove(folder)
            os.replace(tmp, folder)
        write_json(self.listing_path(url, commit, part), {"commit": commit, "files": listing})
        return folder

    def running(self, base):
        """Commit folders in base in which a module process still runs (the want file)."""
        from core import modpages
        out = set()
        for w in modpages.pages(self.state_dir).values():
            if not w.get("cwd"):
                continue
            try:
                rel = os.path.relpath(os.path.abspath(str(w["cwd"])), os.path.abspath(base))
            except ValueError:   # Windows: another drive
                continue
            if not rel.startswith(".."):
                out.add(rel.replace("\\", "/").split("/")[0])
        return out

    def cleanup(self, cfg, url, also=()):
        """Commit folders of the source that nothing refers to (references) and that were not just replaced (also: a
        process may not have moved to the new one yet), and abandoned unfinished unpackings."""
        base = self.source_dir(url)
        names = os.listdir(base) if os.path.isdir(base) else []
        if not names:
            return
        refs = references(self.state_dir, cfg)   # and working folders of processes (want files, hub-up.json)
        for n in names:
            folder = n[:-len(LISTING)] if n.endswith(LISTING) else n.split(".tmp-")[0]
            commit = folder.split("~")[0]   # <commit>~<name>-<hash>: one folder of the commit (commit_dir part)
            if not COMMIT.fullmatch(commit):
                continue
            if ".tmp-" in n or (commit not in also and not _inside(os.path.join(base, folder), refs)):
                path = os.path.join(base, n)
                remove(path) if os.path.isdir(path) else drop(path)
