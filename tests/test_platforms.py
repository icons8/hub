"""OS adapters (platforms/, owner decision 2026-10-05, PR #18): POSIX and Windows share one interface and the core has
no OS branches, so a fix for one OS does not touch the others."""
import tests  # noqa: F401
import inspect
import json
import os
import re
import stat
import subprocess
import sys
import unittest
from unittest import mock

from platforms import posix, windows
from tests import winsafe
from tests.fakes import lang

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# what counts as talking to the OS bypassing the adapter
OS_BRANCH = re.compile(r"os\.name|sys\.platform|platform\.system|\bfcntl\b|\bmsvcrt\b|signal\.SIG|creationflags|"
                       r"killpg|start_new_session|os\.execv")
CORE = ["core", "hosts", "hooks", os.path.join("bin", "hub-up"), os.path.join("bin", "hubd")]


def interface(module):
    return {name: str(inspect.signature(fn)) for name, fn in vars(module).items()
            if inspect.isfunction(fn) and not name.startswith("_") and fn.__module__ == module.__name__}


def sources(path):
    full = os.path.join(ROOT, path)
    if os.path.isfile(full):
        yield full
        return
    for d, _, files in os.walk(full):
        yield from (os.path.join(d, f) for f in files if f.endswith(".py"))


def entry_points(root):
    """Hub entry points are the Python files in bin/ with a "#!" line. Files only, first line as bytes: the hub's bin/
    folder holds things that are not in git (__pycache__/, .DS_Store, editor files)."""
    out = []
    for f in sorted(os.listdir(os.path.join(root, "bin"))):
        path = os.path.join(root, "bin", f)
        if f.startswith(".") or not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            first = fh.readline()
        if first.startswith(b"#!") and b"python" in first:
            out.append(os.path.join("bin", f))
    return out


class Platforms(unittest.TestCase):
    def test_both_adapters_have_one_interface(self):
        self.assertEqual(interface(posix), interface(windows))

    def test_a_path_the_page_asks_for_is_a_path_of_the_machine(self):
        # for file:///D:/x.png the page asks for "/D:/x.png": on Windows that is D:/x.png, on POSIX the path as is
        self.assertEqual(windows.page_path("/D:/x.png"), "D:/x.png")
        self.assertEqual(windows.page_path("/d:\\x.png"), "d:\\x.png")
        self.assertEqual(windows.page_path("/tmp/x.png"), "/tmp/x.png")
        self.assertEqual(windows.page_path("D:/x.png"), "D:/x.png")
        for p in ("/D:/x.png", "/tmp/x.png", "D:/x.png"):
            self.assertEqual(posix.page_path(p), p)

    def test_a_process_tree_takes_no_one_with_an_unknown_start_time(self):
        # 03.11: wininit and csrss have a long-dead parent pid, and a normal user cannot read their start time; a hub
        # process that got that pid had its "tree" walk into wininit -> services -> svchost, and stopping it killed the
        # whole user session (explorer, the IDE, the hub). A descendant needs both start times known, child >= parent
        parent = {100: 1, 864: 1, 500: 864, 600: 500, 101: 100, 102: 100, 103: 100, 104: 101, 105: 103, 900: 4,
                  901: 900}
        born = {100: 50, 101: 60, 102: 40, 104: 70, 105: 80, 901: 10}   # 103, 864, 500, 600, 900 are unreadable
        self.assertEqual(windows._descend(100, parent, born.get), [100, 101, 104])
        self.assertEqual(windows._descend(864, parent, born.get), [864])   # the root's time is unknown: no children
        self.assertEqual(windows._descend(900, parent, born.get), [900])

    def test_the_callers_chain_goes_up_only_through_older_parents(self):
        # review of PR #421: the caller (10) and its ancestors are spared, but the dead parent number of 30 went to the
        # caller's own newer child 99: no ancestor, it and its child 77 are stopped (spared, they leaked). A link with an
        # unreadable time (a launcher that exited, wininit) does not end the chain: the older ancestor above it is spared,
        # and the newer number above an unreadable one (wininit's dead parent) is still no ancestor
        parent = {10: 20, 20: 30, 30: 99, 99: 10, 77: 99, 40: 50, 50: 60, 60: 70, 70: 40}
        born = {10: 300, 20: 200, 30: 100, 99: 400, 77: 450, 40: 20, 60: 5, 70: 30}   # 50 is unreadable
        mine = windows._chain(10, parent, born.get)
        self.assertEqual(mine, {10, 20, 30})
        self.assertEqual(windows._descend(99, parent, born.get, mine), [99, 77])
        self.assertEqual(windows._chain(40, parent, born.get), {40, 50, 60})

    def test_a_start_time_names_one_process(self):
        # a pid written to a file (hub-up.json) is checked by it before a stop: the same process reads the same way
        import subprocess
        import sys
        from platforms import current
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        born = current.process_start(p.pid)
        self.assertIsNotNone(born)
        self.assertEqual(current.process_start(p.pid), born)
        self.assertNotEqual(current.process_start(os.getpid()), None)
        for pid in (None, 0, 1, -1, True, "12"):
            self.assertIsNone(current.process_start(pid), pid)

    def test_reap_of_a_process_it_may_not_signal_is_no_stop(self):
        # the audit of 03.11: the number went to a process of another user with the same words in its command line;
        # PermissionError was taken for "gone", and reap reported a stop that did not happen
        import subprocess
        line = subprocess.CompletedProcess([], 0, "claude --resume s1 --output-format stream-json\n", "")
        with mock.patch.object(posix.subprocess, "run", return_value=line), \
                mock.patch.object(posix.os, "kill", side_effect=PermissionError) as kill:
            self.assertFalse(posix.reap(424242, "s1"))
        kill.assert_called_once()
        with mock.patch.object(posix.subprocess, "run", return_value=line), \
                mock.patch.object(posix.os, "kill", side_effect=ProcessLookupError):
            self.assertTrue(posix.reap(424242, "s1"))   # gone by itself: nothing left to stop
        # review: SIGTERM went, the session exited and its number went to another user's process while reap waited
        with mock.patch.object(posix.subprocess, "run", return_value=line), mock.patch.object(posix.time, "sleep"), \
                mock.patch.object(posix.os, "kill", side_effect=[None, PermissionError]):
            self.assertTrue(posix.reap(424242, "s1"))
        # review: still there after 5 s, but the number is another process's of the same user: no SIGKILL to it
        import signal
        with mock.patch.object(posix.subprocess, "run", return_value=line), mock.patch.object(posix.time, "sleep"), \
                mock.patch.object(posix, "command_line", return_value="vim notes.txt"), \
                mock.patch.object(posix.os, "kill") as kill:
            posix.reap(424242, "s1")
        # Windows has no signal.SIGKILL (9 on every POSIX): there reap reaching it would fail by itself
        self.assertNotIn(getattr(signal, "SIGKILL", 9), [c.args[1] for c in kill.call_args_list])

    def test_a_path_with_a_drive_is_a_path_only_on_windows(self):
        self.assertTrue(windows.native_path("D:\\x"))
        self.assertFalse(posix.native_path("D:\\x"))
        for p in ("/tmp/x", "x/y", "~/x"):
            self.assertTrue(posix.native_path(p), p)
            self.assertTrue(windows.native_path(p), p)

    def test_the_core_talks_to_the_os_only_through_the_adapter(self):
        found = []
        for path in CORE:
            for f in sources(path):
                with open(f, encoding="utf-8") as fh:
                    for n, line in enumerate(fh, 1):
                        if OS_BRANCH.search(line.split("#")[0]):
                            found.append(f"{os.path.relpath(f, ROOT)}:{n}: {line.strip()}")
        self.assertEqual(found, [])

    def test_a_private_folder_is_made_only_through_the_adapter(self):
        # 06.11: os.makedirs(path, mode=0o700) on Windows (Python 3.12.4+) gives the folder an ACL of SYSTEM,
        # Administrators and OWNER RIGHTS alone. Made by a process of the scheduled task, whose owner is the
        # Administrators group, it was closed to the user's own terminal (Administrators there is "deny only"): `hub
        # uninstall --purge` could neither read nor delete update/. The profile folders are closed by their own ACL;
        # platforms.current.makedirs_private passes no mode there
        # (posix-only: the tmpfs folder of the test runs, which is made only where /proc/mounts names tmpfs)
        # the other two: the run folder of a stand (the same user makes and reads it) and the shell launcher, which
        # imports nothing of the hub and spells the same rule itself (no mode on Windows)
        posix_only = {os.path.join("core", "testtmp.py"), os.path.join("tools", "codex_compat.py"),
                      os.path.join("tools", "package", "launch.py")}
        found = []
        for path in CORE + ["modules", "tools", "observers", "plugins"]:
            for f in sources(path):
                if os.path.relpath(f, ROOT) in posix_only:
                    continue
                with open(f, encoding="utf-8") as fh:
                    for n, line in enumerate(fh, 1):
                        if re.search(r"(makedirs\([^#]*(mode=)?0o700|\bmkdir\([^#]*,\s*0o700)", line.split("#")[0]):
                            found.append(f"{os.path.relpath(f, ROOT)}:{n}: {line.strip()}")
        self.assertEqual(found, [])

    def test_windows_makes_a_private_folder_without_a_mode(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as top:
            path = os.path.join(top, "a", "b")
            with mock.patch.object(windows.os, "makedirs", wraps=os.makedirs) as made:
                windows.makedirs_private(path)
            self.assertEqual(made.call_args_list[0], mock.call(path, exist_ok=True))   # (it calls itself for the parents)
            self.assertTrue(os.path.isdir(path))
            windows.makedirs_private(path)   # again: no error

    @unittest.skipIf(os.name == "nt", "POSIX only")
    def test_posix_makes_a_private_folder_closed_to_others(self):
        import tempfile
        with tempfile.TemporaryDirectory() as top:
            old = os.umask(0o022)
            try:
                path = os.path.join(top, "a", "b")
                posix.makedirs_private(path)
                posix.makedirs_private(path)
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700)
            finally:
                os.umask(old)

    class Immediately:
        """threading.Thread that runs its target in start(): the background work of makedirs_private, checked at once."""
        def __init__(self, target, args=(), **kw):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    def test_windows_opens_an_old_closed_folder_to_the_user_once(self):
        # the folders the hub made before: an existing one gets the user's access back, once per process, and a folder
        # this process has just made never (its ACL is the inherited one)
        import tempfile
        from unittest import mock
        calls = []

        def icacls(args, timeout, **kw):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, b"", b"")
        with tempfile.TemporaryDirectory() as top, mock.patch.object(windows, "run", icacls), \
                mock.patch.object(windows, "_opened", set()), mock.patch.object(windows.threading, "Thread", self.Immediately):
            fresh = os.path.join(top, "fresh")
            windows.makedirs_private(fresh)
            windows.makedirs_private(fresh)
            self.assertEqual(calls, [])
            old = os.path.join(top, "old")
            os.mkdir(old)
            windows.makedirs_private(old)
            windows.makedirs_private(old)
            self.assertEqual(calls, [["icacls", old, "/inheritance:e", "/remove", "*S-1-3-4", "/T", "/C", "/Q"]])

    def test_opening_an_old_folder_does_not_hold_the_caller(self):
        import tempfile
        from unittest import mock
        started = []

        class Later(self.Immediately):
            def start(self):
                started.append(self.args)
        with tempfile.TemporaryDirectory() as top, mock.patch.object(windows, "_opened", set()), \
                mock.patch.object(windows.threading, "Thread", Later), mock.patch.object(windows, "run") as ran:
            windows.makedirs_private(top)
            self.assertEqual(started, [(top,)])
            ran.assert_not_called()

    def test_a_failing_icacls_is_said_and_does_not_stop_the_folder(self):
        import io
        import tempfile
        from contextlib import redirect_stdout
        from unittest import mock

        def broken(args, timeout, **kw):
            raise OSError("no icacls")
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as top, mock.patch.object(windows, "run", broken), \
                mock.patch.object(windows, "_opened", set()), mock.patch.object(windows.threading, "Thread", self.Immediately), \
                redirect_stdout(out):
            windows.makedirs_private(top)
        self.assertIn("stays closed to the user's terminal: no icacls", out.getvalue())

    def test_no_kill_zero_outside_the_adapter(self):
        # 08.10: os.kill(pid, 0) on Windows is not a liveness check but Ctrl+C for the whole console
        # (GenerateConsoleCtrlEvent): the update self-check killed the hub after a minute; liveness is
        # platforms.current.alive
        # standalone scripts that cannot import platforms — the update watchdog runs as a copy of the previous release
        # beside possibly broken new code, the app shell's launcher is frozen in the shell; both ask tasklist on Windows
        alone = {os.path.join("modules", "update", "trial.py"), os.path.join("tools", "package", "launch.py")}
        found = []
        for path in CORE + ["modules", "tools", "observers", "plugins"]:
            for f in sources(path):
                if os.path.relpath(f, ROOT) in alone:
                    continue
                with open(f, encoding="utf-8") as fh:
                    for n, line in enumerate(fh, 1):
                        if re.search(r"os\.kill\([^,]+,\s*0\)", line.split("#")[0]):
                            found.append(f"{os.path.relpath(f, ROOT)}:{n}: {line.strip()}")
        self.assertEqual(found, [])

    def test_every_stop_by_number_outside_the_adapter_is_known(self):
        # the audit of 03.11: a number from a file or a list may belong to another program by now (hub-up.json after a
        # crash, board.pid, a collected child), and stopping it hit foreign processes; the core goes through the
        # adapter (test above), the rest is this register. A new place: check who it is before the signal (start time,
        # command line, our own Popen not yet collected), then add it here with how
        known = {
            os.path.join("modules", "browser", "run.py"): 2,        # Chrome by its exact profile path in the command line
            os.path.join("modules", "cloudflare", "cloudflared.py"): 1,   # its binary and state folder in the command
            os.path.join("modules", "directions", "boards.py"): 1,  # is_board: the board's command, its own group
            os.path.join("modules", "telegram", "mermaid.py"): 1,   # its own Popen, not yet collected
            os.path.join("modules", "testchrome", "litter.py"): 1,  # the test-run profile path in the command line
            os.path.join("modules", "update", "trial.py"): 3,       # the start time hub-up wrote, hub-up in the command
            os.path.join("tools", "dev", "readme_demo.py"): 1,      # its own Popen
            os.path.join("tools", "private_check.py"): 1,           # its own Popen after a timeout (not collected)
            os.path.join("tools", "package", "shellswap.py"): 1,    # hub-up of hub-up.json, hub-up in its command line
        }
        stop = re.compile(r"\bos\.kill(?:pg)?\((?![^,()]+,\s*0\))|[\"']taskkill[\"']|\.TerminateProcess\(")
        found = {}
        for path in ["modules", "tools", "deploy", "observers", "plugins"]:
            for f in sources(path):
                with open(f, encoding="utf-8") as fh:
                    n = sum(bool(stop.search(line.split("#")[0])) for line in fh)
                if n:
                    found[os.path.relpath(f, ROOT)] = n
        self.assertEqual(found, known)

    def test_a_child_apart_does_not_share_the_console(self):
        # the update self-check runs in its own console: a Ctrl+C broadcast by some test does not touch the hub
        self.assertEqual(windows.apart(), {"creationflags": 0x08000000})   # CREATE_NO_WINDOW: its own hidden console
        self.assertEqual(posix.apart(), {"start_new_session": True})

    def test_input_reaches_the_child_on_windows_too(self):
        # 26.10, Windows update stand: run() passed input to communicate without stdin=PIPE — the input was lost, and
        # `git check-ignore --stdin` (.worktreeinclude) waited for its stdin up to the 600 s timeout per call: the update
        # self-check did not fit into 1200 s
        import subprocess
        import sys
        from unittest import mock
        with mock.patch.object(windows, "session_tree", return_value=None):   # no Windows job object here
            r = windows.run([sys.executable, "-c", "import sys; print(sys.stdin.read())"], 20, input="a\0b",
                            capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "a\0b")
        with self.assertRaises(ValueError):   # as subprocess.run: own stdin together with input is a caller error
            windows.run([sys.executable, "-c", "pass"], 20, input="x", stdin=subprocess.DEVNULL)

    def test_a_command_line_from_the_settings_keeps_windows_paths(self):
        # 08.10: shlex in POSIX mode ate "\\": the voice recognition command C:\\x\\asr.cmd became C:xasr.cmd
        line = r'C:\x\asr.cmd --file {wav} "C:\Program Files\y" --json'
        self.assertEqual(windows.split_command(line),
                         [r"C:\x\asr.cmd", "--file", "{wav}", r"C:\Program Files\y", "--json"])
        self.assertEqual(posix.split_command("/x/asr --file {wav} '/a b/y'"), ["/x/asr", "--file", "{wav}", "/a b/y"])

    def test_old_claude_code_is_named_on_windows(self):
        # PR #18 review: the hook exec form needs Claude Code 2.1.139; older, and the session is "starting" forever
        # with no explanation
        self.assertIn("2.1.139", windows.claude_problem("2.1.138"))
        self.assertIsNone(windows.claude_problem("2.1.139"))
        self.assertIsNone(windows.claude_problem("2.1.287"))
        self.assertIsNone(windows.claude_problem(None))
        self.assertIsNone(posix.claude_problem("1.0.0"))

    def test_owner_commands_get_the_owners_pythonutf8(self):
        # PR #18 review: sessions and scheduled jobs inherited the supervisor's PYTHONUTF8=1, so the owner's cp1251
        # scripts broke only inside the hub
        hub = {"PATH": "x", "PYTHONUTF8": "1", windows.UTF8_WAS: ""}
        self.assertEqual(windows.owner_env(hub), {"PATH": "x"})
        self.assertEqual(windows.owner_env({**hub, windows.UTF8_WAS: "1"}), {"PATH": "x", "PYTHONUTF8": "1"})
        self.assertEqual(windows.owner_env({"PATH": "x"}), {"PATH": "x"})   # not under the supervisor: as is
        self.assertEqual(posix.owner_env(hub), hub)

    def test_hub_cli_prints_utf8_without_utf8_mode(self):
        # `hub` from a session already runs without PYTHONUTF8: its own output (read by claude) is UTF-8 anyway
        import subprocess
        import sys
        hub = os.path.join(ROOT, "bin", "hub")
        out = subprocess.run([sys.executable, hub, "--help"], env={**os.environ, "PYTHONUTF8": "0"},
                             capture_output=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("hub — commands", out.stdout.decode("utf-8"))   # the help is English, its dash is not ASCII

    def test_hub_cli_prints_utf8_in_a_c_locale(self):
        # LC_ALL=C and PYTHONUTF8=0 (server, container, cron): output is ASCII, and `hub --help` crashed with
        # UnicodeEncodeError
        import subprocess
        import sys
        hub = os.path.join(ROOT, "bin", "hub")
        out = subprocess.run([sys.executable, hub, "--help"], env={**os.environ, "LC_ALL": "C", "PYTHONUTF8": "0"},
                             capture_output=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr.decode("utf-8", "replace")[-800:])
        self.assertIn("hub — commands", out.stdout.decode("utf-8"))   # the help is English, its dash is not ASCII

    def test_own_process_goes_utf8_in_a_c_locale(self):
        # utf8_mode: hub output and files are UTF-8 in any locale; same process (pid is the OS service's and hub-up.json's)
        import subprocess
        import sys
        import tempfile
        d = tempfile.mkdtemp(prefix="utf8-mode-")
        text, script = os.path.join(d, "text.txt"), os.path.join(d, "probe.py")
        with open(text, "w", encoding="utf-8") as f:
            f.write(lang.data("self_name", "name"))   # a Russian word of the language data
        with open(script, "w", encoding="utf-8") as f:
            f.write(f"import os, sys\nsys.path.insert(0, {ROOT!r})\nimport platforms\npid = os.getpid()\n"
                    f"platforms.current.utf8_mode([__file__] + sys.argv[1:])\n"
                    f"print(open({text!r}).read(), sys.argv[1], pid == os.getpid() or os.name == 'nt')\n")
        # interpreter flags are kept too (-O): otherwise exec would lose them
        script_flags = "print(int(sys.flags.optimize))\n"
        with open(script, "a", encoding="utf-8") as f:
            f.write(script_flags)
        out = subprocess.run([sys.executable, "-O", script, "—"], env={**os.environ, "LC_ALL": "C", "PYTHONUTF8": "0"},
                             capture_output=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr.decode("utf-8", "replace")[-800:])
        self.assertEqual(out.stdout.decode("utf-8").split(), [lang.data("self_name", "name"), "—", "True", "1"])

    def test_every_entry_point_goes_utf8_first(self):
        # hub entry points (bin/*, and the page, which hub-up launches as a file): utf8_mode comes before any work
        self.entries_go_utf8_first(ROOT)
        self.assertIn("utf8_mode(", inspect.getsource(posix.run_hub_up))   # hub-up: on Windows, the supervisor

    def test_entry_points_skip_litter_of_a_live_copy(self):
        # 18.10: the update self-check runs in the hub folder, where bin/ has __pycache__/ (an entry point was
        # imported as a module at least once) and .DS_Store; open() on them failed the test and the update rolled
        # back on every such hub
        import shutil
        import tempfile
        d = tempfile.mkdtemp(prefix="entry-points-")
        self.addCleanup(shutil.rmtree, d, True)
        # we add our own junk: a copy without what already sits in this folder's bin/ (else makedirs raises FileExistsError)
        shutil.copytree(os.path.join(ROOT, "bin"), os.path.join(d, "bin"),
                        ignore=shutil.ignore_patterns("__pycache__", ".*"))
        os.makedirs(os.path.join(d, "modules", "web"))
        shutil.copy(os.path.join(ROOT, "modules", "web", "server.py"), os.path.join(d, "modules", "web"))
        os.makedirs(os.path.join(d, "bin", "__pycache__"))
        for junk in (os.path.join("__pycache__", "hub.cpython-312.pyc"), ".DS_Store", ".hub.swp"):
            with open(os.path.join(d, "bin", junk), "wb") as f:
                f.write(b"\x00\x00\x00\x01Bud1\xff\xfe")
        self.entries_go_utf8_first(d)

    def entries_go_utf8_first(self, root):
        entries = entry_points(root) + [os.path.join("modules", "web", "server.py")]
        self.assertIn(os.path.join("bin", "hub-up"), entries)
        for p in entries:
            with self.subTest(p):
                src = open(os.path.join(root, p), encoding="utf-8").read()
                self.assertTrue("platforms.current.utf8_mode(" in src or "plat.run_hub_up(" in src, p)

    def test_a_hook_line_on_windows_keeps_its_last_statements_exit_code(self):
        # PowerShell -Command gave 1 for a guard's exit 2 (winstand, 2026-11-05): the line's own code is passed on
        argv = windows.hook_shell("guard.exe a")
        self.assertEqual(argv[:4], ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"])
        self.assertEqual(argv[4], "guard.exe a\n" + windows.HOOK_EXIT)
        self.assertEqual(posix.hook_shell("guard a"), ["/bin/sh", "-c", "guard a"])

    def test_hook_command_per_os(self):
        self.assertEqual(posix.hook_command("/usr/bin/python3", "/hub/hooks/hub_hook.py"),
                         {"type": "command", "command": '"/usr/bin/python3" "/hub/hooks/hub_hook.py"'})
        # 05.10: on Windows Claude Code runs a hook string through Git Bash, which eats "\" in an unquoted path:
        # `C:Users…python.exe: command not found`, not a single event. Exec form (`args`) uses no shell
        self.assertEqual(windows.hook_command(r"C:\Py\python.exe", r"C:\hub\hooks\hub_hook.py"),
                         {"type": "command", "command": r"C:\Py\python.exe",
                          "args": ["-X", "utf8", r"C:\hub\hooks\hub_hook.py"]})

    def test_plugin_sh_is_found_next_to_git_when_path_has_only_git_cmd(self):
        # the Task Scheduler starts the hub with the machine PATH: Git\cmd only, no sh -> `sh run.sh` of a plugin
        # failed with WinError 2 on every start
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for sub in ("cmd", os.path.join("usr", "bin")):
                os.makedirs(os.path.join(d, sub))
            git = os.path.join(d, "cmd", "git.exe")
            sh = os.path.join(d, "usr", "bin", "sh.exe")
            for f in (git, sh):
                open(f, "w").close()
            which = lambda name: {"git": git}.get(name)
            with mock.patch.object(windows.shutil, "which", which):
                self.assertEqual(windows.ext_command(["sh", "run.sh"]), [os.path.realpath(sh), "run.sh"])
            with mock.patch.object(windows.shutil, "which", lambda name: {"git": git, "sh": "x"}.get(name)):
                self.assertEqual(windows.ext_command(["sh", "run.sh"]), ["sh", "run.sh"])   # sh is in PATH: as is
            with mock.patch.object(windows.shutil, "which", lambda name: None):
                self.assertEqual(windows.ext_command(["sh", "run.sh"]), ["sh", "run.sh"])   # no Git: nothing to substitute
        self.assertEqual(posix.ext_command(["sh", "run.sh"]), ["sh", "run.sh"])

    def test_plugin_python3_is_the_hubs_python_when_path_has_none_or_only_the_store_alias(self):
        # run ["python3", ...] of a plugin: on Windows python3 is often the Microsoft Store stub (exit 9009), and the
        # plugin process stood dead after every reboot; modules already run on the hub's Python (core/modrun.command)
        hub = [sys.executable, *(["-X", "utf8"] if sys.flags.utf8_mode else [])]
        stub = r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\python3.exe"
        real = r"C:\Python312\python3.exe"
        for found, want in ((None, hub + ["main.py"]), (stub, hub + ["main.py"]), (real, ["python3", "main.py"])):
            with mock.patch.object(windows.shutil, "which", lambda name, found=found: found):
                self.assertEqual(windows.ext_command(["python3", "main.py"]), want, found)
        with mock.patch.object(windows.shutil, "which", lambda name: None):
            self.assertEqual(windows.ext_command(["python", "main.py"]), hub + ["main.py"])
            self.assertEqual(windows.ext_command(["node", "main.js"]), ["node", "main.js"])   # not Python: as is
        self.assertEqual(posix.ext_command(["python3", "main.py"]), ["python3", "main.py"])   # elsewhere: as is


class RemoveTreeKeepsLinkTargets(unittest.TestCase):
    """An entry rmtree could not delete is made writable and tried again: a link among them must not pass the chmod to
    the file it points at (someone else's file outside the tree)."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="rmtree-links-")
        self.addCleanup(self.cleanup)
        self.victim = os.path.join(self.dir, "victim")
        with open(self.victim, "w") as f:
            f.write("not the hub's")
        os.chmod(self.victim, 0o644)
        self.was = stat.S_IMODE(os.stat(self.victim).st_mode)
        self.tree = os.path.join(self.dir, "tree")
        self.locked = os.path.join(self.tree, "locked")
        os.makedirs(self.locked)
        self.link = os.path.join(self.locked, "link")
        winsafe.symlink(self, self.victim, self.link)
        os.chmod(self.locked, 0o555)   # POSIX: the link in it cannot be deleted, rmtree calls its handler

    def cleanup(self):
        import platforms
        if os.path.isdir(self.locked):
            os.chmod(self.locked, 0o755)
        platforms.current.rmtree(self.dir)

    def assert_victim_kept(self):
        self.assertTrue(os.path.isfile(self.victim))
        self.assertEqual(stat.S_IMODE(os.stat(self.victim).st_mode), self.was)

    def test_the_platforms_rmtree(self):
        import platforms
        platforms.current.rmtree(self.tree)
        self.assert_victim_kept()

    def test_the_code_store(self):
        import contextlib
        from core import codestore
        with contextlib.suppress(OSError):   # what cannot be deleted is an error there
            codestore._clear(self.link)
        self.assert_victim_kept()
        with contextlib.suppress(OSError):
            codestore.remove(self.tree)
        self.assert_victim_kept()

    def test_the_app_shells_first_copy(self):
        import contextlib
        import shutil
        import sys
        sys.path.insert(0, os.path.join(ROOT, "tools", "package"))
        self.addCleanup(sys.path.remove, os.path.join(ROOT, "tools", "package"))
        import launch
        with contextlib.suppress(OSError):
            shutil.rmtree(self.tree, onexc=launch._writable_again)   # portable: the handler under test is rmtree's
        self.assert_victim_kept()

    def test_the_windows_handler_changes_a_link_itself_and_retries_files(self):
        # the Windows adapter on a fake rmtree: what its handler does with a link and with a read-only file
        plain = os.path.join(self.dir, "plain")
        open(plain, "w").close()
        changed = []

        def fake_rmtree(path, onexc):
            for p in (self.link, plain):
                onexc(os.unlink, p, PermissionError(13, "read-only"))
        with mock.patch("shutil.rmtree", fake_rmtree), \
                mock.patch("os.chmod", lambda p, m, **kw: changed.append((p, kw))), mock.patch("os.unlink"):
            windows.rmtree(self.tree)
        self.assertEqual(changed, [(self.link, {"follow_symlinks": False}), (plain, {})])


class BindAlone(unittest.TestCase):
    def test_a_listened_port_is_not_taken_even_if_its_listener_allows_reuse(self):
        # 27.10, Windows: http.server's SO_REUSEADDR let the built-in gateway bind the port the module's gateway (also
        # http.server) still listened on — two servers on one port instead of waiting for the place
        import socket
        import platforms
        held = socket.socket()
        self.addCleanup(held.close)
        held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        mine = socket.socket()
        self.addCleanup(mine.close)
        platforms.current.bind_alone(mine)
        with self.assertRaises(OSError):
            mine.bind(held.getsockname())

    def test_the_port_of_a_closed_listener_binds_while_its_accepted_connection_lives(self):
        # the gateway handed back to the same port while the old one still streams answers (review 27.10: does
        # SO_EXCLUSIVEADDRUSE keep the port until those connections end? On Windows 11 — no, checked across processes)
        import socket
        import platforms
        old = socket.socket()
        platforms.current.bind_alone(old)
        old.bind(("127.0.0.1", 0))
        old.listen(1)
        port = old.getsockname()[1]
        client = socket.create_connection(("127.0.0.1", port))
        self.addCleanup(client.close)
        accepted, _ = old.accept()
        self.addCleanup(accepted.close)
        old.close()
        new = socket.socket()
        self.addCleanup(new.close)
        platforms.current.bind_alone(new)
        new.bind(("127.0.0.1", port))
        new.listen(1)
        accepted.sendall(b"still")   # the old connection is alive
        self.assertEqual(client.recv(5), b"still")


class Durable(unittest.TestCase):
    """What the hub cannot afford to lose when it rewrites the journal (rotation, repair) and hub.json reaches the disk
    itself: on macOS fsync stops in the drive cache (F_FULLFSYNC goes through it), and a rename is durable only once
    its folder is synced. Events and mailbox facts use a plain fsync: a full flush on macOS costs milliseconds."""

    def setUp(self):
        import shutil
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="durable-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    @unittest.skipIf(os.name == "nt", "POSIX only")
    def test_sync_takes_full_fsync_on_macos(self):
        import fcntl
        from unittest import mock
        with open(os.path.join(self.dir, "f"), "wb") as f, \
                mock.patch.object(fcntl, "F_FULLFSYNC", 51, create=True), \
                mock.patch.object(fcntl, "fcntl") as full, mock.patch("os.fsync") as plain:
            posix.sync(f.fileno(), platform="darwin")
            full.assert_called_once_with(f.fileno(), 51)
            plain.assert_not_called()
            full.side_effect = OSError(45, "not supported")   # a filesystem without it: plain fsync
            posix.sync(f.fileno(), platform="darwin")
            plain.assert_called_once_with(f.fileno())
            full.reset_mock()
            posix.sync(f.fileno(), platform="linux")
            full.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX only")
    def test_sync_dir_syncs_the_folder(self):
        import stat
        from unittest import mock
        synced = []
        with mock.patch.object(posix, "sync", side_effect=lambda fd: synced.append(os.fstat(fd).st_mode)):
            posix.sync_dir(self.dir)
        self.assertEqual([stat.S_ISDIR(m) for m in synced], [True])
        posix.sync_dir(os.path.join(self.dir, "gone"))   # best effort: the rename itself has already happened

    def test_windows_sync_dir_is_a_no_op(self):
        windows.sync_dir(self.dir)

    def test_settings_reach_the_disk_before_the_rename(self):
        # hub.json was replaced without a flush to disk: a power cut could leave an empty file where the settings were
        from unittest import mock
        import platforms
        from core import catalog
        with mock.patch.object(platforms.current, "sync") as sync, \
                mock.patch.object(platforms.current, "sync_dir") as sync_dir:
            catalog.write_settings(os.path.join(self.dir, "hub.json"), {"timezone": "UTC"})
        self.assertEqual((sync.call_count, sync_dir.call_args_list), (1, [mock.call(self.dir)]))

    def held(self, refusals):
        """os.replace refuses `refusals` times (another process holds hub.json open, as on Windows), then works; the
        Windows adapter waits on a fake clock that sleep moves."""
        from unittest import mock
        import platforms
        real, calls, clock = os.replace, [], [0.0]

        def replace(src, dst):
            calls.append(dst)
            if len(calls) <= refusals:
                raise PermissionError(13, "Access is denied", src, 5, dst)
            return real(src, dst)
        fake = mock.Mock(time=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))
        for p in (mock.patch.object(platforms.current, "replace", windows.replace),
                  mock.patch.object(windows, "time", fake), mock.patch("os.replace", replace)):
            p.start()
            self.addCleanup(p.stop)
        return calls

    def test_settings_wait_while_another_process_holds_the_file(self):
        # Windows: the page wrote hub.json while `hub tools import` held it open - the bare rename failed with
        # PermissionError [WinError 5] and the owner's edit was lost
        from core import catalog
        path = os.path.join(self.dir, "hub.json")
        calls = self.held(2)
        catalog.write_settings(path, {"timezone": "UTC"})
        catalog.write_private(os.path.join(self.dir, "mcp.json"), {"a": 1})
        self.assertEqual(len(calls), 4)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"timezone": "UTC"})
        self.assertEqual(sorted(os.listdir(self.dir)), ["hub.json", "mcp.json"])

    def test_settings_give_up_after_five_seconds(self):
        from core import catalog
        path = os.path.join(self.dir, "hub.json")
        calls = self.held(10 ** 6)
        with self.assertRaises(PermissionError):
            catalog.write_settings(path, {"timezone": "UTC"})
        self.assertLess(len(calls), 100)
        self.assertEqual(os.listdir(self.dir), [])   # the temp file is removed

    def test_rotate_and_repair_go_through_the_adapter_events_do_not(self):
        from unittest import mock
        import platforms
        from core import journal, reducer, spool
        path = os.path.join(self.dir, "journal.jsonl")
        with mock.patch.object(platforms.current, "sync", wraps=platforms.current.sync) as sync, \
                mock.patch.object(platforms.current, "sync_dir") as sync_dir:
            j = journal.Journal(path)
            j.append("owner.settings", data={"timezone": "UTC"})
            spool.put(os.path.join(self.dir, "spool"), "x.y")
            self.assertEqual((sync.call_count, sync_dir.call_count), (0, 0))
            journal.rotate(self.dir, reducer.replay(journal.read(j.path)), keep=5, per_session=0)
            self.assertEqual(sync.call_count, 1)
            self.assertEqual(sync_dir.call_args_list, [mock.call(os.path.join(self.dir, journal.ARCHIVE)),
                                                       mock.call(self.dir)])
            with open(path, "ab") as f:   # a broken line in the middle: repair cuts whole events
                f.write(b'{"seq":9,"ty\n{"seq":10,"ts":1,"src":"core","type":"x","entity":null,"data":{}}\n')
            sync.reset_mock()
            sync_dir.reset_mock()
            journal.Journal(path)
            self.assertEqual(sync.call_count, 1)
            self.assertEqual(sync_dir.call_args_list, [mock.call(os.path.join(self.dir, journal.ARCHIVE))])


@unittest.skipIf(os.name == "nt", "POSIX only")
class PosixRun(unittest.TestCase):
    def test_timeout_of_a_session_of_its_own_takes_the_whole_group(self):
        # PR review: the self-check runs tools/test.py, which runs executors; on timeout only the launcher was killed
        import subprocess
        import tempfile
        import time
        from tests.fakes.procs import alive, kill
        pid_file = os.path.join(tempfile.mkdtemp(prefix="posix-run-"), "pid")
        script = f"sleep 60 & echo $! > '{pid_file}'; wait"
        started = time.time()
        # the timeout is also the time the shell has to write the pid file: 1 s was a race with a loaded machine
        with self.assertRaises(subprocess.TimeoutExpired):
            posix.run(["sh", "-c", script], 4, capture_output=True, text=True, **posix.apart())
        self.assertLess(time.time() - started, 20)
        with open(pid_file) as f:
            sleeper = int(f.read())
        self.addCleanup(kill, sleeper, time.time())
        deadline = time.time() + 5
        while alive(sleeper) and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse(alive(sleeper))

    def test_an_interrupt_takes_the_whole_group_without_waiting_for_it(self):
        # PR review: it was killed only on timeout; an exit by signal (SystemExit from a handler) mid-run left
        # Popen.__exit__ waiting for its end (up to 20 min). Not KeyboardInterrupt: __exit__ itself waits only 0.25 s there
        import subprocess
        import tempfile
        import time
        from unittest import mock
        from tests.fakes.procs import alive, kill
        pid_file = os.path.join(tempfile.mkdtemp(prefix="posix-run-"), "pid")
        script = f"sleep 60 & echo $! > '{pid_file}'; wait"
        real = subprocess.Popen.communicate
        fired = []   # only the first call is run() itself; taskkill and other kill subprocesses go through as is
        def interrupted(p, *a, **kw):
            if fired or kw.get("timeout") is None:
                return real(p, *a, **kw)
            deadline = time.time() + 10
            while not (os.path.exists(pid_file) and os.path.getsize(pid_file)) and time.time() < deadline:
                time.sleep(0.05)
            fired.append(1)
            raise SystemExit(143)
        started = time.time()
        with mock.patch.object(subprocess.Popen, "communicate", interrupted), self.assertRaises(SystemExit):
            posix.run(["sh", "-c", script], 30, capture_output=True, text=True, **posix.apart())
        self.assertLess(time.time() - started, 20)
        with open(pid_file) as f:
            sleeper = int(f.read())
        self.addCleanup(kill, sleeper, time.time())
        deadline = time.time() + 5
        while alive(sleeper) and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse(alive(sleeper))

    def test_without_a_session_of_its_own_it_is_subprocess_run(self):
        import sys
        r = posix.run([sys.executable, "-c", "print('ok')"], 30, capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "ok"))

    def test_the_result_of_a_session_of_its_own_has_output_and_code(self):
        import sys
        r = posix.run([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"],
                      30, capture_output=True, text=True, **posix.apart())
        self.assertEqual((r.returncode, r.stdout.strip(), r.stderr.strip()), (3, "out", "err"))


class WindowsTreeOnAFakeSystem(unittest.TestCase):
    """The Windows tree stop on a fake process table: runs on every OS, touches no real process."""

    class FakeKernel32:
        """A process table {pid: (parent, start time or None — not readable)} instead of the system: the snapshot,
        start times and TerminateProcess, which only records. No real process is touched."""

        def __init__(self, procs):
            self.procs, self.killed = procs, []

        def CreateToolhelp32Snapshot(self, flags, pid):
            self.rows = iter(list(self.procs.items()))
            return 1

        def Process32FirstW(self, snap, ref):
            return self.Process32NextW(snap, ref)

        def Process32NextW(self, snap, ref):
            row = next(self.rows, None)
            if row:
                ref._obj.th32ProcessID, ref._obj.th32ParentProcessID = row[0], row[1][0]
            return bool(row)

        def OpenProcess(self, access, inherit, pid):
            if pid not in self.procs or (access & windows._PROCESS_QUERY_LIMITED_INFORMATION
                                         and self.procs[pid][1] is None):
                return None
            if access & windows._PROCESS_TERMINATE:
                self.on_terminate_open(pid)
            # a handle holds the process it was opened on: its start time stays, even if the table moves on
            return ("handle", pid, self.procs.get(pid, (None, None))[1])

        def on_terminate_open(self, pid):
            pass

        def GetProcessTimes(self, handle, created, *rest):
            born = handle[2]
            created._obj.dwHighDateTime, created._obj.dwLowDateTime = born >> 32, born & 0xFFFFFFFF
            return True

        def TerminateProcess(self, handle, code):
            self.killed.append(handle[1])
            return True

        def CloseHandle(self, handle):
            return True

    def fake_system(self, procs):
        import ctypes
        import subprocess
        from ctypes import wintypes

        class Entry(ctypes.Structure):   # the fields of PROCESSENTRY32W the snapshot walk reads
            _fields_ = [("dwSize", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD)]
        k32 = self.FakeKernel32(procs)
        # ctypes and the structure are defined in platforms/windows.py only on Windows
        for patch in (mock.patch.object(windows, "_kernel32", lambda: k32),
                      mock.patch.object(windows, "ctypes", ctypes, create=True),
                      mock.patch.object(windows, "wintypes", wintypes, create=True),
                      mock.patch.object(windows, "_ProcessEntry", Entry, create=True),
                      mock.patch.object(subprocess, "run", side_effect=AssertionError("taskkill"))):
            patch.start()
            self.addCleanup(patch.stop)
        return k32

    def test_the_caller_and_its_ancestors_are_never_killed(self):
        # 03.11 too: whatever the tree turns out to be, the caller does not kill itself or the chain that runs it
        T, me = 133_000_000_000_000_000, os.getpid()
        k32 = self.fake_system({100: (4, T - 300), 200: (100, T - 200), 210: (100, T - 150), me: (200, T - 100),
                                300: (me, T), 310: (300, T + 1)})
        self.assertTrue(windows._kill_tree(300))   # its own child: the whole tree, as before
        self.assertEqual(k32.killed, [300, 310])
        for root in (me, 200, 100):
            k32.killed.clear()
            self.assertFalse(windows._kill_tree(root), root)
            self.assertEqual(k32.killed, [], root)
        # the caller's dead parent number went to its own child, started in the same tick: the caller is in its tree
        k32 = self.fake_system({me: (400, T), 400: (me, T), 410: (400, T + 1)})
        self.assertFalse(windows._kill_tree(400))
        self.assertEqual(k32.killed, [])
        # review: the dead parent number of the caller's grandparent went to the caller's newer child: stopped, not spared
        k32 = self.fake_system({me: (200, T - 100), 200: (100, T - 200), 100: (500, T - 300), 500: (me, T),
                                510: (500, T + 1)})
        self.assertTrue(windows._kill_tree(500))
        self.assertEqual(k32.killed, [500, 510])

    def test_a_number_freed_after_the_snapshot_is_not_stopped(self):
        # the audit of 03.11: a descendant exits between the snapshot and the stop, its number goes to a foreign
        # process; each member is stopped by a handle whose start time is the one the tree was built with
        T = 133_000_000_000_000_000
        k32 = self.fake_system({300: (1, T), 310: (300, T + 1), 320: (300, T + 2)})

        def reused(pid):
            if pid == 300:
                k32.procs[310] = (1, T + 50)   # 310 exited, a foreign program got its number
        k32.on_terminate_open = reused
        self.assertTrue(windows._kill_tree(300))
        self.assertEqual(k32.killed, [300, 320])

    def test_system_numbers_are_never_stopped(self):
        T = 133_000_000_000_000_000
        k32 = self.fake_system({0: (0, T), 4: (0, T), 8: (4, T + 1)})
        for pid in (0, 4):
            self.assertFalse(windows._kill_tree(pid), pid)
        self.assertEqual(k32.killed, [])

    def test_without_a_snapshot_the_root_alone_and_no_taskkill(self):
        # taskkill /T walks parent numbers with no start times: the walk 03.11 was about
        T = 133_000_000_000_000_000
        k32 = self.fake_system({300: (1, T), 310: (300, T + 1)})
        k32.CreateToolhelp32Snapshot = lambda flags, pid: 0
        self.assertTrue(windows._kill_tree(300))
        self.assertEqual(k32.killed, [300])

    def test_reap_spares_a_process_that_took_the_number_while_its_command_was_read(self):
        # reap reads the command line (PowerShell, about a second) and then stops: the root is stopped only if it is
        # still the process whose start time was read before the command
        T = 133_000_000_000_000_000
        line = "claude --resume s1 --output-format stream-json"
        k32 = self.fake_system({300: (1, T), 310: (300, T + 1)})
        with mock.patch.object(windows, "alive", return_value=True), \
                mock.patch.object(windows, "command_line", return_value=line):
            self.assertTrue(windows.reap(300, "s1"))
        self.assertEqual(k32.killed, [300, 310])

        k32 = self.fake_system({300: (1, T), 310: (300, T + 1)})

        def swapped(pid):
            k32.procs[300] = (1, T + 99)   # the session exited, another claude of the same session got the number
            return line
        with mock.patch.object(windows, "alive", return_value=True), \
                mock.patch.object(windows, "command_line", side_effect=swapped):
            self.assertFalse(windows.reap(300, "s1"))
        self.assertEqual(k32.killed, [])

    def test_a_child_already_collected_is_not_stopped(self):
        # kill_detached: once wait/poll collected the child, its number is free
        T = 133_000_000_000_000_000
        k32 = self.fake_system({300: (1, T), 310: (300, T + 1)})
        proc = mock.MagicMock(pid=300, returncode=0)
        windows.kill_detached(proc)
        self.assertEqual(k32.killed, [])
        proc.kill.assert_not_called()
        windows.kill_detached(mock.MagicMock(pid=300, returncode=None))
        self.assertEqual(k32.killed, [300, 310])

    def test_a_root_of_another_start_time_is_not_stopped(self):
        T = 133_000_000_000_000_000
        k32 = self.fake_system({300: (1, T), 310: (300, T + 1)})
        self.assertFalse(windows._kill_tree(300, born=T - 1))
        self.assertEqual(k32.killed, [])
        self.assertTrue(windows._kill_tree(300, born=T))
        self.assertEqual(k32.killed, [300, 310])


class WindowsPluginPartInAJob(unittest.TestCase):
    """A plugin part on Windows stops with everything it started. `sh run.sh` does not replace itself with `exec` under
    Git Bash: python is a child of sh, TerminateProcess of sh left it running, and after every hub restart one more copy
    of the plugin posted each message (2026-11-05). Fakes only: no process is started or stopped."""

    class FakeJob:
        def __init__(self, kill_on_close=False, breakaway_ok=False):
            self.kill_on_close, self.added, self.calls = kill_on_close, [], []

        def add(self, pid):
            self.added.append(pid)
            return True

        def terminate(self, code=1):
            self.calls.append("terminate")

        def close(self):
            self.calls.append("close")

    def setUp(self):
        self.proc = mock.Mock(pid=4242)
        self.proc.poll.return_value = None
        for patch in (mock.patch.object(windows, "Job", self.FakeJob),
                      mock.patch.object(windows.subprocess, "Popen", return_value=self.proc)):
            self.popen = patch.start()
            self.addCleanup(patch.stop)

    def test_a_plugin_part_stops_with_its_whole_tree(self):
        proc, handle = windows.spawn_part(["sh", "run.sh"], False, tree=True, cwd="plugin")
        self.assertIs(proc, self.proc)
        self.assertIsInstance(handle, self.FakeJob)
        self.assertEqual((handle.added, handle.kill_on_close), ([4242], True))   # hub-up died: the plugin goes too
        self.assertNotIn("tree", self.popen.call_args.kwargs)
        windows.stop_part(proc, handle)
        self.assertEqual(handle.calls, ["terminate", "close"])
        self.proc.terminate.assert_not_called()

    def test_other_parts_stay_outside_a_job(self):
        # the browser part's Chrome outlives a restart of the part: only the part's own process is stopped
        proc, handle = windows.spawn_part(["python", "part.py"], False)
        self.assertIsNone(handle)
        windows.stop_part(proc, handle)
        self.proc.terminate.assert_called_once_with()

    def test_hub_up_puts_only_plugins_in_a_job(self):
        import ast
        with open(os.path.join(ROOT, "bin", "hub-up"), encoding="utf-8") as f:
            calls = [n for n in ast.walk(ast.parse(f.read())) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute) and n.func.attr == "spawn_part"]
        tree = {ast.unparse(c.args[0]): any(k.arg == "tree" and ast.literal_eval(k.value) for k in c.keywords)
                for c in calls}
        self.assertEqual(tree, {"plat.below_hub(cmd, cwd=cwd)": False, "cmd": True,   # module, plugin, own part
                                "cmd if name in ABOVE_HUB else plat.below_hub(cmd)": False})

    def test_posix_takes_the_flag_and_keeps_the_part_as_is(self):
        # exec in run.sh replaces sh with python there: stopping the part's process stops the plugin
        self.assertEqual(posix.spawn_part(["sh", "run.sh"], False, tree=True), (self.proc, None))
        self.assertNotIn("tree", self.popen.call_args.kwargs)


class WindowsStdin(unittest.TestCase):
    def test_stdin_text_has_the_line_ends_of_text_mode(self):
        # release stands 11.11: `hub send x -` on Windows kept "\r\n" from a pipe written in text mode (PowerShell,
        # Python's text=True): the message carried a stray "\r" at the end of every line
        import io
        for raw, want in ((b"a\r\n  b\r\n", "a\n  b\n"), (b"\xef\xbb\xbfa\rb\n", "a\nb\n"), (b"a\nb", "a\nb")):
            with mock.patch.object(windows.sys, "stdin", mock.Mock(buffer=io.BytesIO(raw))):
                self.assertEqual(windows.read_stdin(), want, raw)


@unittest.skipUnless(os.name == "nt", "Windows only")
class WindowsAdapter(unittest.TestCase):
    def test_input_goes_through_the_job_to_git(self):
        # the same as test_input_reaches_the_child_on_windows_too, but through the real Windows path (job object, git
        # launcher): the .worktreeinclude list goes to git check-ignore --stdin and the timeout holds
        import tempfile
        from platforms import current
        tmp = tempfile.mkdtemp(prefix="win-stdin-")
        current.run(["git", "init", "-q", tmp], 30, capture_output=True)
        listed = os.path.join(tmp, "list")
        with open(listed, "w") as f:
            f.write(".env\n")
        r = current.run(["git", "-c", f"core.excludesFile={listed}", "-C", tmp, "check-ignore", "--no-index", "-z",
                         "--stdin"], 30, input=".env\0a.txt\0", capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, ".env\0"))

    def test_a_tree_is_stopped_without_taskkill(self):
        # 19.10: taskkill /T and tasklist took 10 s each on a machine with slow system services; the tree is now
        # killed from a process snapshot, grandchild included; taskkill is not called
        import subprocess
        import sys
        import time
        from unittest import mock
        import platforms
        w = platforms.current
        p = subprocess.Popen([sys.executable, "-c", "import subprocess, sys, time; subprocess.Popen([sys.executable, "
                              "'-c', 'import time; time.sleep(60)']); time.sleep(60)"])
        self.addCleanup(p.kill)
        for _ in range(100):
            tree = w._tree_of(p.pid)
            if len(tree) == 2:
                break
            time.sleep(0.1)
        self.assertEqual(len(tree), 2, tree)
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("taskkill")):
            self.assertTrue(w._kill_tree(p.pid))
        p.wait(10)
        for _ in range(50):
            if w._tree_of(tree[1]) == []:
                break
            time.sleep(0.1)
        self.assertEqual(w._tree_of(tree[1]), [])
        self.assertFalse(w._kill_tree(p.pid))   # already gone: nothing to kill

    def test_utf8_mode_passes_on_a_ctrl_c_exit_code(self):
        # review: Ctrl+C gives the child NTSTATUS code 0xC000013A, which sys.exit rejects (OverflowError), so `hub`
        # exited with -1 and a traceback
        import subprocess
        import sys
        import tempfile
        d = tempfile.mkdtemp(prefix="utf8-exit-")
        child, ran, run = (os.path.join(d, n) for n in ("child.py", "ran", "run.py"))
        with open(child, "w") as f:
            f.write(f"open({ran!r}, 'w').close()\nimport ctypes\nctypes.windll.kernel32.ExitProcess(0xC000013A)\n")
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # a script, not -c: interpreter flags (PR #180) come from orig_argv, and with -c the child was called
        # `python -c -X utf8 …`, failing with NameError and the same code 1, so the test passed without running child.py
        with open(run, "w") as f:
            f.write(f"import sys\nsys.path.insert(0, {repo!r})\nfrom platforms import windows\nwindows.utf8_mode([{child!r}])\n")
        env = {k: v for k, v in os.environ.items() if k != "PYTHONUTF8"}
        out = subprocess.run([sys.executable, run], cwd=repo, env=env, capture_output=True, text=True, timeout=60)
        self.assertNotIn("OverflowError", out.stderr)
        self.assertTrue(os.path.exists(ran), out.stderr)
        self.assertEqual(out.returncode, 1)

    def test_hook_reads_utf8_without_pythonutf8(self):
        # review: Claude Code writes UTF-8 to the hook, while the session's PYTHONUTF8 is the owner's (owner_env):
        # without UTF-8 mode a Cyrillic capital I (D0 98) does not decode in cp1251 and the event was lost silently
        import subprocess
        import sys
        import tempfile
        d = tempfile.mkdtemp(prefix="hook-utf8-")
        probe, out = os.path.join(d, "probe.py"), os.path.join(d, "out")
        with open(probe, "w") as f:
            f.write(f"import json, sys\nopen({out!r}, 'w', encoding='utf-8').write(json.load(sys.stdin)['prompt'])\n")
        cmd = windows.hook_command(sys.executable, probe)
        env = {k: v for k, v in os.environ.items() if k != "PYTHONUTF8"}
        word = lang.word("words", "topic_label", 1).capitalize()   # a Russian word of the language data with that capital
        self.assertIn(b"\xd0\x98", word.encode("utf-8"))
        subprocess.run([cmd["command"], *cmd["args"]], input=json.dumps({"prompt": word}, ensure_ascii=False)
                       .encode("utf-8"), env=env, capture_output=True, timeout=60)
        with open(out, encoding="utf-8") as f:
            self.assertEqual(f.read(), word)

    def test_state_is_saved_while_a_reader_holds_the_file(self):
        # PR #18 review: os.replace over a file a reader has open (`hub` reads state.json every 0.3 s) raises
        # PermissionError on Windows: hubd crashed mid-tick and, on stop, left no snapshot
        import subprocess
        import sys
        import tempfile
        import time
        from core import journal
        d = tempfile.mkdtemp(prefix="replace-")
        open(os.path.join(d, "journal.jsonl"), "w").close()
        journal.save_state(d, {"seq": 0, "sessions": {}})
        target = os.path.join(d, journal.STATE_FILE)
        hold = "import sys, time\nf = open(sys.argv[1])\nprint('open', flush=True)\ntime.sleep(1)\nf.close()"
        reader = subprocess.Popen([sys.executable, "-c", hold, target], stdout=subprocess.PIPE, text=True)
        self.addCleanup(reader.wait, 30)
        self.addCleanup(reader.stdout.close)
        self.assertEqual(reader.stdout.readline().strip(), "open")
        started = time.time()
        journal.save_state(d, {"seq": 1, "sessions": {}})
        self.assertLess(time.time() - started, 5)
        self.assertEqual(journal.load_state(d)["seq"], 1)

    def test_timeout_takes_the_whole_tree(self):
        # PR #18 review: `git` from Git/cmd is a launcher; on timeout subprocess.run killed only it and waited for the
        # real git's pipes to close. Here the "launcher" holds the pipes through a child that sleeps 30 s
        import subprocess
        import sys
        import time
        from platforms import windows
        launcher = ("import subprocess, sys; subprocess.run([sys.executable, '-c', 'import time; time.sleep(30)'])")
        started = time.time()
        with self.assertRaises(subprocess.TimeoutExpired):
            windows.run([sys.executable, "-c", launcher], 1, capture_output=True, text=True)
        self.assertLess(time.time() - started, 10)

    def test_an_interrupt_takes_the_whole_tree(self):
        # PR review: it was killed only on timeout; an exit mid self-check released the machine lock while the
        # tools/test.py tree ran on (SystemExit: Popen.__exit__ also waited for its end)
        import subprocess
        import sys
        import tempfile
        import time
        from unittest import mock
        from platforms import windows
        from tests.fakes.procs import alive, kill
        pid_file = os.path.join(tempfile.mkdtemp(prefix="win-run-"), "pid")
        launcher = ("import subprocess, sys; p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                    f"open({pid_file!r}, 'w').write(str(p.pid)); p.wait()")
        real = subprocess.Popen.communicate
        fired = []   # only the first call is run() itself; taskkill and other kill subprocesses go through as is
        def interrupted(p, *a, **kw):
            if fired or kw.get("timeout") is None:
                return real(p, *a, **kw)
            deadline = time.time() + 10
            while not (os.path.exists(pid_file) and os.path.getsize(pid_file)) and time.time() < deadline:
                time.sleep(0.05)
            fired.append(1)
            raise SystemExit(1)
        started = time.time()
        with mock.patch.object(subprocess.Popen, "communicate", interrupted), self.assertRaises(SystemExit):
            windows.run([sys.executable, "-c", launcher], 30, capture_output=True, text=True)
        self.assertLess(time.time() - started, 15)
        with open(pid_file) as f:
            sleeper = int(f.read())
        self.addCleanup(kill, sleeper, time.time())
        deadline = time.time() + 5
        while alive(sleeper) and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse(alive(sleeper))

    def test_notify_reads_console_output_in_the_oem_code_page(self):
        # PR #18 review: a scheduled job pipes cmd output (OEM encoding) into `hub notify -`; in UTF-8 mode stdin was
        # read with surrogates, the mailbox write failed and the notification was lost
        import subprocess
        import sys
        import tempfile
        state = tempfile.mkdtemp(prefix="notify-oem-")
        text = lang.data("words", "no_data")   # Russian words of the language data
        raw = text.encode("oem", "replace")
        subprocess.run([sys.executable, os.path.join(ROOT, "bin", "hub"), "notify", "-"], input=raw, timeout=60,
                       env={**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0"}, capture_output=True)
        spool = os.path.join(state, "spool")
        items = []
        for name in os.listdir(spool):
            with open(os.path.join(spool, name), encoding="utf-8") as f:
                items.append(json.load(f))
        self.assertEqual([i["data"]["text"] for i in items], [raw.decode("oem")])


class DarkWake(unittest.TestCase):
    """A Mac between sleeps (maintenance wake, Power Nap) runs without graphics: the kernel's system capabilities lack
    kIOPMSystemCapabilityGraphics (0x2); a full wake is 15 = CPU, graphics, audio, network (2026-11-03, `pmset -g log`:
    DarkWake [CDNP…], Wake [CDNVA])."""
    def ioreg(self, out=None, error=None):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            if error:
                raise error
            return mock.Mock(stdout=out, returncode=0)
        return run, calls

    def root_domain(self, caps):
        return ('+-o IOPMrootDomain  <class IOPMrootDomain>\n    {\n      "Wake Type" = "Maintenance"\n'
                f'      "System Capabilities" = {caps}\n      "IOPMUserIsActive" = No\n    }}\n')

    def test_a_mac_without_graphics_is_in_a_dark_wake(self):
        run, calls = self.ioreg(self.root_domain(9))
        self.assertTrue(posix.dark_wake(plat="darwin", run=run))
        self.assertEqual(calls[0][0], "/usr/sbin/ioreg")   # the hub's PATH may lack /usr/sbin

    def test_a_full_wake_is_not_dark(self):
        run, _ = self.ioreg(self.root_domain(15))
        self.assertFalse(posix.dark_wake(plat="darwin", run=run))

    def test_an_unknown_answer_is_awake(self):
        # a check that cannot tell must not stop updates for good
        for run, _ in (self.ioreg(""), self.ioreg(error=OSError("no ioreg")),
                       self.ioreg(error=subprocess.TimeoutExpired("ioreg", 10))):
            self.assertFalse(posix.dark_wake(plat="darwin", run=run))

    def test_other_systems_are_never_dark(self):
        run, calls = self.ioreg(self.root_domain(9))
        self.assertFalse(posix.dark_wake(plat="linux", run=run))
        self.assertFalse(windows.dark_wake(plat="win32", run=run))
        self.assertEqual(calls, [])


@unittest.skipUnless(os.name == "nt", "Windows: a part of the hub is stopped by TerminateProcess, with no cleanup")
class WindowsRunDiesWithCaller(unittest.TestCase):
    def test_a_run_dies_with_its_caller(self):
        # 12.10: a hub restart mid update self-check: hub-update was killed with TerminateProcess, while its
        # tools/test.py lived on for 80 more minutes and ate the CPU of the next check (timeout means update rollback)
        import subprocess
        import sys
        import time
        from platforms import current
        parent = ("import subprocess, sys, time\n"
                  "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                  "print(child.pid, flush=True)\ntime.sleep(120)\n")
        caller = (f"import sys\nsys.path.insert(0, {ROOT!r})\nfrom platforms import current\n"
                  f"current.run([sys.executable, '-c', {parent!r}], 120)\n")
        proc = subprocess.Popen([sys.executable, "-c", caller], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, **current.apart())
        self.addCleanup(subprocess.run, ["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        line = proc.stdout.readline()
        if not line:
            proc.kill()
            self.fail(proc.stderr.read())
        child = int(line)
        self.addCleanup(subprocess.run, ["taskkill", "/PID", str(child), "/F"], capture_output=True)
        self.assertTrue(current.alive(child))
        proc.kill()   # TerminateProcess, as parts of the hub are stopped on restart
        proc.wait(10)
        end = time.time() + 10
        while current.alive(child) and time.time() < end:
            time.sleep(0.1)
        self.assertFalse(current.alive(child))


class WindowsStartOutside(unittest.TestCase):
    def test_a_wmi_start_asks_for_a_hidden_window(self):
        # 18.11: every `hub run` (and its tests) opened a console window on the owner's desktop: WMI started the
        # wrapper (python.exe, a console program) with the default STARTUPINFO, so the new console showed. The script
        # passes Win32_ProcessStartup with ShowWindow = 0 (SW_HIDE); the app shell's copy (launch.py) does the same
        import importlib.util
        spec = importlib.util.spec_from_file_location("hub_test_launch_wmi", os.path.join(ROOT, "tools", "package",
                                                                                           "launch.py"))
        launch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launch)
        for name, start in (("platforms.windows", windows.popen_outside), ("launch.py", launch._outside)):
            ran = []
            with self.subTest(name), mock.patch.object(subprocess, "run",
                                                       lambda cmd, **kw: ran.append((cmd, kw)) or
                                                       subprocess.CompletedProcess(cmd, 0)):
                self.assertTrue(start(["C:\\py\\python.exe", "wrap.py"]))
                cmd, kw = ran[0]
                script = cmd[-1]
                self.assertIn("ProcessStartupInformation", script)
                self.assertRegex(script, r"ShowWindow\s*=\s*(\[uint16\])?0\b")
                self.assertEqual(kw["env"]["HUB_OUTSIDE_CMD"], "C:\\py\\python.exe wrap.py")
                self.assertEqual(kw["creationflags"], 0x08000000)   # PowerShell itself: no window either


class Rmdir(unittest.TestCase):
    """rmdir of an empty folder (the worktree leftover cleanup): only Windows clears the read-only attribute and retries."""

    def test_posix_refusal_does_not_touch_the_folders_mode(self):
        # PermissionError here means "the parent is not writable", not an attribute of the folder: chmod would only damage it
        import tempfile
        d = os.path.join(tempfile.mkdtemp(prefix="rmdir-"), "empty")
        from platforms import current
        self.addCleanup(current.rmtree, os.path.dirname(d))
        os.mkdir(d)
        os.chmod(d, 0o755)
        before = os.stat(d).st_mode
        with mock.patch("os.rmdir", side_effect=PermissionError("nope")), \
                mock.patch("os.chmod", side_effect=AssertionError("chmod on a refusal")):
            with self.assertRaises(PermissionError):
                posix.rmdir(d)
        self.assertEqual(os.stat(d).st_mode, before)

    def test_both_adapters_have_it(self):
        self.assertEqual(interface(posix)["rmdir"], interface(windows)["rmdir"])


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux: the command line from /proc")
class CommandLineWithoutPs(unittest.TestCase):
    """A container without procps (python:slim) has no `ps`: the command line came back empty, so `hub stop` said the
    hub was down and `hub uninstall` deleted its data under a running hub-up (docker, deploy/smoke.sh)."""

    def test_the_command_line_is_read_without_ps(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "hub-up-marker"])
        self.addCleanup(lambda: (p.kill(), p.wait()))
        with mock.patch.object(posix.subprocess, "run", side_effect=FileNotFoundError("ps")):
            self.assertTrue(posix.command_line(p.pid).endswith("time.sleep(30) hub-up-marker"))
            self.assertEqual(posix.command_line(2 ** 22 + 12345), "")   # no such process


class CutJunctions(unittest.TestCase):
    """The junctions inside a folder are cut without entering them: what they lead to stays whole (2026-11-09: a copy's
    node_modules was a junction to the main checkout's, and the cleanup emptied the target)."""

    def setUp(self):
        import platforms
        import tempfile
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="cut-junctions-"))
        self.addCleanup(platforms.current.rmtree, self.base)
        self.target = os.path.join(self.base, "elsewhere")
        os.makedirs(os.path.join(self.target, "pkg"))
        with open(os.path.join(self.target, "pkg", "keep.txt"), "w") as f:
            f.write("x")

    def junction(self, link):
        if os.name != "nt":
            self.skipTest("junctions are Windows only")
        import _winapi   # as platforms.windows.link_dir: no privilege needed, no cmd quoting
        _winapi.CreateJunction(self.target, link)

    def test_junctions_at_any_depth_go_and_their_targets_stay(self):
        import platforms
        tree = os.path.join(self.base, "tree")
        os.makedirs(os.path.join(tree, "a", "b"))
        with open(os.path.join(tree, "a", "own.txt"), "w") as f:
            f.write("x")
        self.junction(os.path.join(tree, "node_modules"))
        self.junction(os.path.join(tree, "a", "b", "deep"))
        self.assertEqual(platforms.current.cut_junctions(tree), 2)
        self.assertFalse(os.path.lexists(os.path.join(tree, "node_modules")))
        self.assertFalse(os.path.lexists(os.path.join(tree, "a", "b", "deep")))
        self.assertTrue(os.path.isfile(os.path.join(tree, "a", "own.txt")))
        self.assertTrue(os.path.isfile(os.path.join(self.target, "pkg", "keep.txt")))
        self.assertEqual(platforms.current.cut_junctions(tree), 0)

    def test_a_symlink_stays(self):
        # git does not walk through a symlink, and one that git tracks belongs to a clean copy
        import platforms
        tree = os.path.join(self.base, "tree")
        os.makedirs(tree)
        link = os.path.join(tree, "ln")
        winsafe.symlink(self, self.target, link, target_is_directory=True)
        self.assertEqual(platforms.current.cut_junctions(tree), 0)
        self.assertTrue(os.path.islink(link))
        self.assertTrue(os.path.isfile(os.path.join(self.target, "pkg", "keep.txt")))

    def test_a_folder_that_is_itself_a_junction_is_refused(self):
        import platforms
        link = os.path.join(self.base, "link")
        self.junction(link)
        with self.assertRaises(OSError):
            platforms.current.cut_junctions(link)

    def test_a_mount_point_is_refused_and_stays(self):
        # a volume mounted into a folder is a junction to the API too; not ours to unmount (tests without administrator
        # rights cannot mount one, hence the stand-in for ismount, as ntpath.ismount: a \\?\ path is never one)
        tree = os.path.join(self.base, "tree")
        os.makedirs(tree)
        link = os.path.join(tree, "mnt")
        self.junction(link)
        with mock.patch("os.path.ismount", side_effect=lambda p: not p.startswith("\\\\?\\") and os.path.basename(p) == "mnt"):
            with self.assertRaises(OSError):
                windows.cut_junctions(tree)
        self.assertTrue(os.path.lexists(link))
        self.assertTrue(os.path.isfile(os.path.join(self.target, "pkg", "keep.txt")))

    def test_posix_has_nothing_to_cut(self):
        os.makedirs(os.path.join(self.base, "tree"))
        self.assertEqual(posix.cut_junctions(os.path.join(self.base, "tree")), 0)

    def test_both_adapters_have_it(self):
        self.assertEqual(interface(posix)["cut_junctions"], interface(windows)["cut_junctions"])


if __name__ == "__main__":
    unittest.main()
