"""Take the hub's own edits of the core off and put them back (dev-guard spec §5.7; owner decision q1793545: "see, hold,
roll back"): the core does it itself, with no agent, on the owner's button or `hub update local-edits off|on`. The
updater runs both under the machine lock (update.py Updater.local_edits_switch); this file is plain git, no network.

Take off, with B the release the edits are counted from (core/codeedits.classify, the band's own rule):
- the backup branch backup/local-edits-<date> gets two commits on top of HEAD's state with the edits taken off:
  "return" — the edits of the core as they were (committed and not), and above it "folder" — the whole folder as it
  was, the uncommitted files of the hub's own modules too; what .gitignore hides is in neither;
- the main branch gets one new commit (only when its committed tree had edits of the core): those files as in B,
  the new ones removed; the working tree and the index follow for those files only. The hub's own modules and
  bin/hub-<name> stay where they were, inside git or outside it.
Put back: a merge of the "return" commit of the newest backup branch made here (marked with TRAILER), so a release
merged since stays and the edits come back on top; the hub's own modules are not touched. The merge is the
updater's (a copy, then a fast-forward of the live tree): a conflict leaves the code as it was."""
import os
import re
import shutil
import subprocess
import time

from core import codeedits

PREFIX = "backup/local-edits-"
REFS = "refs/heads/" + PREFIX
NAME = codeedits.BACKUP_NAME
TRAILER = "Hub-Local-Edits"   # the commits' own mark: a branch of the same name made by hand is not ours to merge
RETURN, FOLDER = "return", "folder"
FILES = "Hub-Local-Edits-Files"


class Git:
    """git in the hub's code folder; literal paths (a file named "*" is that file), no optional locks for reads."""

    def __init__(self, repo, umask=-1, identity=()):
        self.repo, self.umask, self.identity = repo, umask, list(identity)

    def __call__(self, *args, input=None, index=None, check=True):
        env = {**os.environ, **({"GIT_INDEX_FILE": index} if index else {})}
        r = subprocess.run(["git", "--literal-pathspecs", *self.identity, *args], cwd=self.repo, input=input,
                           capture_output=True, text=True, env=env, timeout=300, umask=self.umask)   # text as in codeedits
        if check and r.returncode:
            raise RuntimeError(f"git {args[0]}: {r.stderr.strip()[-300:]}")
        return r.stdout if r.returncode == 0 else None

    def sha(self, ref):
        return (self("rev-parse", "-q", "--verify", f"{ref}^{{commit}}", check=False) or "").strip() or None

    def ancestor(self, a, b):
        return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=self.repo, capture_output=True,
                              timeout=120).returncode == 0


def _entries(g, tree, paths=()):
    """{path: "<mode> <type> <object>"} of every file of a tree (or a commit), or of these paths only."""
    out = {}
    for item in g("ls-tree", "-r", "-z", "--full-tree", tree, *(["--", *paths] if paths else [])).split("\0"):
        if item:
            meta, path = item.split("\t", 1)
            out[path] = meta
    return out


def _index_info(entries, paths, zero):
    """update-index --index-info lines: the paths as in `entries`, the ones it lacks removed (mode 0)."""
    lines = []
    for p in paths:
        meta = entries.get(p)
        if meta:
            mode, _, obj = meta.split()
            lines.append(f"{mode} {obj}\t{p}")
        else:
            lines.append(f"0 {zero}\t{p}")
    return "\0".join(lines) + "\0"


class _Index:
    """A temporary index next to the real one: trees are built without touching the hub's index or its files."""

    def __init__(self, g):
        self.g = g
        self.path = os.path.join(g.repo, g("rev-parse", "--git-path", f"index.hub-edits-{os.getpid()}").strip())

    def __enter__(self):
        return self.path

    def __exit__(self, *exc):
        for p in (self.path, self.path + ".lock"):
            if os.path.lexists(p):
                os.unlink(p)


def _overlay(g, start, source, paths):
    """The tree of `start` with `paths` as they are in `source` (absent there: removed)."""
    zero = "0" * len(g.sha(start) or "0" * 40)
    with _Index(g) as index:
        g("read-tree", start, index=index)
        g("update-index", "-z", "--index-info", input=_index_info(_entries(g, source), paths, zero), index=index)
        return g("write-tree", index=index).strip()


def _folder_tree(g):
    """The tree of the whole folder as it is now: the index copied (so unchanged files are not hashed again), then
    everything the hub has, new files too; what .gitignore hides stays out."""
    real = os.path.join(g.repo, g("rev-parse", "--git-path", "index").strip())
    with _Index(g) as index:
        if os.path.exists(real):
            # with its own time: git trusts a file whose size and time match only when the index is older than it
            # (the racy-git rule) — a fresh copy would hide an edit made in the same second as the last index write
            shutil.copy2(real, index)
        else:
            g("read-tree", "HEAD", index=index)
        g("add", "-A", "--", ".", index=index)
        return g("write-tree", index=index).strip()


def _commit(g, tree, parent, subject, kind, count):
    msg = f"{subject}\n\n{TRAILER}: {kind}\n{FILES}: {count}\n"
    return g("commit-tree", tree, "-p", parent, input=msg).strip()


def _free_name(g, now):
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    for n in range(1, 1000):
        name = PREFIX + day + ("" if n == 1 else f"-{n}")
        if not g.sha(f"refs/heads/{name}"):
            return name
    raise RuntimeError("no free name for the backup branch")


def take_off(repo, base, umask=-1, identity=(), now=None):
    """-> {"result": "off", "branch", "count", "commit" (the new commit of main, or None)} | {"result": "none"} (no edits of
    the core) | {"result": "moved"} (HEAD or the folder changed while the trees were built: nothing changed). Raises
    RuntimeError/OSError on a git failure: before anything moved, or — once the files are being written — naming the
    backup branch that holds everything."""
    g = Git(repo, umask, identity)
    got = codeedits.classify(repo, base)
    if got is None:
        raise RuntimeError("the edits could not be counted")
    paths = [f["path"] for f in got["core"] if not f["path"].endswith("/")]   # "dir/": a nested repository — not ours
    if not paths:
        return {"result": "none"}
    head = g.sha("HEAD")
    folder = _folder_tree(g)
    off_tree = _overlay(g, head, base, paths)
    off = _commit(g, off_tree, head, "Take the hub's own edits of the core off: back to the release", "off", len(paths)) \
        if off_tree != g("rev-parse", f"{head}^{{tree}}").strip() else None
    start = off or head
    ret = _commit(g, _overlay(g, start, folder, paths), start, "The hub's own edits of the core, to put back", RETURN,
                  len(paths))
    full = _commit(g, folder, ret, "The rest of the code folder as it was: the hub's own modules, files outside git",
                   FOLDER, len(paths))
    name = _free_name(g, time.time() if now is None else now)
    zero = "0" * len(head)
    # a session wrote into the folder while the trees were built: that write would be in no backup. A write after this
    # look, in the second before the files are written, is not seen — the window is that second, not the whole build
    if _folder_tree(g) != folder:
        return {"result": "moved"}
    ref = f"refs/heads/{name}"
    g("update-ref", "-m", "hub: backup before taking the hub's own edits of the core off", ref, full, zero)
    if off and g("update-ref", "-m", "hub: the hub's own edits of the core taken off", "HEAD", off, head,
                 check=False) is None:
        g("update-ref", "-d", ref, full, check=False)   # HEAD moved: nothing of ours is left
        return {"result": "moved"}
    # the index of those files only: as in `start` (the release's), the new ones out. A refusal (a session's git holds
    # index.lock) changed nothing in the index: HEAD goes back and the backup away, as if not pressed
    keep = _entries(g, start)
    try:
        g("update-index", "-z", "--index-info", input=_index_info(keep, paths, zero))
    except (RuntimeError, subprocess.SubprocessError):
        if off:
            g("update-ref", "-m", "hub: taking the own edits off did not go through", "HEAD", head, off, check=False)
        g("update-ref", "-d", ref, full, check=False)
        raise
    # the files: the new ones removed first (a release file may sit where a new folder is), then the release's written;
    # a folder where a release file should be (a nested repository, a module folder) is left as it is
    try:
        for p in paths:
            if p not in keep:
                path = os.path.join(repo, *p.split("/"))
                if os.path.isfile(path) or os.path.islink(path):
                    os.unlink(path)
                    _drop_empty(repo, os.path.dirname(path))
        here = [p for p in paths if p in keep and not (os.path.isdir(os.path.join(repo, *p.split("/")))
                                                       and not os.path.islink(os.path.join(repo, *p.split("/"))))]
        if here:
            g("checkout-index", "-f", "-z", "--stdin", input="\0".join(here) + "\0")
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:   # HEAD and the index moved: the backup holds it all
        raise RuntimeError(f"{e} — everything that was in the code folder is in {name}") from None
    return {"result": "off", "branch": name, "count": len(paths), "commit": off}


def keep_copy(repo, base, identity=(), now=None, umask=-1):
    """The same backup branch as take_off, with nothing taken off: HEAD, the index and the files stay as they are (the
    move into the app, app spec §8 — the clone is left on the disk as it was, the branch names the hub's own edits of the
    core so that they are not lost when the clone goes). -> {"result": "kept", "branch", "count"} | {"result": "none"}
    (no edits of the core) | {"result": "moved"} (the folder changed while the trees were built: nothing written)."""
    g = Git(repo, umask, identity)
    got = codeedits.classify(repo, base)
    if got is None:
        raise RuntimeError("the edits could not be counted")
    paths = [f["path"] for f in got["core"] if not f["path"].endswith("/")]
    if not paths:
        return {"result": "none"}
    head = g.sha("HEAD")
    folder = _folder_tree(g)
    ret = _commit(g, _overlay(g, head, folder, paths), head, "The hub's own edits of the core, to put back", RETURN,
                  len(paths))
    full = _commit(g, folder, ret, "The rest of the code folder as it was: the hub's own modules, files outside git",
                   FOLDER, len(paths))
    if _folder_tree(g) != folder:
        return {"result": "moved"}
    name = _free_name(g, time.time() if now is None else now)
    g("update-ref", "-m", "hub: a copy of the hub's own edits of the core before the move into the app",
      f"refs/heads/{name}", full, "0" * len(head))
    return {"result": "kept", "branch": name, "count": len(paths)}


def _drop_empty(repo, folder):
    """Folders left empty by a removed new file, up to the code folder (not it)."""
    top = os.path.normcase(os.path.abspath(repo))
    while os.path.normcase(os.path.abspath(folder)) != top and os.path.normcase(os.path.abspath(folder)).startswith(top):
        try:
            os.rmdir(folder)
        except OSError:
            return
        folder = os.path.dirname(folder)


def _read(g, sha):
    """(parents, {trailer: value}) of a commit."""
    text = g("cat-file", "commit", sha, check=False) or ""
    head, _, body = text.partition("\n\n")
    parents = [line.split()[1] for line in head.splitlines() if line.startswith("parent ")]
    marks = dict(m.groups() for m in re.finditer(r"^([\w-]+): (.+)$", body, re.M))
    return parents, marks


def latest(repo):
    """The newest backup branch made by take_off: {"name", "ret" (the commit to merge), "count", "at"}, or None."""
    g = Git(repo)
    out = g("for-each-ref", "--format=%(refname:lstrip=2) %(objectname) %(committerdate:unix)", REFS + "*", check=False) or ""
    found = []
    for line in out.splitlines():
        try:
            name, sha, at = line.split()
        except ValueError:
            continue
        if NAME.fullmatch(name) and at.isdigit():
            # the newest by time; two in one second (or a clock set back) — the higher number of the day
            found.append((int(at), name[len(PREFIX):len(PREFIX) + 10], int(name[len(PREFIX) + 11:] or 1), name, sha))
    for at, _, _, name, sha in sorted(found, reverse=True):
        parents, marks = _read(g, sha)
        if marks.get(TRAILER) != FOLDER or len(parents) != 1:
            continue
        up, ret_marks = _read(g, parents[0])
        if ret_marks.get(TRAILER) != RETURN or len(up) != 1:
            continue
        count = marks.get(FILES, "")
        return {"name": name, "ret": parents[0], "count": int(count) if count.isdigit() else 0, "at": at}
    return None


def returned(repo, found):
    """The edits of this backup are in the code already: put back (or merged by hand), or every file they change is
    already as they have it (a release took the same change in, the files were set so by hand)."""
    g = Git(repo)
    if g.ancestor(found["ret"], "HEAD"):
        return True
    parents, _ = _read(g, found["ret"])
    if len(parents) != 1:
        return False
    changed = [p for p in g("diff-tree", "-r", "-z", "--no-renames", "--name-only", parents[0], found["ret"]).split("\0") if p]
    if len(changed) > 200:   # a long list does not go on a command line (Windows): the whole trees
        after, now = _entries(g, found["ret"]), _entries(g, "HEAD")
    else:
        after, now = _entries(g, found["ret"], changed), _entries(g, "HEAD", changed)
    return all(now.get(p) == after.get(p) for p in changed)
