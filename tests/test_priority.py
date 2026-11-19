"""Windows: the hub task starts at below normal priority (Task Scheduler, priority 7) and every child inherited it. When
a heavy program took the whole CPU at normal priority, the core, the page and Telegram got no processor for minutes:
no event in the journal, the page and the bot silent. The hub's parts run at normal priority whatever
hub-up runs at; sessions and plugin processes stay below normal (their heavy commands must not outrun the owner's work).

macOS: the hub service is Interactive (deploy/mac/app.py — a Standard job's light resource limits slowed the core's loop
under load until it read the sessions' facts minutes late); its heavy children start under the utility QoS clamp, at
the level every child of a Standard job had."""
import tests  # noqa: F401 — hide the machine's settings
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import platforms
from platforms import posix, windows

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIN = unittest.skipUnless(os.name == "nt", "priority classes exist only on Windows")

# runs at below normal (as hub-up under the scheduler), starts a child the way hub-up does, prints the child's class
LAUNCHER = """import sys, subprocess
sys.path.insert(0, sys.argv[1])
from platforms import windows
kw = windows.background() if sys.argv[2] == "background" else {}
child = ("import ctypes; k = ctypes.windll.kernel32; k.GetCurrentProcess.restype = ctypes.c_void_p; "
         "k.GetPriorityClass.argtypes = [ctypes.c_void_p]; print(hex(k.GetPriorityClass(k.GetCurrentProcess())))")
p, handle = windows.spawn_part([sys.executable, "-c", child], False, stdout=subprocess.PIPE, **kw)
print(p.communicate(timeout=60)[0].decode().strip(), flush=True)
"""


@WIN
class Parts(unittest.TestCase):
    def child_class(self, kind):
        r = subprocess.run([sys.executable, "-c", LAUNCHER, REPO, kind], capture_output=True, text=True, timeout=90,
                           creationflags=windows.BELOW_NORMAL_PRIORITY)
        self.assertEqual(r.returncode, 0, r.stderr)
        return int(r.stdout.strip(), 16)

    def test_hub_part_runs_at_normal_priority_under_a_below_normal_hub_up(self):
        self.assertEqual(self.child_class("part"), windows.NORMAL_PRIORITY)

    def test_plugin_process_stays_below_normal(self):
        self.assertEqual(self.child_class("background"), windows.BELOW_NORMAL_PRIORITY)


class Background(unittest.TestCase):
    def test_posix_changes_nothing(self):
        self.assertEqual(posix.background(), {})

    def test_session_starts_with_the_background_arguments(self):
        from hosts import streamjson
        from hosts.streamjson import StreamHost
        state = tempfile.mkdtemp(prefix="prio-host-")
        self.addCleanup(shutil.rmtree, state, True)
        host = StreamHost(state, claude=(sys.executable, "-c", "import sys, time; '--help' in sys.argv or time.sleep(30)"))
        seen = []
        real = subprocess.Popen

        def popen(*a, **kw):
            seen.append(kw.pop("creationflags", None))   # the flag itself exists only on Windows
            return real(*a, **kw)
        with mock.patch.object(streamjson.platforms.current, "background", lambda: {"creationflags": 0x4000}), \
                mock.patch.object(streamjson.subprocess, "Popen", popen):
            host.spawn("a", "00000000-0000-0000-0000-000000000001")
        self.addCleanup(host.kill_server)
        self.assertIn(0x4000, seen)

    def test_mac_starts_heavy_work_under_the_utility_clamp(self):
        service = mock.patch.dict(os.environ, {posix.INTERACTIVE: "1"})
        service.start()
        self.addCleanup(service.stop)
        with mock.patch.object(posix.os.path, "exists", lambda p: p == "/usr/sbin/taskpolicy"), \
                mock.patch.object(posix.shutil, "which", lambda c: "/bin/" + c, create=True):
            self.assertEqual(posix.below_hub(["claude", "-p"], platform="darwin"),
                             ["/usr/sbin/taskpolicy", "-c", "utility", "claude", "-p"])
        with mock.patch.object(posix.os.path, "exists", lambda p: False):   # no taskpolicy: the command as it is
            self.assertEqual(posix.below_hub(["claude"], platform="darwin"), ["claude"])
        with mock.patch.object(posix.os.path, "exists", lambda p: p == "/usr/sbin/taskpolicy"), \
                mock.patch.object(posix.shutil, "which", lambda c: None, create=True):
            # a program that is not there: the command as it is, so that Popen raises OSError as before
            self.assertEqual(posix.below_hub(["no-such-program"], platform="darwin"), ["no-such-program"])
        with mock.patch.object(posix.os.path, "exists", lambda p: p == "/usr/sbin/taskpolicy"), \
                mock.patch.object(posix.shutil, "which", lambda c: c if c == "/home/x/ext/./run" else None, create=True):
            # a program relative to the folder it starts in (a plugin's ./run) is looked for there
            self.assertEqual(posix.below_hub(["./run"], cwd="/home/x/ext", platform="darwin"),
                             ["/usr/sbin/taskpolicy", "-c", "utility", "./run"])
        self.assertEqual(posix.below_hub(["claude"], platform="linux"), ["claude"])
        self.assertEqual(windows.below_hub(["claude"]), ["claude"])

    def test_without_the_interactive_service_nothing_is_clamped(self):
        # a service installed before (Standard: its children are at the utility level already) or a hub started from a
        # terminal: the children start as they did
        with mock.patch.dict(os.environ), mock.patch.object(posix.os.path, "exists", lambda p: True), \
                mock.patch.object(posix.shutil, "which", lambda c: "/bin/" + c, create=True):
            os.environ.pop(posix.INTERACTIVE, None)
            self.assertEqual(posix.below_hub(["claude", "-p"], platform="darwin"), ["claude", "-p"])
            os.environ[posix.INTERACTIVE] = "0"
            self.assertEqual(posix.below_hub(["claude", "-p"], platform="darwin"), ["claude", "-p"])

    @unittest.skipUnless(os.path.exists("/usr/sbin/taskpolicy"), "taskpolicy is on macOS only")
    def test_the_clamped_process_is_the_program_itself(self):
        # the core tracks the session by Popen's pid: taskpolicy must exec the program, not wait beside it
        probe = "import os, sys; print(os.getpid(), sys.argv[1:])"
        with mock.patch.dict(os.environ, {posix.INTERACTIVE: "1"}):
            cmd = posix.below_hub([sys.executable, "-c", probe, "x"], platform="darwin")
        self.assertEqual(cmd[0], posix.TASKPOLICY)
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
        out = p.communicate(timeout=60)[0].split(None, 1)
        self.assertEqual(int(out[0]), p.pid)
        self.assertEqual(out[1].strip(), "['x']")

    def test_session_and_archive_parse_start_below_the_hub(self):
        from hosts import streamjson
        from core import history
        state = tempfile.mkdtemp(prefix="prio-below-")
        self.addCleanup(platforms.current.rmtree, state)
        host = StreamHostFor(state)
        seen = []
        real = subprocess.Popen

        def popen(cmd, *a, **kw):
            seen.append(cmd[0])
            kw.pop("creationflags", None)
            return real(cmd[1:] if cmd[0] == "below" else cmd, *a, **kw)
        mark = lambda cmd, platform=None: ["below", *cmd]   # noqa: E731
        with mock.patch.object(streamjson.platforms.current, "below_hub", mark, create=True), \
                mock.patch.object(streamjson.subprocess, "Popen", popen):
            host.spawn("a", "00000000-0000-0000-0000-000000000001")
        self.addCleanup(host.kill_server)
        self.assertIn("below", seen)
        with open(history.__file__, encoding="utf-8") as f:
            self.assertIn('platforms.current.below_hub([sys.executable, "-c", code])', f.read())

    def test_codex_and_tmux_sessions_and_the_parts_but_the_core_s_own_start_below_the_hub(self):
        # Codex and tmux sessions are sessions as claude's are; the scheduler's commands, the agents' Chrome, the
        # self-check's tests are children of their parts: under an Interactive service they would compete with the core
        with open(os.path.join(REPO, "hosts", "codex.py"), encoding="utf-8") as f:
            self.assertIn("subprocess.Popen(platforms.current.below_hub(cmd),", f.read())
        with open(os.path.join(REPO, "hosts", "tmux.py"), encoding="utf-8") as f:
            self.assertIn('platforms.current.below_hub([*platforms.current.spec_loader(), spec, *cmd])', f.read())
        import ast
        with open(os.path.join(REPO, "bin", "hub-up"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        above = [ast.literal_eval(n.value) for n in ast.walk(tree) if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "ABOVE_HUB" for t in n.targets)]
        self.assertEqual(above, [("hubd", "web", "telegram", "files", "gateway")])
        parts = sorted(ast.unparse(c.args[0]) for c in ast.walk(tree) if isinstance(c, ast.Call)
                       and isinstance(c.func, ast.Attribute) and c.func.attr == "spawn_part")
        self.assertEqual(parts, ["cmd", "cmd if name in ABOVE_HUB else plat.below_hub(cmd)",
                                 "plat.below_hub(cmd, cwd=cwd)"])   # plugin, part, module

    def test_hub_up_starts_plugins_in_the_background(self):
        with open(os.path.join(REPO, "bin", "hub-up"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn('cmd = plat.below_hub(exts[name]["cmd"], cwd=exts[name]["cwd"])', text)
        self.assertRegex(text, r'spawn_part\(cmd, False, tree=True,[^)]*\*\*plat\.background\(\)')


def StreamHostFor(state):
    from hosts.streamjson import StreamHost
    return StreamHost(state, claude=(sys.executable, "-c", "import sys, time; '--help' in sys.argv or time.sleep(30)"))


if __name__ == "__main__":
    unittest.main()
