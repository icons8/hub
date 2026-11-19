#!/usr/bin/env python3
"""Full test run in parallel: the same suite as `python3 -m unittest discover -s tests`, in chunks of several
tests of one module across J processes (a full run took 8.5 min per merge, 40% of it page tests in Chrome in
a single module). Each chunk is its own `python -m unittest` with its own temp folder (tests/__init__.py).

    tools/test.py [-j N] [module|test …]
    tools/test.py [-j N] --changed [base] [--list]

`--changed` runs only the test modules the diff touches (commits since `base` — by default the merge-base with main —
plus the working tree and new files) and prints what it chose and why (core/testsel.py: a static graph of imports and
paths; tests that read the whole tree always run; doubt — a change to this runner, tests/fakes, an unknown file type, too
many files — means a full run, with the reason). A partial run does not replace the full one before a release.

Chunks that were slow last time go first (timing is kept in the system temp folder). The chunks' temp folder (TMPDIR) is in
memory when possible (Linux, /dev/shm: core/testtmp) and is removed after the run. Tests in Chrome run at most three
chunks at once. Failed tests are retried once after all the rest, in one process (as before an update, modules/update);
if only page tests are still red, they get a second retry after a pause; what passed on a retry is green and named in a
"flaky" line. A chunk that died without FAIL/ERROR, a module that does not load, more than RETRY_AT_MOST reds outside
the page or, in the update self-check, a retry that may not fit its time (SELF_CHECK_SECONDS) are red with no retry. The last line is "OK …" or "FAILED …"."""
import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core import appcode, embed, testlock, testtmp  # noqa: E402
CHUNK = 8                       # tests per chunk: the page module (68 tests, ~210 s) splits into nine chunks


def chrome_modules(folder):
    """Test modules for the page in Chrome — those that import tests.test_web_e2e (PageInChrome, its READY): they go into FLAKY
    by themselves, with no manual list (new modules used to be forgotten there — they ran without the shared Chrome lock and without a retry)."""
    out = []
    for f in sorted(os.listdir(folder) if os.path.isdir(folder) else []):
        if not (f.startswith("test_") and f.endswith(".py")):
            continue
        try:
            with open(os.path.join(folder, f), encoding="utf-8") as fh:
                text = fh.read()
            if "test_web_e2e" not in text:   # no such import without the name (parsing all ~430 took 2.5 s a load)
                continue
            tree = ast.parse(text)
        except (OSError, SyntaxError, ValueError):
            continue   # does not parse — will fail by itself on load
        for node in ast.walk(tree):
            if (isinstance(node, ast.ImportFrom) and (node.module == "tests.test_web_e2e" or node.module == "tests" and any(
                    a.name == "test_web_e2e" for a in node.names))) or (isinstance(node, ast.Import) and any(
                    a.name == "tests.test_web_e2e" for a in node.names)):
                out.append(f"tests.{f[:-3]}.")
                break
    return tuple(out)


def chrome_finders(folder):
    """Test modules that find a browser when they load (`CHROME = chrome.find()`, modules/testchrome): they start it."""
    out = []
    for f in sorted(os.listdir(folder) if os.path.isdir(folder) else []):
        if not (f.startswith("test_") and f.endswith(".py")):
            continue
        try:
            with open(os.path.join(folder, f), encoding="utf-8") as fh:
                text = fh.read()
            if "chrome" not in text:   # `chrome.find()` / `testchrome.find()` carry the name
                continue
            body = ast.parse(text).body
        except (OSError, SyntaxError, ValueError):
            continue
        if any(isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Attribute)
               and n.value.func.attr == "find" and isinstance(n.value.func.value, ast.Name)
               and n.value.func.value.id in ("chrome", "testchrome") for n in body):
            out.append(f"tests.{f[:-3]}.")
    return tuple(out)


# the folders of tests: the core's and every embed's (core/embed.py — the tests package looks into them too)
FOLDERS = [os.path.join(ROOT, "tests")] + [d for d in (os.path.join(top, "tests") for _, top in embed.roots(ROOT))
                                           if os.path.isdir(d)]
# in Chrome and "under load": random reds
PAGE_MODULES = tuple(m for f in FOLDERS for m in chrome_modules(f))   # once per load: FLAKY and BROWSER both take them
FLAKY = ("tests.test_web_e2e.", "tests.test_proof.", "tests.test_web_timeline.", "tests.test_stream_hubd.",
         *PAGE_MODULES)
WINDOWS_JOBS = 4                # processes on Windows, which has no getloadavg
WINDOWS = os.name == "nt"
# modules that start a real browser: not in the update self-check (--no-lock, as the updater of any version calls it)
# unless the hub turns them on (update.browser_tests in hub.json; owner 03.11: "let's turn the browser tests off by
# default — they can be turned on separately"): Windows since 27.10 (winstand, 4 cores: with node + Chrome 1096–1139 s of
# the old updater's 1200), every OS since 03.11 (v1.7.0 on a Mac ran out of the 1200 s; the page is 40% of the suite's
# time). The browser tests run before the release; a developer's run (without --no-lock) runs them
BROWSER = ("tests.test_web_e2e.", "tests.test_files_e2e.", *PAGE_MODULES, *(m for f in FOLDERS for m in chrome_finders(f)))
# what the hub installs, the snapshots tested before publishing: release tags the updater fetched from the shared
# repository (modules/update FETCHED) and, for the main channel, the one branch it follows there (update.remote/branch)
RELEASE_TAGS = "refs/hub-update/tags/"
STANDS_ENV = "HUB_TEST_STANDS"   # the folder of the stand cache (tests/fakes/stands.py)
BROWSER_ENV = "AGENTS_HUB_SELF_CHECK_BROWSER"   # on|off from the updater (v1.7.1+): not HUB_*, which every updater strips
UPSTREAM_ENV = "AGENTS_HUB_SELF_CHECK_UPSTREAM"  # <remote>/<branch> the updater follows, from the same updater
RETRY_PAUSE = 30                # seconds before the second retry (an immediate retry meets the same fleet load — a false rollback)
# reds outside the page retried once: more is a breakage, not load — retried in one process, hundreds of them would run the
# update self-check out of its time ("did not finish" instead of "red"); a red run has fewer than five in most cases
RETRY_AT_MOST = 20
# in the update self-check, nor when the red chunks' time (what their retry may take at most: hung tests retried in a row)
# would end it past this — of the updater's 1200 s, with room for listing the tests and the page's second retry
SELF_CHECK_SECONDS = 1000
SECONDS_ENV = "AGENTS_HUB_SELF_CHECK_SECONDS"   # the updater's own limit (update.tests_timeout), if it is not the usual 1200
ROOM = 200                      # seconds of the updater's limit that the retries leave for the rest
STARTED = time.monotonic()
FLAKE_LINE = "flaky (passed on retry): "   # modules/update reads the names after it
SMOKE_SKIPPED = "core tests skipped"   # the line of a run that checked only the smoke step; tools/envmatrix reads it
CHROME_AT_ONCE = 3              # chunks with Chrome at once (eight at load 80 gave 12 reds)
DEV_ONLY = ("tests.test_portability_lint.", "tests.test_public_language.", "tests.test_codeedits_zones.")   # not in the update self-check: a hub's own test is no reason to roll back
CHUNK_KW = {}                   # extra subprocess arguments of a chunk: below normal priority on Windows in the self-check
TIMES = os.path.join(tempfile.gettempdir(), "hub-core-test-times.json")
ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}   # before the tests load: tests/__init__.py changes the environment
RAN = re.compile(r"^Ran (\d+) tests?", re.M)
SKIPPED = re.compile(r"skipped=(\d+)")
FAILED = re.compile(r"^(?:FAIL|ERROR): \S+ \(([\w.]+)\)", re.M)   # in parentheses — the full test name


def ids(names):
    """Test names of the suite in order."""
    sys.path.insert(0, ROOT)
    names = [n if n.startswith("tests.") else "tests." + n for n in names]   # `test_x.A.test_y`, as the docs show it
    loader = unittest.defaultTestLoader
    if names:
        suite = loader.loadTestsFromNames(names)
    else:   # the core's folder, then the embeds' modules by name (tests.test_<name>: the package looks into them)
        suite = loader.discover(os.path.join(ROOT, "tests"), top_level_dir=ROOT)
        more = [f"tests.{f[:-3]}" for folder in FOLDERS[1:] for f in sorted(os.listdir(folder))
                if f.startswith("test_") and f.endswith(".py")]
        if more:
            suite = unittest.TestSuite([suite, loader.loadTestsFromNames(more)])
    out = []

    def walk(s):
        for t in s:
            if isinstance(t, unittest.TestSuite):
                walk(t)
            elif type(t).__name__ in ("_FailedTest", "ModuleImportFailure"):
                out.append(t.id().replace("unittest.loader._FailedTest.", "tests."))   # the module does not load — let it fail by itself
            else:
                out.append(t.id())
    walk(suite)
    return out


def chunks(names):
    """Chunks: consecutive tests of one module, at most CHUNK."""
    out = []
    for name in names:
        mod = name.rsplit(".", 2)[0] if name.count(".") >= 3 else name
        if out and out[-1][0] == mod and len(out[-1][1]) < CHUNK:
            out[-1][1].append(name)
        else:
            out.append((mod, [name]))
    return [names for _, names in out]


CHROME = threading.Semaphore(CHROME_AT_ONCE)


def run(names, env):
    if names and names[0].startswith(FLAKY):
        with CHROME:
            return _run(names, env)
    return _run(names, env)


def spread(parts, j, call, page_at_once=CHROME_AT_ONCE):
    """call(chunk) for every chunk in j threads -> [(chunk, result)] in the order of parts. At most page_at_once chunks in
    Chrome at once, and a thread with no free Chrome place takes a chunk without Chrome instead of waiting for one (03.11:
    the slow Chrome chunks go first, so five of eight pool threads stood on the Chrome semaphore and the rest of the suite
    waited behind the page — the self-check of v1.7.0 on a Mac ran out of the old updater's 1200 s)."""
    page = [i for i, c in enumerate(parts) if c and c[0].startswith(FLAKY)]
    rest = [i for i, c in enumerate(parts) if not (c and c[0].startswith(FLAKY))]
    out, errors, cv, busy = [None] * len(parts), [], threading.Condition(), [0]

    def worker():
        while True:
            with cv:
                while not (page and busy[0] < page_at_once) and not rest and page and not errors:
                    cv.wait()
                if errors or not (page or rest):
                    return
                chrome = bool(page) and busy[0] < page_at_once
                i = page.pop(0) if chrome else rest.pop(0)
                busy[0] += chrome
            try:
                out[i] = call(parts[i])
            except BaseException as e:  # noqa: BLE001 — as pool.map: the first error goes to the caller
                errors.append(e)
            finally:
                with cv:
                    busy[0] -= chrome
                    cv.notify_all()
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, j))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise errors[0]
    return list(zip(parts, out))


def wrap(config_dir=None):
    """What to run each chunk with: `test-wrap.json` in the settings folder — {"argv": [...]} (sandbox, container) — is the
    start of the command before `python -m unittest …`. No file — the chunk runs directly. A broken file — refusal: a run without
    the protection the owner expected is worse than a red one."""
    if config_dir is None:
        sys.path.insert(0, ROOT)
        from core import catalog
        config_dir = catalog.config_dir()
    path = os.path.join(config_dir, "test-wrap.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            argv = json.load(f)["argv"]
        if not (isinstance(argv, list) and argv and all(isinstance(x, str) for x in argv)):
            raise ValueError("argv — a non-empty list of strings")
    except (OSError, ValueError, KeyError, TypeError) as e:
        sys.exit(f"{path}: {e} — fix or delete the file")
    return argv


PREFIX = None   # wrap() for the whole run — main takes it before the layout


def _run(names, env):
    t0 = time.time()
    r = subprocess.run([*(wrap() if PREFIX is None else PREFIX), sys.executable, "-m", "unittest", *names], cwd=ROOT, env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", **CHUNK_KW)   # cmd.exe in a test writes OEM
    return r.returncode, r.stdout + r.stderr, time.time() - t0


def jobs():
    """Up to 8 processes; an overloaded machine (load above two per core) — three: at load 80 eight processes gave
    12 red page tests, while at load 10 with two processes the run took 4.5 min instead of 1.5."""
    cpus = os.cpu_count() or 2
    try:
        busy = os.getloadavg()[0]
    except AttributeError:   # Windows: the load is unknown — at most four (always eight, with their Chrome, took 20 GB)
        return min(WINDOWS_JOBS, cpus)
    except OSError:
        busy = 0
    return min(8, cpus) if busy <= 2 * cpus else min(3, cpus)


load = testlock.load_now   # machine load over a minute, or None if unknown (Windows)


def wait_turn(path=None, every=10, say=print):
    """A full run waits while someone else's is going (the machine lock is core/testlock, shared with the update self-check); keep
    the returned open file until the end of the run."""
    sys.path.insert(0, ROOT)
    from core import testlock
    return testlock.wait_turn(path, every=every, waiting=lambda ahead: say(
        f"waiting: {ahead} {plural(ahead)} ahead in the queue" if ahead else
        "waiting: a full test run is already going on this machine — no free slots or not enough memory, not adding Chrome",
        flush=True))


def modules(n):
    return "page module" if n == 1 else "page modules"


def hub_config():
    """The hub's settings as the self-check can see them (the updater of v1.7.0 and earlier passes only --no-lock and no
    HUB_*): next to the check copy's state folder (<state>/update/work-…), else the hub's usual ones; unreadable — {}."""
    try:
        from core import catalog
        from core.daemon import default_state_dir
        up = os.path.dirname(ROOT)
        return catalog.load_config(os.path.dirname(up) if os.path.basename(up) == "update" else default_state_dir())
    except Exception:  # noqa: BLE001 — the defaults are the safe ones
        return {}


def published(cfg):
    """Refs of what the updater installs: its release tags and the branch it follows (the updater's UPSTREAM_ENV, else
    update.remote/branch as modules/update reads them; a name that is not a plain ref name gives no branch)."""
    said = os.environ.get(UPSTREAM_ENV, "")
    if "/" in said:
        remote, branch = said.split("/", 1)
    else:
        up = cfg.get("update") if isinstance(cfg, dict) else None
        up = up if isinstance(up, dict) else {}
        remote, branch = up.get("remote") or "origin", up.get("branch") or "main"
    ok = all(isinstance(x, str) and re.fullmatch(r"[\w.-]+(/[\w.-]+)*", x)
             and not any(p in (".", "..") for p in x.split("/")) for x in (remote, branch))
    return [RELEASE_TAGS] + ([f"refs/remotes/{remote}/{branch}"] if ok else [])


def release_base(root=ROOT, cfg=None):
    """The published snapshot (published()) the checked copy is built on — a commit or a ref name, or None (not git,
    nothing published here): core/codeedits.release_base, the rule the red band of modules/update uses too. The updater
    merges the release into the hub's code, so it is inside HEAD, and what HEAD has beyond it is the hub's own."""
    from core import codeedits
    refs = published(hub_config() if cfg is None else cfg)
    return codeedits.release_base(root, RELEASE_TAGS, refs[1] if len(refs) > 1 else None)


def own_edits(root=ROOT, cfg=None):
    """(the base snapshot's name or commit, core/codeedits.classify of the copy against it) or None — no answer: tests as
    usual."""
    base = release_base(root, cfg)
    if not base:
        return None
    from core import codeedits
    edits = codeedits.classify(root, base)
    return (base, edits) if edits is not None else None


MODULE_TESTS_LIMIT = 600   # seconds for one own module's tests


def module_tests(root, names, env=None):
    """The own modules' own tests (test*.py in modules/<name>/, run there as the module's repository runs them) ->
    (green, output); a module without tests passes."""
    out, ok = [], True
    for name in names:
        folder = os.path.join(root, "modules", name)
        if not any(f.startswith("test") and f.endswith(".py") for _, _, files in os.walk(folder) for f in files):
            out.append(f"module {name}: no tests of its own")
            continue
        try:
            r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", folder, "-t", folder], cwd=folder,
                               env={**(env or ENV), "PYTHONPATH": root}, capture_output=True, text=True,
                               timeout=MODULE_TESTS_LIMIT, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:   # a hanging test: red with its reason, not a traceback of the runner
            ok = False
            out.append(f"module {name}: red — the tests did not finish within {MODULE_TESTS_LIMIT} s")
            continue
        ok = ok and r.returncode == 0
        tail = (r.stdout + r.stderr).strip()
        out.append(f"module {name}: {'green' if r.returncode == 0 else 'red'}\n{tail[-1500:] if r.returncode else tail[-200:]}")
    return ok, "\n".join(out)


IMPORTED = ("core", "hosts", "platforms")   # the smoke step imports every module of these: the hub's own processes do


def smoke(root=ROOT):
    """What the self-check still checks when the tests are skipped: this Python is one the hub supports, every Python
    file of the hub compiles under it, and the modules of IMPORTED import in a separate process on this machine (a
    stdlib module gone from this Python, a POSIX-only import on Windows) -> the problems, [] if none. About a second."""
    if sys.version_info < (3, 12):
        return [f"Python {sys.version.split()[0]}: the hub needs 3.12 or newer"]
    bad = []
    for path, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__"))
        for f in sorted(files):
            full = os.path.join(path, f)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if not (f.endswith(".py") or rel.startswith("bin/")):
                continue
            try:
                with open(full, "rb") as fh:
                    first = fh.readline(200)
                    if not f.endswith(".py") and not (first[:2] == b"#!" and b"python" in first):
                        continue   # a shell script or data in bin/
                    data = first + fh.read()
            except OSError:
                continue
            try:
                compile(data, full, "exec", dont_inherit=True)
            except (SyntaxError, ValueError) as e:
                bad.append(f"{rel}: {type(e).__name__}: {getattr(e, 'msg', e)} (line {getattr(e, 'lineno', '?')})")
    if bad:
        return sorted(bad)
    # not the shims of a move into an embed (core/embed.SHIMS): they hand on the embed's code, and a tree may have none
    names = [pkg if f == "__init__.py" else f"{pkg}.{f[:-3]}" for pkg in IMPORTED
             for f in sorted(os.listdir(os.path.join(root, pkg)) if os.path.isdir(os.path.join(root, pkg)) else [])
             if f.endswith(".py") and f"{pkg}/{f}" not in embed.SHIMS]
    code = ("import importlib,sys\nsys.path.insert(0,sys.argv[1])\nfor n in sys.argv[2:]:\n    try:\n"
            "        importlib.import_module(n)\n    except BaseException as e:\n"
            "        sys.stderr.write(('SMOKE ' + f'{n}: {type(e).__name__}: {e}'[:300]).replace(chr(10), ' ') + chr(10))")
    try:
        r = subprocess.run([sys.executable, "-I", "-B", "-c", code, root, *names], cwd=root, capture_output=True,
                           text=True, timeout=120, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return [f"import of the hub's modules: {e!r}"[:300]]
    # only its own lines: a module that prints at import is not broken
    return [line[6:] for line in r.stderr.splitlines() if line.startswith("SMOKE ")] or \
        ([f"import of the hub's modules: code {r.returncode}: {r.stderr.strip()[-300:]}"] if r.returncode else [])


def browser_setting():
    """update.browser_tests of this hub (core/version.browser_tests): the updater's word (BROWSER_ENV, from v1.7.1 —
    it knows its own settings, also of a second hub), else what hub_config() sees; unreadable — off."""
    said = os.environ.get(BROWSER_ENV)
    if said in ("on", "off"):
        return said == "on"
    try:
        from core import version
        return version.browser_tests(hub_config())
    except Exception:  # noqa: BLE001 — the default is the safe one
        return False


def plural(n):
    return "run" if n == 1 else "runs"


def yield_machine():
    """The update self-check yields the CPU as the hub's other background jobs do (platforms: POSIX nice +GIVE_WAY,
    inherited by the chunks; Windows: each chunk below normal). Not the lowest: under the hub's own load the core tests
    must still fit the installed updater's 1200 s (code review 03.11)."""
    import platforms
    platforms.current.give_way()
    CHUNK_KW.update(platforms.current.background())


def self_check_jobs(cpus=None):
    """Processes in the self-check: three quarters of the cores, at least two (03.11, chunk times of an 8-core M1 Pro:
    the core alone in four processes took 614 s by hand, and the self-check ran 1.5 times slower than by hand — 15% left
    of the 1200 s; in six, 410 s). Windows keeps its own (jobs: at most four, below normal priority already)."""
    return jobs() if WINDOWS else max(2, (cpus or os.cpu_count() or 2) * 3 // 4)


def main(argv, run=run, names_of=ids, sleep=time.sleep, memory=None, load=load):
    p = argparse.ArgumentParser(description="The whole test run, in parallel")
    p.add_argument("-j", type=int, help="processes (up to 8 by default, fewer on a busy machine; in the self-check — "
                                     "three quarters of the cores)")
    p.add_argument("--no-lock", action="store_true", help="the run lock is already held by the caller (the update self-check)")
    p.add_argument("--browser-tests", choices=("on", "off"),
                   help="browser modules (Chrome): by default all in a manual run, in the self-check — per update.browser_tests")
    p.add_argument("--changed", nargs="?", const="", metavar="base",
                   help="only the tests the diff against the base touches (merge-base with main by default)")
    p.add_argument("--list", action="store_true", help="with --changed: only show the choice, do not run")
    p.add_argument("names", nargs="*")
    a = p.parse_args(argv)
    if a.j is None:
        a.j = min(jobs(), self_check_jobs()) if a.no_lock else jobs()
    if a.no_lock:
        yield_machine()   # before the smoke step too: it imports the whole core
    found = own_edits() if a.no_lock and not a.names and a.changed is None else None
    if found and not found[1]["core"]:
        # owner 03.11: no edits of the core — the core is the release, tested before publishing; own modules (a whole new
        # folder modules/<name>/) do not change it: only their own tests
        base, edits = found
        bad = smoke()
        if bad:
            print("the smoke step failed:\n" + "\n".join(bad[:20]), flush=True)
            print("FAILED (smoke step)")
            return 1
        ok, said = module_tests(ROOT, edits["modules"]) if edits["modules"] else (True, "")
        print(f"{SMOKE_SKIPPED}: the core code matches release {base.rsplit('/', 1)[-1][:12]} (checked before publishing); "
              f"smoke step — Python {sys.version.split()[0]}, the code compiles, the core modules import")
        if said:
            print(said, flush=True)
        print("OK" if ok else "FAILED (tests of the hub's own modules)")
        return 0 if ok else 1
    # Windows: never the browser in the self-check (27.10, 4 cores: 1096–1139 s of the 1200 with it), whatever the setting
    browser = a.browser_tests == "on" if a.browser_tests else (not WINDOWS and browser_setting() if a.no_lock else True)
    if a.list and a.changed is None:
        sys.exit("--list is only for --changed")
    if a.changed is not None and a.names:
        sys.exit("--changed and test names together make no sense: pick one")
    if a.changed and a.changed.startswith("tests."):
        sys.exit(f"--changed takes a base to compare with (a branch, a commit), not a test name: {a.changed}")
    picked, needs_lock = None, False
    if a.changed is not None:
        from core import testsel
        res = testsel.select(ROOT, a.changed or None)
        print("\n".join(testsel.report(res)), flush=True)
        if a.list:
            return 0
        if not res["full"]:
            picked = [testsel.module_name(t) for t in sorted({**res["tests"], **res["sentinels"]})]
            if not picked:   # a list of no names would be read as "everything"
                print("no test touches what changed — nothing to run", flush=True)
                return 0
            a.names = picked
            needs_lock = testsel.heavy(res)
    global PREFIX
    # before the layout (ids): it loads the tests package, and tests/__init__.py sets its own HUB_CONFIG_DIR in this process too —
    # wrap() per chunk then did not see test-wrap.json, and chunks ran without the sandbox
    PREFIX = wrap()
    # a partial run goes without the machine lock, except a heavy selection (core/testsel.heavy: Chrome, a third of the suite)
    held = wait_turn() if not a.no_lock and (needs_lock or not a.names) else None
    try:
        with open(TIMES) as f:
            took = json.load(f)
    except (OSError, ValueError):
        took = {}
    mem = (memory or in_memory)()   # also before the layout: the lock slots are in test-lock.json of the settings folder
    stands = None
    try:
        names = names_of(a.names)
        if a.no_lock and not a.names:   # the self-check: tests of the code, not the lint of how the hub's own tests are written
            names = [n for n in names if not n.startswith(DEV_ONLY)]
        if not browser:
            page = {n.rsplit(".", 2)[0] for n in names if n.startswith(BROWSER)}
            names = [n for n in names if not n.startswith(BROWSER)]
            if page:
                why = ("--browser-tests off" if a.browser_tests else "self-check, Windows" if WINDOWS else
                       "self-check; to turn on — update.browser_tests: true in hub.json")
                print(f"skipped {len(page)} {modules(len(page))} ({why})", flush=True)
        parts = sorted(chunks(names), key=lambda c: -took.get(c[0], 0))
        stands = stands_folder(mem[0] if mem else None)
        env = {**ENV, "TMPDIR": mem[0]} if mem else dict(ENV)
        env.pop(STANDS_ENV, None)
        if stands:
            env[STANDS_ENV] = stands
        return _main(a, parts, took, env, run, sleep, load)
    finally:
        if stands:
            try:   # the cache of stands is no reason to lose the run's result or to leave the memory folder behind
                appcode.remove_tree(stands)
            except OSError:
                pass
        if mem:
            testtmp.remove(*mem)


def stands_folder(parent):
    """The run's cache of built test stands (tests/fakes/stands.py) - one folder for all the chunks, in the run's temp folder
    (parent) or in the system one; removed after the run. It is named as a run of the tests and carries our pid mark: if this
    process is killed, the next run of the tests sweeps it (modules/testchrome/litter). None - no folder, the tests build."""
    try:
        folder = tempfile.mkdtemp(prefix="hub-core-tests-run-", dir=parent)
    except OSError:
        return None
    try:
        with open(os.path.join(folder, "pid"), "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    return folder


def in_memory():
    """Temp folder of the chunks in memory (core/testtmp, Linux) or None — as before, the system one."""
    try:
        cfg = testlock.settings()
    except Exception:  # noqa: BLE001 — the slots setting does not block the run
        cfg = None
    return testtmp.make(cfg["slots"] if cfg else 1)


def names_in(outs):
    """Failed tests' full names in the outputs: in order, each once (a test prints a FAIL line per failing subTest)."""
    return list(dict.fromkeys(n if n.startswith("tests.") else "tests." + n for out in outs for n in FAILED.findall(out)))


def self_check_seconds():
    """The time the self-check's retries may reach: the updater's limit (SECONDS_ENV) less ROOM, else SELF_CHECK_SECONDS."""
    try:
        return max(0, int(ENV[SECONDS_ENV]) - ROOM)
    except (KeyError, ValueError):
        return SELF_CHECK_SECONDS


def _main(a, parts, took, env, run, sleep, load):
    t0 = time.time()
    # a red under load cannot be told from a real one — the load is recorded at the start and before each retry (average
    # over a minute: before the first retry it still has the tail of our own run, before the second, after RETRY_PAUSE, less of it);
    # the line goes into the tail the developer receives
    loads = [("at the start", load())]
    done = spread(parts, a.j, lambda c: run(c, env))
    took.update({c[0]: sec for c, (_, _, sec) in done})
    try:
        with open(TIMES, "w") as f:
            json.dump(took, f)
    except OSError:
        pass
    ran = sum(int(m.group(1)) for _, (_, out, _) in done for m in RAN.finditer(out))
    skipped = sum(int(m.group(1)) for _, (_, out, _) in done for m in SKIPPED.finditer(out.splitlines()[-1] if out else ""))
    bad = [(c, out) for c, (code, out, _) in done if code != 0]
    failed = names_in([out for _, out in bad])
    silent = [(c, out) for c, out in bad if not FAILED.search(out)]   # killed or crashed without FAIL/ERROR: nothing to retry
    note = ""
    other = [n for n in failed if not n.startswith(FLAKY)]
    slow = sum(sec for c, (code, _, sec) in done if code != 0 and not c[0].startswith(FLAKY))   # what their retry may take
    fits = not a.no_lock or time.monotonic() - STARTED + slow <= self_check_seconds()
    if failed and not silent and len(other) <= RETRY_AT_MOST and fits and not any("._FailedTest." in n for n in failed):
        loads.append(("before the retry", load()))
        code, out, _ = run(failed, env)   # once more, in one process (tests research 03.11: a red outside the page cancelled the page retry)
        note = (f"retry of the failed{'' if other else ' page'} tests ({len(failed)}: "
                f"{', '.join(n.rsplit('.', 1)[1] for n in failed)}): {'passed' if code == 0 else 'red again'}\n")
        still = names_in([out]) if code != 0 else []
        page = [n for n in failed if n.startswith(FLAKY)]
        # the page under load: a second retry after a pause, of its tests that failed (a setUpClass line names the whole
        # class), once only page ones are left red — or the retry died without names and they were all page ones; any
        # other red gets one
        if code != 0 and page and (all(n.startswith(FLAKY) for n in still) if still else not other):
            sleep(RETRY_PAUSE)
            loads.append(("before the second retry", load()))
            code, out, _ = run(page, env)
            note += f"second retry after {RETRY_PAUSE} s: {'passed' if code == 0 else 'red again'}\n"
        bad = [] if code == 0 else [(still or failed, out)]
        if code == 0:   # green, but seen: the update self-check puts the names into its journal fact (update.applied)
            note += f"{FLAKE_LINE}{', '.join(failed)}\n"
    for names, out in bad:
        print(f"===== {names[0]}{' …' if len(names) > 1 else ''}\n{out.strip()}\n", flush=True)
    print(note, end="")
    if loads[0][1] is not None:
        print(f"load ({os.cpu_count() or '?'} cores): " + ", ".join(f"{w} {x:.1f}" for w, x in loads if x is not None))
    print(f"Ran {ran} tests in {time.time() - t0:.1f}s ({len(parts)} chunks, {a.j} processes)")
    if bad:
        print(f"FAILED (chunks: {len(bad)})")
        return 1
    print(f"OK (skipped={skipped})" if skipped else "OK")
    return 0


if __name__ == "__main__":
    import platforms
    platforms.current.utf8_mode([os.path.abspath(__file__)] + sys.argv[1:])   # chunk output is UTF-8 in any locale
    platforms.current.oom_first(os.getpid(), 1000)   # Linux: the run and its chunks are the first victims of memory shortage, not the hub
    sys.exit(main(sys.argv[1:]))
