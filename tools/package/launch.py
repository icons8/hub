"""What the app shell does at every start, before hub-up (installer spec S3, variant A; the Mac, Windows and Linux
launchers of S5–S7 call it). It belongs to the shell, not to the code: the copy inside an app is frozen at build time,
so an update of the code cannot break it.

- ensure_code: the hub's code is a git clone of the shared repo in the state folder (<state>/code); the shell carries a
  seed — the release it was built with, already a git clone on its tag (`git clone --depth 1 --branch vX`, origin = the
  shared repo). The first start clones the seed there; later starts leave the code as updates moved it — except that a
  newer installer over an installed app (its seed is a later release than the code's, and the code has no edit of the
  person's) moves the code to the seed the way the update's button does (modules/update/update.py: applying.json for an
  interrupted move, installed and the history for "Return", the previous release's watchdog in trial.json), so that a
  person who cannot reach the shared repo is not left on the old code for good.
- The seed's bytecode (tools/package/build.py compiles it) goes along into the clone at the first start.
- PACKAGED of the shell is copied into the code at every start (core/packaged.py reads it next to the code): a newer
  shell brings its environment number with it. The file is ignored by git (.gitignore, and .git/info/exclude of the
  clone), so it is not an edit of the code.
- resume_trial: an update left a trial (modules/update/trial.py) and the machine rebooted before it ended — the
  previous release's watchdog is started again, the clock from now.

    python3 launch.py <seed> <PACKAGED of the shell> <state folder>   → prints the code folder
"""
import contextlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time


def git(*args, cwd=None):
    # The shell may belong to another user (root's /opt of a .deb): git's "dubious ownership" is not about the seed.
    # safe.directory counts only from the global or system config in an older git (Debian 12: 2.39, `-c` is ignored),
    # so these commands — they touch only the seed and its fresh clone — get a global config of their own.
    fd, conf = tempfile.mkstemp(prefix="hub-launch-", suffix=".gitconfig")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("[safe]\n\tdirectory = *\n")
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=600,
                           stdin=subprocess.DEVNULL, env={**os.environ, "GIT_CONFIG_GLOBAL": conf},
                           creationflags=NO_WINDOW)
    finally:
        os.unlink(conf)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])}: {r.stderr.strip()[-300:]}")
    return r.stdout.strip()


def _writable_again(func, path, exc):
    """rmtree on Windows: git's read-only objects — writable, then again; a file already gone is fine."""
    if isinstance(exc, FileNotFoundError):
        return
    if not isinstance(exc, PermissionError):
        raise exc
    link = os.path.islink(path) or os.path.isjunction(path)
    if link and os.chmod not in os.supports_follow_symlinks:   # Linux: a link has no mode of its own
        raise exc
    # a link itself, never the file it leads to (someone else's)
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD, **({"follow_symlinks": False} if link else {}))
    func(path)


def bytecode(seed, code):
    """The seed's __pycache__ folders into the clone (git does not carry them): the factory compiles them in
    checked-hash mode, valid for the same sources whatever their mtimes — the first start compiles nothing."""
    for root, dirs, _ in os.walk(seed):
        dirs[:] = [d for d in dirs if d != ".git"]
        if "__pycache__" in dirs:
            dirs.remove("__pycache__")
            rel = os.path.relpath(root, seed)
            if os.path.isdir(os.path.join(code, rel)):
                shutil.copytree(os.path.join(root, "__pycache__"), os.path.join(code, rel, "__pycache__"),
                                dirs_exist_ok=True)


UPDATE = "update"   # the update's own folder in the state folder (modules/update/update.py UPDATE_DIR)
RELEASE = re.compile(r"v(\d+)\.(\d+)\.(\d+)\Z")   # core/version.RELEASE: vX.Y.Z is a release, anything else is not
HISTORY_KEEP = 2
TRIAL_WINDOW = 180


def _releases(names):
    return sorted((tuple(map(int, m.groups())), n) for n in names for m in [RELEASE.match(n)] if m)


def _read(path, kind):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return kind()
    return data if isinstance(data, kind) else kind()


WINDOWS = os.name == "nt"
NO_WINDOW = 0x08000000 if WINDOWS else 0   # CREATE_NO_WINDOW: a console program shows no window (app.py runs under pythonw)


def _replace(src, dst, patience=5):
    """os.replace that waits while a reader holds dst: on Windows that makes it fail with PermissionError until the file
    is closed; elsewhere that is a real refusal. This file imports nothing of the hub, so the retry is its own."""
    end = time.monotonic() + patience
    pause = 0.01
    while True:
        try:
            return os.replace(src, dst)
        except PermissionError:
            if not WINDOWS or time.monotonic() >= end:
                raise
            time.sleep(pause)
            pause = min(pause * 2, 0.2)


def _write(path, data, patience=5):
    """As catalog.write_private: whole or not at all, for the owner only (this file imports nothing of the hub); a temp
    file that did not get replaced is removed."""
    # no mode on Windows (as platforms/windows.makedirs_private: a 0o700 folder there has an ACL of SYSTEM, Administrators and
    # OWNER RIGHTS alone, and the user's own terminal cannot open one a process of the task made); this file imports nothing of the hub
    os.makedirs(os.path.dirname(path), exist_ok=True, **({} if WINDOWS else {"mode": 0o700}))
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        _replace(tmp, path, patience)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _tags_at(code, sha):
    """Release tags of the code on this commit (the shared repo's, seen by the update, and the clone's own)."""
    return [n for _, n in _releases(git("for-each-ref", f"--points-at={sha}", "--format=%(refname:lstrip=-1)",
                                        "refs/hub-update/tags/", "refs/tags/", cwd=code).split())]


def _runtime_min(seed):
    try:
        with open(os.path.join(seed, "core", "version.py"), encoding="utf-8") as f:
            m = re.search(r"(?m)^RUNTIME_MIN\s*=\s*(\d+)", f.read())
    except OSError:
        return 0
    return int(m.group(1)) if m else 0


def seed_is_newer(seed, mark, code, state):
    """-> (the seed's commit, its release tag, the code's commit) when the code is to move to the seed, else None. The code
    sits exactly on a release (a commit of the person's or an edit is "not a release" — hers), the seed's release is
    a later number (the shared repo only goes forward; the seed is depth 1, there is no history to ask), the update is
    not in the middle of something, the release was not taken back earlier, and the shell has the environment it needs."""
    up = os.path.join(state, UPDATE)
    trial = _read(os.path.join(up, "trial.json"), dict)
    if os.path.exists(os.path.join(up, "applying.json")) or trial.get("status") == "waiting":
        return None   # the update is moving the code or waiting for the new release to come up: not on top of that
    sha = git("rev-parse", "HEAD", cwd=seed)
    # taken back from this very release: the watchdog's mark (the update writes skipped.json from it later, in a running hub)
    if sha in _read(os.path.join(up, "skipped.json"), dict) or sha == _read(os.path.join(up, "rolledback.json"), dict).get(
            "to") or (trial.get("to") == sha and trial.get("status") in ("rolled-back", "failed")):
        return None
    tags = _releases(git("tag", "--points-at", sha, cwd=seed).split())
    head = git("rev-parse", "HEAD", cwd=code)
    if not tags or head == sha:
        return None
    have = _releases(_tags_at(code, head))
    if not have or tags[-1][0] <= have[-1][0]:   # on no release, or the seed is not later
        return None
    if subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=code, capture_output=True).returncode == 0:
        return None   # the app's code is a detached HEAD on a release; a branch is somebody's work
    if git("status", "--porcelain", "--untracked-files=no", cwd=code):
        return None
    runtime = _read(mark, dict).get("runtime", 0)
    if _runtime_min(seed) > (runtime if isinstance(runtime, int) and not isinstance(runtime, bool) else 0):
        return None
    return sha, tags[-1][1], head


def move_to_seed(seed, mark, code, state):
    """The newer installer's seed over an installed app: the code moves to it the way modules/update does it (_aside,
    _applying, _install, _trial, _after_ff) — without the tests (the factory ran them on this very release). The launcher
    is before hub-up: a hub that is not running takes the new code at its start; one that is running is asked to restart
    (the same restart file the watchdog writes), and the watchdog's clock starts when its core is gone. True when the
    code moved. Never raises: a start must not fail on this."""
    try:
        found = seed_is_newer(seed, mark, code, state)
        if not found:
            return False
        sha, tag, before = found
        up = os.path.join(state, UPDATE)
        installed = _read(os.path.join(up, "installed"), dict)
        history = [h for h in _read(os.path.join(up, "history.json"), list) if isinstance(h, dict)]
        was = next(iter(reversed(_tags_at(code, before))), "") or (
            installed.get("version", "") if installed.get("commit") == before else "")
        # the watchdog is the previous release's own copy: it imports nothing of the new one
        old_trial = os.path.join(code, "modules", "update", "trial.py")
        _write(os.path.join(up, "applying.json"), {"before": before, "result": sha, "installed": installed,
                                                    "history": history})
        git("fetch", "-q", "--no-tags", "--update-shallow", seed, "HEAD", f"+refs/tags/{tag}:refs/tags/{tag}", cwd=code)
        if git("rev-parse", f"{tag}^{{commit}}", cwd=code) != sha or git("rev-parse", "FETCH_HEAD", cwd=code) != sha:
            raise RuntimeError("the seed's release is not what was fetched")
        watchdog = None
        if os.path.isfile(old_trial):
            with open(old_trial, "rb") as f:
                watchdog = f.read()
        try:
            git("checkout", "-q", "--detach", sha, cwd=code)
        except RuntimeError:   # an untracked file where the release has its own, or git refusing: as it was
            git("reset", "-q", "--hard", before, cwd=code)
            raise
        bytecode(seed, code)
        if watchdog:
            with open(os.path.join(up, "trial-run.py"), "wb") as f:
                f.write(watchdog)
        _write(os.path.join(up, "installed"), {"commit": sha, "version": tag})
        _write(os.path.join(up, "history.json"),
               ([h for h in history if h.get("commit") != before] + [{"commit": before, "version": was}])[-HISTORY_KEEP:])
        if git("for-each-ref", "refs/hub-update/", "--count=1", cwd=code):   # core/version.release reads this namespace then
            git("update-ref", f"refs/hub-update/tags/{tag}", sha, cwd=code)
        core = _read(os.path.join(state, "hub-up.json"), dict).get("core")
        core = core if isinstance(core, int) and not isinstance(core, bool) and alive(core) else None
        if watchdog:
            port = os.environ.get("HUB_WEB_PORT", "")
            _write(os.path.join(up, "trial.json"), {
                "from": before, "to": sha, "version": tag, "previous": was, "started": time.time(),
                "window": TRIAL_WINDOW, "status": "waiting", "port": int(port) if port.isdigit() else 8787,
                "core_pid": core, "repo": code,
                "restart_flag": os.path.join(state, "restart"), "up_file": os.path.join(state, "hub-up.json")})
        open(os.path.join(state, "migrate.pending"), "w").close()   # the update's MIGRATE_FLAG: hub migrate --auto at the start
        if watchdog and core:   # a running hub took the move under its feet: restart it (the watchdog sees whether it comes up)
            with contextlib.suppress(OSError):   # best effort: the code has moved either way
                _write(os.path.join(state, "restart"), {"parts": [], "why": "a new release of the installer"})
        os.unlink(os.path.join(up, "applying.json"))
        return True
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as e:
        print(f"Hub by Icons8: the hub's code is not moved to the release of the installer: {e}", file=sys.stderr, flush=True)
        _undo(code, state)
        return False


def _undo(code, state):
    """The move failed in the middle: what applying.json says (the update's _recover_app does the same at hub start)."""
    path = os.path.join(state, UPDATE, "applying.json")
    mark = _read(path, dict)
    if not mark.get("before"):
        return
    try:
        if git("rev-parse", "HEAD", cwd=code) != mark["before"]:
            git("reset", "-q", "--hard", mark["before"], cwd=code)
        for name, key in (("installed", "installed"), ("history.json", "history")):
            if mark.get(key):
                _write(os.path.join(state, UPDATE, name), mark[key])
            elif os.path.exists(os.path.join(state, UPDATE, name)):
                os.unlink(os.path.join(state, UPDATE, name))
        trial = os.path.join(state, UPDATE, "trial.json")
        if _read(trial, dict).get("to") == mark.get("result"):   # written for the move that did not happen
            os.unlink(trial)
        os.unlink(path)
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:   # the hub's own recovery does it at its start
        print(f"Hub by Icons8: the code's rollback did not finish ({e}); the hub completes it at its start", file=sys.stderr, flush=True)


# --- the code layer (app spec §3.1): <state>/layers/<version>-<flavor>/, <state>/code points at the current one ---

LAYERS = "layers"
REPAIR_AFTER = 180   # s: a modified layer whose core has not come up for this long is put back to the release
WATCH_TICK = 3
_AC = []


def appcode():
    """core/appcode.py: the shell's own copy next to this file (the factory puts it there), else the repo's (this file in
    the tree, the tests). Loaded on use: the clone path never needs it."""
    if not _AC:
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        for path in (os.path.join(here, "appcode.py"), os.path.join(here, "..", "..", "core", "appcode.py")):
            if os.path.isfile(path):
                spec = importlib.util.spec_from_file_location("hub_shell_appcode", path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _AC.append(mod)
                break
        else:
            raise RuntimeError("no appcode.py next to the launcher")
    return _AC[0]


def is_layer(seed):
    return os.path.isfile(os.path.join(seed, "MANIFEST.json")) and not os.path.isdir(os.path.join(seed, ".git"))


def layers_dir(state):
    return os.path.join(state, LAYERS)


def _private_dirs(path):
    os.makedirs(path, exist_ok=True, **({} if WINDOWS else {"mode": 0o700}))


def _shell_ok(mark, man):
    """The shell's environment is enough for a layer (its runtime_min, as RUNTIME_MIN of a clone)."""
    runtime = _read(mark, dict).get("runtime", 0)
    runtime = runtime if isinstance(runtime, int) and not isinstance(runtime, bool) else 0
    need = man.get("runtime_min", 0)
    return not isinstance(need, int) or need <= runtime


def _clone_aside(link, now):
    """<state>/code is a real folder (the app's git clone of the earlier layout, S3): moved aside, not deleted."""
    if os.path.lexists(link) and not appcode().is_pointer(link):
        aside = f"{link}-clone-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}"
        os.replace(link, aside)
        print(f"Hub by Icons8: the earlier code folder is kept as {aside}", file=sys.stderr, flush=True)


def _hub_running(state):
    """The core named in hub-up.json is alive: the shell runs again (a menu entry, Finder) beside a working hub. A number
    another user's process holds (hub-up.json outlives a reboot) is not the hub's."""
    core = _read(os.path.join(state, "hub-up.json"), dict).get("core")
    if not isinstance(core, int) or isinstance(core, bool) or core <= 0:
        return False
    if WINDOWS:
        return alive(core)
    try:
        os.kill(core, 0)
    except OSError:   # gone, or not this user's
        return False
    return True


def _later(man, have):
    """The seed's release is to replace the current one: a later version, or the same version of the other flavor (an
    open shell over a team app: the code it runs is its own)."""
    if not have.get("version"):   # the current manifest does not read (edited): no move on a guess
        return False
    key, now = appcode().version_key(man.get("version")), appcode().version_key(have.get("version"))
    return key > now or (key == now and man.get("flavor") != have.get("flavor"))


def ensure_layer(seed, mark, state, now=time.time, run=None, own_deliver=False):
    """The layer the hub runs: the seed installed into <state>/layers/<name> and <state>/code pointed at it on the first
    start — and when the shell carries a later release than the current one (a newer installer over the app; a release
    the update marked as not coming up, layers/failed.json, is not taken). Then PACKAGED of the shell into it, the quick
    check (layers/check.json), the start's record (layers/start.json) and, on a modified layer, a watchdog: the core does
    not mark the start within REPAIR_AFTER — the released files go back (watch). A shell started beside a running hub
    changes nothing of that. own_deliver: layers/deliver.json is the installer's (the one-line install keeps the key of
    its line there), the shell only adds what it lacks (deliver). -> <state>/code (the path the shell and its services
    keep)."""
    ac = appcode()
    codes = layers_dir(state)
    link = os.path.join(state, "code")
    _private_dirs(codes)
    _clone_aside(link, now())
    man = ac.load(seed)
    offered = ac.name_of(man)
    if hasattr(ac, "take_next") and not _hub_running(state):   # an "Update" left for a restart that did not happen
        try:   # (the machine rebooted first): the hub is not running, the layer the owner chose is the code now
            why = (ac.pending(state) or {}).get("why") if hasattr(ac, "pending") else None
            if ac.take_next(state) and why in ("update", "own"):   # as hub-up: the new code's migration at its start
                open(os.path.join(state, "migrate.pending"), "w").close()
        except (OSError, ac.Bad) as e:
            print(f"Hub by Icons8: the layer left for the restart is not taken: {e}", file=sys.stderr, flush=True)
    name = ac.current(link, codes)
    failed = _read(os.path.join(codes, "failed.json"), dict)
    if name is None:   # the first start, or a pointer that lost its layer
        first = not any(os.path.isfile(os.path.join(codes, n, ac.MANIFEST)) for n in os.listdir(codes))
        if not os.path.isfile(os.path.join(codes, offered, ac.MANIFEST)):
            ac.install(seed, codes)
        ac.point(link, codes, offered)
        name = offered
        if first:   # the update installs the channel's latest layer without a button (app spec §4.3, modules/update/layer.py)
            _write(os.path.join(codes, "fresh.json"), {"name": name, "at": now()})
    elif offered != name and offered not in failed and _shell_ok(mark, man):
        if _later(man, _read(os.path.join(codes, name, ac.MANIFEST), dict)):
            if not os.path.isfile(os.path.join(codes, offered, ac.MANIFEST)):
                ac.install(seed, codes)
            ac.point(link, codes, offered)
            name = offered
            if _hub_running(state):   # its processes run from the previous layer's own folder: restart onto this one
                with contextlib.suppress(OSError):
                    _write(os.path.join(state, "restart"), {"parts": [], "why": "a new release of the installer"})
    layer = os.path.join(codes, name)
    with contextlib.suppress(OSError):   # a layer copied from a read-only seed before folders_writable: writable again
        if not all(stat.S_IMODE(os.lstat(d).st_mode) & stat.S_IWUSR for d in (layer, os.path.join(layer, "core"))
                   if os.path.isdir(d)):
            ac.folders_writable(layer)
    tmp = os.path.join(layer, "PACKAGED.part")
    shutil.copyfile(mark, tmp)
    os.replace(tmp, os.path.join(layer, "PACKAGED"))
    _write(os.path.join(codes, "shell.json"), {"launch": os.path.abspath(__file__), "python": sys.executable,
                                               "seed": os.path.abspath(seed)})
    deliver(mark, codes, own=own_deliver)
    resume_shell(state, run)
    try:
        changes = ac.diff(layer, ac.load(layer))
    except ac.Bad:   # its own manifest edited: one change, the release's manifest comes back with a restore
        changes = [{"path": ac.MANIFEST, "kind": "changed"}]
    except OSError:   # not readable now: nothing is known, nothing is done (the core checks again)
        changes = []
    t = now()
    _write(os.path.join(codes, "check.json"), {"name": name, "at": t, "full": False, "count": len(changes),
                                               "changes": changes[:200]})
    if _hub_running(state):
        return link
    _write(os.path.join(codes, "start.json"), {"name": name, "modified": bool(changes), "ok": False, "at": t})
    if changes:
        watch_start(state, run=run)
    return link


def resume_shell(state, run=None):
    """A replacement of the app (shellswap.py next to this file) whose watchdog is gone — the machine rebooted in the
    middle: its watchdog starts again, the clock from now. Never raises: a start must not fail on this."""
    here = os.path.dirname(os.path.abspath(__file__))
    if not os.path.isfile(os.path.join(here, "shellswap.py")):
        return False
    try:
        sys.path.insert(0, here)
        import shellswap
        return shellswap.resume(state, run or detached)
    except Exception as e:  # noqa: BLE001 — see above
        print(f"Hub by Icons8: the app's replacement is not resumed: {e}", file=sys.stderr, flush=True)
        return False


def deliver(mark, codes, own=False):
    """The distribution server of the shell's build (app spec §4.1): deliver.json next to its PACKAGED — {url, key}, the
    build key reads the releases — goes to layers/deliver.json for the update (0600; never into a layer: a layer is
    copied aside and put back, a key is not code). A shell without one (the open flavor) leaves nothing there. own: the
    file there is the installer's (the one-line install writes the key of its line, deploy/oneline/setup.py): the shell's
    only adds the keys it lacks (the report address), and a shell without one leaves it as it is."""
    src = os.path.join(os.path.dirname(os.path.abspath(mark)), "deliver.json")
    dst = os.path.join(codes, "deliver.json")
    got = _read(src, dict)
    if own:
        have = _read(dst, dict)
        if have.get("url") and have.get("key"):
            got = {**got, **have}
        if got and _read(dst, dict) != got:
            _write(dst, got)
        return
    if not got:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(dst)
        return
    if _read(dst, dict) != got:
        _write(dst, got)


def source_of(state, name, seed=None):
    """Where the released bytes of a layer are: the shell's seed of the same release, else the release archive the update
    kept (layers/archives/code-<name>.tar.gz); None — nowhere."""
    seed = seed or _read(os.path.join(layers_dir(state), "shell.json"), dict).get("seed")
    if isinstance(seed, str) and os.path.isfile(os.path.join(seed, "MANIFEST.json")):
        try:
            if appcode().name_of(appcode().load(seed)) == name:
                return seed
        except appcode().Bad:
            pass
    archive = os.path.join(layers_dir(state), "archives", f"code-{name}.tar.gz")
    return archive if os.path.isfile(archive) else None


def repair(state, name, why, seed=None, now=time.time):
    """The released files back into the layer name (appcode.restore), the edit kept aside; layers/restored.json for the
    core (it writes app.code_restored). why: start — the core did not come up on the modified layer; owner — "Return
    the released". -> True when the layer is the release again. Never raises."""
    codes = layers_dir(state)
    record = {"name": name, "at": now(), "why": why}
    try:
        source = source_of(state, name, seed)
        if source is None:
            raise RuntimeError(f"no copy of the release {name} to take the files from")
        got = appcode().restore(os.path.join(codes, name), source)
        record.update(count=len(got["changes"]), paths=[c["path"] for c in got["changes"]][:50], copy=got["copy"])
        ok = True
    except Exception as e:  # noqa: BLE001 — a broken archive (tarfile), anything: said in the record, never raised
        record.update(error=str(e)[:500])
        ok = False
        print(f"Hub by Icons8: the hub's code is not put back to the release: {e}", file=sys.stderr, flush=True)
    with contextlib.suppress(OSError):
        _write(os.path.join(codes, "restored.json"), record)
    return ok


def watch_start(state, run=None):
    """This start's watchdog (watch() in its own process), unless one is already waiting."""
    pid = _read(os.path.join(layers_dir(state), "watch.json"), dict).get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and alive(pid):
        return False
    # -B: nothing is written next to the shell's own files (a signed app on a Mac)
    (run or detached)([sys.executable, "-I", "-B", os.path.abspath(__file__), "--watch", state])
    return True


def watch(state, now=time.time, sleep=time.sleep, clock=time.monotonic):
    """The watchdog of a start on a modified layer: the core marks layers/start.json ok when it comes up; not marked
    within REPAIR_AFTER of the watchdog's own clock (it stops while the machine sleeps; a reboot starts a new watchdog
    and a new window) — the released files go back and hub-up is asked to restart. Starts of the shell meanwhile (the
    service bringing a crashed hub-up back) keep this watchdog and its window. -> what it did: ok | gone (another layer,
    the edit gone) | repaired | failed."""
    codes = layers_dir(state)
    _write(os.path.join(codes, "watch.json"), {"pid": os.getpid(), "at": now()})
    end = clock() + REPAIR_AFTER
    try:
        while True:
            start = _read(os.path.join(codes, "start.json"), dict)
            if start.get("ok"):
                return "ok"
            name = start.get("name")
            if not start.get("modified") or not isinstance(name, str):
                return "gone"
            if clock() >= end:
                break
            sleep(WATCH_TICK)
        if not repair(state, name, "start", now=now):
            return "failed"
        _write(os.path.join(codes, "start.json"), {**start, "modified": False, "at": now()})
        _write(os.path.join(state, "restart"), {"parts": [], "why": "the hub's code was modified and did not start"})
        return "repaired"
    finally:
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(codes, "watch.json"))


def restore_now(state, now=time.time):
    """"Return the released" (the owner, "About the hub"): the current layer back to its release, then a restart.
    -> 0 when done."""
    name = appcode().current(os.path.join(state, "code"), layers_dir(state))
    if not name or not repair(state, name, "owner", now=now):
        return 1
    _write(os.path.join(state, "restart"), {"parts": [], "why": "the released code is back"})
    return 0


def ensure_code(seed, mark, state):
    """The code folder: the layer the hub runs (a seed with MANIFEST.json, app spec §3.1 — ensure_layer), or <state>/code,
    a clone of the seed on the first start (a seed that is a git clone); PACKAGED of the shell into it."""
    if is_layer(seed):
        return ensure_layer(seed, mark, state)
    code = os.path.join(state, "code")
    if not os.path.isdir(os.path.join(code, ".git")):
        part = code + ".part"
        if os.path.lexists(part):   # a first copy that was cut off; git's objects are read-only on Windows
            shutil.rmtree(part, onexc=_writable_again)
        os.makedirs(state, exist_ok=True)
        # --no-hardlinks: the seed sits inside the read-only (signed, on a Mac) shell; the clone must be all its own
        git("clone", "-q", "--no-hardlinks", seed, part)
        git("remote", "set-url", "origin", git("remote", "get-url", "origin", cwd=seed), cwd=part)
        # PACKAGED is the shell's, not the code's: ignored in this clone whatever the release's .gitignore says
        with open(os.path.join(part, ".git", "info", "exclude"), "a") as f:
            f.write("\n/PACKAGED\n/PACKAGED.part\n")
        bytecode(seed, part)
        os.replace(part, code)
    else:
        move_to_seed(seed, mark, code, state)
    tmp = os.path.join(code, "PACKAGED.part")
    shutil.copyfile(mark, tmp)
    os.replace(tmp, os.path.join(code, "PACKAGED"))
    return code


def alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if WINDOWS:
        try:   # bytes, as trial.alive: tasklist's numbers carry the console code page's no-break space
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True,
                                 timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            return True   # cannot tell: do not start a second watchdog on a guess (as trial.alive)
        return f'"{pid}"'.encode() in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:   # a process that exited and nobody reaped (a container whose first process is no init) is gone
        with open(f"/proc/{pid}/stat", "rb") as f:
            return f.read().rsplit(b")", 1)[-1].split()[:1] != [b"Z"]
    except OSError:
        return True


def _outside(cmd):
    """Windows: cmd started by WMI (Win32_Process.Create), outside every job of this process — as platforms/windows
    popen_outside (this file imports nothing of the hub). True when WMI started it."""
    script = ("$ErrorActionPreference = 'Stop'; $si = New-CimInstance -CimClass (Get-CimClass Win32_ProcessStartup) "
              "-ClientOnly -Property @{ShowWindow = [uint16]0}; $r = Invoke-CimMethod -ClassName Win32_Process "
              "-MethodName Create -Arguments @{CommandLine = $env:HUB_OUTSIDE_CMD; ProcessStartupInformation = $si}; "
              "if ($null -eq $r -or $null -eq $r.ProcessId) { exit 9 }; exit [int]$r.ReturnValue")
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                           env={**os.environ, "HUB_OUTSIDE_CMD": subprocess.list2cmdline(cmd)}, capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=60, creationflags=0x08000000)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


_DETACHED = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP (as platforms.windows)
_BREAKAWAY_FROM_JOB = 0x01000000      # CREATE_BREAKAWAY_FROM_JOB: needs the job's BREAKAWAY_OK


# what a process started apart under systemd keeps of the caller's environment (platforms/posix.py _OUTSIDE_ENV)
OUTSIDE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "LC_MESSAGES", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "TMPDIR")


def detached(cmd):
    """Apart from the launcher, outliving it (its own group / outside the job on Windows). A job without the right to
    break away (a scheduler task, runas) refuses CREATE_BREAKAWAY_FROM_JOB: then without it, as
    platforms.windows.popen_detached."""
    if WINDOWS:
        if _outside(cmd):   # outside every job: the scheduler task's ends with `schtasks /End` and may not be left
            return None
        quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        try:
            return subprocess.Popen(cmd, creationflags=_DETACHED | _BREAKAWAY_FROM_JOB, **quiet)
        except PermissionError:   # a job without the right to leave it (the scheduler task's, probe A0): inside it then
            print("Hub by Icons8: the job refuses a breakaway — the detached process stays in the launcher's job",
                  file=sys.stderr, flush=True)
            return subprocess.Popen(cmd, creationflags=_DETACHED, **quiet)
    if sys.platform.startswith("linux") and os.environ.get("INVOCATION_ID") and shutil.which("systemd-run"):
        # a process of a systemd unit: the unit's stop or restart takes its whole cgroup, a session of its own with it —
        # a transient unit of its own (as platforms/posix.py popen_outside)
        keep = [f"--setenv={k}={v}" for k, v in os.environ.items() if (k.startswith("HUB_") and k != "HUB_TREE")
                or k in OUTSIDE_ENV]
        try:
            if subprocess.run(["systemd-run", "--user", "--collect", "--quiet", *keep, "--", *cmd],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=60).returncode == 0:
                return None
        except (OSError, subprocess.SubprocessError):
            pass
    return subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def resume_trial(state, python, run=detached):
    """A trial still waiting whose watchdog is gone: start the previous release's watchdog again. True if started."""
    try:
        with open(os.path.join(state, "update", "trial.json")) as f:
            trial = json.load(f)
    except (OSError, ValueError):
        return False
    script = os.path.join(state, "update", "trial-run.py")
    if not isinstance(trial, dict) or trial.get("status") != "waiting" or alive(trial.get("pid")) \
            or not os.path.isfile(script):
        return False
    run([python, script, state, "--resume"])
    return True


# --- the move of a hub from a git clone into the app (app spec §8, core/appmove.py): the release's own `hub app` ---

MOVE_TIMEOUT = 6 * 3600   # s: `hub app move` waits for busy sessions up to the owner's restart ceiling (0 — none)


def _hub_env(state):
    """The environment of the release's `hub app …`: this state folder, nothing of a session or a plugin (the command
    is the owner's), the app's git first (already in PATH of the shell)."""
    env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
    env["HUB_STATE_DIR"] = state
    return env


def app_hub(code, state, *args, python=None, run=subprocess.run, timeout=600, out=None):
    """`hub app <args>` of the code folder (the seed before the move, the layer after it) with the app's python; ->
    CompletedProcess (returncode 1 when it could not run). out: a file for its words, else they are captured."""
    cmd = [python or sys.executable, "-I", "-B", "-X", "utf8", os.path.join(code, "bin", "hub"), "app", *args]   # -I drops PYTHONUTF8
    kw = {"stdout": out, "stderr": subprocess.STDOUT} if out is not None else {"capture_output": True}
    try:
        return run(cmd, stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", env=_hub_env(state),
                   timeout=timeout, cwd=tempfile.gettempdir(), creationflags=NO_WINDOW, **kw)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def git_hub(seed, state, python=None, run=subprocess.run):
    """The hub from a git clone this state folder's service runs (the seed's `hub app find --json`, in the owner's
    language): {"repo", "version", "texts": {title, text, move, stay, later, waiting, failed}} | None — no such hub, or
    a seed without the command (an older release)."""
    if not os.path.isfile(os.path.join(seed, "core", "appmove.py")):
        return None
    r = app_hub(seed, state, "find", "--json", python=python, run=run, timeout=120)
    if r.returncode != 0:
        return None
    try:
        got = json.loads((r.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    return got if isinstance(got, dict) and isinstance(got.get("repo"), str) and isinstance(got.get("texts"), dict) \
        else None


if __name__ == "__main__":
    if sys.argv[1:2] == ["--watch"]:
        watch(sys.argv[2])
        sys.exit(0)
    if sys.argv[1:2] == ["--restore"]:
        sys.exit(restore_now(sys.argv[2]))
    seed, mark, state = sys.argv[1:4]
    code = ensure_code(seed, mark, state)
    resume_trial(state, sys.executable)
    print(code)
