"""The hub's own Claude Code (spec d2263524): the version its release names (core/version.py CLAUDE_TESTED, pinned by
publish.sh into every snapshot) in `<state>/claude-code/<version>/claude[.exe]`, and the one place every process of the
hub takes its claude from. The person's claude (~/.local/bin, ~/.local/share/claude, npm, Homebrew) is never installed,
updated or read.

- `command()` — what to run: HUB_CLAUDE (as before), else the release's own copy when it is there, else claude from PATH
  or ~/.local/bin as before (a working copy names no version, a hub with updates off, the release before its copy came).
  Only the exact version: the sessions run on the tested one or, until it is there, as they did. A process of the
  previous release imports this file fresh from the new tree (the critic, the watchdog line — core/realclaude), so this
  part needs only the stdlib and `platforms.current` names every release since 1.7 has.
- `fetch()` — the release's version into the folder, by processes of the new code only (the update, «Setup»,
  `hub setup claude`): the same copy of another hub of this user (instance registry) as a hard link when its size and
  sha256 are the signed manifest's, else Anthropic's signed manifest (core/claudeinstall: the pinned fingerprint) and
  the file (core/fetch: in chunks, a broken download goes on, sha256 and size), then the file's own `--version`.
- `gc()` — three versions stay: the running one and the two other most recently active (the prepared one and the
  previous release, which a rollback takes), plus the newest unfinished download and whatever a process still runs.
- ENV — Claude Code's own updates off in every process of the hub: a copy run from this folder could otherwise put a
  newer one into the person's ~/.local/bin (its "native" install method is in the shared ~/.claude.json)."""
import errno
import os
import re
import shutil
import time

import platforms

DIR = "claude-code"
ENV = {"DISABLE_AUTOUPDATER": "1", "DISABLE_UPDATES": "1"}
ACTIVE = ".active"   # its mtime: when the hub last started on this version, switched spawns to it, or fetched it
PART = ".part-"
LOCK = ".lock"
LOCK_TAKEN = (errno.EAGAIN, errno.EACCES, errno.EDEADLK)   # flock / msvcrt.locking: another process holds it
PART_DAYS = 7        # an unfinished download stays longer than the update's retry of a failed one (a day)
KEEP_OTHERS = 2      # besides the running version: the prepared one and the previous release
SPARE = 500 * 10**6  # bytes left free after a download: a chosen margin for the journal and the sessions, not a measure
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTED = re.compile(r'^CLAUDE_TESTED\s*=\s*"(\d+\.\d+\.\d+)"', re.M)
VERSION = re.compile(r"\d+\.\d+\.\d+")


def named(text):
    """The version a core/version.py text names for its own copy (CLAUDE_TESTED) or None: an empty one (a working
    copy), or MIN_CLAUDE of a release before this file, whose code runs claude from PATH and has no use for a copy."""
    m = TESTED.search(text or "")
    return m.group(1) if m else None


def release(repo=REPO):
    """The Claude Code version the code at `repo` names (named(): its core/version.py read as text — the tree on disk,
    not a module in memory) or None. HUB_TEST_CLAUDE_RELEASE, when set — the tests' own for this tree, "" for none
    (tests/__init__.py: a snapshot's pinned version must not change what they test)."""
    if repo == REPO and "HUB_TEST_CLAUDE_RELEASE" in os.environ:
        return os.environ["HUB_TEST_CLAUDE_RELEASE"] or None
    try:
        with open(os.path.join(repo, "core", "version.py"), encoding="utf-8") as f:
            return named(f.read())
    except (OSError, ValueError):
        return None


def state_dir():
    """This hub's state folder as the core finds it (core/daemon.default_state_dir), without importing the core."""
    if os.environ.get("HUB_STATE_DIR"):
        return os.environ["HUB_STATE_DIR"]
    try:
        from core import statemark
        return statemark.default_state() or platforms.current.state_dir()
    except Exception:  # noqa: BLE001 — no state folder known: no own copy
        return None


def folder(state):
    return os.path.join(state, DIR)


def path(state, ver):
    return os.path.join(state, DIR, ver, platforms.current.CLAUDE_EXE)


def mine(state=None, ver=None):
    """The path of the release's own copy when it is there, else None. ver — the version (by default the one the code
    next to this file names)."""
    ver = ver or release()
    state = state or state_dir()
    if not ver or not state:
        return None
    p = path(state, ver)
    return p if os.path.isfile(p) else None


def switches(env=None):
    """Claude Code's own updates off in `env` (os.environ by default): every claude the hub starts inherits it. Only
    where the hub runs its own copy: the person's claude a hub runs from PATH keeps updating itself as before."""
    (os.environ if env is None else env).update(ENV)


def owned(cmd, state):
    """The command `cmd` (a path) is a copy in this hub's folder."""
    try:
        return os.path.commonpath([os.path.realpath(cmd), os.path.realpath(folder(state))]) == \
            os.path.realpath(folder(state))
    except (TypeError, ValueError):
        return False


def command(state=None, ver=None):
    """The claude the hub runs: HUB_CLAUDE (a path, also with spaces, or a command line), the release's own copy (and
    the switches into this process's environment, for whatever it starts), else PATH or ~/.local/bin as before."""
    line = os.environ.get("HUB_CLAUDE")
    if line:
        return [line] if os.path.isfile(line) else platforms.current.split_command(line)
    own = mine(state, ver)
    if own:
        switches()
        return [own]
    return [shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")]


def touch(state, ver):
    """The version is active now (gc keeps the most recently active)."""
    mark = os.path.join(state, DIR, ver, ACTIVE)
    try:
        with open(mark, "a"):
            pass
        os.utime(mark)
    except OSError:
        pass


# --- the processes of the new code only ---------------------------------------------------------------------------

def _active(d):
    try:
        return os.path.getmtime(os.path.join(d, ACTIVE))
    except OSError:
        try:
            return os.path.getmtime(d)
        except OSError:
            return 0.0


def _newest(d):
    """The newest mtime inside a folder (an unfinished download grows its .part)."""
    times = [0.0]
    for base, _, files in os.walk(d):
        for n in files + [""]:
            try:
                times.append(os.path.getmtime(os.path.join(base, n)))
            except OSError:
                pass
    return max(times)


def _remove(p):
    """A version folder away; a file a running program holds (Windows) stays for the next gc. -> removed."""
    try:
        shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.remove(p)
        return True
    except OSError:
        return False


def gc(state, running=None, now=time.time, busy=None, fetching=None):
    """Drop what is not kept (module doc): -> the names removed. running — the version the running code names;
    fetching — the one a fetch is about to download (its unfinished part goes on); busy — a function of real paths ->
    those a program runs from (core/envs.busy; tests give their own)."""
    root = folder(state)
    if not os.path.isdir(root):
        return []
    running = running if running is not None else release()
    names = os.listdir(root)
    versions = [n for n in names if VERSION.fullmatch(n) and os.path.isdir(os.path.join(root, n))]
    parts = [n for n in names if n.startswith(PART)]
    keep = {running} if running else set()
    others = sorted((n for n in versions if n != running), key=lambda n: _active(os.path.join(root, n)), reverse=True)
    keep.update(others[:KEEP_OTHERS])
    fresh = sorted((n for n in parts if now() - _newest(os.path.join(root, n)) < PART_DAYS * 86400),
                   key=lambda n: _newest(os.path.join(root, n)), reverse=True)
    keep.update(fresh[:1])
    if fetching:
        keep.add(PART + fetching)
    drop = [n for n in versions + parts if n not in keep]   # only names fetch makes: nothing else is touched
    if drop:
        if busy is None:
            from core import envs
            busy = envs.busy
        held = busy({os.path.realpath(os.path.join(root, n)) for n in drop})
        drop = [n for n in drop if os.path.realpath(os.path.join(root, n)) not in held]
    removed = [n for n in drop if _remove(os.path.join(root, n))]
    downloads = os.path.join(state, "downloads")   # the previous installer's leftovers (claude-<version>-<platform>)
    if os.path.isdir(downloads):
        for n in os.listdir(downloads):
            if n.startswith("claude-") and _remove(os.path.join(downloads, n)):
                removed.append(os.path.join("downloads", n))
    return removed


def started(state, ver=None):
    """hubd starts: its version is active, the rest is tidied up. Never raises: the start goes on."""
    try:
        ver = ver or release()
        if ver and mine(state, ver):
            touch(state, ver)
        if not os.path.isdir(folder(state)):
            return
        with open(os.path.join(folder(state), LOCK), "a+") as lock:
            try:
                platforms.current.try_lock(lock)
            except OSError:
                return   # a fetch is under way: it tidies up itself
            gc(state, ver)
    except Exception as e:  # noqa: BLE001
        print(f"ownclaude: tidying {folder(state)} failed: {e!r}", flush=True)


def _sibling(state, ver, size, sha):
    """The same version in another hub of this user (the instance registry), checked by size and sha256 -> its path."""
    from core import envs, fetch
    for other in envs.hubs(state)[1:]:
        p = path(other, ver)
        try:
            if os.path.getsize(p) == size and fetch.sha256(p) == sha:
                return p
        except OSError:
            continue
    return None


def _version_of(exe, run):
    import subprocess
    try:
        r = run([exe, "--version"], env={**os.environ, **ENV}, stdin=subprocess.DEVNULL, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    m = VERSION.search(r.stdout or "")
    return m.group(0) if m and r.returncode == 0 else None


def _replace(src, dst, tries=5, pause=1.0):
    """os.replace with a few tries: an antivirus holds a fresh file for a moment (Windows)."""
    for attempt in range(tries):
        try:
            os.replace(src, dst)
            return
        except OSError:
            if attempt == tries - 1:
                raise
            time.sleep(pause)


def fetch(state, ver, force=False, installer=None, run=None, free=None, **kw):
    """The release's Claude Code `ver` into <state>/claude-code/<ver>/ -> its path. claudeinstall.Failed — why not
    (busy: another fetch is under way — not a failure). installer — a claudeinstall.Installer (tests: a fake server);
    kw — its own arguments; run — subprocess.run's signature; free — bytes free (tests)."""
    import subprocess
    from core import claudeinstall, fetch as dl, i18n
    run = run or subprocess.run
    inst = installer or claudeinstall.Installer(state, **kw)
    root = folder(state)
    platforms.current.makedirs_private(root)
    have = path(state, ver)
    if os.path.isfile(have) and not force:
        touch(state, ver)
        return have
    plat = inst.platform or platforms.current.claude_platform()
    if not plat:
        import platform as _p
        raise inst.fail("platform", i18n.t("cli.claude_setup.platform", system=_p.platform()))
    with open(os.path.join(root, LOCK), "a+") as lock:
        try:
            platforms.current.try_lock(lock)
        except OSError as e:
            text = i18n.t("cli.claude_setup.busy")
            if e.errno not in LOCK_TAKEN:   # the lock itself failed (no locks on this disk): a failure, the log says so
                inst.log(f"the lock {os.path.join(root, LOCK)}: {e!r}")
                raise inst.fail("busy", text) from None
            inst.log(f"waiting for the other install: {text}")   # not a failure (the update pass and «Setup» meet here)
            raise claudeinstall.Failed("busy", text) from None
        gc(state, release(), fetching=ver)
        data = inst.manifest(ver)
        name, sha, size = inst.entry(data, ver, plat)
        if os.path.isfile(have) and os.path.getsize(have) == size and dl.sha256(have) == sha:
            inst.log(f"{have} is the signed build {ver} already")
            touch(state, ver)
            return have
        part = os.path.join(root, PART + ver)
        platforms.current.makedirs_private(part)
        new = os.path.join(part, os.path.basename(have))
        twin = None if os.path.isfile(new) else _sibling(state, ver, size, sha)
        if twin:
            try:
                os.link(twin, new)
                inst.log(f"{ver}: linked from {twin}")
            except OSError as e:
                inst.log(f"{ver}: no link from {twin} ({e}), downloading")
                twin = None
        if not twin and not os.path.isfile(new):
            spare = free(root) if free else shutil.disk_usage(root).free
            partial = os.path.getsize(new + ".part") if os.path.isfile(new + ".part") else 0
            need = size - partial + SPARE
            if spare < need:
                raise inst.fail("disk", i18n.t("cli.claude_setup.disk", version=ver, need=f"{need / 1e6:.0f}",
                                                free=f"{spare / 1e6:.0f}", path=root))
            inst.tell(i18n.t("cli.claude_setup.downloading", platform=plat, mb=f"{size / 1e6:.0f}"))
            try:
                dl.download(f"{inst.root}{claudeinstall.RELEASES}/{ver}/{plat}/{name}", new, sha, size,
                            f"Claude Code {ver}", claudeinstall.AGAIN, progress=inst.progress, log=inst.log,
                            say=inst.tell, pause=inst.pause)
            except dl.Failed as e:
                raise inst.fail(e.why, str(e)) from e
            inst.tell(i18n.t("cli.claude_setup.checked"))
        os.chmod(new, 0o755)   # core/fetch puts a plain file; a link carries its source's mode
        got = _version_of(new, run)
        if got != ver:
            shutil.rmtree(part, ignore_errors=True)
            raise inst.fail("version", i18n.t("cli.claude_setup.mismatch", have=got or "?", version=ver))
        if not os.path.isdir(os.path.join(root, ver)):
            _replace(part, os.path.join(root, ver), pause=min(inst.pause, 1.0))
        else:   # --force over a copy that is not the signed one: the file at once, never a moment without one
            try:
                _replace(new, have, pause=min(inst.pause, 1.0))
            except OSError as e:
                raise inst.fail("busy", i18n.t("cli.claude_setup.in_use", path=have)) from e
            shutil.rmtree(part, ignore_errors=True)
        touch(state, ver)
        inst.log(f"Claude Code {ver} at {have}")
        gc(state, release())
    return have


def main(argv, state_dir):
    """hub setup claude [--force]: the release's Claude Code into the hub's folder. -> the exit code."""
    import sys
    from core import claudeinstall, i18n
    if any(a != "--force" for a in argv):
        print(i18n.t("cli.claude_setup.usage"), file=sys.stderr)
        return 2
    ver = release()
    if not ver:   # a working copy: the hub runs claude from PATH — an error when there is none (the install scripts say so)
        found = command(state_dir)[0]
        print(i18n.t("cli.claude_setup.no_release", path=found))
        return 0 if os.path.isfile(found) or shutil.which(found) else 1
    if "--force" not in argv and mine(state_dir, ver):
        print(i18n.t("cli.claude_setup.already", version=ver, path=path(state_dir, ver)))
        return 0
    inst = claudeinstall.Installer(state_dir, say=print, progress=claudeinstall.progress_printer())
    try:
        got = fetch(state_dir, ver, force="--force" in argv, installer=inst)
    except claudeinstall.Failed as e:
        print(str(e), file=sys.stderr)
        return 1
    print(i18n.t("cli.claude_setup.done", version=ver, path=got))
    return 0
