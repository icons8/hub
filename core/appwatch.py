"""The app's code layer seen by the core (app spec §3.1, slice A1): hubd checks the layer it runs from against its
MANIFEST.json at its start and once a day (core/appcode.py, the full check) and says what is edited — it never refuses
and never puts anything back itself. The shell's launcher (tools/package/launch.py) puts the release back only when the
core did not come up on an edited layer, or when the owner asks ("Return the released", POST /api/app/restore); it
leaves app/restored.json, and hubd records it.

- app.code_modified {name, count, paths}: the layer differs from its release (count 0 — it is the release again); written
  only when that changes, so a daily check of an unchanged layer writes nothing;
- app.code_restored {name, at, why: start|owner, count, paths, copy | error}: the launcher put the release back (or
  could not);
- app.moved {repo, at, done, branch?}: the hub came up on the app after a move from a git clone (core/appmove.py leaves
  app-move/record.json); the fold keeps it in st["app_move"], "About the hub" names the clone and `hub app undo` for
  MOVE_DAYS.
The fold keeps the first two in st["app_code"] — "About the hub" shows the line and the button from it. Both facts are hubd's
own: daemon.spooled refuses them from the mailbox. Only the stdlib and core/appcode here: the reducer imports this
file."""
import json
import os

from core import appcode

FACTS = ("app.code_modified", "app.code_restored", "app.moved")
APP = "layers"
FULL_EVERY = 24 * 3600   # s between full checks while the core runs (the first check, at the start, is the quick one)
FULL_FIRST = 600         # s after the start: the first full check (a hub that restarts daily gets one too)
MARK_EVERY = 60          # s between marks of the launcher's start record (a start of the shell while the core runs)
PATHS_MAX = 50
WHY = ("start", "owner")
MOVE = os.path.join("app-move", "record.json")   # core/appmove.py DIR, RECORD
MOVE_DAYS = 14   # core/appmove.UNDO_DAYS


class Bad(Exception):
    """A fact of this kind with a shape the fold does not take."""


def layer(repo):
    """The layer folder the code runs from (its MANIFEST.json is there), or None (a hub from git, an older package)."""
    real = os.path.realpath(repo)
    return real if os.path.isfile(os.path.join(real, appcode.MANIFEST)) else None


def manifest(root):
    """The layer's MANIFEST.json without its file list (what "About the hub" names), {} when unreadable."""
    try:
        man = appcode.load(root)
    except appcode.Bad:
        return {}
    return {k: v for k, v in man.items() if k != "files"}


def release(root):
    """(version, release) the layer folder runs: its manifest's version and the release under a build of edits
    ("<release>+local.<sha>", "<release>"; a release — the same twice); None when unread."""
    man = manifest(root)
    got = man.get("version")
    if not (isinstance(got, str) and got):
        return None
    return got, (appcode.own_of(man) or {}).get("release") or got


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def came_up(state_dir, name):
    """The core is up on the layer name: the launcher's record of this start gets ok (its watchdog leaves, the next start
    does not put the release back). A record of another layer is not touched."""
    path = os.path.join(state_dir, APP, "start.json")
    start = _read(path)
    if start.get("name") != name or start.get("ok"):
        return False
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({**start, "ok": True}, f)
    os.replace(tmp, path)
    return True


def check(root, full=True):
    """{name, count, paths} of the layer folder against its manifest (full — the sha256 too, else the list, sizes and
    times). A layer without a readable manifest is one change: the manifest itself. None — the folder could not be read
    now (a later check tries again)."""
    try:
        man = appcode.load(root)
        changes = appcode.diff(root, man, full=full)
        name = appcode.name_of(man)
    except appcode.Bad:
        return {"name": os.path.basename(root), "count": 1, "paths": [appcode.MANIFEST]}
    except OSError:
        return None
    return {"name": name, "count": len(changes), "paths": [c["path"] for c in changes][:PATHS_MAX]}


def news(st, found):
    """The check's result differs from what the state says (a new fact is due)."""
    have = st.get("app_code") or {}
    if not have:
        return found["count"] > 0
    return any(have.get(k) != found[k] for k in ("name", "count", "paths"))


def restored(state_dir, st, name):
    """The launcher's record of a restore not yet in the state -> the fact's data, else None."""
    rec = _read(os.path.join(state_dir, APP, "restored.json"))
    at = rec.get("at")
    if rec.get("name") != name or not isinstance(at, (int, float)) or isinstance(at, bool):
        return None
    if at <= ((st.get("app_code") or {}).get("restored") or {}).get("at", 0):
        return None
    out = {"name": name, "at": at, "why": rec.get("why") if rec.get("why") in WHY else "start"}
    for key, kind in (("count", int), ("copy", str), ("error", str)):
        if isinstance(rec.get(key), kind) and not isinstance(rec.get(key), bool):
            out[key] = rec[key] if kind is int else rec[key][:500]
    paths = rec.get("paths")
    out["paths"] = [p for p in paths if isinstance(p, str)][:PATHS_MAX] if isinstance(paths, list) else []
    return out


def _paths(data):
    paths = data.get("paths")
    if not (isinstance(paths, list) and all(isinstance(p, str) for p in paths)):
        raise Bad("paths: a list of strings")
    return paths[:PATHS_MAX]


def moved(state_dir, st):
    """The move's record (core/appmove.py) of a move done and not yet in the state -> the fact's data, else None."""
    rec = _read(os.path.join(state_dir, MOVE))
    done = rec.get("done")
    if rec.get("status") != "moved" or not isinstance(done, (int, float)) or isinstance(done, bool):
        return None
    if done <= (st.get("app_move") or {}).get("done", 0):
        return None
    edits = rec.get("edits") if isinstance(rec.get("edits"), dict) else {}
    from core import appmove
    out = {"repo": appmove.where(rec)[:500], "done": done,
           "at": rec["at"] if isinstance(rec.get("at"), (int, float)) and not isinstance(rec.get("at"), bool) else done}
    if isinstance(edits.get("branch"), str):
        out["branch"] = edits["branch"][:200]
    return out


def fold(st, ev, data):
    kind = ev["type"]
    if kind == "app.moved":
        if not isinstance(data.get("repo"), str) or not isinstance(data.get("done"), (int, float)):
            raise Bad("repo, done: the clone and when the move was done")
        st["app_move"] = {k: data[k] for k in ("repo", "at", "done", "branch") if k in data}
        return
    if not isinstance(data.get("name"), str):
        raise Bad("name: the layer's name")
    have = st.get("app_code") or {}
    if kind == "app.code_modified":
        count = data.get("count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise Bad("count: a number of files")
        st["app_code"] = {**({"restored": have["restored"]} if have.get("restored") else {}),
                          "name": data["name"], "count": count, "paths": _paths(data), "at": ev.get("ts")}
        return
    if data.get("why") not in WHY or not isinstance(data.get("at"), (int, float)):
        raise Bad("why, at: who put the release back and when")
    rec = {k: data[k] for k in ("at", "why", "count", "copy", "error") if k in data}
    rec["paths"] = _paths(data)
    st["app_code"] = {**have, "name": have.get("name", data["name"]), "restored": rec}


def move_view(st, now):
    """The snapshot key app_move: the move of MOVE_DAYS ago at most — {repo, done, until, branch?}; else None."""
    have = st.get("app_move")
    if not have or now is None:
        return None
    until = have["done"] + MOVE_DAYS * 86400
    return {**have, "until": until} if now <= until else None


def view(st):
    """What the page gets (the snapshot key app_code), or None."""
    have = st.get("app_code")
    return dict(have) if have else None
