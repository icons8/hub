#!/usr/bin/env python3
"""The suite of one commit in the environments of other people's machines — a check by the publisher before a merge or a
release (not needed by every hub): profiles of what rolled updates back on colleagues' hubs while our own runs were green.

    tools/envmatrix.py [<ref>] [--profiles selfcheck,hostile] [--chrome PATH] [--claude PATH] [--pylib DIR] [-j N]
                       [--work DIR] [--keep] [--only tests.x …]

Each profile gets its own copy of the commit and its own home folder; the suite runs the way the update self-check runs it
(tools/test.py --no-lock in the updater's environment: no HUB_*, the page tests off unless the profile turns them on),
while this script holds the machine's test lock (core/testlock) — other full runs wait for it, as for tools/test.py
(a part of the suite, --only, goes without it, as there).

The copy: a clone of the commit, origin pointing nowhere. One Python — the one that runs this script: a hub from the install
line carries its own runtime, so there is no matrix of Pythons; the supported minimum is held by the suite itself.

The home folder (HOME, USERPROFILE, APPDATA, LOCALAPPDATA) holds decoys: a hub's state and settings, Claude Code's settings
and login, a `hub` command, a service file, an ssh config, a git config with a global ignore. After the run they are
compared: a test that changed or removed one is red (the tests touched the owner's files); files the tests left there are
listed.

Profiles:
  selfcheck  the above
  hostile    LANG=C, TZ=UTC+14, umask 077, only the system's PATH, XDG_* folders of their own, a proxy that answers nothing,
             HUB_* of a live session, a git config that signs commits, has failing hooks and colors its output
  browser    the page tests in Chrome as well (--chrome, else HUB_TEST_CHROME)
  claude     the tests with a real Claude Code (--claude, else `claude` from PATH), without network (proxy 127.0.0.1:9)

The hub bus tests need cryptography >= 47 — without it (--pylib, else what this Python has) they are skipped, and the table
says so.
Exit code: 0 — every profile green and the decoys untouched, 1 — red, 2 — a profile could not run.
It is not a sandbox: run it on your own code (a colleague's PR — through tools/test.py with the machine's wrapper).
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pr_stands import parse, sha_of, tail  # noqa: E402 — the same reading of tools/test.py output as the stands

OK, RED, ERROR = "ok", "red", "error"
DEFAULT = "selfcheck,hostile"
TIMEOUT = 3600
# tools/test.py SMOKE_SKIPPED: the copy is a published release, only the smoke step ran (a ref whose runner printed it
# otherwise is "did not start": run the matrix on a current commit)
SMOKE = "core tests skipped"
NOWHERE = "https://example.invalid/agents-hub-core.git"
DEAD_PROXY = "http://127.0.0.1:9"
TZ = "Pacific/Kiritimati"   # UTC+14: a day ahead of most machines
PROFILES = {
    "selfcheck": {},
    "hostile": {"hostile": True},
    "browser": {"browser": True},
    "claude": {"claude": True},
}
IGNORE = ".DS_Store\n__pycache__/\n*.pyc\n.venv/\n**/.claude/settings.local.json\n.idea/\n*.log\n"
GITCONFIG = ("[user]\n\tname = Decoy Person\n\temail = decoy@example.invalid\n[core]\n\texcludesFile = ~/.config/git/ignore\n"
             "[init]\n\tdefaultBranch = trunk\n[pull]\n\trebase = true\n")
HOSTILE_GIT = (GITCONFIG + "[commit]\n\tgpgsign = true\n[tag]\n\tgpgsign = true\n[gpg]\n\tprogram = envmatrix-no-gpg\n"
               "[core]\n\thooksPath = ~/.githooks\n[color]\n\tui = always\n[status]\n\tshowUntrackedFiles = all\n")
REFUSE = "#!/bin/sh\necho 'envmatrix: the machine hook refuses' >&2\nexit 1\n"
# decoys: what a person's home folder holds; the tests must neither change nor remove any of them
DECOYS = {
    ".hub-core/journal.jsonl": '{"seq": 1, "type": "envmatrix.decoy"}\n',
    ".hub-core/state.json": '{"state": {}}\n',
    ".config/hub-core/hub.json": '{"update": {"enabled": true, "channel": "stable"}}\n',
    ".config/hub-core/accounts.json": '{"accounts": {}}\n',
    ".config/hub-core/contacts.json": "{}\n",
    ".claude.json": '{"envmatrix": "decoy"}\n',
    ".claude/settings.json": '{"envmatrix": "decoy"}\n',
    ".claude/.credentials.json": '{"envmatrix": "decoy"}\n',
    ".local/bin/hub": "#!/bin/sh\necho decoy\n",
    ".config/systemd/user/hub-core.service": "[Unit]\nDescription=decoy\n",
    "Library/LaunchAgents/com.hub-core.plist": "<plist/>\n",
    ".ssh/config": "# decoy\n",
    ".config/git/ignore": IGNORE,
}
# the environment of the process that runs the check is not the updater's: a session's, a hook's, the machine's own XDG
# folders (they point into the real home)
DROP = ("HUB_", "AGENTS_HUB_", "CLAUDE", "ANTHROPIC_", "GIT_")
DROP_EXACT = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "TMPDIR", "TMP", "TEMP",
              "PYTHONDONTWRITEBYTECODE", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "BASH_ENV", "ENV")


def git(cwd, *args, check=True, **kw):
    # the copy is built the same on every machine: none of this machine's git settings (a missing global file is empty)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.path.join(cwd, ".envmatrix-no-gitconfig")})
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, env=env, stdin=subprocess.DEVNULL, **kw)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {tail(r.stderr.decode('utf-8', 'replace'))}")
    return r


def write(root, files, mode=None):
    for rel, text in files.items():
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        if mode:
            os.chmod(path, mode)


def tree(repo, sha, dest):
    """The copy of the commit (see the module text) → dest."""
    git(os.path.dirname(dest), "clone", "-q", "--no-checkout", repo, dest)
    git(dest, "checkout", "-q", "-B", "main", sha)
    # no remote-tracking refs: the runner would otherwise find "the published snapshot" (tools/test.py release_base)
    git(dest, "remote", "remove", "origin")
    git(dest, "remote", "add", "origin", NOWHERE)
    return dest


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def files(root):
    out = {}
    for d, dirs, names in os.walk(root):
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p) and not os.path.islink(p):
                out[os.path.relpath(p, root).replace(os.sep, "/")] = digest(p)
    return out


def home(dest, hostile=False):
    """The home folder with decoys → {path: sha256} of what is there."""
    write(dest, {**DECOYS, ".gitconfig": HOSTILE_GIT if hostile else GITCONFIG})
    if hostile:
        write(dest, {".githooks/pre-commit": REFUSE, ".githooks/commit-msg": REFUSE}, mode=0o755)
    for d in ("AppData/Roaming", "AppData/Local"):
        os.makedirs(os.path.join(dest, *d.split("/")), exist_ok=True)
    return files(dest)


def touched(root, before):
    """What the run did to the home folder: {changed, removed, added} — paths."""
    now = files(root)
    return {"changed": sorted(p for p in before if p in now and now[p] != before[p]),
            "removed": sorted(p for p in before if p not in now),
            "added": sorted(p for p in now if p not in before)}


def environment(profile, base, at_home, tmp, python, chrome=None, claude=None, pylib=None):
    """The environment of the run: the updater's (modules/update run_tests) on a person's machine, plus the profile."""
    env = {k: v for k, v in base.items() if not k.startswith(DROP) and k not in DROP_EXACT}
    env.update({"HOME": at_home, "USERPROFILE": at_home, "APPDATA": os.path.join(at_home, "AppData", "Roaming"),
                "LOCALAPPDATA": os.path.join(at_home, "AppData", "Local"), "TMPDIR": tmp, "TMP": tmp, "TEMP": tmp,
                "PYTHONUTF8": "1", "AGENTS_HUB_SELF_CHECK_BROWSER": "on" if profile.get("browser") else "off",
                "AGENTS_HUB_SELF_CHECK_UPSTREAM": "origin/main", "AGENTS_HUB_SELF_CHECK_SECONDS": "1200"})
    if pylib:
        env["HUB_TEST_PYLIB"] = pylib
    if profile.get("browser") and chrome:
        env["HUB_TEST_CHROME"] = chrome
    if profile.get("claude") and claude:
        env["HUB_TEST_CLAUDE"] = env["HUB_REAL_CLAUDE"] = claude
    if profile.get("claude") or profile.get("hostile"):   # no network: a proxy that answers nothing
        for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            env[k] = DEAD_PROXY
        env.pop("NO_PROXY", None)
        env.pop("no_proxy", None)
    if profile.get("hostile"):
        env.update({"LANG": "C", "LC_ALL": "C", "TZ": TZ,
                    "XDG_CONFIG_HOME": os.path.join(at_home, "xdg", "config"),
                    "XDG_DATA_HOME": os.path.join(at_home, "xdg", "data"),
                    "XDG_STATE_HOME": os.path.join(at_home, "xdg", "state"),
                    "XDG_CACHE_HOME": os.path.join(at_home, "xdg", "cache"),
                    # a run started from a live hub session (2026-10-26: its HUB_* turned a test red)
                    "HUB_SESSION": "envmatrix-session", "HUB_STATE_DIR": os.path.join(at_home, ".hub-core"),
                    "HUB_CONFIG_DIR": os.path.join(at_home, ".config", "hub-core"),
                    "HUB_CONFIG": os.path.join(at_home, ".config", "hub-core", "hub.json")})
        env.pop("LANGUAGE", None)
        if os.name != "nt":
            env["PATH"] = os.pathsep.join(dict.fromkeys([os.path.dirname(python), "/usr/bin", "/bin"]))
    return env


def has_bus_lib(python, copy, env):
    """The hub bus library works for that Python — the core's own check (core/bus.crypto), with HUB_TEST_PYLIB in front as
    tests/test_hubbus.py puts it: a library built for another Python imports its version and fails on the first key."""
    code = ("import os, sys\nlib = os.environ.get('HUB_TEST_PYLIB')\nif lib: sys.path.insert(0, lib)\n"
            "sys.path.insert(0, os.getcwd())\nfrom core import bus\nbus.crypto()\nprint('bus ok')")
    try:
        r = subprocess.run([python, "-c", code], cwd=copy, env=env, capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL)
        return r.returncode == 0 and r.stdout.strip().endswith("bus ok")
    except (OSError, subprocess.SubprocessError):
        return False


def run(name, profile, repo, sha, work, python, only=(), jobs=None, chrome=None,
        claude=None, pylib=None, timeout=TIMEOUT, base=None):
    """One profile: the copy, the home folder, the suite → result {status, detail, ran, failed, reasons, home, bus, seconds}."""
    import platforms
    base = os.environ if base is None else base
    at = os.path.join(work, name)
    if os.path.exists(at):   # a reused --work: git objects are read-only on Windows (platforms.rmtree retries them)
        platforms.current.rmtree(at)
    if os.path.exists(at):
        raise RuntimeError(f"the old profile folder {at} cannot be removed — remove it or take another --work")
    os.makedirs(at)
    copy = tree(repo, sha, os.path.join(at, "hub"))
    at_home = os.path.join(at, "home")
    before = home(at_home, hostile=profile.get("hostile", False))
    tmp = os.path.join(at, "tmp")
    os.makedirs(tmp)
    env = environment(profile, base, at_home, tmp, python, chrome, claude, pylib)
    argv = [python, os.path.join(copy, "tools", "test.py"), "--no-lock", *(["-j", str(jobs)] if jobs else []), *only]
    log = os.path.join(at, "run.log")
    t0 = time.monotonic()
    old = os.umask(0o077) if profile.get("hostile") else None   # inherited by the child: no thread starts a process meanwhile
    try:
        with open(log, "w", encoding="utf-8") as f:
            r = platforms.current.run(argv, timeout=timeout, cwd=copy, env=env, stdout=f, stderr=subprocess.STDOUT,
                                      stdin=subprocess.DEVNULL, **platforms.current.apart())
        code = r.returncode
    except subprocess.TimeoutExpired:
        code = None
    finally:
        if old is not None:
            os.umask(old)
    with open(log, encoding="utf-8", errors="replace") as f:
        out = f.read()
    got = parse(out)
    marks = touched(at_home, before)
    res = {"seconds": round(time.monotonic() - t0), "python": python, "log": log, "home": marks,
           "bus": has_bus_lib(python, copy, env), **got}
    if code is None:
        return {**res, "status": ERROR, "detail": f"did not finish within {timeout} s"}
    if marks["changed"] or marks["removed"]:
        return {**res, "status": RED, "detail": "the tests touched files of the home folder: "
                + ", ".join(marks["changed"] + marks["removed"])}
    smoke = [x for x in out.splitlines() if x.startswith(SMOKE)]
    if smoke and code == 0:   # the copy is the published release: the runner checks only the smoke step, as on a hub
        return {**res, "status": OK, "detail": "smoke step only — " + smoke[0][:200]}
    if got["ran"] is None:
        return {**res, "status": ERROR, "detail": f"the suite did not start (code {code}): {tail(out)}"}
    if code == 0 and got["verdict"] == "OK":
        return {**res, "status": OK, "detail": tail(out, 1)}
    return {**res, "status": RED, "detail": tail(out, 2)}


def table(results):
    lines = []
    for name, r in results.items():
        head = {OK: "green", RED: "red", ERROR: "not run"}[r["status"]]
        bits = [f"{r['ran']} tests" if r.get("ran") is not None else "", f"{r['seconds']} s" if "seconds" in r else "",
                "" if r.get("bus", True) else "bus skipped: no cryptography >= 47"]
        lines.append(f"{name}: {head}" + "".join(f", {b}" for b in bits if b) + f" — {r['detail']}")
        for n in r.get("failed") or []:
            lines.append(f"  {n} — {(r.get('reasons') or {}).get(n, '')}")
        added = (r.get("home") or {}).get("added") or []
        if added:
            lines.append(f"  left in the home folder ({len(added)}): " + ", ".join(added[:10]) + (" …" if len(added) > 10 else ""))
    return "\n".join(lines)


def main(argv=None, say=print, wait_turn=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("ref", nargs="?", default="HEAD")
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--profiles", default=DEFAULT, help=f"comma-separated, of: {', '.join(PROFILES)}")
    ap.add_argument("--chrome")
    ap.add_argument("--claude")
    ap.add_argument("--pylib", help="a folder with cryptography >= 47 for the bus tests (HUB_TEST_PYLIB)")
    ap.add_argument("-j", type=int)
    ap.add_argument("--work", help="the work folder (by default a temporary one, removed if everything is green)")
    ap.add_argument("--keep", action="store_true", help="do not remove the work folder")
    ap.add_argument("--timeout", type=int, default=TIMEOUT)
    ap.add_argument("--only", nargs="+", default=[], metavar="tests.x")
    a = ap.parse_args(argv)
    names = [p.strip() for p in a.profiles.split(",") if p.strip()]
    unknown = [p for p in names if p not in PROFILES]
    if unknown or not names:
        say(f"no such profiles: {', '.join(unknown) or '(empty)'}; there are: {', '.join(PROFILES)}")
        return 2
    try:
        sha = sha_of(a.repo, a.ref)
    except RuntimeError as e:
        say(f"no commit {a.ref}: {e}")
        return 2
    chrome = a.chrome or os.environ.get("HUB_TEST_CHROME")
    claude = a.claude or shutil.which("claude")
    work = a.work or tempfile.mkdtemp(prefix="envmatrix-")
    os.makedirs(work, exist_ok=True)
    results = {}
    if wait_turn is None:
        from core import testlock
        wait_turn = testlock.wait_turn
    # a part of the suite goes without the machine lock, as with tools/test.py
    held = None if a.only else wait_turn(waiting=lambda ahead: say("waiting: a full test run is going on this machine", flush=True))
    try:
        for name in names:
            profile = PROFILES[name]
            if profile.get("browser") and not chrome:
                results[name] = {"status": ERROR, "detail": "no browser for the page tests (--chrome)"}
            elif profile.get("claude") and not claude:
                results[name] = {"status": ERROR, "detail": "no claude (--claude)"}
            else:
                say(f"{name}: running (Python {sys.version.split()[0]})", flush=True)
                try:
                    results[name] = run(name, profile, a.repo, sha, work, sys.executable, a.only, a.j,
                                        chrome, claude, a.pylib, a.timeout)
                except (OSError, RuntimeError) as e:
                    results[name] = {"status": ERROR, "detail": str(e)}
            say(table({name: results[name]}), flush=True)
    finally:
        if held:
            held.close()
    green = all(r["status"] == OK for r in results.values())
    if not a.work and not a.keep and green:
        shutil.rmtree(work, ignore_errors=True)
    say(f"commit {sha[:9]}{' (only ' + ' '.join(a.only) + ')' if a.only else ''}:\n{table(results)}"
        + ("" if green and not a.work and not a.keep else f"\nrun folder: {work}"))
    return 0 if green else 2 if any(r["status"] == ERROR for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
