"""Hub files are for the hub's user only: every entry point sets umask 077, and the state and settings folders that an
earlier hub left open are closed at core start (the old modes are recorded, `hub perms restore` brings them back).
Processes the hub starts on the user's behalf (sessions, schedule jobs, executors' working copies, `hub shot --out`)
get the user's own umask: files in projects get the usual permissions."""
import tests  # noqa: F401 — hide the machine's settings (tests/__init__.py)
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import platforms
from core import perms
from tests import winsafe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(ROOT, "bin", "hub")
POSIX = os.name == "posix"
JOURNAL = '{"seq":1,"ts":1,"src":"core","type":"core.started"}\n'   # a hub's state folder is known by its journal


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


class HubUmask(unittest.TestCase):
    """A hub process with umask 077 and the remembered user umask 022, as under hub-up."""

    def setUp(self):
        self.was = os.umask(0o077)
        self.addCleanup(os.umask, self.was)
        patch = mock.patch.dict(os.environ, {perms.UMASK_ENV: "0o22"})
        patch.start()
        self.addCleanup(patch.stop)
        self.dir = tempfile.mkdtemp(prefix="perms-user-")
        self.addCleanup(shutil.rmtree, self.dir, True)


@unittest.skipUnless(POSIX, "POSIX file permissions")
class Umask(unittest.TestCase):
    def setUp(self):
        self.was = os.umask(0o022)
        self.addCleanup(os.umask, self.was)
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop(perms.UMASK_ENV, None)

    def test_private_sets_077_and_remembers_the_users_umask(self):
        perms.private()
        self.assertEqual(os.umask(0o077), 0o077)
        self.assertEqual(os.environ[perms.UMASK_ENV], "0o22")
        self.assertEqual(perms.user_umask(), 0o022)

    def test_a_child_entry_keeps_what_the_first_one_remembered(self):
        os.environ[perms.UMASK_ENV] = "0o2"
        perms.private()   # hubd under hub-up: umask is already 077, the user's one came in the environment
        self.assertEqual(perms.user_umask(), 0o002)

    def test_without_a_remembered_umask_children_inherit(self):
        self.assertEqual(perms.user_umask(), -1)
        os.environ[perms.UMASK_ENV] = "junk"
        self.assertEqual(perms.user_umask(), -1)

    def test_entry_points_call_private_before_their_code(self):
        # every hub process starts from one of these: hubd, the page, the clock, the updater are children of hub-up
        for rel in (os.path.join("bin", "hub"), os.path.join("bin", "hub-up"), os.path.join("hooks", "hub_hook.py")):
            with self.subTest(rel), open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                self.assertIn("perms.private()", f.read(), rel)
        from core import daemon
        import inspect
        self.assertIn("perms.private()", inspect.getsource(daemon.main))

    def test_the_hook_writes_its_fact_privately(self):
        d = tempfile.mkdtemp(prefix="hook-")
        self.addCleanup(shutil.rmtree, d, True)
        env = {**os.environ, "HUB_SESSION": "w1", "HUB_STATE_DIR": d}
        subprocess.run(["sh", "-c", f"umask 002; exec {sys.executable} {os.path.join(ROOT, 'hooks', 'hub_hook.py')}"],
                       input=b'{"hook_event_name": "Stop", "session_id": "s1"}', env=env, check=True, timeout=60)
        names = os.listdir(os.path.join(d, "spool"))
        self.assertEqual(len(names), 1)
        self.assertEqual(mode(os.path.join(d, "spool", names[0])), 0o600)
        self.assertEqual(mode(os.path.join(d, "spool")), 0o700)

    def test_the_cli_writes_the_inbox_privately(self):
        d = tempfile.mkdtemp(prefix="cli-")
        self.addCleanup(shutil.rmtree, d, True)
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_CONFIG")}
        env.update(HUB_STATE_DIR=d, HUB_CONFIG_DIR=d, HUB_CLI_WAIT="0")
        r = subprocess.run(["sh", "-c", f"umask 002; exec {sys.executable} {HUB} notify 'x'"], env=env, timeout=60,
                           capture_output=True, text=True)
        self.assertTrue(os.listdir(os.path.join(d, "spool")), r.stdout + r.stderr)
        self.assertEqual(mode(os.path.join(d, "spool")), 0o700)


@unittest.skipUnless(POSIX, "POSIX file permissions")
class Tighten(unittest.TestCase):
    def setUp(self):
        self.was = os.umask(0o002)
        self.addCleanup(os.umask, self.was)
        self.state = tempfile.mkdtemp(prefix="state-")
        self.addCleanup(shutil.rmtree, self.state, True)
        os.chmod(self.state, 0o755)
        for rel in ("journal.jsonl", "state.json", "hooks-settings.json", "journal-archive/journal-1-9.jsonl",
                    "sessions/w1/mcp.json", "spool/x.json", "workspaces/w1/site/index.html"):
            path = os.path.join(self.state, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("{}")
            os.chmod(path, 0o664)
        for d in ("journal-archive", "sessions", "sessions/w1", "spool", "workspaces", "workspaces/w1"):
            os.chmod(os.path.join(self.state, d), 0o775)
        self.outside = tempfile.mkdtemp(prefix="outside-")
        self.addCleanup(shutil.rmtree, self.outside, True)
        self.target = os.path.join(self.outside, "dotfile")
        with open(self.target, "w") as f:
            f.write("x")
        os.chmod(self.target, 0o644)
        winsafe.symlink(self, self.target, os.path.join(self.state, "link"))

    def test_state_folder_closed_and_files_private(self):
        perms.tighten(self.state)
        self.assertEqual(mode(self.state), 0o700)
        for rel in ("journal.jsonl", "state.json", "hooks-settings.json", "journal-archive/journal-1-9.jsonl",
                    "sessions/w1/mcp.json", "spool/x.json"):
            self.assertEqual(mode(os.path.join(self.state, rel)), 0o600, rel)
        for rel in ("journal-archive", "sessions", "sessions/w1", "spool"):
            self.assertEqual(mode(os.path.join(self.state, rel)), 0o700, rel)

    def test_sessions_own_work_and_links_outside_are_left_alone(self):
        perms.tighten(self.state)
        self.assertEqual(mode(os.path.join(self.state, "workspaces", "w1", "site", "index.html")), 0o664)
        self.assertEqual(mode(os.path.join(self.state, "workspaces")), 0o700)   # closed: others cannot get below it
        self.assertEqual(mode(self.target), 0o644)

    def test_it_can_be_put_back(self):
        perms.tighten(self.state)
        perms.tighten(self.state)   # a second start changes nothing and does not overwrite the first record
        perms.restore(self.state)
        self.assertEqual(mode(self.state), 0o755)
        self.assertEqual(mode(os.path.join(self.state, "journal.jsonl")), 0o664)
        self.assertEqual(mode(os.path.join(self.state, "spool")), 0o775)

    def test_the_record_holds_paths_and_modes_only(self):
        perms.tighten(self.state)
        with open(os.path.join(self.state, perms.RECORD), encoding="utf-8") as f:
            rows = [json.loads(x) for x in f if x.strip()]
        self.assertTrue(rows)
        self.assertEqual({tuple(sorted(r)) for r in rows}, {("mode", "path")})
        self.assertEqual(mode(os.path.join(self.state, perms.RECORD)), 0o600)

    def test_the_settings_folder_is_closed_one_level_deep(self):
        # HUB_CONFIG_DIR may name a folder that holds foreign things too: its files and folders are closed, what is
        # inside them is not
        conf = os.path.join(self.outside, "conf")
        os.makedirs(os.path.join(conf, "other", "deeper"))
        for path in (os.path.join(conf, "hub.json"), os.path.join(conf, "other", "deeper", "f")):
            with open(path, "w") as f:
                f.write("{}")
            os.chmod(path, 0o644)
        os.chmod(os.path.join(conf, "other"), 0o755)
        perms.tighten(conf, deep=False)
        self.assertEqual(mode(os.path.join(conf, "hub.json")), 0o600)
        self.assertEqual(mode(os.path.join(conf, "other")), 0o700)
        self.assertEqual(mode(os.path.join(conf, "other", "deeper", "f")), 0o644)

    def test_a_single_settings_file(self):
        cfg = os.path.join(self.outside, "hub.json")
        with open(cfg, "w") as f:
            f.write("{}")
        os.chmod(cfg, 0o644)
        perms.tighten_file(cfg)
        self.assertEqual(mode(cfg), 0o600)

    def test_home_is_never_closed(self):
        # HUB_STATE_DIR or HUB_CONFIG_DIR pointing at the home folder do not close all of it
        with mock.patch.dict(os.environ, {"HOME": self.outside}):
            self.assertEqual(perms.tighten(self.outside), 0)
        self.assertEqual(mode(self.target), 0o644)

    def test_a_state_folder_reached_through_a_link(self):
        # the state folder may live on another disk, with ~/.hub-core a link to it
        link = os.path.join(self.outside, "hub-core")
        winsafe.symlink(self, self.state, link)
        perms.tighten(link)
        self.assertEqual(mode(self.state), 0o700)
        self.assertEqual(mode(os.path.join(self.state, "journal.jsonl")), 0o600)
        self.assertEqual(perms.unsafe(link), [])

    def test_still_open_writable_inbox_refuses_to_start(self):
        perms.tighten(self.state)
        self.assertEqual(perms.unsafe(self.state), [])
        os.chmod(os.path.join(self.state, "spool"), 0o777)
        self.assertEqual(perms.unsafe(self.state), [os.path.join(self.state, "spool")])


@unittest.skipUnless(POSIX, "POSIX file permissions")
class CoreStart(unittest.TestCase):
    def setUp(self):
        self.state, self.conf = tempfile.mkdtemp(prefix="start-"), tempfile.mkdtemp(prefix="conf-")
        self.addCleanup(shutil.rmtree, self.state, True)
        self.addCleanup(shutil.rmtree, self.conf, True)
        os.chmod(self.state, 0o755)
        os.chmod(self.conf, 0o755)
        self.cfg = os.path.join(self.conf, "hub.json")
        self.journal = os.path.join(self.state, "journal.jsonl")
        for path in (self.cfg, self.journal):
            with open(path, "w") as f:
                f.write("{}" if path == self.cfg else JOURNAL)
            os.chmod(path, 0o644)
        env = {k: v for k, v in os.environ.items() if k != "HUB_CONFIG"}
        patch = mock.patch.dict(os.environ, {**env, "HUB_STATE_DIR": self.state, "HUB_CONFIG_DIR": self.conf},
                                clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        was = os.umask(0o022)
        self.addCleanup(os.umask, was)

    def start(self):
        from core import daemon
        with mock.patch("core.daemon.default_state_dir", return_value=self.state), \
                mock.patch("core.lock.acquire", return_value=None), self.assertRaises(SystemExit):
            daemon.main()   # stops at the lock: "already running"
        perms.wait()   # the deep walk runs in the background

    def test_the_core_closes_state_and_settings_before_it_starts(self):
        self.start()
        self.assertEqual(mode(self.state), 0o700)
        self.assertEqual(mode(self.conf), 0o700)
        self.assertEqual(mode(self.cfg), 0o600)
        self.assertEqual(mode(self.journal), 0o600)

    def test_restore_keeps_them_open_from_then_on(self):
        self.start()
        env = {**os.environ, "HUB_CLI_WAIT": "0"}
        env.pop("HUB_SESSION", None)
        r = subprocess.run([sys.executable, HUB, "perms", "restore"], env=env, capture_output=True, text=True,
                           encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(mode(self.state), 0o755)
        self.assertEqual(mode(self.journal), 0o644)
        self.assertEqual(mode(self.conf), 0o755)
        with open(self.cfg, encoding="utf-8") as f:
            self.assertEqual(json.load(f).get("perms"), {"keep": True})
        self.start()   # the next core start no longer closes them
        self.assertEqual(mode(self.state), 0o755)
        self.assertEqual(mode(self.journal), 0o644)

    def test_a_session_may_not_open_them(self):
        self.start()
        env = {**os.environ, "HUB_CLI_WAIT": "0", "HUB_SESSION": "w1"}
        r = subprocess.run([sys.executable, HUB, "perms", "restore"], env=env, capture_output=True, text=True,
                           encoding="utf-8", timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(mode(self.state), 0o700)

    def test_an_inbox_it_cannot_close_stops_the_core(self):
        from core import daemon
        with mock.patch("core.daemon.default_state_dir", return_value=self.state), \
                mock.patch("core.perms.unsafe", return_value=[os.path.join(self.state, "spool")]), \
                mock.patch("core.daemon.Hub") as hub, self.assertRaises(SystemExit) as stop:
            daemon.main()
        self.assertIn("spool", str(stop.exception))
        self.assertIn("chmod", str(stop.exception))
        hub.assert_not_called()


@unittest.skipUnless(POSIX, "POSIX file permissions")
class Kept(unittest.TestCase):
    """After `hub perms restore` (perms.keep) hub processes do not close new files either: otherwise the next write of
    state.json is 0600 again and whoever got read access back loses it."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="kept-")
        self.addCleanup(shutil.rmtree, self.state, True)
        self.out = os.path.join(self.state, "umask.txt")
        self.part = os.path.join(self.state, "part.py")
        with open(self.part, "w") as f:
            f.write("import os, sys, time\n"
                    "m = os.umask(0); os.umask(m)\n"
                    "open(sys.argv[1], 'w').write(oct(m))\n"
                    "time.sleep(60)\n")

    def read_umask(self, cfg, start="022", **extra):
        conf = os.path.join(self.state, "hub.json")
        with open(conf, "w") as f:
            json.dump(cfg, f)
        env = {k: v for k, v in os.environ.items() if k not in (perms.UMASK_ENV, perms.KEEP_ENV)}
        env.update(HUB_STATE_DIR=self.state, HUB_CONFIG=conf,
                   HUB_UP_PARTS=json.dumps({"hubd": [self.part, self.out]}))
        env.update(extra)
        p = subprocess.Popen(["sh", "-c", f"umask {start}; exec {sys.executable} {os.path.join(ROOT, 'bin', 'hub-up')}"],
                             env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            import time
            end = time.time() + 30
            while time.time() < end and not (os.path.exists(self.out) and open(self.out).read()):
                time.sleep(0.1)
            with open(self.out) as f:
                return f.read()
        finally:
            p.terminate()
            p.wait(20)
            if os.path.exists(self.out):
                os.unlink(self.out)

    def test_parts_of_the_hub_keep_the_users_umask(self):
        self.assertEqual(self.read_umask({}), "0o77")
        self.assertEqual(self.read_umask({"perms": {"keep": True}}), "0o22")
        # hub restart: hub-up re-executes itself, with the old process's umask 077 and the user's umask in the
        # environment
        self.assertEqual(self.read_umask({"perms": {"keep": True}}, start="077", **{perms.UMASK_ENV: "0o22"}), "0o22")

    def test_private_does_nothing_when_kept(self):
        was = os.umask(0o022)
        self.addCleanup(os.umask, was)
        with mock.patch.dict(os.environ, {perms.KEEP_ENV: "1"}):
            perms.private()
        self.assertEqual(os.umask(0o022), 0o022)


@unittest.skipUnless(POSIX, "POSIX file permissions")
class Interrupted(unittest.TestCase):
    """The old mode is recorded before chmod: a walk cut off midway (hubd exit, crash) is still restored."""

    def test_what_was_closed_before_a_stop_comes_back(self):
        was = os.umask(0o002)
        self.addCleanup(os.umask, was)
        state = tempfile.mkdtemp(prefix="cut-")
        self.addCleanup(shutil.rmtree, state, True)
        for i in range(6):
            d = os.path.join(state, f"d{i}")
            os.mkdir(d)
            with open(os.path.join(d, "f"), "w") as f:
                f.write("x")
            os.chmod(os.path.join(d, "f"), 0o664)
        real, calls = os.chmod, []

        def chmod(path, m):
            calls.append(path)
            if len(calls) == 8:
                raise SystemExit("stop in the middle of the walk")
            return real(path, m)
        with mock.patch("os.chmod", chmod), self.assertRaises(SystemExit):
            perms.tighten(state)
        closed = calls[:7]
        self.assertTrue(any(mode(p) & 0o077 == 0 for p in closed))
        perms.restore(state)
        for p in closed:
            self.assertEqual(mode(p) & 0o077, 0o064 if os.path.isfile(p) else 0o075, p)


@unittest.skipUnless(POSIX, "POSIX file permissions")
class OutsideRecords(unittest.TestCase):
    def test_a_settings_file_elsewhere_gets_no_record_next_to_it(self):
        # HUB_CONFIG in a project folder: the record of old modes goes to the state folder, not as litter next to the
        # file
        state, proj = tempfile.mkdtemp(prefix="rec-state-"), tempfile.mkdtemp(prefix="rec-proj-")
        self.addCleanup(shutil.rmtree, state, True)
        self.addCleanup(shutil.rmtree, proj, True)
        cfg = os.path.join(proj, "hub.json")
        with open(cfg, "w") as f:
            f.write("{}")
        os.chmod(cfg, 0o644)
        with open(os.path.join(state, "journal.jsonl"), "w") as f:
            f.write(JOURNAL)
        with mock.patch.dict(os.environ, {"HUB_CONFIG": cfg}):
            perms.secure(state, cfg)
            perms.wait()
        self.assertEqual(os.listdir(proj), ["hub.json"])
        self.assertEqual(mode(cfg), 0o600)
        perms.restore(state)
        self.assertEqual(mode(cfg), 0o644)

    def test_the_common_settings_root_is_never_closed(self):
        # HUB_CONFIG_DIR=~/.config: other programs' settings in it are left alone
        home = tempfile.mkdtemp(prefix="rec-home-")
        self.addCleanup(shutil.rmtree, home, True)
        root = os.path.join(home, ".config")
        os.makedirs(os.path.join(root, "git"))
        os.chmod(os.path.join(root, "git"), 0o755)
        with mock.patch.dict(os.environ, {"HOME": home}):
            os.environ.pop("XDG_CONFIG_HOME", None)
            self.assertEqual(perms.tighten(root, deep=False), 0)
            os.environ["XDG_CONFIG_HOME"] = os.path.join(home, "elsewhere")   # its own XDG: ~/.config is shared anyway
            self.assertEqual(perms.tighten(root, deep=False), 0)
        self.assertEqual(mode(os.path.join(root, "git")), 0o755)


@unittest.skipUnless(perms.POSIX, "POSIX file permissions")
class NotTheHubs(unittest.TestCase):
    """A wrong HUB_STATE_DIR (a project), a settings folder without the hub's settings in it, a hub run by root: the core
    start closes nothing that is not the hub's."""

    def setUp(self):
        self.was = os.umask(0o002)
        self.addCleanup(os.umask, self.was)
        self.proj, self.conf = tempfile.mkdtemp(prefix="proj-"), tempfile.mkdtemp(prefix="conf-")
        self.addCleanup(platforms.current.rmtree, self.proj)
        self.addCleanup(platforms.current.rmtree, self.conf)
        # another program's state.json and journal.jsonl: names are no proof
        for folder in (self.proj, self.conf):
            for rel, text in (("README.md", "x"), ("state.json", "{}"), ("journal.jsonl", '{"event": 1}\n'),
                              ("src/a.py", "a = 1\n")):
                path = os.path.join(folder, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    f.write(text)
                os.chmod(path, 0o644)
            os.chmod(os.path.join(folder, "src"), 0o755)
            os.chmod(folder, 0o755)
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("HUB_CONFIG", None)

    def modes(self, folder):
        return {top: mode(top) for top in [folder] + [os.path.join(t, n) for t, ds, fs in os.walk(folder)
                                                      for n in ds + fs]}

    def test_a_project_named_as_the_state_folder_is_left_alone(self):
        before = self.modes(self.proj)
        with open(os.path.join(self.conf, "hub.json"), "w") as f:
            f.write("{}")
        self.assertEqual(perms.secure(self.proj, os.path.join(self.conf, "hub.json")), [])
        perms.wait()
        self.assertEqual(self.modes(self.proj), before)
        self.assertFalse(os.path.exists(os.path.join(self.proj, perms.RECORD)))

    def test_a_settings_folder_without_the_hubs_settings_is_left_alone(self):
        state = tempfile.mkdtemp(prefix="hub-state-")
        self.addCleanup(platforms.current.rmtree, state)
        with open(os.path.join(state, "journal.jsonl"), "w") as f:
            f.write(JOURNAL)
        os.chmod(state, 0o755)
        before = self.modes(self.conf)
        perms.secure(state, os.path.join(self.conf, "hub.json"))   # HUB_CONFIG_DIR names a folder with no hub.json
        perms.wait()
        self.assertEqual(self.modes(self.conf), before)
        self.assertEqual(mode(state), 0o700)   # the hub's own state folder is still closed

    def test_a_hub_run_by_root_leaves_a_settings_folder_elsewhere_alone(self):
        # root may chmod anything: HUB_CONFIG_DIR=/etc with a hub.json in it would close the system's files; its own
        # state folder is still closed (else a group-writable one would stop the core for good)
        os.makedirs(os.path.join(self.proj, "hub-home"))
        with open(os.path.join(self.proj, "hub-home", "CLAUDE.md"), "w") as f:   # the install's mark
            f.write("rules")
        with open(os.path.join(self.conf, "hub.json"), "w") as f:
            f.write("{}")
        os.chmod(os.path.join(self.conf, "hub.json"), 0o644)
        before = self.modes(self.conf)
        with mock.patch("os.geteuid", return_value=0):
            self.assertEqual(perms.secure(self.proj, os.path.join(self.conf, "hub.json")), [])
            perms.wait()
        self.assertEqual(self.modes(self.conf), before)
        self.assertEqual(mode(self.proj), 0o700)
        self.assertEqual(mode(os.path.join(self.proj, "README.md")), 0o600)

    def test_the_standard_settings_folder_is_the_hubs_without_hub_json_too(self):
        # an older hub left its bot token there open, hub.json not written yet
        state = tempfile.mkdtemp(prefix="hub-state-")
        self.addCleanup(platforms.current.rmtree, state)
        with open(os.path.join(state, "journal.jsonl"), "w") as f:
            f.write(JOURNAL)
        with mock.patch.object(platforms.current, "config_dir", return_value=self.conf):
            perms.secure(state, os.path.join(self.conf, "hub.json"))
            perms.wait()
        self.assertEqual(mode(self.conf), 0o700)
        self.assertEqual(mode(os.path.join(self.conf, "README.md")), 0o600)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "no named pipes")
    def test_a_pipe_named_like_the_journal_is_not_read(self):
        from core import uninstall
        os.unlink(os.path.join(self.proj, "journal.jsonl"))
        os.mkfifo(os.path.join(self.proj, "journal.jsonl"))   # open() would wait for a writer forever
        self.assertFalse(uninstall.marked(self.proj))


class OldStateFile(unittest.TestCase):
    def test_a_state_file_of_an_older_hub_loads_without_keys(self):
        from core import journal, reducer
        state = tempfile.mkdtemp(prefix="old-state-")
        self.addCleanup(shutil.rmtree, state, True)
        j = journal.Journal(os.path.join(state, "journal.jsonl"))
        j.append("owner.settings", data={"timezone": "UTC"})
        key = "tracker-" + "s3cr3t" * 4
        st = reducer.replay(journal.read(j.path))
        st["catalog"] = {"tracker": {"kind": "mcp", "server": {"command": "npx", "env": {"T": key}}}}
        with open(j.path, "rb") as f:
            offset = f.seek(0, 2)
        with open(os.path.join(state, journal.STATE_FILE), "w", encoding="utf-8") as f:
            json.dump({"seq": st["seq"], "offset": offset, "first": 1, "state": st}, f)
        got = journal.load_state(state)
        self.assertEqual(got["catalog"]["tracker"]["server"], {"command": "npx"})


@unittest.skipUnless(POSIX, "POSIX file permissions")
class UsersProcesses(HubUmask):
    """What the hub starts on the user's behalf creates files with the user's umask, not the hub's 077."""

    def test_a_stream_session_starts_with_the_users_umask(self):
        from hosts.streamjson import StreamHost
        h = StreamHost(tempfile.mkdtemp(prefix="perms-stream-"), claude=("echo",))
        h._fresh = False   # without calling claude --help
        got = {}

        class P:
            stdout, stdin, pid = iter(()), None, 1

            def poll(self):
                return None

        def popen(cmd, **k):
            got.update(k)
            return P()
        with mock.patch("subprocess.Popen", popen), mock.patch("platforms.current.session_tree", lambda p: None), \
                mock.patch("threading.Thread"):
            h.spawn("maps", "sid", cwd=self.dir)
        self.assertEqual(got.get("umask"), 0o022)

    def test_a_tmux_session_starts_with_the_users_umask(self):
        from hosts.tmux import TmuxHost
        h = TmuxHost(tempfile.mkdtemp(prefix="perms-tmux-"), claude=(sys.executable, "-c",
                                                                      "open('made', 'w').close()"))
        calls = []
        h._tmux = lambda *a, **kw: calls.append(a) or mock.Mock(returncode=1, stdout="")
        h.alive = lambda name: False
        h.spawn("maps", "sid", cwd=self.dir)
        args = next(c for c in calls if c[0] == "new-session")
        cmd = list(args[args.index("--") + 1:])   # claude flags after the python -c program do not interfere
        subprocess.run(cmd, cwd=self.dir, check=True, timeout=30)   # this is how a tmux pane will run it
        self.assertEqual(mode(os.path.join(self.dir, "made")), 0o644)

    def test_a_scheduled_command_runs_with_the_users_umask(self):
        from modules.scheduler.clock import Clock
        state = tempfile.mkdtemp(prefix="perms-clock-")
        made = os.path.join(self.dir, "made")
        c = Clock(state, clock=lambda: 0.0)
        c.start({"id": "j1", "command": f"touch {made}", "run_slot": 0}, 0.0)
        proc = c.running["j1"][0]
        proc.wait(timeout=30)
        self.assertEqual(mode(made), 0o644)

    def test_an_executors_worktree_gets_the_users_modes(self):
        from hosts import worktree
        repo = os.path.join(self.dir, "repo")
        os.makedirs(repo)
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", repo]
        subprocess.run(git + ["init", "-q"], check=True)
        with open(os.path.join(repo, "a.txt"), "w") as f:
            f.write("a")
        subprocess.run(git + ["add", "a.txt"], check=True)
        subprocess.run(git + ["commit", "-qm", "a"], check=True)
        copy = os.path.join(self.dir, "copy")
        worktree._git(repo, "worktree", "add", "-b", "task/x", copy)
        self.assertEqual(mode(os.path.join(copy, "a.txt")), 0o644)

    def test_files_of_hub_shot_out_get_the_users_mode(self):
        made = os.path.join(self.dir, "shot.png")
        existing = os.path.join(self.dir, "old.png")
        with open(existing, "w") as f:
            f.write("x")
        os.chmod(existing, 0o640)
        from modules.proof import shot

        def fake(target, out, *a, **k):
            open(out, "w").close()
            return [out]
        with mock.patch.object(shot, "shot", fake):
            self.assertEqual(shot.main(["https://example.com", "--out", made], self.dir), 0)
            self.assertEqual(shot.main(["https://example.com", "--out", existing], self.dir), 0)
        self.assertEqual(mode(made), 0o644)
        self.assertEqual(mode(existing), 0o640)   # existed before the command: its permissions are left alone


if __name__ == "__main__":
    unittest.main()
