"""A path as a session wrote it -> a file on disk (a link once led to
.../proj-wt2/proj-wt2/modules/files/server.py). Sessions write paths relative to their own folder, to the repo root
(when working deeper) or to a neighbouring folder (`proj-wt2/...`), so simply gluing it to the session folder does not work.

Order: as written; from the session folder; from its parents (up to "/" and the users folder, not higher); for an
already doubled absolute path (old links), without the repeat. The first existing one is the answer; if none, a search by name in the
session's repo (search). Stdlib, shared by "Files" and Telegram.
"""
import os
import re
import subprocess
import time

STOP = {"/", os.path.dirname(os.path.expanduser("~"))}
# git in a folder we did not choose (a link named it, a foreign file landed in it): never run the command
# of the found repo's fsmonitor and never take a bare repo found by walking up the folders
GIT = ["git", "-c", "core.fsmonitor=false", "-c", "safe.bareRepository=explicit"]


def _undoubled(path):
    """.../a/b/a/b/x -> .../a/b/x: remove a repeat of consecutive parts (the first one found)."""
    parts = path.split(os.sep)
    for n in range(1, len(parts) // 2 + 1):
        for i in range(n, len(parts) - n + 1):
            if parts[i - n:i] == parts[i:i + n] and all(parts[i:i + n]):
                yield os.sep.join(parts[:i] + parts[i + n:])


def candidates(path, cwd=None):
    path = os.path.expanduser(path or "")
    if os.path.isabs(path):
        yield os.path.normpath(path)
        yield from _undoubled(os.path.normpath(path))
        return
    if not cwd or not path:
        return
    here = os.path.normpath(os.path.expanduser(cwd))
    while here not in STOP:
        yield os.path.normpath(os.path.join(here, path))
        if os.path.dirname(here) == here:   # a disk root that is not "/" (Windows, "D:\\"): nowhere higher to go
            return
        here = os.path.dirname(here)


def find(path, cwd=None, exists=os.path.exists, extra=()):
    """First existing variant of the path or None; if none is found, directly in the extra folders (hub settings: a `hub.json` from
    a session reply is ~/.config/hub-core/hub.json, not a same-named file in the repo), then a search by name
    inside the session's repo."""
    hit = next((p for p in candidates(path, cwd) if exists(p)), None)
    if hit or not path or os.path.isabs(os.path.expanduser(path)):
        return hit
    hit = next((p for p in (os.path.join(d, path) for d in extra if d) if exists(p)), None)
    if not hit and cwd:
        # the session is recorded at the repo root but works in its worktree (.claude/worktrees/<branch>): a path relative to it; if
        # there are several, the freshest file (30.09: a plan from .harness did not open)
        found = [p for p in (os.path.join(w, path) for w in worktrees(cwd)) if exists(p)]
        hit = max(found, key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0) if found else None
    return hit or (search(path, cwd) if cwd else None)


def resolve(path, cwd=None, hint=None, extra=(), projects=(), exists=os.path.exists):
    """Where a path from session text leads: a file on disk or None (a link only for what
    opens). hint is a file the session itself read and whose tail this path is: only while it exists (review of 11.10:
    164 links led to deleted worktrees). Then as find; if the session has no such relative path, in the hub projects' repos
    (the session writes about a file of a neighbouring repo), if there is exactly one there."""
    if not path:
        return None
    if hint and exists(hint):
        return hint
    absolute = os.path.isabs(os.path.expanduser(path))
    if not absolute and not cwd:
        return None
    hit = find(path, cwd, exists, extra)
    if hit or absolute:
        return hit
    here = os.path.realpath(_top(os.path.expanduser(cwd)) or cwd) if os.path.isdir(os.path.expanduser(cwd)) else None
    found = {s for s in (search(path, p) for p in projects if p and os.path.realpath(p) != here) if s}
    return found.pop() if len(found) == 1 else None


# a domain without http (`example.com`, `example.de/icons/set/x`) is a link to a site, not to a file (`hello.com`
# without http was highlighted as a file). Common zones at once; others (`.de`, `.info`, `.link`) coincide with fields in code
# (`translations.de`, `session.info`): only with a digit or hyphen in the name, with www., or with a path after; capitals are code (`oauthClients.info`)
WEB_ZONES = "com|org|net|io|ai|app|dev|ru"
WEB_RARE = ("de|fr|it|es|nl|pl|uk|us|eu|co|me|info|biz|tv|cc|xyz|site|online|store|tech|cloud|page|link|ua|by|kz|jp|"
            "cn|in|br|ca|au|ch|se|no|fi|cz|at|be|pt|gg|so")
WEB = re.compile(rf"((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+)({WEB_ZONES}|{WEB_RARE})(/[^\s]*)?")


def web_address(text):
    """https://... for a domain without http, or None."""
    m = WEB.fullmatch(text or "")
    if not m or re.fullmatch(WEB_RARE, m.group(2)) and not (re.search(r"[\d-]", m.group(1)) or text.startswith("www.") or m.group(3)):
        return None
    return "https://" + text


def worktrees(cwd):
    """Git worktree folders of the repo that contains cwd (except cwd itself)."""
    try:
        out = subprocess.run([*GIT, "-C", cwd, "worktree", "list", "--porcelain"], capture_output=True, text=True,
                             timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    here = os.path.realpath(cwd)
    return [line[9:] for line in out.stdout.splitlines()
            if line.startswith("worktree ") and os.path.realpath(line[9:]) != here] if out.returncode == 0 else []


# A session writes `brief.md`, but the file is docs/brief.md. Look for the path as a tail of the repo's file paths (git ls-files, otherwise
# a folder walk with a limit); if several, the shortest: closer to the root is more likely the one meant.
SEARCH_TTL = 30
SEARCH_MAX = 50_000
SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", ".nuxt"}
_listed = {}  # root -> (when, [paths from the root])


_tops = {}  # folder -> (when, repo root or None)


def _top(cwd):
    hit = _tops.get(cwd)
    if hit and time.monotonic() - hit[0] < SEARCH_TTL:
        return hit[1]
    _tops[cwd] = (time.monotonic(), _git_top(cwd))
    return _tops[cwd][1]


def _git_top(cwd):
    try:
        out = subprocess.run([*GIT, "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _listing(root, git):
    hit = _listed.get(root)
    if hit and time.monotonic() - hit[0] < SEARCH_TTL:
        return hit[1]
    files = []
    if git:
        try:
            out = subprocess.run([*GIT, "-C", root, "ls-files", "-co", "--exclude-standard"], capture_output=True,
                                 text=True, timeout=10).stdout
            files = out.splitlines()[:SEARCH_MAX]
        except (OSError, subprocess.SubprocessError):
            files = []
    else:
        for d, dirs, names in os.walk(root):
            dirs[:] = [x for x in dirs if x not in SKIP and not x.startswith(".")]
            files += [os.path.relpath(os.path.join(d, n), root).replace(os.sep, "/") for n in names]   # as git does
            if len(files) >= SEARCH_MAX:
                break
    _listed[root] = (time.monotonic(), files)
    return files


def search(path, cwd):
    cwd = os.path.normpath(os.path.expanduser(cwd))
    if not os.path.isdir(cwd):
        return None
    top = _top(cwd)
    root = top or cwd
    tail = os.path.normpath(path).replace(os.sep, "/")  # "./a" -> "a"; ".env" stays; listing paths use "/"
    found = [f for f in _listing(root, bool(top)) if f == tail or f.endswith("/" + tail)]
    if not found:
        return None
    best = os.path.normpath(os.path.join(root, min(found, key=lambda f: (f.count("/"), len(f), f))))
    return best if os.path.exists(best) else None
