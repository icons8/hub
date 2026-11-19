"""`hub uninstall` of a hub the one-line install put on (its spec §8): besides the service and the commands it takes off
the hub's pointer to its environment, the environments no other hub of this user stands on (read-only folders: the
write right back first), the whole <data> folder when nothing of it is in use, the marked PATH line of the shell's
profiles — unless ~/.local/bin keeps other programs (Claude Code's `claude`) — and linger the install turned on. The
data stays without --purge, and the install line takes such a hub as its own again. The live runs (Docker, winstand,
the Mac bridge) are the task's record."""
import tests  # noqa: F401 - keeps the machine's settings out
import functools
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import platforms
from core import appcode, instance, shortcut, uninstall
from tests import winsafe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOURNAL = '{"seq":1,"type":"core.started"}\n'
MARK = "\n# hub-core: ~/.local/bin in PATH — the commands hub and hub-telegram\nexport PATH=\"$HOME/.local/bin:$PATH\"\n"


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


def read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hub-oluninst-")
        self.addCleanup(self.cleanup)
        self.home = os.path.join(self.tmp, "home")
        self.data = os.path.join(self.home, ".local", "share", "agents-hub")
        self.state = os.path.join(self.tmp, "hub-state")
        self.registry = os.path.join(self.tmp, "registry")
        os.makedirs(self.registry)
        for p, v in ((platforms.current, "oneline_data"), (instance, "siblings")):
            m = mock.patch.object(p, v, (lambda home=None, environ=None: self.data) if v == "oneline_data"
                                  else (lambda default_state=None: []))
            m.start()
            self.addCleanup(m.stop)
        env = mock.patch.dict(os.environ, {"USER": "u"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("XDG_CONFIG_HOME", None)
        os.environ.pop("XDG_DATA_HOME", None)

    def cleanup(self):
        uninstall._writable_dirs(self.tmp)
        platforms.current.rmtree(self.tmp)

    def env(self, name="1-team"):
        """An environment as the install leaves it: read-only files and folders."""
        env = os.path.join(self.data, "shells", name)
        write(os.path.join(env, "runtime", "python", "bin", "python3"), "#!/bin/sh\n", 0o755)
        write(os.path.join(env, "start.py"), "# start\n")
        # deep, as node_modules: rights put back level by level by a retry would not reach the bottom
        write(os.path.join(env, "runtime", "node", "lib", "node_modules", "a", "b", "c", "d", "x.js"), "x\n")
        write(os.path.join(env, ".agents-hub-shell.json"), "{}\n")
        for d, dirs, names in os.walk(env, topdown=False):
            for n in names:
                os.chmod(os.path.join(d, n), stat.S_IMODE(os.stat(os.path.join(d, n)).st_mode) & ~0o222)
            os.chmod(d, 0o555)
        return env

    def hub(self, state, env, default=True):
        """A hub of the one-line install: its journal, its code layer under <state>/code, <state>/app → env, the
        commands of the main hub and the PATH line."""
        write(os.path.join(state, "journal.jsonl"), JOURNAL)
        lay = os.path.join(state, "layers", "9.9.1-team")
        write(os.path.join(lay, "bin", "hub"), "print('hub')\n")
        appcode.make(lay, "9.9.1", "team", "c0ffee", mtime=1700000000)
        appcode.point(os.path.join(state, "code"), os.path.join(state, "layers"), "9.9.1-team")
        winsafe.symlink(self, env, os.path.join(state, uninstall.APP))
        uninstall.write_mark(state, self.data)
        os.makedirs(os.path.join(state, "bin"), exist_ok=True)
        winsafe.symlink(self, os.path.join(state, "app", "runtime", "python", "bin", "python3"),
                        os.path.join(state, "bin", "python3"))
        if default:
            write(os.path.join(self.home, ".config", "hub-core", "state-dir"), state + "\n")
            py, code = os.path.join(state, "app", "runtime", "python", "bin", "python3"), os.path.join(state, "code")
            for cmd in ("hub", "hub-telegram"):
                write(os.path.join(self.home, ".local", "bin", cmd),
                      f'#!/bin/sh\nPATH="{state}/app/runtime/node/bin:$PATH" exec "{py}" "{code}/bin/{cmd}" "$@"\n', 0o755)
        return state

    def plan(self, purge=False, repo=None):
        return uninstall.plan(self.state, os.path.join(self.tmp, "cfg"), home=self.home, repo=repo or self.tmp,
                              purge=purge, registry=self.registry)

    def run_plan(self, steps):
        said = []
        with mock.patch.object(uninstall, "halt"):
            bad = uninstall.execute(steps, run=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""),
                                    say=said.append)
        return bad, said


@unittest.skipIf(platforms.current.APP_OS == "windows", "the POSIX layout: links, folder rights, shell profiles")
class TakesTheInstallOff(Base):
    def test_the_pointer_the_environment_and_the_path_line_go_the_data_stays(self):
        env = self.env()
        self.hub(self.state, env)
        zshrc = os.path.join(self.home, ".zshrc")
        write(zshrc, "alias ll='ls -l'" + MARK + "export EDITOR=vi\n")
        write(os.path.join(self.home, ".bash_profile"), MARK.lstrip("\n"))   # the install made it: nothing else in it
        write(os.path.join(self.data, "downloads", "x.part"), "half")
        write(os.path.join(self.data, "install.log"), "log\n")
        steps = self.plan()
        kinds = [(s["do"], s.get("path")) for s in steps]
        self.assertIn(("pointer", os.path.join(self.state, "app")), kinds)
        self.assertIn(("shell", self.data), kinds)   # nothing of <data> is in use: all of it
        self.assertIn(("profile", zshrc), kinds)
        self.assertLess(kinds.index(("pointer", os.path.join(self.state, "app"))), kinds.index(("shell", self.data)))
        self.assertEqual(kinds[-2:], [("shell", self.data), ("prune", None)])   # the Python this runs on lies there
        self.assertFalse([k for k in kinds if k[0] == "tree"])
        bad, said = self.run_plan(steps)
        self.assertEqual(bad, 0, said)
        self.assertFalse(os.path.lexists(self.data))
        self.assertFalse(os.path.lexists(os.path.join(self.state, "app")))
        self.assertFalse(os.path.lexists(os.path.join(self.state, "bin", "python3")))
        self.assertFalse(os.path.lexists(os.path.join(self.home, ".local", "bin", "hub")))
        self.assertEqual(read(zshrc), "alias ll='ls -l'\nexport EDITOR=vi\n")
        self.assertFalse(os.path.exists(os.path.join(self.home, ".bash_profile")))
        self.assertFalse(os.path.lexists(os.path.join(self.home, ".local")))   # the folders it made, left empty
        # the data and the code layer stay: the install line takes this hub as its own again
        self.assertTrue(os.path.isfile(os.path.join(self.state, "journal.jsonl")))
        self.assertTrue(uninstall.oneline(self.state))
        spec = importlib.util.spec_from_file_location("hub_ol_setup", os.path.join(ROOT, "deploy", "oneline", "setup.py"))
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        job = setup.Install.__new__(setup.Install)
        job.state = self.state
        self.assertEqual(job.kind(), ("ours", None))

    def test_purge_takes_the_state_and_the_environments(self):
        env = self.env()
        self.hub(self.state, env)
        steps = self.plan(purge=True)
        kinds = [(s["do"], s.get("path")) for s in steps]
        self.assertIn(("tree", self.state), kinds)
        self.assertNotIn(("pointer", os.path.join(self.state, "app")), kinds)   # it goes with the state folder
        bad, said = self.run_plan(steps)
        self.assertEqual(bad, 0, said)
        self.assertFalse(os.path.lexists(self.state))
        self.assertFalse(os.path.lexists(self.data))

    def test_the_environment_of_another_hub_stays(self):
        mine, theirs = self.env("2-team"), self.env("1-team")
        self.hub(self.state, mine)
        other = self.hub(os.path.join(self.tmp, "hub-other"), theirs, default=False)
        instance.register({"name": "other", "state": other}, self.registry)
        steps = self.plan()
        shells = [s["path"] for s in steps if s["do"] == "shell"]
        self.assertEqual(shells, [mine])
        self.assertTrue([s for s in steps if s["do"] == "keep" and os.path.join(self.data, "shells") in s["what"]])
        bad, said = self.run_plan(steps)
        self.assertEqual(bad, 0, said)
        self.assertFalse(os.path.lexists(mine))
        self.assertTrue(os.path.isfile(os.path.join(theirs, "start.py")))

    def test_an_environment_the_next_update_of_another_hub_names_stays(self):
        mine, next_one = self.env("1-team"), self.env("2-team")
        self.hub(self.state, mine)
        other = self.hub(os.path.join(self.tmp, "hub-other"), mine, default=False)
        write(os.path.join(other, "layers", "shell-update.json"), json.dumps({"state": "ready", "to": next_one}))
        instance.register({"name": "other", "state": other}, self.registry)
        self.assertEqual([s["path"] for s in self.plan() if s["do"] == "shell"], [])

    def test_claude_in_local_bin_keeps_the_path_line(self):
        self.hub(self.state, self.env())
        write(os.path.join(self.home, ".local", "bin", "claude"), "#!/bin/sh\n", 0o755)
        zshrc = os.path.join(self.home, ".zshrc")
        write(zshrc, MARK)
        steps = self.plan()
        self.assertFalse([s for s in steps if s["do"] == "profile"])
        self.assertTrue([s for s in steps if s["do"] == "keep" and "claude" in s["what"]])
        self.run_plan(steps)
        self.assertEqual(read(zshrc), MARK)

    def test_an_environment_a_running_program_holds_stays(self):
        env = self.env()
        self.hub(self.state, env)
        with mock.patch.object(platforms.current, "processes_in", return_value=[4242]), \
                mock.patch.object(uninstall.time, "sleep"):
            bad, said = self.run_plan(self.plan())
        self.assertEqual(bad, 1)
        self.assertTrue(any("4242" in x for x in said), said)
        self.assertTrue(os.path.isfile(os.path.join(env, "start.py")))

    def test_an_install_at_work_keeps_the_environments(self):
        self.hub(self.state, self.env())
        write(os.path.join(self.data, "install.lock", "owner"), f"pid={os.getpid()} 2026-11-08\n")
        steps = self.plan()
        self.assertFalse([s for s in steps if s["do"] == "shell"])
        self.assertTrue([s for s in steps if s["do"] == "keep"])

    def test_a_folder_of_its_own_in_data_keeps_data(self):
        env = self.env()
        self.hub(self.state, env)
        write(os.path.join(self.data, "notes.txt"), "the person's\n")
        self.assertEqual([s["path"] for s in self.plan() if s["do"] == "shell"], [env])

    def test_the_install_marks_its_hub_when_it_points_it_at_the_environment(self):
        env = self.env()
        os.makedirs(os.path.join(self.state, "layers"))
        spec = importlib.util.spec_from_file_location("hub_ol_setup2", os.path.join(ROOT, "deploy", "oneline", "setup.py"))
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        job = setup.Install.__new__(setup.Install)
        job.state, job.env = self.state, env
        os.chmod(self.data, 0o755)
        write(os.path.join(self.data, uninstall.LATER), "1\n")   # a hub uninstall just before left it for later
        self.assertTrue(job.point_env())
        self.assertFalse(os.path.exists(os.path.join(self.data, uninstall.LATER)))
        self.assertTrue(uninstall.oneline(self.state))
        self.assertEqual(uninstall.data_dirs(self.state, self.home), [self.data])

    def test_a_hub_of_a_packaged_app_is_not_this_installs(self):
        # the .deb and the app shells make <state>/code a layer pointer and <state>/app a pointer too — into their app:
        # no mark, no environment of the line, nothing of the line taken off
        self.env()
        app = os.path.join(self.tmp, "opt", "agents-hub-app")
        os.makedirs(app)
        self.hub(self.state, app)
        os.remove(os.path.join(self.state, uninstall.MARK))
        self.assertFalse(uninstall.oneline(self.state))
        kinds = {s["do"] for s in self.plan()}
        self.assertFalse(kinds & {"shell", "pointer", "profile", "prune"}, kinds)

    def test_a_hub_the_install_put_before_its_mark_is_known_by_its_pointer(self):
        self.hub(self.state, self.env())
        os.remove(os.path.join(self.state, uninstall.MARK))
        self.assertTrue(uninstall.oneline(self.state))

    def test_a_lock_folder_without_its_owner_yet_is_an_install_at_work_an_old_one_is_not(self):
        lock = os.path.join(self.data, "install.lock")
        os.makedirs(lock)
        self.assertTrue(uninstall._install_running(self.data))
        os.utime(lock, (1700000000, 1700000000))
        self.assertFalse(uninstall._install_running(self.data))

    def test_the_commands_through_the_code_pointer_whatever_path_runs_this(self):
        self.hub(self.state, self.env())
        cmds = uninstall.commands(self.state, os.path.join(self.state, "layers", "9.9.1-team"), self.home)
        self.assertEqual(sorted(os.path.basename(c) for c in cmds), ["hub", "hub-telegram"])

    def test_linger_the_install_turned_on_goes_with_the_last_hub(self):
        self.hub(self.state, self.env())
        write(os.path.join(self.data, uninstall.LINGER), '{"user": "u"}\n')
        mine = os.path.join(self.home, ".config", "systemd", "user", "agents-hub.service")
        write(mine, f"[Service]\nExecStart=x/app/start.py\nEnvironment=HUB_STATE_DIR={self.state}\n")
        with mock.patch.object(platforms.current, "APP_OS", "linux"):
            argv = [s["argv"] for s in self.plan() if s["do"] == "run"]
            self.assertIn(["loginctl", "disable-linger", "u"], argv)
            write(os.path.join(os.path.dirname(mine), "agents-hub-two.service"),
                  "[Service]\nExecStart=x/hub-up\nEnvironment=HUB_STATE_DIR=/elsewhere\n")
            argv = [s["argv"] for s in self.plan() if s["do"] == "run"]
            self.assertNotIn(["loginctl", "disable-linger", "u"], argv)

    def test_main_says_what_stays_and_ends_in_its_own_words(self):
        self.hub(self.state, self.env())
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch.dict(os.environ, {"HOME": self.home}), \
                mock.patch.object(uninstall, "plan", lambda *a, **kw: []), mock.patch.object(uninstall, "execute",
                                                                                           return_value=0):
            self.assertEqual(uninstall.main(["--yes"], self.state, os.path.join(self.tmp, "cfg"), None), 0)
        text = out.getvalue()
        self.assertIn("the install line brings the hub back", text)
        self.assertIn("the hub is removed from this computer", text)


@unittest.skipIf(platforms.current.APP_OS == "windows", "the POSIX layout")
class MenuShortcut(Base):
    """The shortcut the install made (core/shortcut.py) goes with the hub — with and without --purge; another hub's,
    and a file of someone else's under its name, stay."""

    def setUp(self):
        super().setUp()
        # a Linux machine's menu, on a Mac too (it has none); the tests of Windows and of a Mac say theirs
        from platforms import posix   # (on Linux the very module patched: its own function taken first)
        for name, value in (("APP_OS", "linux"), ("shortcut_dir", functools.partial(posix.shortcut_dir, platform="linux"))):
            p = mock.patch.object(platforms.current, name, value)
            p.start()
            self.addCleanup(p.stop)

    def apps(self, name):
        return os.path.join(self.home, ".local", "share", "applications", name)

    def put(self, name, hub=""):
        write(self.apps(name), shortcut.desktop(hub, ["env", "x", "open"], "c"))

    def removed(self, steps):
        return [s["path"] for s in steps if s["do"] == "remove" and s["path"].endswith(".desktop")]

    def test_the_desktop_file_goes_the_rest_of_the_menu_stays(self):
        self.hub(self.state, self.env())
        self.put("agents-hub.desktop")
        write(self.apps("other.desktop"), "[Desktop Entry]\nName=Other\n")
        for purge in (False, True):
            self.assertEqual(self.removed(self.plan(purge=purge)), [self.apps("agents-hub.desktop")])
        bad, said = self.run_plan(self.plan())
        self.assertEqual(bad, 0, said)
        self.assertEqual(os.listdir(os.path.dirname(self.apps("x"))), ["other.desktop"])

    def test_a_file_of_someone_else_s_under_its_name_stays(self):
        self.hub(self.state, self.env())
        write(self.apps("agents-hub.desktop"), "[Desktop Entry]\nName=Mine\n")
        self.assertEqual(self.removed(self.plan()), [])

    def test_a_second_hub_takes_its_own_the_main_one_stays(self):
        env = self.env("1-team")
        main = self.hub(self.state, env)
        other = self.hub(os.path.join(self.tmp, "hub-work2"), env, default=False)
        instance.write(other, {"name": "work2", "title": "work2", "config_dir": os.path.join(self.tmp, "cfg2")})
        self.put("agents-hub.desktop")
        self.put("agents-hub-work2.desktop", "work2")
        steps = uninstall.plan(other, os.path.join(self.tmp, "cfg2"), home=self.home, repo=self.tmp,
                               registry=self.registry)
        self.assertEqual(self.removed(steps), [self.apps("agents-hub-work2.desktop")])
        self.assertEqual(self.removed(self.plan()), [self.apps("agents-hub.desktop")])
        self.assertTrue(main)

    def test_a_hub_not_of_this_install_has_no_shortcut_step(self):
        write(os.path.join(self.state, "journal.jsonl"), JOURNAL)
        self.put("agents-hub.desktop")
        self.assertEqual(self.removed(self.plan()), [])

    def test_windows_the_lnk_and_its_icon_go(self):
        self.hub(self.state, self.env())
        menu = os.path.join(self.tmp, "StartMenu")
        hub_cmd = os.path.join(self.state, "bin", "hub.cmd").encode("utf-16-le")
        os.makedirs(menu)
        # its own and the one under the name it had before ("Agents Hub"); the second hub's stay
        for n in ("Hub by Icons8.lnk", "Agents Hub.lnk", "Hub by Icons8 (work2).lnk", "Agents Hub (work2).lnk"):
            with open(os.path.join(menu, n), "wb") as f:
                f.write(b"L\x00\x00\x00" + hub_cmd)
        write(shortcut.icon_path(self.state), "ico")
        with mock.patch.object(platforms.current, "APP_OS", "windows"), \
                mock.patch.object(platforms.current, "shortcut_dir", lambda *a, **k: menu):
            before, _ = uninstall.oneline_steps(self.state, self.home, False)
        self.assertEqual(sorted(s["path"] for s in before if s["do"] == "remove"),
                         sorted([os.path.join(menu, "Hub by Icons8.lnk"), os.path.join(menu, "Agents Hub.lnk"),
                                 shortcut.icon_path(self.state)]))

    def test_windows_a_lnk_of_someone_else_s_stays(self):
        self.hub(self.state, self.env())
        menu = os.path.join(self.tmp, "StartMenu")
        write(os.path.join(menu, "Agents Hub.lnk"), "another program's shortcut" + "." * 60)
        with mock.patch.object(platforms.current, "APP_OS", "windows"), \
                mock.patch.object(platforms.current, "shortcut_dir", lambda *a, **k: menu):
            before, _ = uninstall.oneline_steps(self.state, self.home, False)
        self.assertEqual([s["path"] for s in before if s["do"] == "remove"], [])

    def test_windows_the_lnk_of_another_hub_of_the_install_stays(self):
        self.hub(self.state, self.env())
        menu = os.path.join(self.tmp, "StartMenu")
        os.makedirs(menu)
        with open(os.path.join(menu, "Agents Hub.lnk"), "wb") as f:   # points at another state folder's hub.cmd
            f.write(b"L" * 80 + os.path.join(self.tmp, "else", "bin", "hub.cmd").encode("utf-16-le"))
        with mock.patch.object(platforms.current, "APP_OS", "windows"), \
                mock.patch.object(platforms.current, "shortcut_dir", lambda *a, **k: menu):
            before, _ = uninstall.oneline_steps(self.state, self.home, False)
        self.assertEqual([s["path"] for s in before if s["do"] == "remove" and s["path"].endswith(".lnk")], [])

    def test_windows_a_path_in_capitals_and_in_cyrillic_is_still_this_hub_s(self):
        shell = os.path.join(self.tmp, "Cap" + chr(0x422) + "x" + chr(0x435) + "st", "bin", "hub.cmd")   # Cyrillic letters in it
        menu = os.path.join(self.tmp, "StartMenu")
        os.makedirs(menu)
        with open(os.path.join(menu, "Agents Hub.lnk"), "wb") as f:
            f.write(b"L" * 80 + "".join(c.upper() if c.isascii() else c for c in shell).encode("utf-16-le") + b"\x00\x00")
        self.assertTrue(shortcut.ours(os.path.join(menu, "Agents Hub.lnk"), shell.replace("Cap", "CAP")))
        self.assertFalse(shortcut.ours(os.path.join(menu, "Agents Hub.lnk"), shell.replace("Cap", "Cop")))

    def test_a_desktop_file_that_only_names_the_mark_further_down_is_not_ours(self):
        self.hub(self.state, self.env())
        write(self.apps("agents-hub.desktop"), "[Desktop Entry]\nName=Mine\n" + shortcut.MARK + "\n")
        self.assertEqual(self.removed(self.plan()), [])

    def test_a_file_in_the_folder_xdg_data_home_named_at_the_install_and_one_in_the_default_both_go(self):
        self.hub(self.state, self.env())
        data = os.path.join(self.tmp, "xdg")
        write(os.path.join(data, "applications", "agents-hub.desktop"), shortcut.desktop("", ["x"], "c"))
        self.put("agents-hub.desktop")
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": data}):
            got = self.removed(self.plan())
        self.assertEqual(sorted(got), sorted([os.path.join(data, "applications", "agents-hub.desktop"),
                                              self.apps("agents-hub.desktop")]))

    def test_the_applications_folder_the_install_made_goes_when_empty_and_so_do_its_parents(self):
        self.hub(self.state, self.env())
        self.put("agents-hub.desktop")
        bad, said = self.run_plan(self.plan())
        self.assertEqual(bad, 0, said)
        self.assertFalse(os.path.lexists(os.path.join(self.home, ".local")))

    def test_a_mac_has_none(self):
        self.hub(self.state, self.env())
        with mock.patch.object(platforms.current, "APP_OS", "mac"), \
                mock.patch.object(platforms.current, "shortcut_dir", lambda *a, **k: None):
            before, _ = uninstall.oneline_steps(self.state, self.home, False)
        self.assertEqual([s for s in before if s["do"] == "remove"], [])


class Processes(unittest.TestCase):
    def test_what_of_this_hub_runs_from_the_environment_is_stopped_another_hubs_keeps_it(self):
        tmp = tempfile.mkdtemp(prefix="hub-olproc-")
        self.addCleanup(platforms.current.rmtree, tmp)
        env, state = os.path.join(tmp, "1-team"), os.path.join(tmp, "hub-state")
        os.makedirs(env)
        j = os.path.join   # the command line names the paths as this machine writes them
        lines = {11: f"{j(env, 'python3')} {j(state, 'layers', '9.9.2-team', 'modules', 'setup', 'job.py')} {state} login",
                 12: f"{j(env, 'python3')} {j(os.sep, 'elsewhere', '.hub-two', 'layers', 'x', 'bin', 'hub-up')}"}
        running, stopped = [11, 12], []
        with mock.patch.object(uninstall, "WINDOWS", lambda: False), \
                mock.patch.object(platforms.current, "processes_in", lambda path: list(running)), \
                mock.patch.object(platforms.current, "command_line", lambda pid: lines[pid]), \
                mock.patch.object(platforms.current, "stop_processes",
                                  lambda pids: [stopped.extend(pids), running.remove(11)] and []), \
                mock.patch.object(uninstall.time, "sleep"):
            with self.assertRaises(OSError):
                uninstall.remove_shell(env, state)
        self.assertEqual((stopped, running), ([11], [12]))
        self.assertTrue(os.path.isdir(env))

    @unittest.skipUnless(os.path.isdir("/proc/self") or platforms.current.APP_OS == "mac", "no way to find a program")
    def test_a_program_running_from_a_folder_is_found(self):
        tmp = tempfile.mkdtemp(prefix="hub-olproc-")
        self.addCleanup(platforms.current.rmtree, tmp)
        import shutil
        sleep = shutil.which("sleep")
        if not sleep:
            self.skipTest("no sleep program")
        real, env = os.path.realpath(sleep), os.path.join(tmp, "env")
        os.makedirs(os.path.join(tmp, "other"))
        if os.path.isdir("/proc/self"):   # Linux: a copy of the program in an environment folder
            exe = os.path.join(env, "bin", "sleep")
            os.makedirs(os.path.dirname(exe))
            shutil.copy2(real, exe)
        else:
            # macOS: copy2 may not set a system file's flags on its copy (EPERM), and a plain copy of the system's sleep
            # was not found running (the Mac bridge, 09.11) — the folder of the program itself under the test's name: a
            # program is found by its real path wherever it was started from
            winsafe.symlink(self, os.path.dirname(real), env)
            exe = os.path.join(env, os.path.basename(real))
        # argv[0] stays "sleep": a multi-call program (busybox, coreutils) picks its applet by it
        p = subprocess.Popen(["sleep", "30"], executable=exe)  # portable: Linux or macOS only (skipUnless)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        for _ in range(50):
            if p.pid in platforms.current.processes_in(os.path.join(tmp, "env")):
                break
            import time
            time.sleep(0.1)
        self.assertIn(p.pid, platforms.current.processes_in(os.path.join(tmp, "env")))
        self.assertEqual(platforms.current.processes_in(os.path.join(tmp, "other")), [])


class ProfileLine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hub-olprofile-")
        self.addCleanup(platforms.current.rmtree, self.tmp)

    def test_only_the_two_lines_go_the_rest_byte_for_byte(self):
        rc = os.path.join(self.tmp, ".bashrc")
        text = "# mine\r\nexport A=1" + MARK + "\n\nexport B=2\n"
        with open(rc, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.chmod(rc, 0o640)
        self.assertTrue(uninstall.drop_profile_line(rc))
        self.assertEqual(read(rc), "# mine\r\nexport A=1\n\n\nexport B=2\n")
        if platforms.current.APP_OS != "windows":
            self.assertEqual(stat.S_IMODE(os.stat(rc).st_mode), 0o640)
        self.assertFalse(uninstall.drop_profile_line(rc))

    def test_a_comment_that_names_the_mark_without_the_line_stays(self):
        rc = os.path.join(self.tmp, ".zshrc")
        write(rc, "# hub-core: ~/.local/bin is mine\nexport PATH=/x:$PATH\n")
        self.assertFalse(uninstall.drop_profile_line(rc))

    def test_through_a_link_into_the_file_it_leads_to(self):
        real = os.path.join(self.tmp, "dotfiles", "zshrc")
        write(real, "alias x=y" + MARK)
        link = os.path.join(self.tmp, ".zshrc")
        winsafe.symlink(self, real, link)
        self.assertTrue(uninstall.drop_profile_line(link))
        self.assertTrue(os.path.islink(link))
        self.assertEqual(read(real), "alias x=y\n")


class WindowsRunsOn(Base):
    def test_the_environment_the_command_runs_on_through_the_pointer_the_plan_takes_away(self):
        # sys.executable is <state>\app\…\python.exe: once the pointer step has gone, it no longer resolves into
        # the environment — which program this is was settled before the steps
        env = self.env()
        self.hub(self.state, env)
        exe = os.path.join(self.state, uninstall.APP, "runtime", "python", "bin", "python3")
        later = []
        steps = [{"do": "pointer", "path": os.path.join(self.state, uninstall.APP), "what": "pointer"},
                 {"do": "shell", "path": env, "state": self.state, "what": "env"}]
        with mock.patch.object(uninstall, "WINDOWS", lambda: True), mock.patch.object(uninstall.sys, "executable", exe), \
                mock.patch.object(uninstall.os, "rename", side_effect=PermissionError(32, "in use")), \
                mock.patch.object(uninstall, "defer", lambda paths, pids=None: later.append(paths)):
            bad = uninstall.execute(steps, say=lambda x: None)
        self.assertEqual((bad, later), (0, [[env]]))


class Windows(unittest.TestCase):
    """The environment `hub uninstall` runs on: its python.exe cannot go while it runs — renaming the folder aside fails,
    and a PowerShell apart deletes it once the command has ended."""

    def test_the_running_environment_is_left_for_later(self):
        tmp = tempfile.mkdtemp(prefix="hub-olwin-")
        self.addCleanup(platforms.current.rmtree, tmp)
        env = os.path.join(tmp, "shells", "1-team")
        os.makedirs(os.path.join(env, "runtime"))
        theirs = os.path.join(tmp, "shells", "2-team")
        os.makedirs(theirs)
        exe = os.path.join(env, "runtime", "python.exe")
        with mock.patch.object(uninstall, "WINDOWS", lambda: True), \
                mock.patch.object(uninstall.os, "rename", side_effect=PermissionError(32, "in use")), \
                mock.patch.object(uninstall.sys, "executable", exe):
            self.assertEqual(uninstall.remove_shell(env), env)
            with self.assertRaises(OSError):   # another hub's: it stays, said
                uninstall.remove_shell(theirs)
        later = []
        with mock.patch.object(platforms.current, "remove_later", lambda paths, pids: later.append((paths, pids))), \
                mock.patch.object(platforms.current, "command_line", lambda pid: "powershell.exe"), \
                mock.patch.object(uninstall, "remove_shell", return_value=env):
            bad = uninstall.execute([{"do": "shell", "path": env, "what": "env"}], say=lambda x: None)
        self.assertEqual((bad, later), (0, [([env], [os.getpid()])]))

    def test_purge_leaves_bin_with_the_running_hub_cmd_for_after_the_command(self):
        tmp = tempfile.mkdtemp(prefix="hub-olwin-")
        self.addCleanup(platforms.current.rmtree, tmp)
        state = os.path.join(tmp, "hub-core")
        write(os.path.join(state, "bin", "hub.cmd"), "@echo off\r\n")
        write(os.path.join(state, "journal.jsonl"), JOURNAL)
        write(os.path.join(state, "logs", "hub-up.log"), "x\n")
        os.makedirs(os.path.join(tmp, "env"))
        linked = winsafe.try_symlink(os.path.join(tmp, "env"), os.path.join(state, "app"), target_is_directory=True)
        with mock.patch.object(uninstall, "WINDOWS", lambda: True), \
                mock.patch.object(platforms.current, "uninstall_path", lambda state: []):
            steps = uninstall.plan(state, os.path.join(tmp, "cfg"), home=os.path.join(tmp, "home"), repo=tmp,
                                   purge=True, registry=os.path.join(tmp, "registry"))
        tree = [x for x in steps if x["do"] == "tree"]
        self.assertEqual([(x["path"], x["keep"]) for x in tree], [(state, "bin")])
        later = []
        with mock.patch.object(platforms.current, "remove_later", lambda paths, pids: later.append(paths)):
            bad = uninstall.execute(tree, say=lambda x: None)
        self.assertEqual((bad, later, sorted(os.listdir(state))), (0, [[state]], [uninstall.LATER, "bin"]))
        if linked:   # the link went, what it leads to stayed
            self.assertTrue(os.path.isdir(os.path.join(tmp, "env")))

    def test_a_folder_left_for_later_is_marked_and_the_cmd_of_hub_cmd_waited_for(self):
        tmp = tempfile.mkdtemp(prefix="hub-olwin-")
        self.addCleanup(platforms.current.rmtree, tmp)
        got = []
        for parent, waited in (("cmd.exe /c C:\\Users\\x\\AppData\\Local\\hub-core\\bin\\hub.cmd uninstall", True),
                               ("C:\\Windows\\system32\\cmd.exe", False)):
            with mock.patch.object(platforms.current, "command_line", lambda pid, p=parent: p), \
                    mock.patch.object(platforms.current, "remove_later", lambda paths, pids: got.append(pids)):
                uninstall.defer([tmp])
            self.assertEqual(got.pop(), [os.getpid(), os.getppid()] if waited else [os.getpid()])
        self.assertTrue(os.path.isfile(os.path.join(tmp, uninstall.LATER)))

    def test_the_powershell_apart_waits_for_the_command_then_deletes(self):
        from platforms import windows
        got = {}
        with mock.patch.object(windows, "popen_outside", lambda cmd: bool(got.setdefault("cmd", cmd))):
            windows.remove_later([r"C:\Users\x\AppData\Local\Programs\agents-hub", "C:\\it's"], [4321, 99])
        import base64
        self.assertEqual(got["cmd"][-2], "-EncodedCommand")
        script = base64.b64decode(got["cmd"][-1]).decode("utf-16-le")
        self.assertEqual(got["cmd"][0], "powershell.exe")
        self.assertIn("Wait-Process -Id 4321,99", script)
        self.assertIn(r"'C:\Users\x\AppData\Local\Programs\agents-hub'", script)
        self.assertIn("'C:\\it''s'", script)
        self.assertIn("rd /s /q", script)
        self.assertIn(uninstall.LATER, script)
        self.assertIn("install.lock", script)


if __name__ == "__main__":
    unittest.main()
