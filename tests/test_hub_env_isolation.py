"""Tests do not see the environment of the hub's processes (tests/__init__.py). A run started from a hub process or one
of its sessions carries HUB_*: the previous umask (HUB_USER_UMASK), the "do not tighten permissions" mark
(HUB_PERMS_KEEP), the service, ports, state. Running a test directly with such an environment failed (test_perms.Kept:
HUB_USER_UMASK set umask 002 instead of 022). The update self-check already strips these variables (modules/update);
a manual run did not."""
import glob
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest

import platforms
from tests.fakes import can

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SENTINEL = "polluted-by-hub"
# inputs of the tests and of the release, not the hub's environment: set by whoever starts the run
INPUTS = ("HUB_TEST_", "HUB_COMPAT_")
INPUT_NAMES = ("HUB_REAL_CLAUDE", "HUB_REPO", "HUB_CORE_REPO")
# what the hub puts into its parts (bin/hub-up, core/perms.py, restart, modrun, platforms): read from the code, not from a list
SOURCES = ("bin/hub-up", "core/perms.py", "core/restart.py", "core/modrun.py", "core/modlaunch.py",
           *[os.path.relpath(p, ROOT) for p in glob.glob(os.path.join(ROOT, "platforms", "*.py"))])
NAME = re.compile(r"\bHUB_[A-Z][A-Z0-9_]*\b")

PROBE = textwrap.dedent("""
    import json, os, sys
    sys.path.insert(0, sys.argv[1])
    import tests  # noqa: F401 — isolation, as in any run
    print(json.dumps({k: v for k, v in os.environ.items() if k.startswith("HUB_")}))
""")


def hub_names():
    names = {"HUB_USER_UMASK", "HUB_PERMS_KEEP", "HUB_SERVICE", "HUB_OWNER_CLAUDE_CONFIG_DIR", "HUB_WEB_PORT",
             "HUB_STATE_DIR", "HUB_CONFIG", "HUB_CONFIG_DIR", "HUB_SESSION", "HUB_LANG", "HUB_NO_MODEL"}
    for rel in SOURCES:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            names.update(NAME.findall(f.read()))
    return {n for n in names if not n.endswith("_") and n != "HUB_MOD_"}


def is_input(name):
    return name in INPUT_NAMES or name.startswith(INPUTS)


class HubEnvironment(unittest.TestCase):
    def probe(self, extra=None):
        env = dict(os.environ)
        for k in [k for k in env if k.startswith("HUB_")]:
            del env[k]
        env.update({n: SENTINEL for n in hub_names()})
        env.update(extra or {})
        r = subprocess.run([sys.executable, "-c", PROBE, ROOT], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_names_come_from_the_code_that_starts_the_hub(self):
        names = hub_names()
        self.assertTrue({"HUB_USER_UMASK", "HUB_PERMS_KEEP", "HUB_UP_CHILD", "HUB_TREE"} <= names)

    def test_hubs_variables_do_not_reach_the_tests(self):
        left = {k: v for k, v in self.probe().items() if v == SENTINEL}
        self.assertEqual(sorted(k for k in left if not is_input(k)), [])

    def test_inputs_of_the_run_stay(self):
        out = self.probe({"HUB_TEST_CHROME": "/x/chrome", "HUB_REAL_CLAUDE": "/x/claude", "HUB_COMPAT_SSH": "box"})
        self.assertEqual([out.get(k) for k in ("HUB_TEST_CHROME", "HUB_REAL_CLAUDE", "HUB_COMPAT_SSH")],
                         ["/x/chrome", "/x/claude", "box"])

    def test_the_umask_of_the_hub_does_not_change_kept(self):
        out = self.probe({"HUB_USER_UMASK": "0o2"})
        self.assertNotIn("HUB_USER_UMASK", out)


# Where a program finds the person's profile: the home folder (HOME on Linux and macOS, USERPROFILE on Windows), the
# Windows settings folders and the XDG ones
PROFILE = ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
           "XDG_CACHE_HOME")
# what a test that forgot its own home does: writes into ~ itself, through pathlib, into every profile folder from the
# environment and from a child process; then says where its home was
CARELESS = textwrap.dedent("""
    import json, os, pathlib, subprocess, sys, tempfile
    sys.path.insert(0, sys.argv[1])
    import tests  # noqa: F401 — isolation, as in any run
    for path in (os.path.expanduser("~/.claude.json"), pathlib.Path.home() / ".gitconfig",
                 *[os.path.join(os.environ[k], "hub-test-probe") for k in sys.argv[2:] if os.environ.get(k)]):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
    subprocess.run([sys.executable, "-c", "import os; open(os.path.expanduser('~/child-probe'), 'w').close()"], check=True)
    print(json.dumps({"home": os.path.expanduser("~"), "tmp": tempfile.gettempdir()}))
""")


class Profile(unittest.TestCase):
    """Tests do not write into the person's profile (a test-suite review finding): the update self-check runs the
    whole suite in everyone's real profile, and a test without its own home (`home=`, HOME) would write into
    ~/.claude.json, ~/.local/bin, ~/Library/LaunchAgents, %APPDATA% — tests/__init__.py gives the run its own."""

    def person(self, **extra):
        """The environment of someone whose profile is a temporary folder: every profile variable points into it."""
        home = tempfile.mkdtemp(prefix="person-")
        self.addCleanup(platforms.current.rmtree, home)
        env = {k: v for k, v in os.environ.items() if k not in PROFILE + ("PYTHONUSERBASE", "HUB_TEST_CHROME",
                                                                          "HUB_TEST_CLAUDE", "HUB_TEST_REAL_HOME")}
        env.update(HOME=home, USERPROFILE=home, APPDATA=os.path.join(home, "AppData", "Roaming"),
                   LOCALAPPDATA=os.path.join(home, "AppData", "Local"), XDG_CONFIG_HOME=os.path.join(home, ".config"),
                   XDG_DATA_HOME=os.path.join(home, ".local", "share"), XDG_STATE_HOME=os.path.join(home, ".local", "state"),
                   XDG_CACHE_HOME=os.path.join(home, ".cache"), **extra)
        return home, env

    def run_py(self, code, env, *args):
        r = subprocess.run([sys.executable, "-c", code, ROOT, *args], env=env, capture_output=True, text=True,
                           timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_a_test_without_its_own_home_writes_into_the_runs_profile(self):
        home, env = self.person()
        out = json.loads(self.run_py(CARELESS, env, *PROFILE))
        left = [os.path.relpath(os.path.join(d, f), home) for d, dirs, files in os.walk(home) for f in dirs + files]
        self.assertEqual(left, [], "a test without its own HOME wrote into the person's profile")
        self.assertTrue(os.path.normcase(out["home"]).startswith(os.path.normcase(out["tmp"]) + os.sep), out)

    def test_child_processes_keep_the_persons_python_packages(self):
        # pip --user (tzdata on Windows, cryptography) lies in the profile: hubd, the CLI and modules started by the
        # tests import from there as before
        code = ("import site, subprocess, sys\nsys.path.insert(0, sys.argv[1])\n%s\n"
                "print(subprocess.run([sys.executable, '-c', 'import site; print(site.getuserbase())'],"
                " capture_output=True, text=True, check=True).stdout.strip())")
        _, env = self.person()
        self.assertEqual(self.run_py(code % "import tests", env), self.run_py(code % "", env))

    def test_the_tests_browser_from_the_persons_profile_is_still_found(self):
        # chrome-headless-shell is installed into ~/.cache (puppeteer): without it the page tests on macOS are
        # skipped silently, on Linux they take another browser
        home, env = self.person()
        shell = os.path.join(home, ".cache", "puppeteer", "chrome-headless-shell", "x-1", "chrome-x", "chrome-headless-shell")
        os.makedirs(os.path.dirname(shell))
        open(shell, "w").close()
        os.chmod(shell, 0o755)
        code = ("import sys\nsys.path.insert(0, sys.argv[1])\nfrom modules.testchrome import chrome\n"
                "chrome.sandbox_blocked = lambda: False   # not the system Chrome first (Ubuntu with AppArmor)\n"
                "import tests\nprint(chrome.find())")
        self.assertEqual(self.run_py(code, env), os.path.normpath(shell))

    def test_a_browser_not_from_the_profile_is_not_made_a_setting(self):
        # the system Chrome or none: found the same with the run's home — HUB_TEST_CHROME stays unset, as it was
        _, env = self.person()
        code = "import os, sys\nsys.path.insert(0, sys.argv[1])\nimport tests\nprint(os.environ.get('HUB_TEST_CHROME'))"
        self.assertEqual(self.run_py(code, env), "None")

    @unittest.skipUnless(can.shebang(), "a program by its #! line — only where that line is read (not Windows)")
    def test_a_program_by_its_shebang_gets_the_runs_python(self):
        # the fakes (Chrome, MCP, claude) and tools/publish.sh start `python3` from PATH; the shim of a version manager
        # (mise, asdf) finds its version through the profile — in the run's home there is none
        home, env = self.person()
        shims, prog = os.path.join(home, "shims"), os.path.join(home, "prog")
        os.makedirs(shims)
        for path, text in ((os.path.join(shims, "python3"), '#!/bin/sh\n[ "$HOME" = %s ] || exit 97\nexec %s "$@"\n'
                            % (shlex.quote(home), shlex.quote(sys.executable))),
                           (prog, "#!/usr/bin/env python3\nimport sys\nprint(sys.executable)\n")):
            with open(path, "w", newline="\n") as f:
                f.write(text)
            os.chmod(path, 0o755)
        env["PATH"] = shims + os.pathsep + env.get("PATH", "")
        code = ("import subprocess, sys\nsys.path.insert(0, sys.argv[1])\nimport tests\n"
                "print(subprocess.run([sys.argv[2]], capture_output=True, text=True, check=True).stdout.strip())")
        self.assertTrue(os.path.samefile(self.run_py(code, env, prog), sys.executable))

    def test_the_real_claude_of_the_live_tests_is_still_found(self):
        # the live tests (test_gateway_live and the ones after it) take the claude installed in ~/.local/bin
        home, env = self.person()
        claude = os.path.join(home, ".local", "bin", "claude")
        os.makedirs(os.path.dirname(claude))
        open(claude, "w").close()
        os.chmod(claude, 0o755)
        found = ("import subprocess, sys\nsys.path.insert(0, sys.argv[1])\nimport tests\n"
                 "from tests.fakes.realclaude import real_claude\nprint(real_claude(), flush=True)\n")
        # ...and in a run started from a test: its HOME is already the run's
        nested = found + "subprocess.run([sys.executable, '-c', %r, sys.argv[1]], check=True)" % found
        self.assertEqual(os.path.normcase(self.run_py(nested, env)).split(), [os.path.normcase(claude)] * 2)


if __name__ == "__main__":
    unittest.main()
