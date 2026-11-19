"""Chrome litter in the system temp folder (outside the tests' TMPDIR): com.google.Chrome.* directories with a
SingletonSocket and empty BlobRegistryFiles*. On 10-06 there were ~4700 and ~870 of them, 800 a day: only a cleanly
closed page test removed them; runs cut off by a timeout or an update rollback, and `hub shot`/`hub video` and mermaid
diagrams for Telegram left them behind. Abandoned test-run directories were removed only after a day.

Someone else's live Chrome (the user's, an agent's) is left alone: a directory with a live SingletonSocket (connect
answers) stays, and so does an empty directory created after the oldest live Chrome started.

03.11: a live Chrome that had not accepted for a while (hung: the sweeps of earlier test processes filled its queue)
lost its directory — connect did not get through, and anything but success counted as dead; the same profile then could
not start. The cleaners take only what is surely ours: this account's, not a link, the exact mkdtemp name."""
import tests  # noqa: F401 — keeps the machine's settings out
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import platforms
from modules.testchrome import litter
from tests import winsafe

HOUR = 3600
DEAD_PID = 2 ** 22 + 12345   # above any pid_max: no such process


def short_dir():
    """An AF_UNIX socket path is at most 104 bytes on macOS: a directory right in /tmp, not in a long temp folder."""
    d = tempfile.mkdtemp(prefix="lt-", dir="/tmp" if os.path.isdir("/tmp") else None)
    return d


def age(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


@unittest.skipIf(os.name == "nt", "Chrome AF_UNIX sockets: macOS and Linux")
class Sweep(unittest.TestCase):
    def setUp(self):
        self.base = short_dir()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.live = []

    def chrome_dir(self, name, alive, old=True):
        d = os.path.join(self.base, name)
        os.mkdir(d)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(os.path.join(d, "SingletonSocket"))
        if alive:
            s.listen(1)
            self.live.append(s)
            self.addCleanup(s.close)
        else:
            s.close()   # the socket file is left but nobody listens: connect will refuse
        if old:
            age(d, 2 * HOUR)
        return d

    def test_dead_singleton_dirs_go_live_and_young_stay(self):
        dead = self.chrome_dir("com.google.Chrome.dead", alive=False)
        live = self.chrome_dir("com.google.Chrome.live", alive=True)
        young = self.chrome_dir("com.google.Chrome.young", alive=False, old=False)
        litter.sweep(self.base, starts=[])
        self.assertFalse(os.path.exists(dead))
        self.assertTrue(os.path.exists(live))
        self.assertTrue(os.path.exists(young))

    def test_empty_dirs_of_dead_chromes_go(self):
        old = os.path.join(self.base, "BlobRegistryFiles-old")
        mine = os.path.join(self.base, "BlobRegistryFiles-ofalivechrome")
        full = os.path.join(self.base, "BlobRegistryFiles-full")
        fetch = os.path.join(self.base, "com.google.Chrome.chrome_chrome_url_fetcher_.1")
        for d in (old, mine, full, fetch):
            os.mkdir(d)
        open(os.path.join(full, "blob"), "w").close()
        for d in (old, full, fetch):
            age(d, 5 * HOUR)
        age(mine, 57 * HOUR - 30)
        # a live Chrome has run for 57 hours: its directory was created half a minute after its start, so it stays; the others do not
        litter.sweep(self.base, starts=[time.time() - 57 * HOUR])
        self.assertFalse(os.path.exists(old))
        self.assertFalse(os.path.exists(fetch))
        self.assertTrue(os.path.exists(mine))
        self.assertTrue(os.path.exists(full))

    def hung(self, name):
        """A live Chrome that does not accept: its queue is full (the clients of earlier sweeps stay in it) — on Linux
        connect() gets EAGAIN, on macOS it is refused, as from a dead socket."""
        d = os.path.join(self.base, name)
        os.mkdir(d)
        path = os.path.join(d, "SingletonSocket")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(path)
        s.listen(0)
        self.addCleanup(s.close)
        for _ in range(64):
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.setblocking(False)
            self.addCleanup(c.close)
            try:
                c.connect(path)
            except OSError:
                break
        age(d, 2 * HOUR)
        return d

    def test_a_hung_live_chrome_keeps_its_dir(self):
        d = self.hung("com.google.Chrome.hung")
        made = os.path.getmtime(d)   # portable: set by os.utime (age) and read back
        probe = mock.Mock(wraps=litter._dead)
        with mock.patch.object(litter, "_dead", probe):
            litter.sweep(self.base, starts=[made - 1])   # the live Chrome that made it at its start
        self.assertTrue(os.path.exists(d))
        probe.assert_not_called()   # its socket is not even probed: each probe adds to the full queue
        litter.sweep(self.base, starts=[])
        self.assertTrue(os.path.exists(d) or sys.platform == "darwin")   # macOS: refused, only the start time tells

    def test_a_refused_socket_of_a_chrome_started_with_its_dir_stays(self):
        # macOS refuses a connect to a full queue just like one to a dead socket: refused alone is not proof
        d = self.chrome_dir("com.google.Chrome.started", alive=False)
        made = os.path.getmtime(d)   # portable: set by os.utime (age) and read back
        litter.sweep(self.base, starts=[made - 1])
        self.assertTrue(os.path.exists(d))
        with mock.patch.object(litter, "chrome_starts", lambda now: None):   # ps did not answer: live Chromes unknown
            litter.sweep(self.base)
        self.assertTrue(os.path.exists(d))
        litter.sweep(self.base, starts=[made - 3 * HOUR])   # the live one is older: this one is dead
        self.assertFalse(os.path.exists(d))

    def test_only_a_refused_or_missing_socket_is_dead(self):
        class Sock:
            error = None

            def __init__(self, *a):
                pass

            def settimeout(self, t):
                pass

            def close(self):
                pass

            def connect(self, path):
                raise Sock.error
        fake = mock.Mock(socket=Sock, AF_UNIX=socket.AF_UNIX, SOCK_STREAM=socket.SOCK_STREAM)
        for error, dead in ((ConnectionRefusedError(), True), (FileNotFoundError(), True), (TimeoutError(), False),
                            (BlockingIOError(), False), (PermissionError(), False),
                            (OSError("AF_UNIX path too long"), False)):
            Sock.error = error
            with mock.patch.object(litter, "socket", fake):
                self.assertEqual(litter._dead("/x/SingletonSocket"), dead, repr(error))

    def test_a_directory_of_another_account_is_left(self):
        d = self.chrome_dir("com.google.Chrome.theirs", alive=False)
        blob = os.path.join(self.base, "BlobRegistryFiles-theirs")
        os.mkdir(blob)
        age(blob, 5 * HOUR)
        uid = os.getuid()
        with mock.patch("os.getuid", return_value=uid + 1):
            litter.sweep(self.base, starts=[])
        self.assertTrue(os.path.exists(d))
        self.assertTrue(os.path.exists(blob))

    def test_other_folders_are_not_touched(self):
        d = os.path.join(self.base, "hub-proof-x")
        os.mkdir(d)
        age(d, 5 * HOUR)
        litter.sweep(self.base, starts=[])
        self.assertTrue(os.path.exists(d))


@unittest.skipIf(os.name == "nt", "the SingletonSocket symlink: macOS and Linux")
class Own(unittest.TestCase):
    def test_the_closed_chromes_own_dir_goes(self):
        base, profile = short_dir(), tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        mine = os.path.join(base, "com.google.Chrome.abc")
        os.mkdir(mine)
        open(os.path.join(mine, "SingletonSocket"), "w").close()
        winsafe.symlink(self, os.path.join(mine, "SingletonSocket"), os.path.join(profile, "SingletonSocket"))
        litter.own(profile)
        self.assertFalse(os.path.exists(mine))

    def test_a_link_elsewhere_is_left(self):
        base, profile = short_dir(), tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        other = os.path.join(base, "something")
        os.mkdir(other)
        winsafe.symlink(self, os.path.join(other, "SingletonSocket"), os.path.join(profile, "SingletonSocket"))
        litter.own(profile)
        self.assertTrue(os.path.exists(other))


class Runs(unittest.TestCase):
    """Abandoned test-run directories: older than an hour and without a live process are removed at the next run's start."""

    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, self.base)

    def run_dir(self, name, hours, pid=None):
        d = os.path.join(self.base, "hub-core-tests-run-" + name)   # mkdtemp: 8 characters
        os.mkdir(d)
        if pid is not None:
            with open(os.path.join(d, "pid"), "w") as f:
                f.write(str(pid))
        age(d, hours * HOUR)
        return d

    def test_old_abandoned_go_running_and_young_stay(self):
        gone = self.run_dir("gone1234", 2, pid=DEAD_PID)
        running = self.run_dir("running1", 2, pid=os.getpid())
        young = self.run_dir("young123", 0, pid=DEAD_PID)
        litter.sweep_runs(self.base)
        self.assertFalse(os.path.exists(gone))
        self.assertTrue(os.path.exists(running))
        self.assertTrue(os.path.exists(young))

    def test_only_our_runs_go(self):
        # audit 03.11: any folder named hub-core-tests-run-* older than an hour without a live pid was deleted
        notes = self.run_dir("notes", 2, pid=DEAD_PID)          # not a mkdtemp name: the user's folder
        open(os.path.join(notes, "todo.txt"), "w").close()
        age(notes, 2 * HOUR)
        unmarked = self.run_dir("abcd1234", 2)                   # no pid mark: nothing says it is a run
        cut = self.run_dir("cutshort", 2, pid="")                # the mark cut short (killed while writing, disk full)
        text = self.run_dir("textmark", 2, pid="none")           # not a pid: not our mark
        huge = self.run_dir("hugemark", 2, pid=10 ** 20)        # os.kill: OverflowError, not a process
        for d in (cut, text, huge):
            age(d, 2 * HOUR)
        litter.sweep_runs(self.base)
        self.assertTrue(os.path.exists(os.path.join(notes, "todo.txt")))
        self.assertTrue(os.path.exists(unmarked))
        self.assertFalse(os.path.exists(cut))
        self.assertTrue(os.path.exists(text))
        self.assertTrue(os.path.exists(huge))

    def test_a_link_or_junction_named_as_a_run_is_not_followed(self):
        # a link to someone else's folder with a dead pid mark in it: the cleaner does not even ask rmtree (which
        # refuses links today) — on Windows lstat calls a junction a directory
        victim = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, victim)
        with open(os.path.join(victim, "pid"), "w") as f:
            f.write(str(DEAD_PID))
        links = []
        if winsafe.try_symlink(victim, os.path.join(self.base, "hub-core-tests-run-symlink1"), target_is_directory=True):
            links.append("symlink")
        try:
            import _winapi
            _winapi.CreateJunction(victim, os.path.join(self.base, "hub-core-tests-run-junction"))
            links.append("junction")
        except (ImportError, AttributeError, OSError):   # not Windows; a file system without junctions
            pass
        if not links:
            self.skipTest("neither a link nor a junction can be created here")
        age(victim, 2 * HOUR)
        with mock.patch.object(platforms.current, "rmtree") as rmtree:
            litter.sweep_runs(self.base)
        rmtree.assert_not_called()
        self.assertTrue(os.path.exists(os.path.join(victim, "pid")), links)

    @unittest.skipUnless(hasattr(os, "getuid"), "the folder owner is a uid (macOS and Linux)")
    def test_a_run_of_another_account_is_left(self):
        d = self.run_dir("theirs12", 2, pid=DEAD_PID)
        uid = os.getuid()
        with mock.patch("os.getuid", return_value=uid + 1):
            litter.sweep_runs(self.base)
        self.assertTrue(os.path.exists(d))

    def test_an_unreadable_mark_keeps_the_run(self):
        d = self.run_dir("locked12", 2, pid=DEAD_PID)
        mark = os.path.join(d, "pid")
        os.chmod(mark, 0)
        self.addCleanup(os.chmod, mark, stat.S_IREAD | stat.S_IWRITE)
        try:
            open(mark).close()
            self.skipTest("read permission cannot be taken away here (root, Windows)")
        except PermissionError:
            pass
        age(d, 2 * HOUR)
        litter.sweep_runs(self.base)
        self.assertTrue(os.path.exists(d))

    def test_abandoned_run_with_read_only_files_goes_too(self):
        """git makes .git/objects read-only (Windows): shutil.rmtree(ignore_errors=True) did not delete them, the run
        directory stayed forever, and every test start walked all such directories again (25-50 s per process)."""
        d = self.run_dir("readonly", 2, pid=DEAD_PID)
        objects = os.path.join(d, "repo", ".git", "objects", "0b")
        os.makedirs(objects)
        obj = os.path.join(objects, "5e1dbf")
        open(obj, "w").close()
        os.chmod(obj, stat.S_IREAD)
        age(d, 2 * HOUR)
        litter.sweep_runs(self.base)
        self.assertFalse(os.path.exists(d))

    def test_abandoned_runs_are_removed_the_way_that_takes_read_only_files(self):
        """The same on any OS: the platform rmtree removes them (with chmod and a retry), not shutil with ignore_errors."""
        d = self.run_dir("gone1234", 2, pid=DEAD_PID)
        with mock.patch.object(platforms.current, "rmtree") as rmtree:
            litter.sweep_runs(self.base)
        rmtree.assert_called_once_with(d)

    def test_this_run_marks_itself(self):
        with open(os.path.join(tempfile.gettempdir(), "pid")) as f:
            self.assertEqual(int(f.read()), os.getpid())


class RunChromes(unittest.TestCase):
    """10-09: three headless Chromes from runs killed by a timeout lived for 2-3 days: cleanup removed the run directory
    but nobody stopped its Chrome. We stop the main Chrome (by PID from ps) whose profile is in a run without a live process."""

    ME = os.getuid() if hasattr(os, "getuid") else None

    def again(self, out, changed=()):
        """The second look right before the signal (litter._ps_one): the same lines unless changed, by pid."""
        now = {int(pid): (self.ME, cmd.strip()) for pid, _, cmd in (x.strip().partition(" ") for x in out.splitlines())}
        now.update(changed)
        return mock.patch.object(litter, "_ps_one", lambda pid: now.get(int(pid)), create=True)

    def run_dir(self, base, name, pid=None, older=0):
        d = os.path.join(base, "hub-core-tests-run-" + name)
        os.mkdir(d)
        if pid is not None:
            with open(os.path.join(d, "pid"), "w") as f:
                f.write(str(pid))
        if older:
            age(d, older)
        return d

    def test_chrome_of_a_dead_or_removed_run_is_stopped_live_run_and_others_stay(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        live = self.run_dir(base, "live1234", os.getpid())
        dead = self.run_dir(base, "dead1234", DEAD_PID, older=2 * HOUR)
        young = self.run_dir(base, "young123")   # just created, no pid marker yet
        notes = self.run_dir(base, "notes", DEAD_PID, older=2 * HOUR)   # not a mkdtemp name: the user's own profile
        exe = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        out = (f"101 {exe} --headless=new --remote-debugging-port=1 --user-data-dir={dead}/tmpab/p\n"
               f"102 {exe} --headless=new --user-data-dir={base}/hub-core-tests-run-gone1234/x\n"  # the directory is already removed
               f"103 {exe} --headless=new --user-data-dir={live}/p\n"
               f"104 {exe} --headless=new --user-data-dir={young}/p\n"
               f"105 {exe} --type=renderer --user-data-dir={dead}/tmpab/p\n"                    # a child: goes away by itself
               f"106 {exe} --user-data-dir=/home/x/agent-chrome\n"
               f"107 /usr/bin/python3 x.py --user-data-dir={dead}/p\n"
               f"108 {exe} --user-data-dir={notes}/p\n")
        killed = []
        with self.again(out):
            litter.stop_run_chromes(base, ps=lambda: out, kill=killed.append)
        self.assertEqual(sorted(killed), [101, 102])

    def test_abandoned_fake_chrome_of_a_dead_run_is_stopped(self):
        # 23.10: the stub tests/fakes/fake_chrome.py runs under python, not Chrome: the filter by program name did not see
        # it, and two abandoned ones lived for 4 days and slowed the runs. A live run, someone else's python, a
        # reused pid are left alone
        base = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, base)
        live = self.run_dir(base, "live1234", os.getpid())
        dead = self.run_dir(base, "dead1234", DEAD_PID, older=2 * HOUR)
        fake = "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3 /src/tests/fakes/fake_chrome.py"
        out = (f"501 {fake} --remote-debugging-port=9333 --user-data-dir={dead}/tmpab/p\n"
               f"502 /usr/bin/python3 /src/tests/fakes/fake_chrome.py --remote-debugging-port=9334 "
               f"--user-data-dir={base}/hub-core-tests-run-gone1234/x\n"
               f"503 {fake} --remote-debugging-port=9335 --user-data-dir={live}/p\n"
               f"504 /usr/bin/python3 /src/other.py --user-data-dir={dead}/p\n"
               f"505 {fake} --remote-debugging-port=9336 --user-data-dir={dead}/tmpcd/p\n")
        changed = {505: (self.ME, "/usr/bin/sleep 600")}   # the pid went to someone else before the signal
        killed = []
        with self.again(out, changed):
            litter.stop_run_chromes(base, ps=lambda: out, kill=killed.append)
        self.assertEqual(sorted(killed), [501, 502])

    def test_a_chrome_of_another_account_or_a_reused_pid_is_left(self):
        # audit 03.11: the list is every account's, and the signal goes after several more ps calls
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        dead = self.run_dir(base, "dead1234", DEAD_PID, older=2 * HOUR)
        exe = "/usr/bin/google-chrome"
        out = "".join(f"{pid} {exe} --headless=new --user-data-dir={dead}/p{pid}\n" for pid in (201, 202, 203, 204))
        changed = {202: ((self.ME or 0) + 1, f"{exe} --headless=new --user-data-dir={dead}/p202"),   # another account's
                   203: (self.ME, "/usr/bin/sleep 600"),                                              # the pid went to someone else
                   204: None}                                                                         # gone by now
        killed = []
        with self.again(out, changed):
            litter.stop_run_chromes(base, ps=lambda: out, kill=killed.append)
        self.assertEqual(killed, [201])

    def test_a_profile_that_is_a_link_is_not_a_finished_run(self):
        # a link named as a profile, to a folder with a dead pid mark: not a directory of ours, its Chrome is left
        base, victim = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, base)
        self.addCleanup(platforms.current.rmtree, victim)
        with open(os.path.join(victim, "pid"), "w") as f:
            f.write(str(DEAD_PID))
        link = os.path.join(base, "hub-proof-link1234")
        winsafe.symlink(self, victim, link, target_is_directory=True)
        out = f"501 /x/chrome-headless-shell --headless=new --user-data-dir={link}\n"
        killed = []
        with self.again(out):
            litter.stop_proof_chromes(base, ps=lambda: out, kill=killed.append)
        self.assertEqual(killed, [])

    def test_an_unreadable_mark_is_not_a_finished_run(self):
        base = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, base)
        run = self.run_dir(base, "locked12", DEAD_PID, older=2 * HOUR)
        mark = os.path.join(run, "pid")
        os.chmod(mark, 0)
        self.addCleanup(os.chmod, mark, stat.S_IREAD | stat.S_IWRITE)
        try:
            open(mark).close()
            self.skipTest("read permission cannot be taken away here (root, Windows)")
        except PermissionError:
            pass
        age(run, 2 * HOUR)
        out = f"301 /usr/bin/google-chrome --headless=new --user-data-dir={run}/p\n"
        killed = []
        with self.again(out):
            litter.stop_run_chromes(base, ps=lambda: out, kill=killed.append)
        self.assertEqual(killed, [])

    @unittest.skipUnless(hasattr(os, "getuid") and shutil.which("ps"), "ps -o uid= is macOS and Linux (not the ps from Git on Windows)")
    def test_the_second_look_reads_the_same_line_as_the_list(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "--user-data-dir=/x/p"],
                             stdin=subprocess.DEVNULL)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        line = next(x for x in litter._ps_pids().splitlines() if x.split(None, 1)[0] == str(p.pid))
        self.assertEqual(litter._ps_one(p.pid), (self.ME, line.strip().partition(" ")[2].strip()))

    def test_proof_chrome_of_a_dead_owner_is_stopped(self):
        # 10-12: three headless Chromes of `hub shot`/cdp.Page from scripts that crashed before close() lived for hours;
        # with their helpers, dozens of processes. A hub-proof-* profile keeps its owner's pid: if the owner died, the
        # directory is gone, or there is no marker and it is older than an hour, we stop it; a live owner and a
        # just-created profile are left alone
        base = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, base)

        def profile(name, pid=None, older=0):
            d = os.path.join(base, name)
            os.mkdir(d)
            if pid is not None:
                with open(os.path.join(d, "pid"), "w") as f:
                    f.write(str(pid))
            if older:
                age(d, older)
            return d
        live, dead = profile("hub-proof-live1234", os.getpid()), profile("hub-proof-dead1234", DEAD_PID)
        young, old = profile("hub-proof-young123"), profile("hub-proof-old12345", pid="", older=2 * HOUR)   # mark cut short
        mine = profile("hub-proof-mine", DEAD_PID, older=2 * HOUR)   # not a mkdtemp name: the user's own profile
        unmarked = profile("hub-proof-nomark12", older=2 * HOUR)   # no mark: not known to be a proof profile
        exe = "/x/chrome-headless-shell"
        out = (f"201 {exe} --headless=new --user-data-dir={dead} --remote-debugging-port=0 about:blank\n"
               f"202 {exe} --headless=new --user-data-dir={base}/hub-proof-gone1234 --remote-debugging-port=0\n"
               f"203 {exe} --headless=new --user-data-dir={old}\n"
               f"204 {exe} --headless=new --user-data-dir={live}\n"
               f"205 {exe} --headless=new --user-data-dir={young}\n"
               f"206 {exe} --type=renderer --user-data-dir={dead}\n"
               f"207 {exe} --headless=new --user-data-dir=/elsewhere/hub-proof-x\n"
               f"208 {exe} --headless=new --user-data-dir={mine}\n"
               f"209 {exe} --headless=new --user-data-dir={unmarked}\n")
        killed = []
        with self.again(out):
            litter.stop_proof_chromes(base, ps=lambda: out, kill=killed.append)
        self.assertEqual(sorted(killed), [201, 202, 203])
        # review 10-13: the old Playwright layout, a headless_shell binary (chrome.SHELL_PATTERNS)
        killed = []
        out = f"301 /x/headless_shell --headless=new --user-data-dir={dead}\n"
        with self.again(out):
            litter.stop_proof_chromes(base, ps=lambda: out, kill=killed.append)
        self.assertEqual(killed, [301])
        # 10-17: /usr/bin/google-chrome is a script with `exec -a "$0"`, so the main process is named google-chrome (the
        # test sandbox starts Chrome this way); its Chrome used to be left running
        killed = []
        out = f"401 /usr/bin/google-chrome --no-sandbox --headless=new --user-data-dir={dead}\n"
        with self.again(out):
            litter.stop_proof_chromes(base, ps=lambda: out, kill=killed.append)
        self.assertEqual(killed, [401])

    @unittest.skipIf(os.name == "nt", "zombies: macOS and Linux")
    def test_a_run_whose_process_is_a_zombie_is_over(self):
        # PR review: right after the self-check timeout the executors of the killed run are still zombies; signal 0
        # finds them, and the run's Chrome used to be left running
        import platforms
        import subprocess
        import sys
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        self.addCleanup(p.wait)
        deadline = time.time() + 10
        while time.time() < deadline and "Z" not in subprocess.run(["ps", "-o", "stat=", "-p", str(p.pid)],
                                                                   capture_output=True, text=True).stdout:
            time.sleep(0.05)
        self.assertTrue(platforms.current.alive(p.pid))   # not reaped: signal 0 goes through
        self.assertFalse(litter._alive(p.pid))


class ChromeStarts(unittest.TestCase):
    def test_main_chrome_processes_only(self):
        out = ("  12:34 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome\n"
               "1-02:00:00 /opt/chrome/chrome-headless-shell --headless\n"
               "  01:00 /Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary\n"   # its dir: com.google.Chrome.canary.*
               "99-00:00:00 /usr/bin/python3 server.py\n"
               "  00:05 /Applications/Google Chrome.app/Contents/Frameworks/x/Google Chrome Helper --type=renderer\n"
               "  09:00 /usr/bin/node /home/x/.npm/_npx/1/node_modules/chrome-devtools-mcp/build/index.js\n")
        now = 1_000_000
        self.assertEqual(litter.chrome_starts(now, ps=lambda: out), [now - 754, now - (86400 + 2 * 3600), now - 60])
        self.assertEqual(litter.chrome_starts(now, ps=lambda: ""), [])


if __name__ == "__main__":
    unittest.main()
