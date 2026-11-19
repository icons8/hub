"""The environment folders of the one-line install (spec §4.3, §6.2–6.3): <data>/shells/<runtime>-<flavor>, shared by
the hubs of a user, each hub on its own through its pointer <state>/app. A folder there stays while
- a hub of the machine (this one, the main one, the registry's) points at it;
- a replacement names it (layers/shell-update.json of any of them): the new one until it is written up, the old one —
  the way back — until the next replacement;
- a program of it runs (Linux: /proc; macOS: lsof; Windows: the folder cannot be renamed while one does);
- it is being unpacked (a .part younger than a day).
The rest goes: renamed aside first (".<name>.gone": a removal cut short leaves nothing that looks like an environment),
then removed with its read-only modes taken off. Only names the install makes are touched."""
import contextlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time

import platforms

NEXT = "env-next.json"   # <state>/layers: a newer environment the install line left for the update
NEXT_DAYS = 30
NAME = re.compile(r"(\d+-(?:team|open)(?:-\d+)?)(\.part|\.broken-\d+)?\Z")
GONE = re.compile(r"\.(.+)\.gone(?:-\d+)?\Z")
PART_DAYS = 1


def shells_of(state):
    """The shells folder of the environment this hub runs on, or None (not a hub of the one-line install)."""
    from core import shellnext
    env = shellnext.folder_env(state)
    return os.path.dirname(env) if env else None


def _norm(path):
    return os.path.normcase(os.path.realpath(path))


def _json(path):
    try:
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def hubs(state):
    """The state folders of this machine's hubs: this one and the others (core/instance.py)."""
    out = [state]
    with contextlib.suppress(Exception):   # a registry that does not read keeps what is pointed at here only
        from core import instance
        out += [h["state"] for h in instance.others(state)]
    return out


def keep_next(state, env, now=time.time):
    """A newer environment the install line unpacked and left to the update: kept from the pruning for NEXT_DAYS."""
    from core import catalog
    catalog.write_private(os.path.join(state, "layers", NEXT), {"file": os.path.realpath(env), "at": now()})


def kept(states, now=time.time):
    """The real paths that stay: what the hubs point at, what their replacement records name and a newer environment
    left for the update (not older than NEXT_DAYS)."""
    keep = set()
    for s in states:
        nxt = _json(os.path.join(s, "layers", NEXT))
        at = nxt.get("at")
        if isinstance(nxt.get("file"), str) and isinstance(at, (int, float)) and now() - at < NEXT_DAYS * 86400:
            keep.add(_norm(nxt["file"]))
        link = os.path.join(s, "app")
        if os.path.lexists(link):
            keep.add(_norm(link))
        rec = _json(os.path.join(s, "layers", "shell-update.json"))
        if rec.get("status") == "preparing" and isinstance(rec.get("file"), str):   # being unpacked or sealed now
            keep.add(_norm(rec["file"]))
        if not rec.get("folder"):
            continue
        if isinstance(rec.get("old"), str):
            keep.add(_norm(rec["old"]))
        if isinstance(rec.get("new"), str) and rec.get("status") not in ("returned", "failed"):
            keep.add(_norm(rec["new"]))
    return keep


def busy(paths, run=subprocess.run):
    """The real paths of `paths` a running program of this user comes from. Windows: none here (rename tells)."""
    if not paths or _windows():
        return set()
    roots = {p: os.path.join(p, "") for p in paths}
    found = set()
    if os.path.isdir("/proc/self"):
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                exe = os.readlink(f"/proc/{pid}/exe")
            except OSError:
                continue
            found |= {p for p, root in roots.items() if exe.startswith(root)}
        return found
    lsof = "/usr/sbin/lsof" if os.path.exists("/usr/sbin/lsof") else "lsof"   # a service's PATH may not name /usr/sbin
    try:   # macOS: the text files (programs, libraries) of this user's processes
        r = run([lsof, "-nP", "-w", "-u", str(os.getuid()), "-d", "txt", "-Fn"], capture_output=True, text=True,
                errors="replace", stdin=subprocess.DEVNULL, timeout=120)
        names = [x[1:] for x in r.stdout.splitlines() if x.startswith("n")]
    except (OSError, subprocess.SubprocessError):
        return set(paths)   # not known: nothing goes
    for n in names:
        real = os.path.realpath(n) if n.startswith("/") else n
        found |= {p for p, root in roots.items() if real.startswith(root)}
    return found


def _windows():
    return platforms.current.APP_OS == "windows"


def remove(path):
    """A folder of the install away: its read-only modes off first (the environment is made read-only), links not
    followed. Windows: through the long-path prefix (an environment's paths pass 260 characters)."""
    if _windows() and not path.startswith("\\\\?\\"):
        path = "\\\\?\\" + os.path.abspath(path)
    for d, dirs, _ in os.walk(path):
        for n in dirs:
            p = os.path.join(d, n)
            if not os.path.islink(p):
                with contextlib.suppress(OSError):
                    os.chmod(p, stat.S_IRWXU)
    with contextlib.suppress(OSError):
        os.chmod(path, stat.S_IRWXU)

    def again(func, p, exc):
        if isinstance(exc, FileNotFoundError):
            return
        with contextlib.suppress(OSError):
            os.chmod(p, stat.S_IREAD | stat.S_IWRITE)
        func(p)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=again)
    else:   # the environment's Python is 3.12+; the hub's own may be older
        shutil.rmtree(path, onerror=lambda f, p, e: again(f, p, e[1]))


def prune(state, shells=None, now=time.time, run=subprocess.run, say=None):
    """The environment folders no hub needs any more, removed. -> the names removed."""
    shells = shells or shells_of(state)
    if not shells or not os.path.isdir(shells):
        return []
    keep = kept(hubs(state), now)
    candidates = {}
    for n in sorted(os.listdir(shells)):
        path = os.path.join(shells, n)
        if os.path.islink(path) or not os.path.isdir(path):
            continue
        m, g = NAME.match(n), GONE.match(n)
        if not (m or g) or _norm(path) in keep:
            continue
        if m and m.group(2) == ".part":
            with contextlib.suppress(OSError):
                if now() - os.path.getmtime(path) < PART_DAYS * 86400:
                    continue
        candidates[_norm(path)] = (n, path, bool(g))
    held = busy([p for p, (_, _, gone) in candidates.items() if not gone], run)
    removed = []
    for real, (n, path, gone) in candidates.items():
        if real in held:
            continue
        aside = path if gone else os.path.join(shells, f".{n}.gone-{int(now())}")
        try:
            if not gone:
                # macOS renames a folder only with its own write bit (the environment is read-only)
                os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) | stat.S_IWUSR)
                os.rename(path, aside)   # Windows: refused while a process holds a file of it
            remove(aside)
        except OSError as e:
            if say:
                say(f"the environment {n} is not removed now: {e}")
            continue
        removed.append(n)
    if removed and say:
        say(f"environments removed: {', '.join(removed)}")
    return removed
