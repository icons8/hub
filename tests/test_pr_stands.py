"""tools/pr_stands.py: the tests of a commit on Windows as a standard user and on the Mac before a merge — the run is driven
over ssh, the machine shared by sessions is taken under its lock, woken and put back only by the run that woke it, a run
that does not end kills nothing there, and a green under an administrator's token is not a green."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

import platforms

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))
import pr_stands  # noqa: E402
from core import lock  # noqa: E402

STANDARD = ("BUILTIN\\Administrators Alias S-1-5-32-544 Group used for deny only\r\n"
            "Mandatory Label\\Medium Mandatory Level Label S-1-16-8192\r\n")
ADMIN = ("BUILTIN\\Administrators Alias S-1-5-32-544 Mandatory group, Enabled by default, Enabled group, Group owner\r\n"
         "Mandatory Label\\High Mandatory Level Label S-1-16-12288\r\n")
# what fltmc says to a standard token (winstand, 03.11: exit code 1) and to an elevated one (exit code 0)
DENIED = "\r\nFilter listing failed with error: 0x80070005\r\nAccess is denied.\r\n"
LISTED = "\r\nFilter Name                     Num Instances    Altitude    Frame\r\n-----\r\nbindflt  1  409800  0\r\n"
GREEN = "skipped 15 page modules (self-check, Windows)\nRan 7038 tests in 930.9s (1062 chunks, 4 processes)\nOK (skipped=60)\n"
RED = """===== tests.test_update_app.Trial.test_a …
E.......
======================================================================
ERROR: test_a (tests.test_update_app.Trial.test_a)
The first line of the test's docstring, which unittest prints under the header
----------------------------------------------------------------------
Traceback (most recent call last):
  File "C:\\hubrev\\x\\modules\\update\\trial.py", line 65, in save
    os.replace(tmp, path(state, "trial.json"))
PermissionError: [WinError 5] Access is denied: 'trial.json.6308' -> 'trial.json'

----------------------------------------------------------------------
Ran 8 tests in 20.991s

FAILED (errors=1)

======================================================================
FAIL: test_b (tests.test_setup.ClaudeLogin.test_b)
----------------------------------------------------------------------
Traceback (most recent call last):
AssertionError: 'redirect_uri' not found in 'https://x'

----------------------------------------------------------------------
Ran 8 tests in 0.200s

FAILED (failures=1)

Ran 7038 tests in 930.9s (1062 chunks, 4 processes)
FAILED (chunks: 2)
"""


def quiet(*a, **k):
    pass


def make_repo(root):
    os.makedirs(root)
    for args in (["init", "-q", "-b", "main"], ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q",
                                                "--allow-empty", "-m", "one"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


class Box:
    """The Windows machine as pr_stands sees it over ssh: its files and the scheduled task, which plays the suite's outcome."""

    def __init__(self, groups=STANDARD, token="fltmc 1", log=GREEN, starts=True, finishes=True, up=True,
                 user="hub", runas=True, late=False, fltmc=DENIED):
        self.groups, self.token, self.log, self.user, self.fltmc = groups, token, log, user, fltmc
        self.starts, self.finishes, self.runas, self.late = starts, finishes, runas, late
        self.up = up
        self.files, self.commands, self.got = {}, [], {}

    def alive(self):
        return self.up() if callable(self.up) else self.up

    def run(self, command, timeout=120):
        self.commands.append(command)
        if command == "echo %USERNAME%":
            return 0, self.user + "\r\n"
        m = re.fullmatch(r'type "([^"]+)"', command)
        if m:
            return (0, self.files[m.group(1)].decode()) if m.group(1) in self.files else (1, "cannot find the file")
        m = re.fullmatch(r"schtasks /delete /tn (\S+) /f", command)
        if m and self.late and not self.starts:   # it started in the moment the run gave up on it
            self.starts = True
            self.start(f"{pr_stands.FOLDER}\\{m.group(1)}")
            return 0, "SUCCESS"
        m = re.search(r"schtasks /run /tn (\S+)", command)
        if m:
            at = f"{pr_stands.FOLDER}\\{m.group(1)}"
            task, inner = self.files[at + ".cmd"].decode(), self.files[at + ".inner.cmd"].decode()
            assert f'runas /trustlevel:0x20000 "{at}.inner.cmd"' in task, task
            assert "tools\\test.py --no-lock" in inner and "\r\n" in inner, inner
            assert inner.index("fltmc") < inner.index("tools\\test.py") < inner.index(f'> "{at}.status"'), inner
            # cmd reads "echo x 1> f" as a redirect of handle 1: a one-digit code right before > is lost (03.11, live)
            assert not re.search(r"%errorlevel%>", inner), inner
            if not self.runas:   # task.cmd: runas refused — it writes the status itself
                self.files[at + ".status"] = b"done runas 1\r\n"
            elif self.starts:
                self.start(at)
            return 0, "SUCCESS: Attempted to run the scheduled task"
        return 0, ""

    def start(self, at):
        self.files[at + ".groups"] = self.groups.encode()
        self.files[at + ".token"] = (self.token + "\r\n").encode()
        self.files[at + ".fltmc"] = self.fltmc.encode()
        if self.finishes:
            self.files[at + ".log"] = self.log.encode()
            self.files[at + ".status"] = b"done 0\r\n"

    def put(self, local, remote):
        with open(local, "rb") as f:
            self.files[remote] = f.read()
        return True, ""

    def get(self, remote, local):
        if remote not in self.files:
            return False, "No such file"
        with open(local, "wb") as f:
            f.write(self.files[remote])
        return True, ""


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def pause(self, s):
        self.now += s


class Windows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pr-stands-test-")
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.repo = os.path.join(self.tmp, "repo")
        self.sha = make_repo(self.repo)
        self.clock = Clock()

    def windows(self, box, **kw):
        return pr_stands.windows(box, self.repo, self.sha, say=quiet, pause=self.clock.pause, clock=self.clock, **kw)

    def name(self, box):
        return next(re.search(r"/tn (\S+)", c).group(1) for c in box.commands if "schtasks /create" in c)

    def test_a_green_run_on_a_standard_token_is_green_and_the_machine_is_tidied(self):
        box = Box()
        res = self.windows(box)
        self.assertEqual((res["status"], res["ran"], res["failed"]), (pr_stands.OK, 7038, []))
        name = self.name(box)
        self.assertTrue(name.startswith("ps-" + self.sha[:12]))
        self.assertTrue(any(f"schtasks /delete /tn {name} /f" in c and f'rmdir /s /q "{pr_stands.FOLDER}\\{name}"' in c
                            for c in box.commands), box.commands)
        # the bundle carried the commit, and its ref did not stay in the repo
        path = os.path.join(self.tmp, "b.bundle")
        with open(path, "wb") as f:
            f.write(box.files[f"{pr_stands.FOLDER}\\{name}.bundle"])
        heads = subprocess.run(["git", "bundle", "list-heads", path], capture_output=True, text=True, check=True).stdout
        self.assertIn(self.sha, heads)
        left = subprocess.run(["git", "for-each-ref", "refs/pr-stands"], cwd=self.repo, capture_output=True, text=True).stdout
        self.assertEqual(left, "")

    def test_reds_come_with_the_last_line_of_their_traceback(self):
        res = self.windows(Box(log=RED))
        self.assertEqual(res["status"], pr_stands.RED)
        self.assertEqual(res["failed"], ["tests.test_update_app.Trial.test_a", "tests.test_setup.ClaudeLogin.test_b"])
        self.assertTrue(res["reasons"]["tests.test_update_app.Trial.test_a"].startswith("PermissionError: [WinError 5]"))
        self.assertEqual(res["reasons"]["tests.test_setup.ClaudeLogin.test_b"],
                         "AssertionError: 'redirect_uri' not found in 'https://x'")
        self.assertIn("PermissionError", pr_stands.table({"windows": res}))

    def test_a_green_under_an_administrators_token_is_an_error_not_a_green(self):
        # colleagues start their hub as a standard user: links, a replaced open file, folders of others behave otherwise for
        # an administrator — a green there says nothing about them (v1.7.1 had 11 reds only a standard user saw)
        # the check is `fltmc` (only an elevated token may run it), not the words of whoami: they differ by the language
        # of Windows, and a plain standard account has no Administrators line at all
        res = self.windows(Box(groups=ADMIN, token="fltmc 0", fltmc=LISTED))
        self.assertEqual(res["status"], pr_stands.ERROR)
        self.assertIn("standard user", res["detail"])
        self.assertIn("S-1-5-32-544", res["detail"])
        # the probe itself failed some other way (not "access denied"), or wrote nothing: unknown — not a green
        for token, said in (("", DENIED), ("fltmc", DENIED), ("standard", DENIED), ("fltmc 0", DENIED),
                            ("fltmc 9009", "'fltmc' is not recognized as an internal or external command\r\n"),
                            ("fltmc 1", "Filter listing failed with error: 0x80070002\r\n")):
            self.assertEqual(self.windows(Box(token=token, fltmc=said))["status"], pr_stands.ERROR, (token, said))
        # a localized Windows: other words around the same code
        self.assertEqual(self.windows(Box(fltmc="Fehler: 0x80070005\r\nZugriff verweigert.\r\n"))["status"], pr_stands.OK)
        self.assertEqual(self.windows(Box(groups="BUILTIN\\Users Alias S-1-5-32-545\r\n"))["status"], pr_stands.OK)

    def test_a_refused_runas_or_a_task_that_never_starts_is_said_at_once_not_after_the_whole_timeout(self):
        res = self.windows(Box(runas=False))
        self.assertEqual(res["status"], pr_stands.ERROR)
        self.assertIn("runas", res["detail"])
        box = Box(starts=False)
        res = self.windows(box, timeout=3600, poll=30)
        self.assertEqual(res["status"], pr_stands.ERROR)
        self.assertIn("did not start", res["detail"])
        self.assertLessEqual(res["seconds"], pr_stands.START_WAIT + 30)
        name = self.name(box)
        self.assertTrue(any(c.startswith("del ") and f"{name}.bundle" in c for c in box.commands), box.commands)

    def test_a_run_that_started_just_as_its_task_was_given_up_on_is_waited_for(self):
        res = self.windows(Box(starts=False, late=True), timeout=3600, poll=30)
        self.assertEqual((res["status"], res["ran"]), (pr_stands.OK, 7038))

    def test_the_one_time_task_is_deleted_as_soon_as_the_run_started(self):
        # schtasks /sc once /st 23:59 is a real trigger: a run still going at 23:59 would otherwise get a second copy
        box = Box(finishes=False)
        self.windows(box, timeout=100, poll=30)
        name = self.name(box)
        first_delete = next(i for i, c in enumerate(box.commands) if f"schtasks /delete /tn {name}" in c)
        token_seen = next(i for i, c in enumerate(box.commands) if c.endswith('.token"'))
        self.assertEqual(first_delete, token_seen + 1)

    def test_two_runs_of_one_commit_have_their_own_folders(self):
        # a run that timed out may go on there: the next one must not delete its copy or read its status
        one, two = Box(), Box()
        self.windows(one)
        self.windows(two)
        self.assertNotEqual(self.name(one), self.name(two))

    def test_a_run_that_does_not_end_kills_nothing_and_keeps_its_copy(self):
        box = Box(finishes=False)
        res = self.windows(box, timeout=100, poll=30)
        self.assertEqual(res["status"], pr_stands.ERROR)
        self.assertIn("nothing was killed", res["detail"])
        started = next(i for i, c in enumerate(box.commands) if "schtasks /run" in c)
        sent = " ".join(box.commands[started:]).lower()   # what went there once the suite was running
        for word in ("taskkill", "/end", "stop-process", "rmdir", "del "):
            self.assertNotIn(word, sent)

    def test_a_folder_or_test_name_that_would_break_the_cmd_line_is_refused_before_anything_is_sent(self):
        for kw in ({"folder": "C:\\hub rev"}, {"folder": "C:\\x&del y"}, {"folder": "C:\\x%PATH%"},
                   {"only": ["tests.x & calc"]}, {"python": "3.14 & calc"}):
            box = Box()
            self.assertEqual(self.windows(box, **kw)["status"], pr_stands.ERROR, kw)
            self.assertEqual(box.commands, [], kw)

    def test_a_log_without_a_suite_is_an_error(self):
        res = self.windows(Box(log="'py' is not recognized as an internal or external command\r\n"))
        self.assertEqual(res["status"], pr_stands.ERROR)
        self.assertIn("did not start", res["detail"])


class Machine(unittest.TestCase):
    """on_machine: the lock of a shared machine, waking and putting to sleep only what this run woke."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pr-stands-test-")
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.up = os.path.join(self.tmp, "up")
        self.down = os.path.join(self.tmp, "down")
        self.clock = Clock()

    def touch(self, path):
        exe = sys.executable.replace("\\", "/")
        return f'"{exe}" -c "open(r\'{path}\', \'w\').close()"'

    def machine(self, box, work, **kw):
        return pr_stands.on_machine(box, work, say=quiet, pause=self.clock.pause, clock=self.clock, poll=1, **kw)

    def test_a_machine_already_up_is_neither_woken_nor_put_to_sleep(self):
        res = self.machine(Box(up=True), lambda: "worked", wake=self.touch(self.up), sleep=self.touch(self.down))
        self.assertEqual(res, "worked")
        self.assertFalse(os.path.exists(self.up))
        self.assertFalse(os.path.exists(self.down))   # someone else's run may be on it

    def test_a_sleeping_machine_is_woken_and_put_back_to_sleep(self):
        box = Box(up=lambda: os.path.exists(self.up))
        res = self.machine(box, lambda: "worked", wake=self.touch(self.up), sleep=self.touch(self.down))
        self.assertEqual(res, "worked")
        self.assertTrue(os.path.exists(self.down))

    def test_a_failed_wake_is_an_error_and_the_machine_is_put_back(self):
        box = Box(up=False)
        res = self.machine(box, lambda: self.fail("no work on a dead machine"),
                           wake=f'"{sys.executable.replace(chr(92), "/")}" -c "raise SystemExit(3)"', sleep=self.touch(self.down))
        self.assertEqual(res["status"], pr_stands.ERROR)
        self.assertTrue(os.path.exists(self.down))
        res = self.machine(Box(up=False), lambda: self.fail("no work"), wake=self.touch(self.up), sleep=self.touch(self.down),
                           wake_wait=5)
        self.assertIn("ssh did not answer", res["detail"])
        self.assertEqual(self.machine(Box(up=False), lambda: "x")["status"], pr_stands.ERROR)   # nothing to wake it with

    def test_a_sleep_command_that_fails_does_not_lose_the_result_or_the_lock(self):
        path = os.path.join(self.tmp, "box.lock")
        box = Box(up=lambda: os.path.exists(self.up))
        res = pr_stands.on_machine(box, lambda: "worked", wake=self.touch(self.up), sleep="\0", lock=path, say=quiet,
                                   pause=self.clock.pause, clock=self.clock, poll=1)
        self.assertEqual(res, "worked")
        again = lock.acquire(path)
        self.assertIsNotNone(again)
        again.close()

    def test_the_machine_is_used_under_its_lock_and_waits_for_another_holder(self):
        path = os.path.join(self.tmp, "box.lock")
        other = lock.acquire(path)
        said, inside = [], []

        def pause(s):   # the other run ends while we wait
            other.close()

        def work():
            probe = lock.acquire(path)
            inside.append(probe is None)
            if probe:
                probe.close()
            return "worked"
        res = pr_stands.on_machine(Box(), work, lock=path, say=lambda *a, **k: said.append(a[0]), pause=pause)
        self.assertEqual(res, "worked")
        self.assertEqual(inside, [True])   # held while the work runs
        self.assertTrue(any("busy" in s for s in said))
        again = lock.acquire(path)   # and released afterwards
        self.assertIsNotNone(again)
        again.close()


class Main(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pr-stands-test-")
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.repo = os.path.join(self.tmp, "repo")
        self.sha = make_repo(self.repo)
        self.out = os.path.join(self.tmp, "out")

    def main(self, *argv, box=None, environ=None):
        said = []
        box = box or Box()
        orig = pr_stands.windows

        def windows(remote, repo, sha, *a, **k):   # a fast clock: the run polls in seconds, not minutes
            c = Clock()
            return orig(remote, repo, sha, *a, **{**k, "pause": c.pause, "clock": c, "say": quiet})
        pr_stands.windows = windows
        try:
            code = pr_stands.main(["--repo", self.repo, "--out", self.out, *argv],
                                  environ={"HUB_STAND_WIN": "hub@box"} if environ is None else environ,
                                  remote=lambda target: box, say=said.append)
        finally:
            pr_stands.windows = orig
        return code, "\n".join(said)

    def test_the_verdict_is_for_exactly_the_commit_that_ran(self):
        code, text = self.main("--no-mac")
        self.assertEqual(code, 0, text)
        with open(os.path.join(self.out, self.sha + ".json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["machines"]["windows"]["status"], pr_stands.OK)
        self.assertEqual(self.main("--verdict")[0], 0)
        # a new commit on the branch after the run: not checked
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty",
                        "-m", "two"], cwd=self.repo, check=True)
        code, text = self.main("--verdict")
        self.assertEqual(code, 2)
        self.assertIn("has not run", text)

    def test_red_and_partial_runs_are_not_a_green_verdict(self):
        self.assertEqual(self.main("--no-mac", box=Box(log=RED))[0], 1)
        self.assertEqual(self.main("--verdict")[0], 1)
        # a green part of the suite (--only) is kept apart and does not turn the commit's red into a green
        self.assertEqual(self.main("--no-mac", "--only", "tests.test_x")[0], 0)
        self.assertTrue(os.path.exists(os.path.join(self.out, self.sha + "-only.json")))
        self.assertEqual(self.main("--verdict")[0], 1)

    def test_a_run_of_one_machine_keeps_the_others_earlier_result(self):
        # a full run: Windows red; then the Mac alone, green — the commit is still red
        self.assertEqual(self.main("--no-mac", box=Box(log=RED))[0], 1)
        script = os.path.join(self.tmp, "ok.py")
        with open(script, "w") as f:
            f.write("print('Ran 1 test in 0.1s')\nprint('OK')\n")
        exe, script = (p.replace("\\", "/") for p in (sys.executable, script))
        code, text = self.main("--no-windows", environ={"HUB_STAND_MAC": f'"{exe}" "{script}"'})
        self.assertEqual(code, 1, text)   # the run says what --verdict says: the commit, with the earlier Windows
        self.assertIn("windows: red", text)
        code, text = self.main("--verdict")
        self.assertEqual(code, 1, text)
        self.assertIn("mac: green", text)

    def test_the_whole_output_of_each_machine_is_kept_next_to_the_result(self):
        # the table has only the last line of a traceback: a red seen only there is taken apart from the whole log
        self.assertEqual(self.main("--no-mac", box=Box(log=RED))[0], 1)
        with open(os.path.join(self.out, self.sha + ".windows.log"), encoding="utf-8") as f:
            self.assertIn('trial.py", line 65, in save', f.read())
        with open(os.path.join(self.out, self.sha + ".json"), encoding="utf-8") as f:
            self.assertNotIn("output", json.load(f)["machines"]["windows"])
        self.main("--no-mac", "--only", "tests.test_x")
        self.assertTrue(os.path.exists(os.path.join(self.out, self.sha + "-only.windows.log")))
        # a stand failure after the log came keeps it (here: an administrator's token); one with no log takes the
        # earlier run's away — the log next to the verdict is always of that run
        log = os.path.join(self.out, self.sha + ".windows.log")
        self.assertEqual(self.main("--no-mac", box=Box(groups=ADMIN, token="fltmc 0", fltmc=LISTED, log=RED))[0], 2)
        with open(log, encoding="utf-8") as f:
            self.assertIn("trial.py", f.read())
        self.assertEqual(self.main("--no-mac", box=Box(runas=False))[0], 2)
        self.assertFalse(os.path.exists(log))

    def test_a_mac_that_does_not_answer_in_time_keeps_what_it_printed(self):
        script = os.path.join(self.tmp, "hang.py")
        with open(script, "w") as f:
            f.write("import sys, time\nprint('===== tests.test_x.A.test_hangs', flush=True)\ntime.sleep(30)\n")
        exe, script = (p.replace("\\", "/") for p in (sys.executable, script))
        code, text = self.main("--no-windows", "--timeout", "3", environ={"HUB_STAND_MAC": f'"{exe}" "{script}"'})
        self.assertEqual(code, 2, text)
        with open(os.path.join(self.out, self.sha + ".mac.log"), encoding="utf-8") as f:
            self.assertIn("test_hangs", f.read())

    def test_only_a_part_of_the_suite_is_no_verdict(self):
        self.assertEqual(self.main("--no-mac", "--only", "tests.test_x")[0], 0)
        code, text = self.main("--verdict")
        self.assertEqual(code, 2)
        self.assertIn("has not run", text)

    def test_no_stand_set_is_not_a_green(self):
        code, text = self.main(environ={})
        self.assertEqual(code, 2)
        self.assertIn("HUB_STAND_WIN is not set", text)

    def test_the_mac_command_gets_the_repo_and_the_commit_and_its_code_decides(self):
        script = os.path.join(self.tmp, "bridge.py")
        args = os.path.join(self.tmp, "args.json")
        with open(script, "w") as f:
            f.write("import json, sys\njson.dump(sys.argv[1:], open(sys.argv[1], 'w'))\n"
                    "print('Ran 7616 tests in 361.0s')\nprint('OK' if sys.argv[2] == '0' else 'FAILED')\n"
                    "sys.exit(int(sys.argv[2]))\n")
        exe, script, at = (p.replace("\\", "/") for p in (sys.executable, script, args))
        for code, want, status in ((0, 0, pr_stands.OK), (1, 1, pr_stands.RED), (2, 2, pr_stands.ERROR)):
            got, text = self.main("--no-windows", environ={"HUB_STAND_MAC": f'"{exe}" "{script}" "{at}" {code}'})
            self.assertEqual(got, want, text)
            with open(args) as f:
                self.assertEqual(json.load(f)[-4:], ["--repo", self.repo, "--sha", self.sha])
            with open(os.path.join(self.out, self.sha + ".json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["machines"]["mac"]["status"], status)


class Ssh(unittest.TestCase):
    def test_the_answer_is_what_the_command_printed_not_the_ssh_clients_warnings(self):
        # 03.11, the first live run: the client's warning about the key exchange (stderr) was taken for the user name
        d = tempfile.mkdtemp(prefix="pr-stands-test-")
        self.addCleanup(platforms.current.rmtree, d)
        fake = os.path.join(d, "ssh.py")
        with open(fake, "w") as f:
            f.write("import sys\nsys.stderr.write('** WARNING: connection is not using a post-quantum key exchange\\n')\n"
                    "print('hub')\nsys.exit(0 if sys.argv[-1] == 'echo %USERNAME%' else 1)\n")
        box = pr_stands.Ssh("hub@box", ssh=[sys.executable, fake])
        code, said = box.run("echo %USERNAME%")
        self.assertEqual((code, said.splitlines()), (0, ["hub"]))   # \r\n on Windows
        code, said = box.run("type x")
        self.assertEqual(code, 1)
        self.assertIn("WARNING", said)   # a failure keeps the client's words


class Paths(unittest.TestCase):
    def test_a_windows_path_for_scp_gets_the_leading_slash(self):
        # without it scp takes "C" for a host and copies nothing
        self.assertEqual(pr_stands.scp_path("C:\\hubrev\\pr-stands\\x.log"), "/C:/hubrev/pr-stands/x.log")
        self.assertEqual(pr_stands.scp_path("/tmp/x"), "/tmp/x")


if __name__ == "__main__":
    unittest.main()
