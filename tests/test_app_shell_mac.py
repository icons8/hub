"""The stand "a broken app" on a real Mac (app spec §4.4, slice A4): tools/package/shellswap.py with the real hdiutil,
ditto, codesign and xattr on two ad-hoc signed bundles in a disk image, under a supervisor of its own that does what
launchd does with KeepAlive — starts the app by its path again whenever it exits, `kickstart -k` kills it (the Mac
bridge's hidden account takes no launchd agents; the real gui/<uid> agent is the release's check, slice A9).

- a good new app: swapped in one step, checked in place, started by its path, its core marks the start — done, the
  old app kept as .old;
- a new app that never brings the core up (exits at once, the supervisor keeps starting it): its window passes, the
  old app is back at the path and runs again, the new one is .bad;
- a seal broken after signing, and a quarantined image: the first is refused before anything moves, the second is
  taken without its quarantine (A0: a quarantined ad-hoc app is killed with SIGKILL at every start)."""
import tests  # noqa: F401 - keeps the machine's settings out
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import platforms

MAC = sys.platform == "darwin" and all(shutil.which(p) for p in ("codesign", "hdiutil", "ditto", "clang", "xattr"))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(REPO, "tools", "package")

LAUNCHER = r"""
#include <limits.h>
#include <mach-o/dyld.h>
#include <spawn.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
extern char **environ;
int main(void) {
    char exe[PATH_MAX], script[PATH_MAX];
    uint32_t n = sizeof exe;
    if (_NSGetExecutablePath(exe, &n)) return 126;
    char *cut = strrchr(exe, '/'); if (!cut) return 126; *cut = 0;     /* .../Contents/MacOS */
    cut = strrchr(exe, '/'); if (!cut) return 126; *cut = 0;           /* .../Contents */
    snprintf(script, sizeof script, "%s/Resources/run.sh", exe);
    char *args[] = {"/bin/sh", script, NULL};
    pid_t p;
    if (posix_spawn(&p, "/bin/sh", NULL, NULL, args, environ)) return 127;
    int st;
    if (waitpid(p, &st, 0) < 0) return 1;
    return WIFEXITED(st) ? WEXITSTATUS(st) : 128 + WTERMSIG(st);
}
"""
# what each app does when started: the old one runs like a hub; the good one marks its core's start; the broken exits
RUN = {
    "old": 'echo "old $$" >> "$STAND_MARKS"\nexec sleep 1000\n',
    "good": 'echo "good $$" >> "$STAND_MARKS"\n"$STAND_PY" -c \'import json, sys, time; json.dump({"name": "x", '
            '"ok": True, "at": time.time()}, open(sys.argv[1], "w"))\' "$STAND_STATE/layers/start.json"\n'
            'exec sleep 1000\n',
    "broken": 'echo "broken $$" >> "$STAND_MARKS"\nexit 1\n',
}


def sh(*args):
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode:
        raise AssertionError(f"{args}: {r.returncode} {r.stdout}{r.stderr}")
    return r.stdout


@unittest.skipUnless(MAC, "macOS with codesign, hdiutil, ditto, clang")
class BrokenApp(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="hub-a4-mac-"))
        self.addCleanup(platforms.current.rmtree, self.root)
        sys.path.insert(0, PKG)
        import shellswap
        self.sw = shellswap
        self.state = os.path.join(self.root, "state")
        os.makedirs(os.path.join(self.state, "layers"))
        self.marks = os.path.join(self.root, "marks")
        open(self.marks, "w").close()
        self.exe = os.path.join(self.root, "launcher")
        src = os.path.join(self.root, "launcher.c")
        with open(src, "w") as f:
            f.write(LAUNCHER)
        sh("clang", "-Os", "-o", self.exe, src)
        self.apps = os.path.join(self.root, "Applications")
        self.app = os.path.join(self.apps, "Agents Hub.app")
        self.bundle(self.app, 1, "old")
        with open(os.path.join(self.state, "layers", "shell.json"), "w") as f:
            json.dump({"launch": os.path.join(self.app, "Contents", "Resources", "hub", "launch.py"),
                       "python": sys.executable}, f)
        self.pidfile = os.path.join(self.root, "child.pid")
        kick = [sys.executable, "-c", "import os, signal, sys\ntry:\n os.killpg(int(open(sys.argv[1]).read()), "
                "signal.SIGTERM)\nexcept (OSError, ValueError):\n pass", self.pidfile]
        env = mock.patch.dict(os.environ, {"HUB_SHELL_KICK": json.dumps(kick), "HUB_SHELL_WINDOW": "25",
                                           "STAND_MARKS": self.marks, "STAND_STATE": self.state,
                                           "STAND_PY": sys.executable})
        env.start()
        self.addCleanup(env.stop)
        self.stop = threading.Event()
        self.supervisor = threading.Thread(target=self.keep_alive, daemon=True)
        self.supervisor.start()
        self.addCleanup(self.end_supervisor)
        self.wait_mark("old", 1)

    # --- the stand

    def bundle(self, app, runtime, behaviour, quarantine=False):
        contents = os.path.join(app, "Contents")
        hub = os.path.join(contents, "Resources", "hub")
        os.makedirs(os.path.join(contents, "MacOS"))
        os.makedirs(hub)
        shutil.copy2(self.exe, os.path.join(contents, "MacOS", "Agents Hub"))
        with open(os.path.join(contents, "Resources", "run.sh"), "w") as f:
            f.write(RUN[behaviour])
        for n in ("launch.py", "shellswap.py"):
            shutil.copy2(os.path.join(PKG, n), hub)
        shutil.copy2(os.path.join(REPO, "core", "appcode.py"), hub)
        with open(os.path.join(hub, "PACKAGED"), "w") as f:
            json.dump({"format": "app", "layout": "layer", "version": "1.3.0", "runtime": runtime,
                       "platform": "mac-arm64"}, f)
        with open(os.path.join(contents, "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleIdentifier": "com.hub-core.app", "CFBundleExecutable": "Agents Hub",
                           "CFBundleName": "Agents Hub", "CFBundlePackageType": "APPL"}, f)
        sh("codesign", "--force", "--sign", "-", "--identifier", "com.hub-core.app", app)
        sh("codesign", "--verify", "--deep", "--strict", app)
        if quarantine:   # as a browser's download leaves it
            for d, dirs, names in os.walk(app):
                for n in dirs + names:
                    sh("xattr", "-w", "com.apple.quarantine", "0083;00000000;Safari;", os.path.join(d, n))

    def image(self, behaviour, quarantine=False, tamper=False):
        folder = os.path.join(self.root, f"img-{behaviour}")
        self.bundle(os.path.join(folder, "Agents Hub.app"), 2, behaviour, quarantine)
        if tamper:   # a sealed resource changed after signing
            with open(os.path.join(folder, "Agents Hub.app", "Contents", "Resources", "run.sh"), "a") as f:
                f.write("# changed\n")
        dmg = os.path.join(self.root, f"agents-hub-v1.3.0-mac-arm64-{behaviour}.dmg")
        sh("hdiutil", "create", "-quiet", "-fs", "APFS", "-srcfolder", folder, "-volname", "Agents Hub", dmg)
        return dmg

    def keep_alive(self):
        """launchd's KeepAlive for the app's path: started again a second after it exits."""
        while not self.stop.is_set():
            p = subprocess.Popen([os.path.join(self.app, "Contents", "MacOS", "Agents Hub")], start_new_session=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with open(self.pidfile, "w") as f:
                f.write(str(p.pid))
            while p.poll() is None and not self.stop.is_set():
                time.sleep(0.2)
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
            time.sleep(1)   # portable: launchd's own pause between starts of a KeepAlive job (ThrottleInterval)

    def end_supervisor(self):
        self.stop.set()
        self.supervisor.join(15)
        try:
            with open(self.pidfile) as f:
                os.killpg(int(f.read()), signal.SIGKILL)
        except (OSError, ValueError):
            pass

    def marks_of(self, kind):
        with open(self.marks) as f:
            return [line for line in f.read().splitlines() if line.startswith(kind + " ")]

    def wait_mark(self, kind, count, seconds=30):
        end = time.monotonic() + seconds
        while len(self.marks_of(kind)) < count:
            if time.monotonic() > end:
                with open(self.marks) as f:
                    self.fail(f"{kind} did not start {count} time(s): {f.read()}")
            time.sleep(0.2)

    def runtime_at(self, app):
        with open(os.path.join(app, "Contents", "Resources", "hub", "PACKAGED")) as f:
            return json.load(f)["runtime"]

    def replace(self, dmg):
        """What the hub and hub-up do: prepare after the download, the watchdog started, hub-up exiting once swapped."""
        self.sw.prepare(self.state, dmg, 2)
        got = {}
        t = threading.Thread(target=lambda: got.setdefault("end", self.sw.swap(self.state)), daemon=True)
        t.start()
        end = time.monotonic() + 60
        while self.sw.read(self.state).get("status") not in ("swapped", "failed"):
            self.assertLess(time.monotonic(), end, "no swap in 60 s")
            time.sleep(0.2)
        with open(self.pidfile) as f:
            os.killpg(int(f.read()), signal.SIGTERM)   # hub-up exits: the supervisor starts the app by its path
        t.join(120)
        return got.get("end")

    # --- the cases

    def test_a_good_new_app_takes_the_place_and_the_old_one_is_kept(self):
        self.assertEqual(self.replace(self.image("good")), "done")
        self.assertEqual(self.runtime_at(self.app), 2)
        self.assertEqual(self.runtime_at(self.app + ".old"), 1)
        self.assertTrue(self.marks_of("good"))
        sh("codesign", "--verify", "--deep", "--strict", self.app)

    def test_a_new_app_that_never_comes_up_gives_the_place_back(self):
        self.assertEqual(self.replace(self.image("broken")), "returned")
        rec = self.sw.read(self.state)
        self.assertEqual((rec["why"], rec["back"]), ("timeout", True))
        self.assertGreater(len(self.marks_of("broken")), 3)   # the supervisor kept starting it, as launchd would
        self.assertEqual(self.runtime_at(self.app), 1)
        self.assertEqual(self.runtime_at(self.app + ".bad"), 2)
        self.wait_mark("old", 2)   # the old app runs again by its path
        sh("codesign", "--verify", "--deep", "--strict", self.app)

    def test_a_broken_seal_is_refused_before_anything_moves(self):
        with self.assertRaises(self.sw.Refused) as e:
            self.sw.prepare(self.state, self.image("good", tamper=True), 2)
        self.assertIn("codesign", str(e.exception))
        self.assertFalse(os.path.exists(os.path.join(self.apps, ".Agents Hub.app.new")))
        self.assertEqual(self.runtime_at(self.app), 1)

    def test_a_quarantined_image_is_taken_without_its_quarantine(self):
        # a quarantined ad-hoc app is killed at every start (A0): what the hub downloaded and checked loses the mark
        self.assertEqual(self.replace(self.image("good", quarantine=True)), "done")
        left = subprocess.run(["xattr", "-r", "-p", "com.apple.quarantine", self.app], capture_output=True, text=True)
        self.assertEqual(left.stdout.strip(), "")

if __name__ == "__main__":
    unittest.main()
