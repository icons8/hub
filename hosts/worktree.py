"""A working copy of its own for an executor (hubd, before it starts the executor: disk work, not a reduction; owner
decision 2026-10-01: use a worktree, but check that the worktree works correctly).

prepare(folder, executor name) -> (executor folder, details, error):
- not a git repo: (None, None, None): the executor works in the folder as is;
- git: `git worktree add <repo>/.claude/worktrees/<name> -b task/<name>`, then checks (it is a working copy, its root is
  that folder, the branch is task/<name>, it is in `git worktree list`, clean). If anything is wrong: (None, details,
  reason): there is no executor, and whoever started it gets the reason. The copy gets the project's MCP choice from
  .claude/settings.local.json of the source folder (mcp_settings).
- several hubs on one machine (slice M2): an instance's copy is <repo>/.claude/worktrees/@<instance>/<name> on
  task/@<instance>/<name>; a copy of another hub is neither taken nor removed (its owner mark, its live session); git
  of the hubs in one repo goes one at a time under the repo's file lock (repo_lock).
"""
import contextlib
import fnmatch
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

import platforms
from core import codestore, coretext, perms
from core.coretext import T

# why there is no copy, what the cleanup did (to whoever raised the executor, the hub's notice) and the copy's checks
NO_FOLDER = T("core.worktree.no_folder", "there is no folder {path}")
NOT_GIT = T("core.worktree.not_git", "{path} is not a git working copy")
NOT_ROOT = T("core.worktree.not_root", "{path} is not the root of a git working copy (the root is {top}): --here only "
             "in the root of a ready copy")
STATUS_SILENT = T("core.worktree.status_silent", "git status did not answer in the folder")
UNCOMMITTED = T("core.worktree.uncommitted", "the folder {path} has uncommitted changes (git status: {count}) — the "
                "branch {branch} cannot be taken from it: first sort out the uncommitted (stash) or an executor "
                "without --branch")
NO_MAIN = T("core.worktree.no_main", "did not find the repo's main branch (origin/HEAD, main, master) — switch the "
            "folder {path} yourself")
NO_SWITCH = T("core.worktree.no_switch", "the folder {path} cannot be switched to {main} ({error}) — switch it from "
              "{branch} yourself or an executor without --branch")
FORWARD_MOVED = T("core.worktree.forward_moved", "the folder {path} changed branch or commit since the check — not "
                  "fast-forwarded")
FORWARD_DIRTY = T("core.worktree.forward_dirty", "the folder {path} got uncommitted changes (git status: {count}) — "
                  "not fast-forwarded")
FORWARD_FAILED = T("core.worktree.forward_failed", "git did not fast-forward the folder {path}: {error}")
FORWARD_TIMEOUT_TEXT = T("core.worktree.forward_timeout", "git was fast-forwarding the folder {path} for longer than "
                         "{seconds} s and was stopped")
NO_BRANCH = T("core.worktree.no_branch", "no branch {branch} in {top} (neither its own nor on origin)")
HALF_DROPPED = T("core.worktree.half_dropped", "the unfinished copy was removed")
HALF_LEFT = T("core.worktree.half_left", "the unfinished copy could not be removed: {path}")
ADD_TIMEOUT = T("core.worktree.add_timeout", "git worktree add did not finish within {minutes} min (a big repo or a "
                "slow disk) — {left}; without a copy: --repo <folder> --here")
COPY_DROPPED = T("core.worktree.copy_dropped", "the copy was removed")
COPY_LEFT = T("core.worktree.copy_left", "the copy could not be removed: {path}")
NOT_INCLUDED = T("core.worktree.not_included", "files from .worktreeinclude were not copied: {error}")
CHECK_TREE = T("core.worktree.check_tree", "it is a git working copy")
CHECK_ROOT = T("core.worktree.check_root", "its root is this folder")
CHECK_BRANCH = T("core.worktree.check_branch", "branch {branch}")
CHECK_LISTED = T("core.worktree.check_listed", "listed in git worktree list")
CHECK_CLEAN = T("core.worktree.check_clean", "clean")
CHECK_INCLUDED = T("core.worktree.check_included", "files from .worktreeinclude: {count}")
CHECK_MCP = T("core.worktree.check_mcp", "the project's MCP choice from .claude/settings.local.json")
CHECK_FAILED = T("core.worktree.check_failed", "the worktree failed the check: {failed}")
NO_SUBFOLDER = T("core.worktree.no_subfolder", "the working copy has no folder {rel}: it is in git, but not in the "
                 "commit the copy starts from")
IN_PLACE = T("core.worktree.in_place", "the working copy is in place")
NO_REPO = T("core.worktree.no_repo", "no repo of its copy — it starts in the default folder")
RESTORED_FROM = T("core.worktree.restored_from", "the working copy was restored from the branch {branch}")
RESTORED_NEW = T("core.worktree.restored_new", "the working copy was restored {base}, a new branch {branch}")
FROM_MAIN = T("core.worktree.from_main", "from the main branch")
FROM_HEAD = T("core.worktree.from_head", "from HEAD")
FETCH_FAILED = T("core.worktree.fetch_failed", "origin was not fetched ({error}): the base is origin as git saw it at the last fetch")
ALREADY_GONE = T("core.worktree.already_gone", "the working copy is already gone")
NO_DOT_GIT = T("core.worktree.no_dot_git", "{path} has no .git — leaving it alone: check the work in it yourselves")
NOT_A_COPY = T("core.worktree.not_a_copy", "not a git working copy — leaving it alone")
KEPT_DIRTY = T("core.worktree.kept_dirty", "{path} has uncommitted changes — keeping the copy")
KEPT_JUNCTION = T("core.worktree.kept_junction", "a junction in {path} cannot be cut ({error}) — keeping the copy: git would "
                  "walk through it and erase what it leads to")
NOT_WIPED = T("core.worktree.not_wiped", "did not remove {path} completely ({error})")
SHELL_LOCKED = T("core.worktree.shell_locked", "{path} is emptied, but the folder itself cannot be removed ({error}) — a "
                 "program may be standing in it")
KEPT_COMMITS = T("core.worktree.kept_commits", "the working copy was removed, the branch {branch} kept: it has commits "
                 "merged nowhere")
KEPT_BRANCH = T("core.worktree.kept_branch", "the working copy was removed, the branch kept")
REMOVED = T("core.worktree.removed", "the working copy and the branch were removed")
# several hubs on one machine (slice M2): a copy another hub made or another hub's session works in
FOREIGN_COPY = T("core.worktree.foreign_copy", "the working copy {path} is another hub's ({hub}) — not taken: an "
                 "executor under another name")
FOREIGN_LIVE = T("core.worktree.foreign_live", "a session of another hub ({hub}) works in {path} — not taken: an "
                 "executor under another name")
FOREIGN_KEPT = T("core.worktree.foreign_kept", "{path} is another hub's ({hub}) — leaving it alone")
LOCK_BUSY = T("core.worktree.lock_busy", "git of a hub of this machine has held this repo longer than {seconds} s — "
              "try again later")

# several hubs on one machine (slice M2, core/instance.machine): an instance's executors' copies and branches go under
# its name — .claude/worktrees/@<instance>/<executor>, task/@<instance>/<executor>; a session name starts with a letter or
# a digit (reducer.NAME), so no copy of the main hub is ever under @. Each copy a hub makes on a machine with more than
# one hub gets its mark in the copy's own git folder (<git-common-dir>/worktrees/<id>/), and git that adds, removes or
# fast-forwards there takes the file lock of the repo; on a machine with one hub neither file is written
LOCK = "agents-hub.lock"
OWNER_MARK = "agents-hub-owner.json"


def copy_path(top, name, space=None):
    """The folder of an executor's copy: <top>/.claude/worktrees/<name>, an instance's under @<instance>."""
    return os.path.join(top, ".claude", "worktrees", *([f"@{space}"] if space else []), name)


def task_branch(name, space=None):
    """The branch of an executor's copy: task/<name>, an instance's task/@<instance>/<name>."""
    return f"task/@{space}/{name}" if space else f"task/{name}"


@contextlib.contextmanager
def repo_lock(common, timeout):
    """The file lock of a repo between the hubs of a machine: <git-common-dir>/agents-hub.lock (the shared .git folder of
    all its working copies). common None (not git, or a hub alone on the machine) — no lock. Waits at most timeout
    seconds, then TimeoutError; a .git this user cannot write to (another person's repo) — no lock, git itself refuses."""
    path = os.path.join(common, LOCK) if common else None
    try:
        if path:
            with open(path, "a"):
                pass
    except OSError:
        path = None
    if not path:
        yield
        return
    with platforms.current.exclusive(path, timeout=timeout):
        yield


def _copy_gitdir(path):
    """The copy's own git folder (<git-common-dir>/worktrees/<id>), or None: not a linked copy. Read from the files git
    keeps, without a git process (a slow git must not make another hub's copy look unmarked): the copy's `.git` is a
    file `gitdir: <folder>`, and that folder has a `commondir` (the repo's own .git, a folder, has none)."""
    try:
        with open(os.path.join(path, ".git"), encoding="utf-8") as f:
            line = f.readline().strip()
    except (OSError, UnicodeDecodeError):   # no .git, or .git is a folder (the repo itself)
        return None
    if not line.startswith("gitdir:"):
        return None
    folder = os.path.normpath(os.path.join(path, line[len("gitdir:"):].strip()))
    return folder if os.path.isfile(os.path.join(folder, "commondir")) else None


def _read_mark(folder):
    try:
        with open(os.path.join(folder, OWNER_MARK), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("state"), str) and data["state"] else None


def owner_of(path):
    """The mark of the hub that made (or took) the copy at path: {"state", "hub"} or None (no mark: a copy of a hub alone
    on the machine, or made before marks; not a linked copy)."""
    folder = _copy_gitdir(path)
    return _read_mark(folder) if folder else None


def _mark(path, owner):
    """Mark the copy at path as the owner's if it has no mark yet, or the hub its mark names is removed (best effort:
    the copy works without it)."""
    folder = _copy_gitdir(path) if owner else None
    mark = _read_mark(folder) if folder else None
    if not folder or mark and os.path.isdir(mark["state"]):
        return
    try:
        with open(os.path.join(folder, OWNER_MARK), "w", encoding="utf-8") as f:
            json.dump({"state": os.path.realpath(owner["state"]), "hub": owner.get("hub") or ""}, f)
    except OSError:
        pass


def _foreign(path, owner, foreign):
    """Why the existing copy at path is not this hub's -> ("mark"|"live", the other hub) or (None, None). Its mark
    names another hub whose state folder is there (a mark of a hub removed since is no one's); or a live session of
    another hub of the machine works in it (foreign(path) -> that hub's command): a copy made before marks (an
    instance on an earlier core, under the plain name)."""
    mark = owner_of(path) if owner else None
    if mark and os.path.realpath(mark["state"]) != os.path.realpath(owner["state"]) and os.path.isdir(mark["state"]):
        return "mark", mark.get("hub") or mark["state"]
    hub = foreign(path) if foreign else None
    return ("live", hub) if hub else (None, None)


def lock_target(repo, owner, common=None):
    """What repo_lock locks for git in repo: its git-common-dir (common, if the caller has it already) on a machine with
    more than one hub (owner set); None for a hub alone on the machine or a folder that is gone."""
    if not owner or not repo or not os.path.isdir(repo):
        return None
    try:
        return common or common_dir(repo)
    except (OSError, subprocess.SubprocessError):
        return None


def slug(path):
    """The Claude Code project folder for a path: every character except Latin letters and digits becomes "-" (checked on a real claude)."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(path))


def _git(cwd, *args, raw=False, timeout=30, english=False):
    # git output is UTF-8 (paths, branch names): the system encoding (Windows cp1251) broke parsing on a Cyrillic letter
    # in a path; raw is the output as is (`status -z`). GIT_OPTIONAL_LOCKS=0: the core's status does not rewrite the
    # index, otherwise it holds index.lock, and a stash, merge or commit in the same folder at that moment fails with
    # "index.lock exists". english: the core parses a git refusal by its text: LC_ALL=C for this call only (git hooks
    # inherit it too, and they should get the owner's locale)
    # the user's repo: working copy files get the user's umask, not the hub's closed one (core/perms.py)
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", **({"LC_ALL": "C"} if english else {})}
    r = platforms.current.run(["git", "-c", "core.longpaths=true", "-C", cwd, *args], timeout, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", env=env, umask=perms.user_umask())
    out = r.stdout or ""
    return r.returncode, out if raw else out.strip(), (r.stderr or "").strip()


def include(top, path):
    """Files from <repo>/.worktreeinclude go into the new working copy, as `claude --worktree` does: those that are both
    in the list and ignored by git (.env, local settings); other working copies in .claude/worktrees do not, and ones
    already there are left alone (2026-10-06: an executor's copy lacked the project's .env, tests failed at startup).
    As in Claude Code, git returns an ignored directory as one line (`--directory`); we go inside only those the list's
    pattern may reach: on 2026-10-23, in a monorepo with gigabytes of node_modules and logs, walking all files took ~30 s,
    a timeout, and the executor did not start. -> how many were copied."""
    n = 0
    for rel in wanted(top):
        src, dst = os.path.join(top, rel), os.path.join(path, rel)
        if os.path.isfile(src) and not os.path.exists(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            n += 1
    return n


def wanted(top):
    """Paths (from the repo root) of files for include. Matching against the list is done by git itself (`_listed`);
    here only which ignored directories to expand (`_may`)."""
    listed = os.path.join(top, ".worktreeinclude")
    try:
        with open(listed, encoding="utf-8", errors="replace") as f:
            lines = [x.rstrip() for x in f.read().splitlines()]
    except OSError:
        return []
    lines = [x for x in lines if x and not x.startswith("#")]
    if not lines:
        return []
    fold = _git(top, "config", "--bool", "core.ignorecase")[1] == "true"
    ls = ("ls-files", "-z", "--others", "--ignored", "--exclude-standard")
    found = sorted(_git(top, *ls, "--directory", raw=True, timeout=COPY_TIMEOUT)[1].split("\0"))
    # a directory git entered itself (untracked, with only ignored files inside) comes as one line together with its
    # contents: expand only those whose contents the list does not already name; other working copies, never
    dirs = [d for d, nxt in zip(found, found[1:] + [""]) if d.endswith("/") and not nxt.startswith(d) and
            not d.startswith(".claude/worktrees/")]
    tmp = tempfile.mkdtemp(prefix="hub-wti-")
    try:
        named = _listed(tmp, listed, fold, dirs)
        dirs = [d for d in dirs if d in named or _may(lines, d, fold)]
        while dirs:   # in batches: command line length (Windows: 32 thousand characters)
            n, size = 0, 0
            while n < len(dirs) and (not n or size + len(dirs[n]) < 16000):
                size, n = size + len(dirs[n]) + 1, n + 1
            found += _git(top, "--literal-pathspecs", *ls, "--", *dirs[:n], raw=True,
                          timeout=COPY_TIMEOUT)[1].split("\0")
            dirs = dirs[n:]
        files = [x for x in found if x and not x.endswith("/") and not x.startswith(".claude/worktrees/")]
        return sorted(_listed(tmp, listed, fold, files))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _listed(tmp, listed, fold, paths):
    """Which paths match the list's patterns, by git's own rules: `check-ignore` in an empty temporary repo tmp, where the
    list is the only source of patterns (in the project repo its .gitignore would be mixed in; so would GIT_* variables
    of the environment). A directory ends with "/". A git failure is an OSError, not "nothing matched"."""
    if not paths:
        return set()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    if not os.path.isdir(os.path.join(tmp, ".git")):
        r = platforms.current.run(["git", "init", "-q", "--template=", tmp], COPY_TIMEOUT, capture_output=True,
                                  text=True, env=env)
        if r.returncode != 0:
            raise OSError(f"git init: {(r.stderr or '').strip()[:200]}")
    r = platforms.current.run(
        ["git", "-c", f"core.excludesFile={listed}", "-c", f"core.ignorecase={str(fold).lower()}", "-C", tmp,
         "check-ignore", "--no-index", "-z", "--stdin"], COPY_TIMEOUT, input="\0".join(paths) + "\0",
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    if r.returncode not in (0, 1):   # 1: none matched
        raise OSError(f"git check-ignore: {(r.stderr or '').strip()[:200]}")
    return set((r.stdout or "").split("\0")) - {""}


def _may(lines, d, fold):
    """Whether a pattern with "/" (anchored to the root) can name a path inside the ignored directory d ("a/b/"): its
    parts agree with the parts of d, so we expand the directory. As in Claude Code: a pattern without "/" (".env", at any
    depth) does not lead into a directory; a leading "**/" leads only to a directory whose path has a part starting with
    the literal beginning of the pattern's next part ("**/config/x" to .../config/..., "**/*.local" nowhere): otherwise
    the whole node_modules gets walked. In doubt ("\\" in the pattern), expand."""
    def low(x):
        return x.lower() if fold else x
    have = [low(x) for x in d.rstrip("/").split("/")]

    def into(segs):
        for i, part in enumerate(have):
            if i >= len(segs) or "\\" in segs[i]:
                return True
            if segs[i] == "**":   # after a named part: inside
                if i:
                    return True
                head = low(re.split(r"[*?\[]", segs[i + 1])[0]) if i + 1 < len(segs) else ""
                return bool(head) and any(x.startswith(head) for x in have)
            if not fnmatch.fnmatchcase(part, low(segs[i]).replace("[^", "[!")):
                return False
        return True
    return any(into(line.strip("/").split("/")) for line in lines
               if not line.startswith("!") and "/" in line.rstrip("/"))


MCP_KEYS = ("disabledMcpjsonServers", "enabledMcpjsonServers", "enableAllProjectMcpServers")
APPROVALS = ("enabledMcpjsonServers", "enableAllProjectMcpServers")


def _bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def mcp_settings(src, dst):
    """The project's MCP choice (MCP_KEYS) from <src>/.claude/settings.local.json goes into the same file of the copy
    <dst>; other keys of both files are left alone (2026-10-20: the file is ignored by git, and servers of .mcp.json
    disabled in the main folder were started again in an executor's copy and crashed). Approvals (APPROVALS) only when
    the copy's .mcp.json is the same as the source's: that one was approved, and a branch from origin (`--branch`) may
    bring its own servers. If the copy's file is not ignored by git, we do not write it: the copy would become dirty.
    If it fails, the copy works without it. -> whether it was carried over."""
    rel = os.path.join(".claude", "settings.local.json")
    try:
        with open(os.path.join(src, rel), encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError):
        return False
    same = _bytes(os.path.join(src, ".mcp.json")) == _bytes(os.path.join(dst, ".mcp.json"))
    keys = {k: got[k] for k in MCP_KEYS if isinstance(got, dict) and k in got and (same or k not in APPROVALS)}
    if not keys or _git(dst, "check-ignore", "-q", rel)[0] != 0:
        return False
    target = os.path.join(dst, rel)
    try:
        with open(target, encoding="utf-8") as f:
            have = json.load(f)
    except (OSError, ValueError):
        have = None
    if not isinstance(have, dict):
        if os.path.exists(target):
            return False   # the copy's own file can't be parsed: do not overwrite it
        have = {}
    tmp = target + ".tmp"
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({**have, **keys}, f, ensure_ascii=False, indent=2)
        platforms.current.replace(tmp, target)
    except OSError:
        try:
            os.remove(tmp)   # do not leave the copy dirty
        except OSError:
            pass
        return False
    return True


def folder(cwd, dirty=False):
    """A specialist's folder for the mini-hub (core/minihub.py) -> (git, uncommitted): git is "gone" (no folder, or none
    given), "none" (not git), "empty" (a repo without commits) or "ok"; uncommitted is how many lines `git status` has
    (without executors' working copies in .claude/worktrees), only if asked and git is "ok", otherwise None."""
    if not cwd or not os.path.isdir(cwd):
        return "gone", None
    code, top, _ = _git(cwd, "rev-parse", "--show-toplevel")
    if code != 0 or not top:
        return "none", None
    if _git(top, "rev-parse", "--verify", "--quiet", "HEAD")[0] != 0:
        return "empty", None
    if not dirty:
        return "ok", None
    lines = _changes(top)
    return "ok", None if lines is None else len(lines)


def changes(cwd, timeout=30):
    """Uncommitted changes in the folder's repo, one git call -> (repo root, lines as in `git -C <root> status --short`:
    paths from the root, as is, without git's C-quoting; without executors' working copies in .claude/worktrees; []
    means clean). Not git, or git answered with an error: None; a git timeout is an exception."""
    top = top_of(cwd)
    lines = _changes(cwd, timeout) if top else None
    return None if lines is None else (top, lines)


def top_of(cwd):
    """The root of the folder's working tree without calling git: the nearest folder above with .git (a directory or a
    file, as for a working copy and a submodule), otherwise None."""
    if not cwd or not os.path.isdir(cwd):
        return None
    p = os.path.realpath(cwd)
    while True:
        if os.path.exists(os.path.join(p, ".git")):
            return p
        up = os.path.dirname(p)
        if up == p:
            return None
        p = up


def _changes(cwd, timeout=30):
    # porcelain: paths from the repo root from any subfolder; -z: names as is (without -z git puts a space and a quote
    # in C-quotes, and ':/<path>' from the list would not match); top: the exclusion is from the root, not the folder
    code, out, _ = _git(cwd, "status", "--porcelain", "-z", "--", ":(top,exclude).claude/worktrees", raw=True,
                        timeout=timeout)
    if code:
        return None
    lines, parts = [], iter(out.split("\0"))
    for part in parts:
        if len(part) < 4:
            continue
        xy, path = part[:2], part[3:]
        if "R" in xy or "C" in xy:   # rename: the old path follows the new one
            path = f"{next(parts, '')} -> {path}"
        lines.append(f"{xy} {path}")
    return lines


def in_tree(path, top):
    """path is in the working tree top: the folder itself or under it, but not in executors' copies <top>/.claude/worktrees.
    Compared by the folders on disk (device and number, as os.path.samefile), not by strings: on a case-insensitive
    filesystem (macOS, Windows) a path in another case is the same folder, and realpath does not fix it (nor does normcase on macOS)."""
    def key(p):
        try:
            st = os.stat(p)
        except OSError:
            return None   # no folder: look higher
        return st.st_dev, st.st_ino
    want, own = key(top), key(os.path.join(top, ".claude", "worktrees"))
    p = os.path.abspath(path)
    while want:
        k = key(p)
        if k and k == own:
            return False
        if k == want:
            return True
        up = os.path.dirname(p)
        if up == p:
            return False
        p = up
    return False


def same(a, b):
    """The same folder on disk (device and number, as os.path.samefile): a path in another case or through a link is the
    same folder. If either folder does not exist, they are not the same."""
    try:
        x, y = os.stat(a), os.stat(b)
    except (OSError, TypeError, ValueError):
        return False
    return (x.st_dev, x.st_ino) == (y.st_dev, y.st_ino)


def root(path):
    """A ready copy for an executor `--here` (mini-hubs, slice 3) -> (folder, refusal reason): the folder exists and is the root
    of a git working copy (main or linked)."""
    if not path or not os.path.isdir(path):
        return None, NO_FOLDER.format(path=path)
    code, top, _ = _git(path, "rev-parse", "--show-toplevel")
    if code != 0 or not top:
        return None, NOT_GIT.format(path=path)
    if not same(top, path):
        return None, NOT_ROOT.format(path=path, top=top)
    return os.path.realpath(path), None


def opened(cwd, branch):
    """Where a branch is open: the working copy folder, in the repo of folder cwd, that it is in now, or None."""
    out = _git(cwd, "worktree", "list", "--porcelain")[1]
    where = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            where = line[9:]
        elif line == f"branch refs/heads/{branch}":
            return where
    return None


def to_main(path, branch, main):
    """Switch the mini-hub folder where the branch is open to the repo's main branch main (main_of), so that an executor
    can take the branch (`--branch`, slice 3). Only a clean one: uncommitted changes mean refusal, the folder stays as it was. -> (main, refusal reason)."""
    lines = _changes(path)
    if lines is None:
        return None, STATUS_SILENT
    if lines:
        return None, UNCOMMITTED.format(path=path, count=len(lines), branch=branch)
    if not main:
        return None, NO_MAIN.format(path=path)
    code, _, err = _git(path, "switch", "--quiet", main)
    if code != 0:   # for example, main is open in another copy of the repo (a mini-hub in a linked copy): it switches itself
        return None, NO_SWITCH.format(path=path, main=main, error=err[:200], branch=branch)
    return main, None


FETCH_TIMEOUT = 20   # s: fetching origin's main branch before a base is chosen: the network must not hold the core
FETCH_EVERY = 300    # s: forwardable fetches a repo this seldom (a lead's folder is checked every minute), failures too
_fetched = {}        # repo top -> when (monotonic) the last throttled fetch was tried


# a fast-forward checks out the files that differ: 23 s measured on a big repo; it runs in the core's background
# (core/daemon.forward_folders)
FORWARD_TIMEOUT = 300


def forwardable(cwd, names):
    """Can the core fast-forward a lead's folder (core/daemon.forward_folders)? -> (plan, None) or (None, why).
    Only HEAD on the repo's default branch or on task/<name> of one of names, with no commits of its own, and a clean
    folder (executors' copies in .claude/worktrees do not count); the target is origin/<default> when it is strictly
    ahead of the local branch, otherwise the local one, after a short fetch of origin (fetch_main; failing, what was
    already downloaded). why is a code for
    the hub's log: gone, not_git, detached, branch, no_main, level (nothing to do), own (commits of its own or
    diverged), dirty, git (git answered with an error). plan: top, branch, head, target, ref, count."""
    if not cwd or not os.path.isdir(cwd):
        return None, "gone"
    code, top, _ = _git(cwd, "rev-parse", "--show-toplevel")
    if code != 0 or not top:
        return None, "not_git"
    code, branch, _ = _git(top, "symbolic-ref", "--quiet", "--short", "HEAD")
    if code != 0 or not branch:
        return None, "detached"
    main = main_of(top)
    if not main:
        return None, "no_main"
    if branch != main and branch not in {f"task/{n}" for n in names}:
        return None, "branch"
    now = _sha(top, "HEAD")
    if not now:
        return None, "git"
    fetch_main(top, main, every=FETCH_EVERY)
    target, ref = main_tip(top, main)
    if not target:
        return None, "no_main"
    if target == now:
        return None, "level"
    if not ancestor(top, now, target):
        return None, "own"
    lines = _changes(top)
    if lines is None:
        return None, "git"
    if lines:
        return None, "dirty"
    code, count, _ = _git(top, "rev-list", "--count", f"{now}..{target}")
    return {"top": top, "branch": branch, "head": now, "target": target, "ref": ref,
            "count": int(count) if code == 0 and count.isdigit() else None}, None


def git_busy(top):
    """A git writes the index of the folder now (its index.lock exists): a fast-forward of a previous core process may
    still run after a restart. Not git, or git did not answer — not busy."""
    code, gitdir, _ = _git(top, "rev-parse", "--absolute-git-dir")
    return code == 0 and bool(gitdir) and os.path.exists(os.path.join(gitdir, "index.lock"))


def ancestor(top, a, b):
    """Commit a is an ancestor of b (or the same)."""
    return _git(top, "merge-base", "--is-ancestor", a, b)[0] == 0


def forward(plan, timeout=FORWARD_TIMEOUT):
    """Fast-forward the folder by the plan of forwardable -> (done, refusal reason, the merge step): None — git was not
    asked to merge (what changed since the plan holds it: the branch, the commit, a change in the folder); "refused" —
    `merge --ff-only` said no (it refuses before it writes a file: a lock, an untracked file in the way); "timeout" — git
    was killed mid-way, the checkout may be half done."""
    top = plan["top"]
    code, branch, _ = _git(top, "symbolic-ref", "--quiet", "--short", "HEAD")
    now = _git(top, "rev-parse", "--verify", "--quiet", "HEAD")[1]
    if code != 0 or branch != plan["branch"] or now != plan["head"]:
        return False, FORWARD_MOVED.format(path=top), None
    lines = _changes(top)
    if lines is None:
        return False, STATUS_SILENT, None
    if lines:
        return False, FORWARD_DIRTY.format(path=top, count=len(lines)), None
    # without the repo's hooks: a post-merge hook (`npm install`, a call to a service) is the owner's own `git pull`, not
    # the core's background move; --no-overwrite-ignore: an ignored file of the owner at a path the target tracks makes
    # git refuse instead of replacing it (git overwrites ignored files by default)
    try:
        code, _, err = _git(top, "-c", f"core.hooksPath={os.devnull}", "merge", "--ff-only", "--no-overwrite-ignore",
                            "--quiet", plan["target"], timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, FORWARD_TIMEOUT_TEXT.format(path=top, seconds=timeout), "timeout"
    if code != 0:
        return False, FORWARD_FAILED.format(path=top, error=err[:200]), "refused"
    return True, None, "done"


def main_of(cwd):
    """The main branch of the folder's repo: origin/HEAD, otherwise its own main, otherwise master; if none is found, None."""
    code, head, _ = _git(cwd, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if code == 0 and "/" in head:
        return head.split("/", 1)[1]
    return next((n for n in ("main", "master") if _git(cwd, "rev-parse", "--verify", "--quiet", f"refs/heads/{n}")[0]
                 == 0), None)


# checking out or removing a copy takes minutes for a big repo on a slow disk or one synced to the cloud (2026-10-21: 0.5 GB
# took 12 s idle and over 30 s while the disk was busy; removing a copy with node_modules on Windows takes minutes too); it
# runs in the core's background (core/daemon.run_tasks, clean_worktrees) and does not hold the tick
COPY_TIMEOUT = 600
DROP_TIMEOUT = 120   # removing an unfinished one is under the repo lock (core/daemon.start_prepare): other copies wait


def common_dir(cwd):
    """The shared .git folder of the repo (one for all its working copies) or None: git writes the copies' records there."""
    code, out, _ = _git(cwd, "rev-parse", "--git-common-dir")
    return os.path.realpath(os.path.join(cwd, out)) if code == 0 and out else None


HIDE_LINE = "/.claude/worktrees/"


def hide_copies(top):
    """The executors' copies <top>/.claude/worktrees are not the project's files: one line in the shared
    <git-common-dir>/info/exclude (local to the machine, not the committed .gitignore) keeps `git status` of the project
    from listing them. Written once, the line is not repeated; a failure leaves things as they were (it is only tidiness)."""
    common = common_dir(top)
    if not common:
        return
    path = os.path.join(common, "info", "exclude")
    try:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            text = ""
        if HIDE_LINE in text.splitlines():
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(("" if not text or text.endswith("\n") else "\n") + HIDE_LINE + "\n")
    except (OSError, UnicodeError):
        pass


def _registered(top, path):
    """The lines of a copy's record in `git worktree list --porcelain` (`locked initializing` etc.) or None."""
    want = os.path.realpath(path)
    for block in _git(top, "worktree", "list", "--porcelain")[1].split("\n\n"):
        lines = block.splitlines()
        if lines and lines[0].startswith("worktree ") and os.path.realpath(lines[0][9:]) == want:
            return lines
    return None


def _drop_half(top, path, branch=None, start=None):
    """A copy that git did not finish (killed by a timeout or a hub restart: locked "initializing"): remove its record
    and folder, and the branch if it was created here, otherwise the executor's name stays taken by a half copy. The
    branch only while it is on the commit it was created from (`update-ref -d` with the old value): do not touch a
    branch with foreign commits under the same name. Not `worktree prune`: .git may be shared with another machine.
    -> whether the folder and the record are removed."""
    try:
        if os.path.exists(path) or _registered(top, path) is not None:
            if os.path.isdir(path):
                platforms.current.cut_junctions(path)   # git below must not reach what a junction leads to
            _git(top, "worktree", "remove", "-f", "-f", path, timeout=DROP_TIMEOUT)
            if os.path.exists(path):
                platforms.current.rmtree(path)
        if branch and start:
            _git(top, "update-ref", "-d", f"refs/heads/{branch}", start)
        return not os.path.exists(path) and _registered(top, path) is None
    except (OSError, subprocess.SubprocessError):
        return False   # cleanup is best effort: we will state the refusal reason anyway


def fetch_main(top, main, every=0):
    """Bring origin's main branch up to date before a base is chosen (main_tip reads what is downloaded) -> None when it
    is, or when there is nothing to fetch (the repo has no origin: our own hub's working repo has none), otherwise why not
    (no network, no access, no such branch, FETCH_TIMEOUT): then the base is origin as git saw it at the last fetch.
    Quiet git (no prompt, no window, its own session: codestore.git). every: not more often than that per repo, a failed
    try counts too."""
    if _git(top, "remote", "get-url", "origin")[0] != 0:
        return None
    now = time.monotonic()
    if every:
        if now - _fetched.get(top, -every) < every:
            return None
        _fetched[top] = now
    try:
        done = codestore.git("-C", top, "fetch", "--quiet", "--no-tags", "origin",
                             f"+refs/heads/{main}:refs/remotes/origin/{main}", timeout=FETCH_TIMEOUT)
    except RuntimeError as e:
        return str(e)[:200]
    return None if done.returncode == 0 else codestore.said(done)[:200]


def _start(top, from_main=False, info=None):
    """The commit to create the executor's new branch from. By default HEAD of the main copy: a session may be on its own
    branch on purpose (an epic, a long migration) and expects executors to start from it. from_main (`hub task run
    --from-main`) is the repo's main branch (main_of), even if the main copy is on a foreign branch: the local one, and if
    it is strictly behind origin, origin's (our own unpushed commits are not lost); if there is no main, HEAD. A repo
    without commits: None (git will create it itself)."""
    main = main_of(top) if from_main else None
    why = fetch_main(top, main) if main else None
    if why and info is not None:
        info["fetch"] = why   # for the answer: _finish puts it among the checks
    tip = main_tip(top, main)[0] if main else None
    return tip or _sha(top, "HEAD")


def _sha(top, ref):
    code, out, _ = _git(top, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
    return out if code == 0 and out else None


def main_tip(top, main):
    """The tip of the repo's main branch main -> (commit, ref) or (None, None): the local one, and origin's when the
    local one is strictly behind it (our own unpushed commits are not lost); origin's when there is no local one. No
    fetch here: origin as git saw it at the last one (the callers fetch first, fetch_main). The executor's base (`--from-main`) and the target of the core's
    fast-forward of a lead's folder (forwardable) — one rule."""
    local, remote = _sha(top, f"refs/heads/{main}"), _sha(top, f"refs/remotes/origin/{main}")
    if local and remote and local != remote and ancestor(top, local, remote):
        return remote, f"origin/{main}"
    if local:
        return local, main
    return (remote, f"origin/{main}") if remote else (None, None)


def base_of(top, from_main=False):
    """The base of an executor's copy: the branch the
    executor merges into — the one its task/<name> starts from. By default the branch the raiser's folder is on (an
    executor raised from an experiment branch merges into it, not into main with foreign commits); from_main, or HEAD
    detached — the repo's main branch (main_of). No such local branch (only origin/HEAD) — empty: no merge check.
    -> {"base": branch, "base_sha": its commit}."""
    base = ("" if from_main else _git(top, "branch", "--show-current")[1]) or main_of(top) or ""
    sha = _git(top, "rev-parse", "--verify", "--quiet", f"refs/heads/{base}")[1] if base else ""
    return {"base": base if sha else "", "base_sha": sha}


def _pushed(repo, branch, timeout=30):
    """The branch went to a remote (its own branch there contains its tip): handing in through an MR is not the merge
    check's business. Only its own branch on the remote: --contains over all remote branches of a big repo takes seconds."""
    code, out, _ = _git(repo, "for-each-ref", "--contains", f"refs/heads/{branch}", "--format=%(refname)",
                        f"refs/remotes/*/{branch}", timeout=timeout)
    return code == 0 and bool(out)


def merged(info, timeout=10):
    """Whether the executor handed in (experiment, slice C): the branch of its copy is merged into the base
    (info["base"]) -> (commits ahead of the base, uncommitted in the copy, a git error or None). Handed in — (0, False,
    None); the branch went to a remote (MR) — its commits do not count; an error (no base, a lock) is not a fact of
    not being merged."""
    repo, branch, base, path = info.get("repo"), info.get("branch"), info.get("base"), info.get("path")
    try:
        code, out, err = _git(repo, "rev-list", "--count", f"refs/heads/{base}..refs/heads/{branch}", timeout=timeout)
        if code != 0 or not out.isdigit():
            return None, None, err[:200] or "git rev-list did not answer"
        if out != "0" and _pushed(repo, branch, timeout):
            out = "0"
        lines = _changes(path, timeout) if path and os.path.isdir(path) else []
    except subprocess.TimeoutExpired:
        return None, None, "git did not answer in time"
    if lines is None:
        return None, None, "git status did not answer in the copy"
    # only compiled bytecode untracked (a test run's __pycache__) is clean, as the cleanup counts it (remove)
    dirty = bool(lines) and _bytecode_junk(path, "\0".join(lines)) is None
    return int(out), dirty, None


def prepare(cwd, name, branch=None, from_main=False, with_base=False, space=None, owner=None, foreign=None):
    """Branch task/<name> from HEAD of the main copy, with from_main from the repo's main branch (_start); branch: on this
    existing branch (own or from origin; `--branch`, slice 3): the copy does not create it and cleanup does not delete it (keep_branch).
    with_base — remember the copy's base (experiment, slice C: only a hub on Codex asks; without it the copy is as in main).
    Several hubs on one machine (core/instance.machine): space — the instance's namespace (copy_path, task_branch);
    owner — this hub ({"state", "hub"}): the copy gets its mark, an existing copy marked by another hub is refused;
    foreign(path) — the command of another hub whose live session works there (refused too). The caller holds the
    repo's lock (repo_lock) while owner is set."""
    code, top, _ = _git(cwd, "rev-parse", "--show-toplevel")
    if code != 0 or not top:
        return None, None, None
    top = os.path.realpath(top)
    rel = os.path.relpath(os.path.realpath(cwd), top)
    path = copy_path(top, name, space)
    info = {"path": path, "branch": branch or task_branch(name, space), "repo": top, "checks": []}
    if os.path.exists(path):
        why, hub = _foreign(path, owner, foreign)
        if why:   # not ours: neither taken nor redone (a half copy of another hub is its own business)
            return None, info, (FOREIGN_COPY if why == "mark" else FOREIGN_LIVE).format(path=path, hub=hub)
    if from_main and not branch:
        info["from_main"] = True   # restoring the copy (restore) is also from the main branch
    if with_base and not branch:
        info.update(base_of(top, from_main))   # where it merges into (experiment, slice C: core/executors.on_unmerged)
    if branch:
        info["keep_branch"] = True
        if not any(_git(top, "rev-parse", "--verify", "--quiet", ref)[0] == 0
                   for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}")):
            return None, info, NO_BRANCH.format(branch=branch, top=top)
    branch = info["branch"]
    # the previous git was killed halfway (a hub restart in the middle of checkout): the record is locked "initializing"; redo it
    if "locked initializing" in (_registered(top, path) or []):
        _drop_half(top, path)   # keeping the branch: its commit is HEAD, add will take it
    made = None   # the copy and branch were created here: if it does not reach the end, remove (only our own: the branch without foreign commits)
    if not os.path.exists(path):
        hide_copies(top)
        # a branch from origin without a local one: git creates its own, tracking it
        has_branch = info.get("keep_branch") or _git(top, "rev-parse", "--verify", "--quiet", branch)[0] == 0
        start = None if has_branch else _start(top, from_main, info)
        args = ["worktree", "add", path, branch] if has_branch else ["worktree", "add", "-b", branch, path] + ([start] if start else [])
        try:
            code, _, err = _git(top, *args, timeout=COPY_TIMEOUT)
        except subprocess.TimeoutExpired:
            code, err = None, ""
        if code != 0:
            gone = _drop_half(top, path, None if has_branch else branch, start)
            if code is None:
                left = HALF_DROPPED if gone else HALF_LEFT.format(path=path)
                return None, info, ADD_TIMEOUT.format(minutes=COPY_TIMEOUT // 60, left=left)
            return None, info, f"git worktree add: {err[:300]}"
        made = (None if has_branch else branch, start)
    # 2026-10-23: preparation that failed after `worktree add` (a git timeout) left the copy and branch without a session;
    # cleanup did not see it, and a retry under the same name went into the half copy
    try:
        own, info, err = _finish(top, rel, path, info)
    except BaseException:   # any failure: a copy without a session is worse than none
        if made:
            _drop_half(top, path, *made)
        raise
    if err and made:
        err += "; " + (COPY_DROPPED if _drop_half(top, path, *made) else COPY_LEFT.format(path=path))
    if own:
        _mark(path, owner)
    return own, info, err


def _finish(top, rel, path, info):
    """A checked-out copy: files from .worktreeinclude, checks, the MCP choice -> (executor folder, details, error)."""
    branch = info["branch"]
    try:
        copied, missed = include(top, path), None
    except (OSError, subprocess.SubprocessError) as e:   # the copy works without them: report it, do not fail the preparation
        copied, missed = 0, NOT_INCLUDED.format(error=str(e)[:250])
    listed = _git(top, "worktree", "list", "--porcelain")[1]
    checks = [
        (CHECK_TREE, _git(path, "rev-parse", "--is-inside-work-tree")[1] == "true"),
        (CHECK_ROOT, os.path.realpath(_git(path, "rev-parse", "--show-toplevel")[1] or "/") ==
         os.path.realpath(path)),
        (CHECK_BRANCH.format(branch=branch), _git(path, "branch", "--show-current")[1] == branch),
        (CHECK_LISTED, f"worktree {os.path.realpath(path)}" in
         {f"worktree {os.path.realpath(x[9:])}" for x in listed.splitlines() if x.startswith("worktree ")}),
        (CHECK_CLEAN, _git(path, "status", "--porcelain")[1] == ""),
    ]
    info["checks"] = [[what, ok] for what, ok in checks]
    # a copy with a commit (git gives a copy for a repo without commits too): the direction in it is a mini-hub from birth (core/minihub.born)
    info["commits"] = _git(path, "rev-parse", "--verify", "--quiet", "HEAD")[0] == 0
    if copied:
        info["checks"].append([CHECK_INCLUDED.format(count=copied), True])
    if missed:
        info["checks"].append([missed, False])
    if info.get("fetch"):
        info["checks"].append([FETCH_FAILED.format(error=info.pop("fetch")), False])
    bad = [what for what, ok in checks if not ok]
    if bad:
        return None, info, CHECK_FAILED.format(failed=coretext.join(*[x for what in bad for x in (", ", what)][1:]))
    own = os.path.normpath(os.path.join(path, rel))
    if not os.path.isdir(own):   # neither the commit nor .worktreeinclude brought the session's folder
        if _tracked(top, rel):   # in git, but not in the commit the copy starts from (--from-main, --branch)
            return None, info, NO_SUBFOLDER.format(rel=rel)
        # the folder is ignored by git or untracked, so a copy does not have it (an agent working in an `output/`
        # folder): the executor works in the root of the copy and gets the absolute path of the source folder in its brief
        info["source_cwd"] = os.path.normpath(os.path.join(top, rel))
        own, rel = path, os.curdir   # the MCP choice too: of the root, where it works
    if _mcp_settings(top, rel, path):
        info["checks"].append([CHECK_MCP, True])
    return own, info, None


def _tracked(top, rel):
    """Whether git tracks a file in the folder rel of the main copy; git failed — tracked (the copy is refused, as before)."""
    code, out, _ = _git(top, "ls-files", "-z", "--", ":(literal)" + rel.replace(os.sep, "/"), raw=True)
    return code != 0 or bool(out)


def _mcp_settings(top, rel, path):
    """The MCP choice: for the repo root and for the session's folder in it, if it is deeper (Claude Code reads its .claude)."""
    pairs = {(top, path), (os.path.normpath(os.path.join(top, rel)), os.path.normpath(os.path.join(path, rel)))}
    return any([mcp_settings(src, dst) for src, dst in pairs])


def restore(info, owner=None, foreign=None):
    """Put the working copy of a session being revived back in its old place (revive failed: the copy was removed when the
    session left): from its branch if it is still there, otherwise a new branch from HEAD of the repo, with from_main from
    the main branch (_start). The path is the same: the Claude Code conversation lies by the folder's path, and `--resume`
    will find it. -> (whether it is ready, what was done)."""
    path, repo, branch = info.get("path"), info.get("repo"), info.get("branch")
    if path and os.path.isdir(path):
        why, hub = _foreign(path, owner, foreign)   # several hubs on one machine: not a copy another hub holds now
        if why:
            return False, (FOREIGN_COPY if why == "mark" else FOREIGN_LIVE).format(path=path, hub=hub)
        _mark(path, owner)   # ours: a copy from before marks gets one, as in prepare
        return True, IN_PLACE
    if not path or not repo or not branch or not os.path.isdir(repo):
        return False, NO_REPO
    # not `worktree prune`: it drops the records of every copy whose folder is missing here (a .git shared between
    # machines, a disk that is not mounted), not only this one. Only the stale record at this path goes; not `add -f`:
    # it would also check out a branch that a live copy already holds (two copies on one branch)
    _git(repo, "worktree", "remove", "-f", "-f", path)
    # the `--branch` branch (keep_branch) is the same one, own or from origin: do not create a new one
    has_branch = info.get("keep_branch") or _git(repo, "rev-parse", "--verify", "--quiet", branch)[0] == 0
    start = None if has_branch else _start(repo, info.get("from_main"))
    args = ["worktree", "add", path, branch] if has_branch else ["worktree", "add", "-b", branch, path] + ([start] if start else [])
    code, _, err = _git(repo, *args)
    if code != 0:
        return False, f"git worktree add: {err[:200]}"
    include(repo, path)
    mcp_settings(repo, path)   # the details have no session folder, only the repo root
    _mark(path, owner)   # several hubs on one machine: owner — this hub (the caller holds repo_lock)
    if has_branch:
        return True, RESTORED_FROM.format(branch=branch)
    return True, RESTORED_NEW.format(base=FROM_MAIN if info.get("from_main") else FROM_HEAD, branch=branch)


def remove(info, owner=None, foreign=None):
    """Remove the working copy of a deleted session (2026-10-01: worktree cleanup on deletion was done by hand).
    We do not lose work: with uncommitted changes the copy is left alone; a branch with commits merged nowhere stays
    (`git branch -d` refuses by itself). Several hubs on one machine: a copy marked by another hub, or one a live
    session of another hub works in (owner, foreign — as in prepare), is left alone. -> (whether it is removed, what
    was done); None instead of the answer: the copy is an empty shell whose folder cannot be removed right now (a program
    stands in it), nothing is lost, the caller asks again later."""
    path, repo, branch = info.get("path"), info.get("repo"), info.get("branch")
    if not path or not repo:
        return True, ALREADY_GONE
    if not os.path.isdir(path):   # the folder was deleted by hand: git still lists the copy and holds its branch
        # not `worktree prune` (as in restore): it would drop the records of other copies whose folders are missing here
        why, hub = _foreign(path, owner, foreign)   # a live session of another hub still works at this path
        if why:
            return False, FOREIGN_KEPT.format(path=path, hub=hub)
        if not os.path.isdir(repo):
            return True, ALREADY_GONE
        code, _, err = _git(repo, "worktree", "remove", "-f", "-f", path, english=True)
        if code != 0:
            listed = _git(repo, "worktree", "list", "--porcelain")[1].splitlines()
            if f"worktree {os.path.realpath(path)}" not in {f"worktree {os.path.realpath(x[9:])}" for x in listed
                                                              if x.startswith("worktree ")}:
                return True, ALREADY_GONE   # no record at this path: nothing to clean (a repeated call lands here)
            return False, "git worktree remove: " + err   # the record is there and stays: the whole reason
        return _drop_branch(info, _merged_into_base(info))
    why, hub = _foreign(path, owner, foreign)
    if why:
        return False, FOREIGN_KEPT.format(path=path, hub=hub)
    if not os.path.exists(os.path.join(path, ".git")):
        stuck = []
        if _erase_empty(path, stuck):   # git took everything but the root (or only the files): a shell, nothing to lose
            _git(repo, "worktree", "remove", "-f", "-f", path)   # the folder is gone: git removes the one record
            return _drop_branch(info)
        if stuck and _only_folders(path):   # a shell that does not go now (a program stands in it, Windows): try again later
            return None, SHELL_LOCKED.format(path=path, error=stuck[0])
        # the link to the repo is cut (the cleanup was killed in the middle of worktree remove, or the session did it itself): whose
        # work it is can't be told from the folder, and status would answer for the parent repo: do not erase
        return False, NO_DOT_GIT.format(path=path)
    code, top, _ = _git(path, "rev-parse", "--show-toplevel")
    if code != 0 or os.path.realpath(top) != os.path.realpath(path):   # otherwise status would answer for the parent repo
        return False, NOT_A_COPY
    code, dirty, _ = _git(path, "status", "--porcelain", "-z", raw=True)
    if code != 0:
        return False, NOT_A_COPY
    junk = _bytecode_junk(path, dirty)
    if junk is None:
        return False, KEPT_DIRTY.format(path=path)
    for name in junk:   # git worktree remove refuses untracked files; only compiled bytecode is deleted here
        try:
            os.remove(platforms.current.long_path(name))
        except OSError:
            return False, KEPT_DIRTY.format(path=path)
    into_base = _merged_into_base(info)
    try:   # git worktree remove (2.33, Windows) walks through a junction and erases its target (a copy's node_modules
        platforms.current.cut_junctions(path)   # linked to the main checkout's): the junctions go first, without entering them
    except OSError as e:
        return False, KEPT_JUNCTION.format(path=path, error=e)
    # a big copy (node_modules, Windows) takes minutes to delete: git killed halfway leaves it half deleted and without
    # .git, and then it can't be told from a session's work. Cleanliness was just checked: finishing without git is safe
    try:
        code, _, err = _git(repo, "worktree", "remove", path, english=True, timeout=COPY_TIMEOUT)
    except subprocess.TimeoutExpired:
        code, err = 1, "timed out"
    gone = not os.path.exists(os.path.join(path, ".git"))   # git managed to cut the link and dropped the rest
    if code == 0 and os.path.lexists(path):   # git can answer 0 and leave the folder (Git for Windows 2.56: junctions)
        code, err = 1, "git left the folder"   # finished below as after a failed git; the report goes by the folder
    if code != 0 and ("too long" in err.lower() or err == "timed out" or gone):
        stuck = []
        if _wipe(repo, path):
            code, err = 0, ""
        elif os.path.isdir(path) and _only_folders(path):   # the files went, the root stays (Windows: a program stands in it)
            if _erase_empty(path, stuck):   # the program has just left
                _git(repo, "worktree", "remove", "-f", "-f", path)
                code, err = 0, ""
            else:   # what git said and what the folder itself answers now
                return None, SHELL_LOCKED.format(path=path, error="; ".join(x for x in (err, *stuck[:1]) if x) or "timed out")
        else:
            code, err = 1, NOT_WIPED.format(path=path, error=err)
    if code != 0:
        return False, "git worktree remove: " + err   # the whole reason: it names what git could not delete
    return _drop_branch(info, into_base)


def space_dir(top, space):
    """The folder of an instance's copies in a repo: <top>/.claude/worktrees/@<instance>."""
    return os.path.join(top, ".claude", "worktrees", f"@{space}")


def _space_records(top, space):
    """[(path, branch or None)] of the repo's registered copies under the instance's folder, from `git worktree list`
    (a record whose folder is gone is in it too)."""
    base, out = os.path.realpath(space_dir(top, space)), []
    for block in _git(top, "worktree", "list", "--porcelain")[1].split("\n\n"):
        lines = block.splitlines()
        if not lines or not lines[0].startswith("worktree "):
            continue
        path = lines[0][9:]
        if os.path.realpath(os.path.dirname(path)) == base:
            ref = next((x[7:] for x in lines if x.startswith("branch ")), "")
            out.append((path, ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else None))
    return out


def drop_space(space, infos, owner, wait=60):
    """Take an instance's working copies out of the repos when the hub is removed (several hubs on one machine, slice M2;
    `hub uninstall` of the instance, core/uninstall.py): infos are the `worktree` details of its sessions (their repos
    and copies), and the repos' own records under <repo>/.claude/worktrees/@<space> are added (a copy of a session the
    state no longer holds; its branch is not ours unless it is task/@<space>/*, so it is kept). Each git step of a repo
    takes the repo's file lock for itself (waits at most `wait` s; a removal can take minutes, the other hubs' git must
    not wait for all of them): each copy goes by `remove` — one with uncommitted changes, another hub's mark or no link
    to its repo stays, as the core's own cleanup leaves it — then the branches task/@<space>/* that are merged
    (`branch -d`); the folder @<space> goes when empty. A copy only in the instance's folder; another path in the
    details is never touched. A repo whose lock is not given in time: its remaining copies stay.
    -> {"removed": [path], "kept": [(path, why)], "branches": [(repo, branch)], "busy": [repo]}"""
    out = {"removed": [], "kept": [], "branches": [], "busy": []}
    repos = {}
    for info in infos:
        repo, path = info.get("repo"), info.get("path")
        if isinstance(repo, str) and isinstance(path, str) and repo and path and os.path.isdir(repo):
            repos.setdefault(os.path.realpath(repo), []).append(info)
    for top, got in repos.items():
        base = os.path.realpath(space_dir(top, space))
        mine = {os.path.realpath(i["path"]): i for i in got if os.path.realpath(os.path.dirname(i["path"])) == base}
        try:
            with repo_lock(lock_target(top, owner), wait):
                for path, branch in _space_records(top, space):
                    own = bool(branch) and branch.startswith(f"task/@{space}/")
                    mine.setdefault(os.path.realpath(path), {"path": path, "repo": top, "branch": branch,
                                                             **({} if own else {"keep_branch": True})})
            for _, info in sorted(mine.items()):
                path = info["path"]   # as the session knew it: what the messages of git name
                with repo_lock(lock_target(top, owner), wait):
                    if not os.path.isdir(path):   # a record of a folder that is gone (inside the repo itself): this one
                        code, _, err = _git(top, "worktree", "remove", "-f", "-f", path)
                        (out["removed"].append(path) if code == 0 else out["kept"].append((path, err[:200])))
                        continue
                    ok, why = remove({**info, "repo": top}, owner=owner)
                    if ok:
                        out["removed"].append(path)
                    else:
                        out["kept"].append((path, str(why)))
            with repo_lock(lock_target(top, owner), wait):
                try:
                    os.rmdir(base)   # empty: the last copy went
                except OSError:
                    pass
                held = {b for _, b in _space_records(top, space)}
                names = _git(top, "for-each-ref", "--format=%(refname)", f"refs/heads/task/@{space}/")[1]
                for ref in names.splitlines():
                    branch = ref[len("refs/heads/"):]   # the full ref: a tag of the same name makes the short one ambiguous
                    if branch not in held and _git(top, "branch", "-d", branch)[0] != 0:
                        out["branches"].append((top, branch))
        except TimeoutError:
            out["busy"].append(top)
        except (OSError, subprocess.SubprocessError) as e:   # one repo's failure does not stop the others
            out["kept"].append((top, f"{type(e).__name__}: {e}"[:200]))
    return out


def _bytecode_junk(path, porcelain):
    """The *.pyc files of a copy whose only untracked content is compiled bytecode inside `__pycache__` folders (a run
    of the project's tests leaves them when the repo does not ignore them): the copy counts as clean. Any other
    change, any other untracked file, or a `__pycache__` holding something but *.pyc -> None (the work stays)."""
    found = []
    for entry in porcelain.split("\0"):
        if not entry:
            continue
        if not entry.startswith("?? "):
            return None
        full = os.path.normpath(os.path.join(path, entry[3:]))
        if not entry.endswith("/"):
            return None
        try:
            for root, dirs, files in os.walk(full, onerror=_raise):
                if any(os.path.islink(os.path.join(root, d)) for d in dirs):   # os.walk does not enter a link: its content is unseen
                    return None
                for name in files:
                    if not name.endswith(".pyc") or os.path.basename(root) != "__pycache__":
                        return None
                    found.append(os.path.join(root, name))
        except OSError:   # a folder that can't be read is unseen content too
            return None
    return found


def _raise(error):
    raise error


def _only_folders(path):
    """Whether the folder holds nothing but (nested) folders, no link among them: the shell _erase_empty would erase."""
    if os.path.islink(path) or os.path.isjunction(path) or os.path.ismount(path):
        return False
    try:
        with os.scandir(platforms.current.long_path(path)) as it:
            entries = list(it)
        return all(not (e.is_symlink() or e.is_junction()) and e.is_dir(follow_symlinks=False)
                   and _only_folders(os.path.join(path, e.name)) for e in entries)
    except OSError:
        return False


def _erase_empty(path, errors=None):
    """Erase the folder if it holds nothing but (nested) empty folders, bottom-up with rmdir: rmdir refuses by itself on
    a folder that is not empty, so a file that appears meanwhile is never erased. Empty folders next to a file may be
    erased before the walk meets it; the file and its folder stay whole. A link, a junction and a mount point count as
    content (not ours to judge), and the folder itself being one is a refusal; on NTFS a volume mounted into a folder
    is caught by isjunction too. Not os.walk: on Windows it walks into junctions. The first error of an erase that was
    refused goes into `errors` (a list, when given). -> whether the folder is gone."""
    # the checks go by the plain path: ntpath.ismount says False for anything with the \\?\ prefix but a drive root
    if os.path.islink(path) or os.path.isjunction(path) or os.path.ismount(path):
        return False
    long = platforms.current.long_path(path)
    try:
        with os.scandir(long) as it:
            entries = list(it)
        for e in entries:
            if e.is_symlink() or e.is_junction() or not e.is_dir(follow_symlinks=False) \
                    or not _erase_empty(os.path.join(path, e.name), errors):
                return False
        platforms.current.rmdir(path)
    except OSError as e:
        if errors is not None and not errors:
            errors.append(str(e))
        return False   # cannot look inside or erase: do not guess
    return True


def _wipe(repo, path):
    """Erase the copy's folder and remove its record in .git. Only its own: `worktree prune` would remove the records of
    neighbouring abandoned ones too. -> whether it is erased."""
    if platforms.current.rmtree(path):
        return False
    _git(repo, "worktree", "remove", "-f", "-f", path)   # the folder is gone: git removes the one record
    return True


def _merged_into_base(info):
    """The copy's branch is merged into the copy's base (experiment, slice C): it goes even when HEAD of the folder has
    moved to another branch (`-d` checks HEAD); not merged: the copy goes, `git branch -d` keeps the branch and its commits."""
    branch, repo = info.get("branch"), info.get("repo")
    if not branch or info.get("keep_branch") or not info.get("base"):
        return False
    code, out, _ = _git(repo, "rev-list", "--count", f"refs/heads/{info['base']}..refs/heads/{branch}")
    return code == 0 and out == "0"


def _drop_branch(info, into_base=False):
    """The copy is gone: remove branch task/<name> if it has no commits merged nowhere. -> the result of remove."""
    branch, repo = info.get("branch"), info.get("repo")
    if branch and not info.get("keep_branch"):   # the `--branch` branch was not created by the copy, and cleanup should not delete it
        # merged into its base (slice C): -D — `-d` checks HEAD of the folder, which may be on another branch
        code, _, _ = _git(repo, "branch", "-D" if into_base else "-d", branch)
        if code != 0:
            return True, KEPT_COMMITS.format(branch=branch)
    return True, KEPT_BRANCH if info.get("keep_branch") else REMOVED
