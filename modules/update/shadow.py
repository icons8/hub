"""The shadow of the update self-check, and the check of what a merge kept of the release (dev-guard spec §5.4, §5.5,
slice 3; owner decision q1793545: "see, hold, roll back"). Neither holds an update back: what they find goes to the band
about the hub's own edits of the core in Settings, to Hub Developer of this hub and, as counts, to the statistics report.

The shadow. A hub that edited test files of the release runs its edited versions in the self-check, so a release test
its edit breaks never goes red there. After a green self-check and the install, the updater writes what to run (PENDING:
the release's versions of the test modules the hub changed); a later pass of its loop runs them against the installed
merge — in a check copy, as a separate process (tools/test.py of the copy, without the browser modules, at the
self-check's priority), under the machine lock, with its own limit (`update.shadow_seconds`, SECONDS by default; 0 —
off), outside the self-check's 1200 s. A red test is "your edit changes what the release checks": a line in the band and
a message to Hub Developer, once per release and tests; a run that did not fit its limit is "not checked", not red.

The check of the merge. When the release the edits are counted from moves (codeedits.release_base: an update, a release
merged by hand), the hunks of `git diff -U0 <old> <new>` in the files of the zones (codeedits.ZONES) the hub edits whose
added non-blank lines are not all in the merged file (HEAD) are signals: "check whether this change of the release was
lost"; so is a hunk that only removes lines when the merged file keeps a removed line the release has nowhere any more
(a dangerous entry taken out). They go to Hub Developer, not to the owner (the probe on real merges: most of them are
rewrites), and stay as lines in the band until it answers `hub update lost ok|restored <id>` (ANSWERS). Coarse on
purpose: a line found anywhere in the file counts as kept (a repeated line lost once is not seen), lines of three
characters or fewer are not looked at."""
import contextlib
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time

from core import catalog, codeedits

SHADOW = os.path.join("update", "shadow.json")   # {"pending": what to run, "last": the last result}
LOST = os.path.join("update", "lost.json")       # {"base", "open": [signals], "signals", "ok", "restored", "answered"}
ANSWERS = os.path.join("update", "lost-answers")   # one file per answer, named by the signal: `hub update lost` writes
SECONDS = 600   # the shadow's own limit: chosen, not measured (spec §5.5); its time goes to the report to be measured
SECONDS_MAX = 3600
TRIES = 3   # a run killed in the middle (a restart of the hub) is tried again, then "not checked"
SLEEPS = 5   # runs cut by a sleep of the machine (not a kill): again on the next wake, then "not checked"
# the run waits for the restart the install asks for (plan); one that has not come in this long (sessions stay busy, the
# clock went back at boot) does not hold it any more
RESTART_GRACE = 6 * 3600
SHOWN = 10   # failed tests and lost hunks the band names; the counts are whole
FAILED_KEEP = 30
SIGNALS_MAX = 50   # signals of one move of the base: a merge that lost more is one big red flag anyway
ANSWERED_KEEP = 50
MIN_LINE = 4   # a hunk line shorter than this ("}", "pass" is kept) is not looked for: it is everywhere
ID = re.compile(r"[0-9a-f]{8}")
TEST_NAME = re.compile(r"tests\.[\w.]+")
WORDS = ("ok", "restored")   # `hub update lost ok <id>`: a rewrite, nothing was lost; `restored <id>`: put back by hand


def seconds(cfg):
    """The shadow's limit from hub.json `update.shadow_seconds`: a number in [0, SECONDS_MAX], 0 — the shadow is off;
    anything else (a string, NaN, a bool, out of range) — SECONDS."""
    up = (cfg or {}).get("update") if isinstance((cfg or {}).get("update"), dict) else {}
    v = up.get("shadow_seconds", SECONDS)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or isinstance(v, float) and not math.isfinite(v) \
            or not 0 <= v <= SECONDS_MAX:
        return SECONDS
    return int(v)


def _read(state_dir, name):
    try:
        with open(os.path.join(state_dir, name), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(state_dir, name, data):
    import platforms
    platforms.current.makedirs_private(os.path.dirname(os.path.join(state_dir, name)))
    catalog.write_private(os.path.join(state_dir, name), data)


def _git(repo, *args, check=True):
    r = subprocess.run(["git", "--no-optional-locks", "--literal-pathspecs", *args], cwd=repo, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    if check and r.returncode:
        raise RuntimeError(f"git {args[0]}: {r.stderr.strip()[-300:]}")
    return r.stdout if r.returncode == 0 else None


def _sha(v):
    return v if isinstance(v, str) and re.fullmatch(r"[0-9a-f]{40,64}", v) else None


# --- the shadow ---------------------------------------------------------------------------------------------------------

def changed_tests(repo, release, got=None):
    """(files, modules): the test files of the release the hub's code has changed or deleted (codeedits.classify against
    the release: M or D under tests/ — a test module, a fake, a replay; deleting a release test is the plainest way past
    it), and the test modules among them (tests/test_*.py) as names to run — only importable names. A changed helper alone
    names no module: nothing to run. `got` — classify's answer already at hand."""
    got = codeedits.classify(repo, release) if got is None else got
    if got is None:
        return [], []
    files = [f["path"] for f in got["core"] if f["st"] in ("M", "D") and f["path"].startswith("tests/")]
    mods = [n for n in (p[:-3].replace("/", ".") for p in files if codeedits.OWN_TEST.fullmatch(p)) if TEST_NAME.fullmatch(n)]
    return files, mods


def plan(state_dir, repo, at, release, version, cfg):
    """After an install (update.py _after_ff): what the shadow runs on the installed merge `at`. Nothing when the hub has
    no edited test module or the shadow is off; a pending run of an earlier release is replaced (it is not this code).
    The run waits for a process started after the plan: the install asks for a restart of the whole hub, which would kill
    a run started before it (an orphan of tools/test.py outside the machine lock)."""
    files, mods = changed_tests(repo, release)
    rec = _read(state_dir, SHADOW)
    rec.pop("pending", None)
    if mods and seconds(cfg):
        rec["pending"] = {"at": at, "release": release, "version": str(version or "")[:20], "files": files[:200],
                          "modules": mods[:200], "tries": 0, "ts": time.time()}
    _write(state_dir, SHADOW, rec)
    return bool(rec.get("pending"))


def pending(state_dir):
    p = _read(state_dir, SHADOW).get("pending")
    return p if isinstance(p, dict) and _sha(p.get("at")) and _sha(p.get("release")) \
        and isinstance(p.get("modules"), list) and p["modules"] else None


def run_release_tests(copy, names, limit, run=None):
    """tools/test.py of the copy on these modules, as the self-check runs it (--no-lock: the machine lock is the
    caller's; its priority and processes), without the browser modules -> (ok, output, failed names) or None — the limit
    ran out (not red: not checked)."""
    import platforms
    from modules.update import update
    run = run or platforms.current.run
    env = {k: v for k, v in os.environ.items() if not k.startswith("HUB_")}
    env["PYTHONUTF8"] = "1"
    env["AGENTS_HUB_SELF_CHECK_SECONDS"] = str(int(limit))   # as update.run_tests: the runner keeps its retries inside this limit
    try:
        r = run([sys.executable, os.path.join(copy, "tools", "test.py"), "--no-lock", "--browser-tests", "off", *names],
                cwd=copy, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=limit,
                stdin=subprocess.DEVNULL, **platforms.current.apart())
    except subprocess.TimeoutExpired:
        update.sweep_killed_run()
        return None
    except BaseException:
        update.sweep_killed_run()
        raise
    out = r.stdout + r.stderr
    return r.returncode == 0, out, update.failed_tests(out)


STARTED = time.time()   # when this process started: a run planned before it waits for the restart (plan)


def step(u, runner=run_release_tests, started=None):
    """One pass of the updater's loop: the pending shadow run, in a process started after the plan, if the machine lock
    is free and the Mac is not between sleeps (on schedule an update waits for neither). It blocks the loop for up to
    its limit, as the self-check does. -> 'green' | 'red' | 'unchecked' | 'busy' | 'waiting' (the restart after the
    install, a dark wake) | None (nothing pending)."""
    from core import testlock
    p = pending(u.state_dir)
    if not p:
        return None
    limit = seconds(u.cfg)
    if not limit or not isinstance(p.get("tries", 0), int) or p.get("tries", 0) >= TRIES:
        return _done(u, p, "unchecked", why="off" if not limit else "interrupted")
    ts = p.get("ts")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts >= (STARTED if started is None else started) \
            and time.time() - ts < RESTART_GRACE:
        return "waiting"
    if u.dark():
        return "waiting"
    names = [m for m in p["modules"] if isinstance(m, str) and TEST_NAME.fullmatch(m)]
    if not names:   # tools/test.py without names runs the whole suite
        return _done(u, p, "unchecked", why="no-modules")
    try:
        with u.turn(0):
            return _run(u, p, names, limit, runner)
    except testlock.Busy:
        return "busy"


def _run(u, p, names, limit, runner):
    from modules.update import update
    p = {**p, "tries": int(p.get("tries") or 0) + 1}   # first: a run killed in the middle counts
    rec = _read(u.state_dir, SHADOW)
    rec["pending"] = p
    _write(u.state_dir, SHADOW, rec)
    copy = u._copy(p["at"], "shadow-" + p["release"][:7])
    try:
        files = [f for f in p.get("files") or [] if isinstance(f, str)]
        if files:   # the release's versions of the files the hub changed; its own new tests stay as they are
            _git(copy, "checkout", "-q", p["release"], "--", *files)
        wall, mono = u.clock()
        try:
            got = runner(copy, names, limit)
        except Exception as e:  # noqa: BLE001 — a failed run is not checked, not red
            print(f"update: the shadow run failed: {e!r}", flush=True)
            return _done(u, p, "unchecked", why="error")
        now_wall, now_mono = u.clock()
        took = round(now_mono - mono)
        if (now_wall - wall) - (now_mono - mono) > update.SLEPT:   # the machine slept: timing tests say nothing, again
            slept = int(p.get("sleeps") or 0) + 1
            if slept >= SLEEPS:
                return _done(u, p, "unchecked", why="slept", took=took)
            rec = _read(u.state_dir, SHADOW)   # a sleep is not a kill: the try is given back
            rec["pending"] = {**p, "tries": p["tries"] - 1, "sleeps": slept}
            _write(u.state_dir, SHADOW, rec)
            print(f"update: the shadow of {p['release'][:7]} — the machine slept, again later", flush=True)
            return "waiting"
        if got is None:
            return _done(u, p, "unchecked", why="timeout", took=took)
        ok, out, failed = got
        if ok:
            return _done(u, p, "green", took=took)
        # red without a name (the runner or a chunk died, the smoke step): the modules that were run stand for them
        return _done(u, p, "red", took=took, failed=failed, shown=None if failed else names, tail=out[-800:])
    finally:
        u.drop_copies_quietly()


def _done(u, p, result, why=None, took=None, failed=(), shown=None, tail=""):
    """The result into shadow.json (`failed` — the names the band shows: the failed tests, or the modules run when the
    output named none — `unnamed`; `red` — how many tests failed, whole) and, red, a message to Hub Developer."""
    failed, shown = list(failed), list(shown or failed)
    rec = _read(u.state_dir, SHADOW)
    rec.pop("pending", None)
    rec["last"] = {"release": p["release"], "at": p["at"], "version": p.get("version") or "", "result": result,
                   "files": len(p.get("files") or []), "modules": len(p.get("modules") or []),
                   "failed": shown[:FAILED_KEEP], "red": len(failed), **({"unnamed": True} if not failed and shown else {}),
                   "ts": round(time.time()),
                   **({"seconds": took} if took is not None else {}), **({"why": why} if why else {})}
    _write(u.state_dir, SHADOW, rec)
    print(f"update: the shadow of {p['release'][:7]} — {result}" + (f" ({why})" if why else "")
          + (f", {took} s" if took is not None else ""), flush=True)
    if result == "red":
        names = ", ".join(shown[:10]) + (f" … +{len(shown) - 10}" if len(shown) > 10 else "")
        mark = hashlib.sha256("\n".join(sorted(shown)).encode()).hexdigest()[:12]
        u.tell_dev(("shadow-red", p["release"], mark), u._t(
            "core.update.shadow_red_dev", version=p.get("version") or p["release"][:7],
            files=", ".join(p.get("files") or [])[:600], names=names or "?", tail=tail))
    return result


def band(state_dir, repo, base, got=None):
    """The shadow's line in the band: the release tests that went red on the hub's code, while the base is still that
    release and the hub still edits their files (it put the release's version back: the next update checks it as usual).
    `got` — codeedits.classify of the code against the base, if at hand. None — nothing to show."""
    last = _read(state_dir, SHADOW).get("last")
    if not isinstance(last, dict) or last.get("result") != "red" or last.get("release") != base:
        return None
    edited = set(changed_tests(repo, base, got)[0])
    kept = [n for n in last.get("failed") or [] if isinstance(n, str) and TEST_NAME.fullmatch(n)]
    failed = [n for n in kept if _module_file(n) in edited]
    if not failed:
        return None
    red = last.get("red")   # names beyond FAILED_KEEP count only while none of the kept ones was put back
    more = red - len(kept) if isinstance(red, int) and not isinstance(red, bool) and red > len(kept) \
        and len(failed) == len(kept) else 0
    return {"failed": failed[:SHOWN], "count": len(failed) + more,
            **({"version": last["version"]} if isinstance(last.get("version"), str) and last["version"] else {})}


def _module_file(name):
    """tests.test_x.Case.test_y -> tests/test_x.py (a test module is a top-level file of tests/)."""
    parts = name.split(".")
    return f"{parts[0]}/{parts[1]}.py" if len(parts) >= 2 else ""


# --- the check of the merge -----------------------------------------------------------------------------------------------

def lost_hunks(repo, old, new, paths, head="HEAD"):
    """The hunks of `git diff -U0 old new` in these files whose added lines (stripped, MIN_LINE characters and more) are
    not all in head's file, or — a hunk that only removes (a dangerous entry taken out) — whose removed lines that the
    release has nowhere any more are still in head's -> [{"id", "path", "release", "from", "hunk", "lines"}]; a file head
    has not — all its adding hunks."""
    out = []

    def lines(ref, path):
        return {line.strip() for line in (_git(repo, "show", f"{ref}:{path}", check=False) or "").splitlines()}
    for path in paths:
        d = _git(repo, "diff", "-U0", "--no-renames", "--no-color", "--no-ext-diff", old, new, "--", path, check=False) or ""
        have = gone = None
        for h in re.split(r"\n(?=@@)", d)[1:]:
            rows = h.splitlines()
            added = [a for a in (line[1:].strip() for line in rows[1:] if line.startswith("+")) if len(a) >= MIN_LINE]
            removed = [a for a in (line[1:].strip() for line in rows[1:] if line.startswith("-")) if len(a) >= MIN_LINE]
            if not added and not removed:
                continue
            if have is None:
                have = lines(head, path)
            if any(line.startswith("+") for line in rows[1:]):   # it adds: are its lines there
                miss = [a for a in added if a not in have]
            else:
                gone = lines(new, path) if gone is None else gone
                miss = [r for r in removed if r in have and r not in gone]
            if miss:
                hunk = rows[0][:80]
                out.append({"id": hashlib.sha256(f"{new}\0{path}\0{hunk}".encode()).hexdigest()[:8], "path": path[:200],
                            "release": new, "from": old, "hunk": hunk,
                            "lines": [("+" if added else "-") + m[:120] for m in miss[:3]]})
    return out


def zone_files(repo, base, got=None):
    """The files of the zones (codeedits.ZONES) the hub edits against the base: the only ones the check looks at. `got`
    — classify's answer already at hand."""
    got = codeedits.classify(repo, base) if got is None else got
    return [f["path"] for f in (got or {}).get("core", []) if "zone" in f and f["st"] != "?"]


def answer(state_dir, ident, word):
    """`hub update lost ok|restored <id>`: one file per answer (the updater's pass takes it), written whole."""
    if word not in WORDS or not ID.fullmatch(ident or ""):
        raise ValueError(ident)
    folder = os.path.join(state_dir, ANSWERS)
    import platforms
    platforms.current.makedirs_private(folder)
    path = os.path.join(folder, ident)
    with open(path + ".part", "w") as f:
        f.write(word)
    os.replace(path + ".part", path)


def open_signals(state_dir):
    return _valid(_read(state_dir, LOST).get("open"))


def take_answers(state_dir):
    """The answers of `hub update lost` into lost.json, their files removed. -> whether any was there (the band changes)."""
    folder = os.path.join(state_dir, ANSWERS)
    if not (os.path.isdir(folder) and any(ID.fullmatch(n) for n in os.listdir(folder))):
        return False
    rec = _read(state_dir, LOST)
    rec["open"] = _valid(rec.get("open"))
    _take_answers(state_dir, rec)
    _write(state_dir, LOST, rec)
    return True


def _valid(signals):
    return [s for s in signals or [] if isinstance(s, dict) and ID.fullmatch(str(s.get("id")))]


def _take_answers(state_dir, rec):
    folder = os.path.join(state_dir, ANSWERS)
    names = [n for n in os.listdir(folder) if ID.fullmatch(n)] if os.path.isdir(folder) else []
    took = False
    for name in names:
        path = os.path.join(folder, name)
        try:
            with open(path) as f:
                word = f.read().strip()
        except OSError:
            continue
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        took = True   # an answer to a signal already gone is taken too: its file must not wait forever
        signal = next((s for s in rec["open"] if s.get("id") == name), None)
        if word not in WORDS or signal is None:
            continue
        rec["open"] = [s for s in rec["open"] if s is not signal]
        rec[word] = int(rec.get(word) or 0) + 1
        rec["answered"] = (rec.get("answered") or [])[-(ANSWERED_KEEP - 1):] + [
            {"id": name, "answer": word, "path": signal.get("path"), "ts": round(time.time())}]
    return took


def reconcile(u, base, got=None):
    """A pass of the updater (update.py _edits): the answers taken, and when the base moved since the last pass, the
    hunks of the release lost by the merge. -> the band's part {"count", "items": [{"id", "path"}]} or None."""
    rec = _read(u.state_dir, LOST)
    rec["open"] = _valid(rec.get("open"))
    changed = _take_answers(u.state_dir, rec)
    old = _sha(rec.get("base"))
    if old != base:
        changed = True
        if old and u.ancestor(old, base):
            found = lost_hunks(u.repo, old, base, zone_files(u.repo, base, got))[:SIGNALS_MAX]
            seen = {s["id"] for s in rec["open"]}
            fresh = [s for s in found if s["id"] not in seen]
            rec["open"] = (rec["open"] + fresh)[-SIGNALS_MAX:]
            rec["signals"] = int(rec.get("signals") or 0) + len(fresh)
            if fresh:
                listed = "\n".join(f"{s['id']}  {s['path']}  {s['hunk']}\n    " + "\n    ".join(s["lines"])
                                   for s in fresh[:20])
                u.tell_dev(("lost", old, base), u._t("core.update.lost_dev", old=old[:12], new=base[:12],
                                                     count=len(fresh), hunks=listed))
        rec["base"] = base
    if changed:
        _write(u.state_dir, LOST, rec)
    if not rec["open"]:
        return None
    return {"count": len(rec["open"]), "items": [{"id": s["id"], "path": s.get("path", "")} for s in rec["open"][:SHOWN]]}


# --- the statistics report ------------------------------------------------------------------------------------------------

def report(state_dir):
    """Numbers for the statistics report (modules/stats local_changes; spec §5.1): the last shadow run (result, seconds,
    how many tests it ran and how many went red) and the signals of the merge check (open, total, answered). No contents:
    no names of tests or files. {} — nothing yet."""
    out = {}
    last = _read(state_dir, SHADOW).get("last")
    if isinstance(last, dict) and last.get("result") in ("green", "red", "unchecked"):
        out["shadow"] = {"result": last["result"], "modules": int(last.get("modules") or 0),
                         "red": last["red"] if isinstance(last.get("red"), int) and not isinstance(last["red"], bool)
                         else len(last.get("failed") or []), "release": str(last.get("release") or "")[:7],
                         **({"seconds": last["seconds"]} if isinstance(last.get("seconds"), int) else {}),
                         **({"unnamed": True} if last.get("unnamed") is True else {}),
                         **({"why": str(last["why"])[:20]} if last.get("why") else {})}
    lost = _read(state_dir, LOST)
    if lost.get("base"):
        out["lost"] = {"open": len(open_signals(state_dir)), "signals": int(lost.get("signals") or 0),
                       "ok": int(lost.get("ok") or 0), "restored": int(lost.get("restored") or 0)}
    return out
