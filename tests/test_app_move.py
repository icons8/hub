"""The move of a hub from a git clone into the app (app spec §8, core/appmove.py): which clone a state folder's service
runs, what the move saves and changes, the backup branch of the clone's own edits, and the undo that puts every file
back and the clone's service on again; the fact the core records and the line of "About the hub"."""
import tests  # noqa: F401 — the machine's settings stay out
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import platforms
from tests import winsafe

from core import appmove, appwatch, reducer
from modules.update import takeoff

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def g(cwd, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def clone(root):
    """A git clone of the core in small: bin/hub-up, a core file, a release tag."""
    repo = os.path.join(root, "agents-hub-core")
    os.makedirs(os.path.join(repo, "bin"))
    os.makedirs(os.path.join(repo, "core"))
    for rel, text in (("bin/hub-up", "#!/usr/bin/env python3\n"), ("core/x.py", "X = 1\n"), ("README.md", "r\n")):
        with open(os.path.join(repo, rel), "w") as f:
            f.write(text)
    g(repo, "init", "-q", "-b", "main")
    g(repo, "add", "-A")
    g(repo, "commit", "-q", "-m", "release")
    g(repo, "tag", "v1.0.0")
    return repo


class Ran:
    def __init__(self, rcs=()):
        self.calls = []
        self.rcs = list(rcs)   # (the command's first words, its exit codes one per call; the last one stays)

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        for words, codes in self.rcs:
            if list(cmd[:len(words)]) == list(words):
                rc = codes.pop(0) if len(codes) > 1 else codes[0]
                return subprocess.CompletedProcess(cmd, rc, "", "" if rc == 0 else "Input/output error")
        return subprocess.CompletedProcess(cmd, 0, "", "")


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="appmove-")
        self.addCleanup(platforms.current.rmtree, self.root)
        self.home = os.path.join(self.root, "home")
        self.state = os.path.join(self.home, ".hub-core")
        self.cfg = os.path.join(self.home, ".config", "hub-core")
        for d in (os.path.join(self.state, "hub-home"), os.path.join(self.state, "bin"), self.cfg,
                  os.path.join(self.home, ".local", "bin")):
            os.makedirs(d)
        self.repo = clone(self.root)
        for name in ("CLAUDE.md", "ONBOARDING.md"):
            winsafe.symlink(self, os.path.join(self.repo, "hub-home", name), os.path.join(self.state, "hub-home", name))
        winsafe.symlink(self, sys.executable, os.path.join(self.state, "bin", "python3"))
        for cmd in ("hub", "hub-telegram"):
            with open(os.path.join(self.home, ".local", "bin", cmd), "w") as f:
                f.write(f'#!/bin/sh\nexec "/usr/bin/python3" "{self.repo}/bin/{cmd}" "$@"\n')
        self.conf = os.path.join(self.cfg, "hub.json")
        self.settings = {"hub": {"enabled": True}, "projects": {"core": {"path": self.repo}},
                         "browser": {"mcp": ["/opt/homebrew/bin/npx", "-y", "chrome-devtools-mcp@latest"],
                                     "env": {"PATH": "/opt/homebrew/bin:$PATH"}}}
        with open(self.conf, "w") as f:
            json.dump(self.settings, f)
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": os.path.join(self.home, ".config"),
                                                "HUB_STATE_DIR": self.state})
        self.env.start()
        self.addCleanup(self.env.stop)

    def unit(self):
        path = os.path.join(self.home, ".config", "systemd", "user", "agents-hub.service")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(f"[Service]\nExecStart=/usr/bin/python3 {self.repo}/bin/hub-up\nWorkingDirectory={self.repo}\n"
                    f"Environment=HUB_STATE_DIR={self.state}\n")
        wants = os.path.join(os.path.dirname(path), "default.target.wants")
        os.makedirs(wants)
        winsafe.symlink(self, path, os.path.join(wants, "agents-hub.service"))
        return path

    def plist(self, args=None):
        path = os.path.join(self.home, "Library", "LaunchAgents", "com.hub-core.plist")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        bundle = os.path.join(self.state, "Agents Hub.app")
        os.makedirs(os.path.join(bundle, "Contents", "MacOS"))
        with open(path, "wb") as f:
            plistlib.dump({"Label": "com.hub-core", "ProgramArguments": args or [
                os.path.join(bundle, "Contents", "MacOS", "Agents Hub"), "/opt/homebrew/bin/python3.12",   # portable: a path in a plist, only read as text
                os.path.join(self.repo, "bin", "hub-up")], "WorkingDirectory": self.repo,
                "EnvironmentVariables": {"HUB_STATE_DIR": self.state}}, f)
        return path

    def deb(self):
        root = os.path.join(self.root, "opt-agents-hub")
        os.makedirs(os.path.join(root, "bin"), exist_ok=True)
        return root

    def snapshot(self):
        """Every file under home as (kind, content): what an undo must give back exactly."""
        out = {}
        for top, dirs, files in os.walk(self.home):
            dirs[:] = [d for d in dirs if d not in (appmove.DIR,) and not (top == self.state and d == "layers")]
            # hub.json's lock of catalog.edit_settings; hub.json and its copies — compared by their settings (config())
            files = [n for n in files if not n.endswith(".lock") and not n.startswith("hub.json")]
            for name in dirs + files:
                p = os.path.join(top, name)
                rel = os.path.relpath(p, self.home)
                if os.path.islink(p):
                    out[rel] = ("link", os.readlink(p))
                elif os.path.isfile(p):
                    with open(p, "rb") as f:
                        out[rel] = ("file", f.read())
                else:
                    out[rel] = ("dir", None)
        return out


class Find(Base):
    def test_a_service_with_a_settings_folder_of_its_own_is_not_offered(self):
        path = self.unit()
        with open(path, "a") as f:
            f.write(f"Environment=HUB_CONFIG_DIR={self.home}/hubcfg\n")
        self.assertIsNone(appmove.find(self.state, "linux", self.home))

    def test_settings_moved_by_any_way_of_the_unit_are_not_offered(self):
        for line in ('Environment="HUB_CONFIG_DIR=/home/x/cfg"', "Environment=A=1 HUB_CONFIG=/home/x/hub.json",
                     "Environment=XDG_CONFIG_HOME=/home/x/xdg", "EnvironmentFile=/home/x/hub.env"):
            path = self.unit()
            with open(path, "a") as f:
                f.write(line + "\n")
            self.assertIsNone(appmove.find(self.state, "linux", self.home), line)
            platforms.current.rmtree(os.path.dirname(path))

    def test_a_clone_inside_the_state_folder_is_not_offered(self):
        inside = os.path.join(self.state, "elsewhere")
        shutil.move(self.repo, inside)
        self.repo = inside
        self.unit()
        self.assertIsNone(appmove.find(self.state, "linux", self.home))

    def test_a_linux_unit_of_this_state_folder_names_the_clone(self):
        self.unit()
        got = appmove.find(self.state, "linux", self.home)
        self.assertEqual(got["repo"], self.repo)
        self.assertEqual(got["unit"]["kind"], "systemd")
        self.assertEqual(got["version"], "v1.0.0")

    def test_a_mac_agent_names_the_clone_and_the_apps_own_does_not(self):
        self.plist()
        self.assertEqual(appmove.find(self.state, "mac", self.home)["repo"], self.repo)
        os.unlink(os.path.join(self.home, "Library", "LaunchAgents", "com.hub-core.plist"))
        platforms.current.rmtree(os.path.join(self.state, "Agents Hub.app"))
        self.plist(["/Applications/Agents Hub.app/Contents/MacOS/Agents Hub", "--serve"])
        self.assertIsNone(appmove.find(self.state, "mac", self.home))

    def test_a_windows_launcher_of_the_install_script_names_the_clone_and_the_apps_does_not(self):
        cmd = os.path.join(self.state, "hub-up.cmd")
        with open(cmd, "w", newline="") as f:
            f.write(f'@echo off\r\ncd /d "{self.repo}"\r\n"C:\\py\\python.exe" "{self.repo}/bin/hub-up" --scheduled\r\n')
        self.assertEqual(appmove.find(self.state, "windows", self.home)["repo"], self.repo)
        with open(cmd, "w", newline="") as f:
            f.write(f'@echo off\r\n"C:\\a\\python.exe" "C:\\a\\app.py" launch\r\ncd /d "{self.repo}"\r\n'
                    f'"C:\\a\\python.exe" "{self.repo}/bin/hub-up" --scheduled\r\n')
        self.assertIsNone(appmove.find(self.state, "windows", self.home))

    def test_a_service_whose_code_is_not_a_git_clone_is_not_offered(self):
        self.unit()
        platforms.current.rmtree(os.path.join(self.repo, ".git"))
        self.assertIsNone(appmove.find(self.state, "linux", self.home))

    def test_another_state_folders_service_is_not_this_hubs(self):
        self.unit()
        other = os.path.join(self.home, ".hub-personal")
        os.makedirs(other)
        self.assertIsNone(appmove.find(other, "linux", self.home))


class EditsCopy(Base):
    def test_the_backup_branch_names_the_edits_and_the_clone_stays_as_it_was(self):
        with open(os.path.join(self.repo, "core", "x.py"), "w") as f:
            f.write("X = 2   # mine\n")
        with open(os.path.join(self.repo, "core", "new.py"), "w") as f:
            f.write("N = 1\n")
        head, status = g(self.repo, "rev-parse", "HEAD"), g(self.repo, "status", "--porcelain")
        got = appmove.edits_copy(self.repo, now=1_800_000_000)
        self.assertEqual(got["count"], 2)
        self.assertTrue(got["branch"].startswith(takeoff.PREFIX))
        self.assertEqual(g(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(g(self.repo, "status", "--porcelain"), status)
        with open(os.path.join(self.repo, "core", "x.py")) as f:
            self.assertEqual(f.read(), "X = 2   # mine\n")
        found = takeoff.latest(self.repo)   # "Put back" of the update finds it, as a take-off's
        self.assertEqual(found["name"], got["branch"])
        self.assertEqual(g(self.repo, "show", f"{found['ret']}:core/x.py"), "X = 2   # mine")

    def test_no_edits_no_branch(self):
        self.assertIsNone(appmove.edits_copy(self.repo))
        self.assertEqual(g(self.repo, "branch", "--list", "backup/*"), "")


class Config(Base):
    def test_the_apps_programs_replace_the_clone_installers_defaults_only(self):
        own = os.path.join(self.root, "my-ffmpeg")
        with open(own, "w") as f:
            f.write("")
        with open(self.conf, "w") as f:
            json.dump({"browser": {"mcp": ["/home/x/gone-mcp"]}, "voice": {"ffmpeg": own}}, f)
        programs = {"mcp": ["/opt/a/node", "/opt/a/mcp.js"], "ffmpeg": "/opt/a/ffmpeg"}
        # a program by its full path that is gone is replaced; one that is there stays
        self.assertEqual(appmove.rewrite_config(self.conf, self.repo, programs), ["browser.mcp"])
        with open(self.conf) as f:
            now = json.load(f)
        self.assertEqual(now["browser"]["mcp"], programs["mcp"])
        self.assertEqual(now["voice"]["ffmpeg"], own)

    def test_the_npx_default_of_the_mac_installer_and_an_unset_ffmpeg(self):
        programs = {"mcp": ["/s/app/runtime/node/bin/node", "/s/app/m.js"], "ffmpeg": "/s/app/ffmpeg"}
        self.assertEqual(sorted(appmove.rewrite_config(self.conf, self.repo, programs)), ["browser.mcp", "voice.ffmpeg"])
        with open(self.conf) as f:
            now = json.load(f)
        self.assertEqual(now["projects"], self.settings["projects"])   # a project in the clone is the owner's
        self.assertEqual(now["browser"]["env"], self.settings["browser"]["env"])

    def test_an_owners_own_runner_stays(self):
        mine = os.path.join(self.root, "mine", "python3")   # not sys.executable: a Mac's is Homebrew's, which the move replaces
        os.makedirs(os.path.dirname(mine))
        with open(mine, "w") as f:
            f.write("")
        with open(self.conf, "w") as f:
            json.dump({"browser": {"mcp": [mine, "-m", "something_else"]}}, f)
        self.assertEqual(appmove.rewrite_config(self.conf, self.repo, {"mcp": ["/a/node", "/a/m.js"]}), [])

    def test_the_apps_programs_by_its_paths_json(self):
        app = os.path.join(self.root, "Resources", "hub")
        os.makedirs(app)
        with open(os.path.join(app, "paths.json"), "w") as f:
            json.dump({"node": "runtime/node/bin/node", "mcp": "runtime/mcp/m.js", "ffmpeg": "runtime/ffmpeg/bin/ffmpeg"}, f)
        mac = appmove.app_programs(app, self.state, "mac")
        self.assertEqual(mac["mcp"][0], os.path.join(self.state, "app", "runtime/node/bin/node"))   # through <state>/app
        deb = appmove.app_programs(self.deb(), self.state, "linux")
        self.assertTrue(deb["ffmpeg"].endswith(os.path.join("bin", "ffmpeg")))


class SaveRestore(Base):
    def test_what_was_saved_before_a_failure_is_known(self):
        d, bad = os.path.join(self.root, "bundle"), os.path.join(self.root, "f")
        os.makedirs(d)
        with open(bad, "w") as h:
            h.write("x")
        out, into = [], os.path.join(self.root, "saved")
        real = shutil.copy2
        with mock.patch.object(appmove.shutil, "copy2", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                appmove.save([d, bad], into, out)
        self.assertEqual([i["kind"] for i in out], ["dir"])   # the folder moved aside is in the list
        self.assertEqual(appmove.restore(out, into), [])
        self.assertTrue(os.path.isdir(d))
        self.assertIs(shutil.copy2, real)

    def test_files_links_folders_and_absent_paths_come_back_exactly(self):
        f, link, d, none = (os.path.join(self.root, n) for n in ("f.txt", "l", "d", "none"))
        with open(f, "w") as h:
            h.write("one")
        winsafe.symlink(self, "/somewhere", link)
        os.makedirs(os.path.join(d, "in"))
        into = os.path.join(self.root, "saved")
        saved = appmove.save([f, link, d, none], into)
        self.assertFalse(os.path.exists(d))   # a folder is moved aside (the app's install deletes the bundle)
        with open(f, "w") as h:
            h.write("two")
        os.unlink(link)
        winsafe.symlink(self, "/elsewhere", link)
        with open(none, "w") as h:
            h.write("the app's")
        self.assertEqual(appmove.restore(saved, into), [])
        with open(f) as h:
            self.assertEqual(h.read(), "one")
        self.assertEqual(os.readlink(link), "/somewhere")
        self.assertTrue(os.path.isdir(os.path.join(d, "in")))
        self.assertFalse(os.path.lexists(none))
        self.assertEqual(appmove.restore(saved, into), [])   # again: nothing to do, nothing lost


class Pointer(Base):
    """<state>/app, the one-line install's pointer to its environment: a junction on Windows, which shutil.rmtree
    refuses (and platforms' rmtree swallowed) — undo left it, and the next line took the hub back on the clone for its
    own."""

    def test_undo_takes_the_pointer_away_without_what_it_leads_to(self):
        env = os.path.join(self.root, "env")
        os.makedirs(env)
        with open(os.path.join(env, "keep.txt"), "w") as f:
            f.write("the environment\n")
        app = os.path.join(self.state, "app")
        saved = [{"path": app, "kind": "none"}]
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(env, app)
            self.assertEqual(appmove.restore(saved, os.path.join(self.root, "saved")), [])
        else:   # a junction as Windows sees it: not a link, a folder whose contents are the target's
            os.makedirs(app)
            real = os.rmdir
            with mock.patch("core.appcode.is_pointer", side_effect=lambda p: p == app), \
                    mock.patch.object(appmove.os, "rmdir", side_effect=real) as rmdir, \
                    mock.patch.object(platforms.current, "rmtree", side_effect=AssertionError("rmtree of a pointer")):
                self.assertEqual(appmove.restore(saved, os.path.join(self.root, "saved")), [])
            rmdir.assert_called_once_with(app)
        self.assertFalse(os.path.lexists(app))
        self.assertTrue(os.path.isfile(os.path.join(env, "keep.txt")))


class Move(Base):
    def move(self, osname, app, **kw):
        ran = kw.pop("run", None) or Ran()
        kw.setdefault("wait", lambda state, ceiling, say: [])
        kw.setdefault("stopper", lambda state, unit, osname, run: True)
        rec = appmove.move(self.state, self.cfg, self.repo, app, osname, run=ran, say=lambda line: None,
                           home_dir=self.home, now=lambda: 1_800_000_000, **kw)
        return rec, ran

    def config(self):
        with open(self.conf) as f:
            return json.load(f)

    def test_an_edit_made_on_the_app_survives_the_undo_and_the_moves_keys_go_back(self):
        self.unit()
        self.move("linux", self.deb())
        appmove.done(self.state, now=lambda: 1_800_000_100)
        from core import catalog
        with catalog.edit_settings(self.conf) as cfg:   # a week on the app: a project, the owner's own ffmpeg
            cfg["projects"]["maps"] = {"path": "/home/x/maps"}
            cfg["voice"]["ffmpeg"] = "/home/x/ffmpeg"
        appmove.undo(self.state, run=Ran(), now=lambda: 1_800_000_200, stopper=lambda *a: True)
        now = self.config()
        self.assertEqual(now["projects"]["maps"], {"path": "/home/x/maps"})
        self.assertEqual(now["voice"]["ffmpeg"], "/home/x/ffmpeg")   # changed since the move: the owner's
        self.assertEqual(now["browser"]["mcp"], self.settings["browser"]["mcp"])   # the move's key: back
        self.assertTrue(os.path.isfile(self.conf + ".bak-undo-1800000200"))

    def test_a_hub_json_the_apps_install_wrote_goes_aside_on_undo(self):
        os.unlink(self.conf)
        self.unit()
        self.move("linux", self.deb())
        with open(self.conf, "w") as f:   # first_run.setup of the package: a fresh hub.json of the app
            json.dump({"browser": {"mcp": ["/opt/agents-hub/bin/chrome-devtools-mcp"]}}, f)
        appmove.undo(self.state, failed=True, run=Ran(), now=lambda: 1_800_000_300, stopper=lambda *a: True)
        self.assertFalse(os.path.exists(self.conf))
        self.assertTrue(os.path.isfile(self.conf + ".bak-undo-1800000300"))

    def test_a_hub_up_its_service_does_not_hold_gets_the_soft_stop(self):
        asked = []
        looks = iter([False, True])
        with mock.patch("core.restart.running", return_value={"pid": 1}), \
                mock.patch("core.restart.ask_stop", side_effect=asked.append):
            self.assertTrue(appmove.stop(self.state, {"kind": "systemd", "id": "agents-hub.service"}, "linux", run=Ran(),
                                         gone=lambda state, seconds=0: next(looks)))
        self.assertEqual(asked, [self.state])

    def test_a_second_attempt_keeps_the_copies_of_the_first(self):
        self.unit()
        with mock.patch.object(appmove, "edits_copy", side_effect=OSError("disk full")):
            with self.assertRaises(appmove.Refused):
                self.move("linux", self.deb())
        first = appmove.record(self.state)["into"]
        rec = appmove.move(self.state, self.cfg, self.repo, self.deb(), "linux", run=Ran(), say=lambda line: None,
                           home_dir=self.home, now=lambda: 1_800_000_500, wait=lambda *a, **k: [],
                           stopper=lambda *a: True)
        self.assertNotEqual(rec["into"], first)
        self.assertTrue(os.path.isdir(os.path.join(self.state, appmove.DIR, first)))

    def test_undo_starts_the_clones_own_unit(self):
        path = self.unit()
        work = os.path.join(os.path.dirname(path), "hub-work.service")
        os.rename(path, work)
        os.unlink(os.path.join(os.path.dirname(path), "default.target.wants", "agents-hub.service"))
        self.move("linux", self.deb())
        ran = Ran()
        appmove.undo(self.state, failed=True, run=ran, stopper=lambda *a: True)
        self.assertIn(["systemctl", "--user", "restart", "hub-work.service"], ran.calls)
        self.assertNotIn(["systemctl", "--user", "restart", "agents-hub.service"], ran.calls)
        self.assertTrue(os.path.isfile(work))

    def test_linux_move_then_undo_gives_every_file_back(self):
        self.unit()
        before = self.snapshot()
        rec, ran = self.move("linux", self.deb())
        self.assertEqual(rec["status"], "moving")
        self.assertEqual(appmove.record(self.state)["status"], "moving")
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "systemd", "user", "agents-hub.service")))
        self.assertIn(["systemctl", "--user", "disable", "agents-hub.service"], ran.calls)
        with open(os.path.join(self.home, ".local", "bin", "hub")) as f:
            self.assertIn(os.path.join(self.state, "code", "bin", "hub"), f.read())   # the terminal's hub is the app's
        with open(self.conf) as f:
            self.assertEqual(json.load(f)["browser"]["mcp"], [os.path.join(self.deb(), "bin", "chrome-devtools-mcp")])
        self.assertTrue(os.path.isfile(os.path.join(self.cfg, appmove.CONFIG_BACKUP)))
        # the shell's install: the app's hub-home links, its app.json
        os.unlink(os.path.join(self.state, "hub-home", "CLAUDE.md"))
        winsafe.symlink(self, os.path.join(self.state, "code", "hub-home", "CLAUDE.md"), os.path.join(self.state, "hub-home", "CLAUDE.md"))
        appmove.done(self.state, now=lambda: 1_800_000_100)
        self.assertEqual(appmove.record(self.state)["status"], "moved")
        ran2 = Ran()
        rec = appmove.undo(self.state, run=ran2, now=lambda: 1_800_000_200, stopper=lambda *a: True)
        self.assertEqual(rec["status"], "undone")
        self.assertEqual(rec["left"], [])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.config(), self.settings)
        self.assertIn(["systemctl", "--user", "restart", "agents-hub.service"], ran2.calls)
        self.assertIn(["systemctl", "--user", "enable", "agents-hub.service"], ran2.calls)

    def test_mac_move_keeps_the_clones_bundle_and_undo_boots_its_agent_again(self):
        plist = self.plist()
        before = self.snapshot()
        rec, _ = self.move("mac", os.path.join(self.root, "Resources", "hub"))
        self.assertFalse(os.path.exists(os.path.join(self.state, "Agents Hub.app")))   # aside: install.sh deletes it
        # the app's install.sh: its own agent and commands
        with open(plist, "wb") as f:
            plistlib.dump({"Label": "com.hub-core", "ProgramArguments": ["/Applications/Agents Hub.app/x", "--serve"]}, f)
        with open(os.path.join(self.home, ".local", "bin", "hub"), "w") as f:
            f.write("the app's wrapper\n")
        ran = Ran()
        stopped = []
        appmove.undo(self.state, failed=True, run=ran, stopper=lambda state, unit, osname, run: stopped.append(unit) or True)
        self.assertEqual(stopped, [{"kind": "launchd", "id": "com.hub-core"}])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.config(), self.settings)
        self.assertIn(["launchctl", "bootstrap", f"gui/{os.getuid()}", plist], ran.calls)
        self.assertEqual(appmove.record(self.state)["status"], "failed")

    def test_busy_sessions_past_the_ceiling_change_nothing(self):
        self.unit()
        before = self.snapshot()
        with self.assertRaises(appmove.Refused):
            self.move("linux", self.deb(), wait=lambda state, ceiling, say: ["maps: a turn is running"])
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(appmove.record(self.state))

    def test_a_hub_that_does_not_stop_changes_nothing_and_runs_on(self):
        self.unit()
        before = self.snapshot()
        with self.assertRaises(appmove.Refused):
            self.move("linux", self.deb(), stopper=lambda *a: False)
        self.assertEqual(self.snapshot(), before)

    def test_a_failure_midway_puts_everything_back_and_starts_the_clone(self):
        self.unit()
        before = self.snapshot()
        with mock.patch.object(appmove, "rewrite_config", side_effect=OSError("disk full")):
            with self.assertRaises(appmove.Refused) as cm:
                self.move("linux", self.deb())
        self.assertIn("disk full", str(cm.exception))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.config(), self.settings)
        self.assertEqual(appmove.record(self.state)["status"], "failed")

    def test_a_second_move_and_an_undo_without_a_move_are_refused(self):
        with self.assertRaises(appmove.Refused):
            appmove.undo(self.state, run=Ran(), stopper=lambda *a: True)
        self.unit()
        self.move("linux", self.deb())
        with self.assertRaises(appmove.Refused):
            self.move("linux", self.deb())

    def test_another_folder_than_the_running_clone_is_refused(self):
        self.unit()
        with self.assertRaises(appmove.Refused):
            appmove.move(self.state, self.cfg, os.path.join(self.root, "elsewhere"), self.deb(), "linux", run=Ran(),
                         say=lambda line: None, home_dir=self.home, wait=lambda *a, **k: [], stopper=lambda *a: True)
        self.assertIsNone(appmove.record(self.state))

    def test_undo_after_the_days_is_refused_the_installers_own_is_not(self):
        self.unit()
        self.move("linux", self.deb())
        appmove.done(self.state, now=lambda: 1_800_000_000)
        late = lambda: 1_800_000_000 + (appmove.UNDO_DAYS + 1) * 86400   # noqa: E731
        with self.assertRaises(appmove.Refused):
            appmove.undo(self.state, run=Ran(), now=late, stopper=lambda *a: True)
        self.assertEqual(appmove.undo(self.state, failed=True, run=Ran(), now=late, stopper=lambda *a: True)["status"],
                         "failed")

    def test_a_return_whose_clone_does_not_start_says_so_and_can_be_done_again(self):
        # the clone's agent refused by launchd: the hub runs nowhere — never "back on the clone", and `hub app undo`
        # (what the words tell the person) still has the move to undo
        plist = self.plist()
        self.move("mac", os.path.join(self.root, "Resources", "hub"))
        refused = Ran([(["launchctl", "bootstrap"], [5]), (["launchctl", "print"], [113])])
        rec = appmove.undo(self.state, failed=True, run=refused, stopper=lambda *a: True)
        self.assertTrue(any("launchctl bootstrap" in line for line in rec["left"]), rec["left"])
        self.assertEqual(appmove.record(self.state)["status"], "moving")
        ran = Ran()
        rec = appmove.undo(self.state, run=ran, stopper=lambda *a: True)
        self.assertEqual((rec["status"], rec["left"]), ("undone", []))
        self.assertIn(["launchctl", "bootstrap", f"gui/{os.getuid()}", plist], ran.calls)

    def test_a_bootstrap_while_launchd_still_holds_the_apps_agent_is_tried_again(self):
        self.plist()
        self.move("mac", os.path.join(self.root, "Resources", "hub"))
        ran = Ran([(["launchctl", "bootstrap"], [5, 0]), (["launchctl", "print"], [0, 113])])
        with mock.patch.object(appmove.time, "sleep"):
            rec = appmove.undo(self.state, failed=True, run=ran, stopper=lambda *a: True)
        self.assertEqual((rec["status"], rec["left"]), ("failed", []))
        self.assertEqual(len([c for c in ran.calls if c[:2] == ["launchctl", "bootstrap"]]), 2)

    def test_a_clone_unit_that_does_not_start_or_a_task_that_does_not_run_is_said(self):
        self.unit()
        self.move("linux", self.deb())
        rec = appmove.undo(self.state, failed=True, run=Ran([(["systemctl", "--user", "restart"], [1])]),
                           stopper=lambda *a: True)
        self.assertTrue(any("systemctl" in line for line in rec["left"]), rec["left"])
        self.assertEqual(appmove.record(self.state)["status"], "moving")
        with open(os.path.join(self.state, "hub-up.cmd"), "w", newline="") as f:
            f.write(f'@echo off\r\n"C:\\py\\python.exe" "{self.repo}\\bin\\hub-up" --scheduled\r\n')
        with mock.patch.object(appmove, "record", return_value={**appmove.record(self.state), "os": "windows",
                                                                "unit": {"kind": "task", "id": "Agents Hub"}}):
            rec = appmove.undo(self.state, failed=True, run=Ran([(["schtasks", "/Run"], [1])]),
                               stopper=lambda *a: True)
        self.assertTrue(any("schtasks" in line for line in rec["left"]), rec["left"])

    def test_a_file_that_cannot_go_back_is_said_but_does_not_hold_the_move_open(self):
        # the clone's service started: undo again cannot bring the file, the move is over (its leftovers named)
        self.unit()
        self.move("linux", self.deb())
        rec = appmove.record(self.state)
        rec["saved"].append({"path": os.path.join(self.state, "gone.txt"), "kind": "file", "copy": "99-gone.txt"})
        appmove._write(os.path.join(appmove.folder(self.state), appmove.RECORD), rec)
        rec = appmove.undo(self.state, failed=True, run=Ran(), stopper=lambda *a: True)
        self.assertEqual(rec["status"], "failed")
        self.assertTrue(any("gone.txt" in line for line in rec["left"]), rec["left"])

    def test_a_done_move_whose_return_does_not_start_the_clone_can_be_undone_after_the_days(self):
        self.plist()
        self.move("mac", os.path.join(self.root, "Resources", "hub"))
        appmove.done(self.state, now=lambda: 1_800_000_000)
        refused = Ran([(["launchctl", "bootstrap"], [5]), (["launchctl", "print"], [113])])
        rec = appmove.undo(self.state, run=refused, now=lambda: 1_800_000_100, stopper=lambda *a: True)
        self.assertEqual(rec["status"], "moving")
        self.assertNotIn("undone", rec)
        late = lambda: 1_800_000_000 + (appmove.UNDO_DAYS + 1) * 86400   # noqa: E731
        self.assertEqual(appmove.undo(self.state, run=Ran(), now=late, stopper=lambda *a: True)["status"], "undone")

    def test_a_return_that_did_not_finish_is_never_marked_a_done_move(self):
        self.plist()
        self.move("mac", os.path.join(self.root, "Resources", "hub"))
        refused = Ran([(["launchctl", "bootstrap"], [5]), (["launchctl", "print"], [113])])
        appmove.undo(self.state, failed=True, run=refused, stopper=lambda *a: True)
        self.assertIsNone(appmove.done(self.state))   # the app's hub brought up by another way: still a return
        self.assertEqual(appmove.record(self.state)["status"], "moving")

    def test_ctrl_c_while_the_clones_hub_stops_and_its_service_does_not_start_leaves_it_to_undo(self):
        self.unit()

        def stopping(*a):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.move("linux", self.deb(), run=Ran([(["systemctl", "--user", "restart"], [1])]), stopper=stopping)
        rec = appmove.record(self.state)
        self.assertEqual((rec["status"], rec["returning"]), ("moving", True))
        self.assertEqual(appmove.undo(self.state, failed=True, run=Ran(), stopper=lambda *a: True)["left"], [])

    def test_a_hub_that_did_not_stop_and_still_runs_is_not_a_return_to_finish(self):
        self.unit()
        with mock.patch("core.restart.running", return_value={"pid": 1}):
            with self.assertRaises(appmove.Refused) as cm:
                self.move("linux", self.deb(), stopper=lambda *a: False,
                          run=Ran([(["systemctl", "--user", "restart"], [1])]))
        self.assertNotIsInstance(cm.exception, appmove.NotBack)
        self.assertIsNone(appmove.record(self.state))

    def test_a_failure_midway_whose_files_did_not_all_go_back_names_them(self):
        self.unit()
        real = appmove.restore
        with mock.patch.object(appmove, "rewrite_config", side_effect=OSError("disk full")), \
                mock.patch.object(appmove, "restore", side_effect=lambda saved, into: real(saved, into) + ["/x: gone"]):
            with self.assertRaises(appmove.Refused) as cm:
                self.move("linux", self.deb())
        self.assertIn("/x: gone", str(cm.exception))
        self.assertEqual(appmove.record(self.state)["status"], "failed")

    def test_ctrl_c_while_the_clones_hub_stops_starts_it_again(self):
        self.unit()
        ran = Ran()

        def stopping(*a):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.move("linux", self.deb(), run=ran, stopper=stopping)
        self.assertIn(["systemctl", "--user", "restart", "agents-hub.service"], ran.calls)

    def test_a_hub_that_does_not_stop_and_whose_service_does_not_start_again_leaves_a_move_to_undo(self):
        self.unit()
        with self.assertRaises(appmove.NotBack):
            self.move("linux", self.deb(), stopper=lambda *a: False,
                      run=Ran([(["systemctl", "--user", "restart"], [1])]))
        self.assertEqual(appmove.record(self.state)["status"], "moving")
        self.assertEqual(appmove.undo(self.state, failed=True, run=Ran(), stopper=lambda *a: True)["left"], [])

    def test_a_failure_midway_whose_clone_does_not_start_again_leaves_the_move_to_undo(self):
        self.unit()
        with mock.patch.object(appmove, "rewrite_config", side_effect=OSError("disk full")):
            with self.assertRaises(appmove.NotBack) as cm:
                self.move("linux", self.deb(), run=Ran([(["systemctl", "--user", "restart"], [1])]))
        self.assertIn("app undo", str(cm.exception))
        self.assertEqual(appmove.record(self.state)["status"], "moving")
        self.assertEqual(appmove.undo(self.state, failed=True, run=Ran(), stopper=lambda *a: True)["left"], [])

    def test_the_main_hub_with_its_clone_in_the_code_folder_moves_and_comes_back_too(self):
        code = os.path.join(self.state, "code")
        shutil.move(self.repo, code)
        self.repo = code
        self.unit()
        self.assertEqual(appmove.find(self.state, "linux", self.home)["repo"], code)
        before = self.snapshot()
        rec = appmove.move(self.state, self.cfg, code, self.deb(), "linux", run=Ran(), say=lambda line: None,
                           home_dir=self.home, now=lambda: 1_800_000_000, wait=lambda state, ceiling, say: [],
                           stopper=lambda state, unit, osname, run: True)
        self.assertFalse(os.path.lexists(code))
        self.assertTrue(os.path.isfile(os.path.join(rec["aside"]["to"], "bin", "hub-up")))
        self.assertEqual(os.path.basename(rec["aside"]["to"]), "hub-core-clone")
        rec = appmove.undo(self.state, failed=True, run=Ran(), stopper=lambda *a: True)
        self.assertEqual(rec["left"], [])
        after = self.snapshot()
        self.assertEqual({k: v for k, v in after.items() if not k.endswith(os.path.join(".git", "index"))},
                         {k: v for k, v in before.items() if not k.endswith(os.path.join(".git", "index"))})


class Relink(Base):
    """What names the clone's programs beyond browser.mcp and voice.ffmpeg: any path inside the clone in hub.json whose file
    the app's code layer has, and the hub session's rules links where its folder is not <state>/hub-home."""
    move, config = Move.move, Move.config

    def settings_with_servers(self):
        bin_ = os.path.join(self.repo, "bin")
        servers = {"browser": {"command": os.path.join(bin_, "hub-browser"), "args": ["mcp", "main"]},
                   "outside": {"command": "/usr/local/bin/tool", "args": [os.path.join(bin_, "hub-browser")]},
                   "gone": {"command": os.path.join(bin_, "not-in-the-layer")}}
        browsers = {"main": {"server": {"command": "/usr/bin/python3",
                                        "args": [os.path.join(bin_, "hub-browser"), "mcp", "main"]}}}
        self.settings.update(mcp=servers, browsers=browsers)
        with open(self.conf, "w") as f:
            json.dump(self.settings, f)
        return servers

    def test_paths_to_the_clones_programs_go_to_the_layers(self):
        self.unit()
        self.settings_with_servers()
        before = self.snapshot()
        rec, _ = self.move("linux", self.deb())
        now = self.config()
        want = os.path.join(self.state, "code", "bin", "hub-browser")
        self.assertEqual(now["mcp"]["browser"], {"command": want, "args": ["mcp", "main"]})
        self.assertEqual(now["mcp"]["outside"]["args"], [want])          # inside a list, too
        self.assertEqual(now["mcp"]["outside"]["command"], "/usr/local/bin/tool")   # outside the clone: not touched
        self.assertEqual(now["mcp"]["gone"], self.settings["mcp"]["gone"])   # a file the layer does not have: stays
        self.assertEqual(now["browsers"]["main"]["server"]["args"], [want, "mcp", "main"])
        self.assertEqual(now["projects"], self.settings["projects"])
        self.assertIn("mcp.browser.command", rec["config"])
        appmove.undo(self.state, failed=True, run=Ran(), stopper=lambda *a: True)
        self.assertEqual(self.config(), self.settings)
        self.assertEqual(self.snapshot(), before)

    def test_an_edit_made_on_the_app_to_such_a_path_stays_on_undo(self):
        self.unit()
        self.settings_with_servers()
        self.move("linux", self.deb())
        from core import catalog
        with catalog.edit_settings(self.conf) as cfg:
            cfg["mcp"]["browser"]["command"] = "/home/x/my-browser"
        appmove.undo(self.state, failed=True, run=Ran(), stopper=lambda *a: True)
        now = self.config()
        self.assertEqual(now["mcp"]["browser"]["command"], "/home/x/my-browser")
        self.assertEqual(now["browsers"], self.settings["browsers"])

    def test_the_hub_sessions_folder_outside_the_state_gets_its_links_on_the_layer(self):
        self.unit()
        folder = os.path.join(self.root, "elsewhere-home")
        os.makedirs(folder)
        for name in ("CLAUDE.md", "ONBOARDING.md"):
            winsafe.symlink(self, os.path.join(self.repo, "hub-home", name), os.path.join(folder, name))
        with open(os.path.join(folder, "notes.md"), "w") as f:
            f.write("mine\n")
        self.settings["hub"]["cwd"] = folder
        with open(self.conf, "w") as f:
            json.dump(self.settings, f)
        before = self.snapshot()
        self.move("linux", self.deb())
        for name in ("CLAUDE.md", "ONBOARDING.md"):
            self.assertEqual(os.readlink(os.path.join(folder, name)), os.path.join(self.state, "code", "hub-home", name))
        appmove.undo(self.state, failed=True, run=Ran(), stopper=lambda *a: True)
        for name in ("CLAUDE.md", "ONBOARDING.md"):
            self.assertEqual(os.readlink(os.path.join(folder, name)), os.path.join(self.repo, "hub-home", name))
        self.assertEqual(self.snapshot(), before)

    def test_a_rules_file_the_person_wrote_or_a_link_elsewhere_stays(self):
        self.unit()
        folder = os.path.join(self.root, "elsewhere-home")
        os.makedirs(folder)
        with open(os.path.join(folder, "CLAUDE.md"), "w") as f:
            f.write("my rules\n")
        winsafe.symlink(self, "/home/x/rules/ONBOARDING.md", os.path.join(folder, "ONBOARDING.md"))
        self.settings["hub"]["cwd"] = folder
        with open(self.conf, "w") as f:
            json.dump(self.settings, f)
        self.move("linux", self.deb())
        with open(os.path.join(folder, "CLAUDE.md")) as f:
            self.assertEqual(f.read(), "my rules\n")
        self.assertEqual(os.readlink(os.path.join(folder, "ONBOARDING.md")), "/home/x/rules/ONBOARDING.md")

    def test_a_failure_midway_puts_the_links_outside_the_state_back(self):
        self.unit()
        folder = os.path.join(self.root, "elsewhere-home")
        os.makedirs(folder)
        winsafe.symlink(self, os.path.join(self.repo, "hub-home", "CLAUDE.md"), os.path.join(folder, "CLAUDE.md"))
        self.settings["hub"]["cwd"] = folder
        with open(self.conf, "w") as f:
            json.dump(self.settings, f)
        before = self.snapshot()
        with mock.patch.object(appmove, "rewrite_config", side_effect=OSError("disk full")):
            with self.assertRaises(appmove.Refused):
                self.move("linux", self.deb())
        self.assertEqual(self.snapshot(), before)

    def test_a_relative_value_and_a_layer_that_is_the_clone_change_nothing(self):
        self.unit()
        self.settings["mcp"] = {"x": {"command": "bin/hub-browser", "args": [os.path.join(self.repo, "bin", "hub-browser")]}}
        with open(self.conf, "w") as f:
            json.dump(self.settings, f)
        os.makedirs(os.path.join(self.repo, "hub-home"))
        with mock.patch("os.getcwd", return_value=self.repo):   # a relative value is no one's path, wherever the move runs
            self.move("linux", self.deb())
        self.assertEqual(self.config()["mcp"]["x"]["command"], "bin/hub-browser")
        appmove.undo(self.state, failed=True, run=Ran(), stopper=lambda *a: True)
        # the move run by the clone's own code: its files are not the layer's
        self.move("linux", self.deb(), layer=self.repo)
        self.assertEqual(self.config()["mcp"], self.settings["mcp"])

    def test_the_undo_finds_a_moved_path_after_the_list_was_reordered_on_the_app(self):
        self.unit()
        self.settings_with_servers()
        self.move("linux", self.deb())
        from core import catalog
        with catalog.edit_settings(self.conf) as cfg:
            cfg["mcp"]["outside"]["args"].insert(0, "--first")
        appmove.undo(self.state, failed=True, run=Ran(), stopper=lambda *a: True)
        self.assertEqual(self.config()["mcp"]["outside"]["args"], ["--first", self.settings["mcp"]["outside"]["args"][0]])

    def test_a_link_that_cannot_be_made_stays_as_it_was(self):
        self.unit()
        folder = os.path.join(self.root, "elsewhere-home")
        os.makedirs(folder)
        old = os.path.join(self.repo, "hub-home", "CLAUDE.md")
        winsafe.symlink(self, old, os.path.join(folder, "CLAUDE.md"))
        self.settings["hub"]["cwd"] = folder
        with open(self.conf, "w") as f:
            json.dump(self.settings, f)
        real = os.symlink

        def refuse(target, link, *a, **kw):
            if str(link).endswith(".moving"):
                raise OSError("a privilege is not held")
            return real(target, link, *a, **kw)
        with mock.patch.object(appmove.os, "symlink", side_effect=refuse):
            rec, _ = self.move("linux", self.deb())
        self.assertEqual(os.readlink(os.path.join(folder, "CLAUDE.md")), old)
        self.assertEqual(os.listdir(folder), ["CLAUDE.md"])
        self.assertEqual(len(rec["unlinked"]), 1)


class Instance(Base):
    """A second hub from git (an installer run with HUB_INSTANCE: ~/.hub-<name>, its own settings folder named in its
    service): offered and moved as itself — its anchor, its service, its commands; the main hub's are never touched."""

    WEB, FILES = 8801, 8802

    def setUp(self):
        super().setUp()
        self.istate = os.path.join(self.home, ".hub-personal")
        self.icfg = os.path.join(self.home, ".config", "hub-personal")
        self.irepo = os.path.join(self.root, "agents-hub-personal")
        shutil.copytree(self.repo, self.irepo, symlinks=True)
        for d in (os.path.join(self.istate, "hub-home"), os.path.join(self.istate, "bin"), self.icfg):
            os.makedirs(d)
        with open(os.path.join(self.istate, "journal.jsonl"), "w") as f:
            f.write('{"seq":1}\n')
        self.iconf = os.path.join(self.icfg, "hub.json")
        self.isettings = {"title": "personal", "browser": {"mcp": ["/opt/homebrew/bin/npx", "-y",
                                                                    "chrome-devtools-mcp@latest"]}}
        with open(self.iconf, "w") as f:
            json.dump(self.isettings, f)
        for path, cmd in ((os.path.join(self.home, ".local", "bin", "hub-personal"), "hub"),
                          (os.path.join(self.home, ".local", "bin", "hub-telegram-personal"), "hub-telegram"),
                          (os.path.join(self.istate, "bin", "hub"), "hub")):
            with open(path, "w") as f:
                f.write(f'#!/bin/sh\n# agents-hub instance\nHUB_STATE_DIR="{self.istate}" exec "/usr/bin/python3" '
                        f'"{self.irepo}/bin/{cmd}" "$@"\n')

    def iunit(self, name="agents-hub-personal.service", cfg=None):
        path = os.path.join(self.home, ".config", "systemd", "user", name)
        os.makedirs(os.path.join(os.path.dirname(path), "default.target.wants"), exist_ok=True)
        with open(path, "w") as f:
            f.write(f"[Service]\nExecStart=/usr/bin/python3 {self.irepo}/bin/hub-up\nWorkingDirectory={self.irepo}\n"
                    f"Environment=HUB_CONFIG_DIR={cfg or self.icfg}\nEnvironment=HUB_WEB_PORT={self.WEB}\n"
                    f"Environment=HUB_FILES_PORT={self.FILES}\nEnvironment=HUB_STATE_DIR={self.istate}\n")
        winsafe.symlink(self, path, os.path.join(os.path.dirname(path), "default.target.wants", name))
        return path

    def iplist(self):
        path = os.path.join(self.home, "Library", "LaunchAgents", "com.hub-personal.plist")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.makedirs(os.path.join(self.istate, "Agents Hub.app", "Contents", "MacOS"))
        with open(path, "wb") as f:
            plistlib.dump({"Label": "com.hub-personal", "ProgramArguments": [
                "/opt/homebrew/bin/python3.12", os.path.join(self.irepo, "bin", "hub-up")],   # portable: a path in a plist, only read as text
                "EnvironmentVariables": {"HUB_STATE_DIR": self.istate, "HUB_CONFIG_DIR": self.icfg,
                                         "HUB_WEB_PORT": str(self.WEB), "HUB_FILES_PORT": str(self.FILES)}}, f)
        return path

    def anchor(self, kind="launchd", id_="com.hub-personal", cfg=None):
        from core import instance
        data = {"name": "personal", "title": "personal", "config_dir": cfg or self.icfg,
                "ports": {"web": self.WEB, "files": self.FILES}, "service": {"kind": kind, "id": id_},
                "command": "hub-personal", "code": {"kind": "shared", "path": self.irepo}, "by": "adopted"}
        instance.register({**data, "state": self.istate})
        instance.write(self.istate, data)
        return data

    def registry(self):
        from core import instance
        return os.path.join(instance.registry_dir(), "personal.json")

    def shell(self, name="shell"):
        """An environment of the one-line install (its mark): the only app a second hub moves onto."""
        env = os.path.join(self.root, name)
        os.makedirs(os.path.join(env, "bin"), exist_ok=True)
        with open(os.path.join(env, appmove.ONELINE_ENV), "w") as f:
            f.write("{}")
        return env

    def move(self, osname, app):
        ran = Ran()
        rec = appmove.move(self.istate, self.cfg, self.irepo, app, osname, run=ran, say=lambda line: None,
                           home_dir=self.home, now=lambda: 1_800_000_000, wait=lambda state, ceiling, say: [],
                           stopper=lambda state, unit, osname, run: True)
        return rec, ran

    def snap(self):
        """The whole home but the registry's folders (an adoption made them; its file is compared)."""
        return {k: v for k, v in self.snapshot().items()   # (a clone's index: git refreshes its stat data on any look)
                if not (v[0] == "dir" and k.startswith(os.path.join(".config", "agents-hub")))
                and not k.endswith(os.path.join(".git", "index"))}

    def configs(self):
        out = []
        for path in (self.conf, self.iconf):
            with open(path) as f:
                out.append(json.load(f))
        return out

    def test_an_anchored_instance_is_offered_as_itself_with_its_settings_folder(self):
        self.anchor(kind="systemd", id_="agents-hub-personal.service")
        self.iunit()
        got = appmove.find(self.istate, "linux", self.home)
        self.assertEqual((got["repo"], got["instance"], got["config_dir"]), (self.irepo, "personal", self.icfg))
        self.assertEqual(got["unit"]["id"], "agents-hub-personal.service")
        with mock.patch.dict(os.environ, {"HUB_LANG": "en"}):
            texts = appmove.texts(got)
        self.assertIn("hub-personal app undo", texts["text"])   # its own command: `hub app undo` is the main hub's
        self.assertIn("hub-personal app undo", texts["undo_failed"])
        self.assertIsNone(appmove.find(self.state, "linux", self.home))   # the main hub has no service here

    def test_one_from_before_the_anchor_is_offered_by_its_folder_and_its_own_settings(self):
        self.iunit()
        got = appmove.find(self.istate, "linux", self.home)
        self.assertEqual((got["instance"], got["config_dir"]), ("personal", self.icfg))
        self.assertFalse(os.path.exists(os.path.join(self.istate, "instance.json")))   # finding writes nothing

    def test_settings_that_are_not_the_instances_own_are_not_offered(self):
        path = self.iunit(cfg=self.cfg)   # the main hub's settings: hub-up would not adopt it either
        self.assertIsNone(appmove.find(self.istate, "linux", self.home))
        platforms.current.rmtree(os.path.dirname(path))
        self.anchor(kind="systemd", id_="agents-hub-personal.service")
        self.iunit(cfg=os.path.join(self.home, "elsewhere"))   # not the folder its anchor names
        self.assertIsNone(appmove.find(self.istate, "linux", self.home))

    def test_a_service_under_another_name_or_more_moved_settings_are_not_offered(self):
        path = self.iunit(name="agents-hub.service")   # the main hub's name: the move's own would not replace it
        self.assertIsNone(appmove.find(self.istate, "linux", self.home))
        os.rename(path, path.replace("agents-hub.service", "agents-hub-personal.service"))
        with open(path.replace("agents-hub.service", "agents-hub-personal.service"), "a") as f:
            f.write("Environment=HUB_CONFIG=/home/x/hub.json\n")
        self.assertIsNone(appmove.find(self.istate, "linux", self.home))

    def test_a_folder_not_named_as_an_instance_is_not_offered(self):
        odd = os.path.join(self.home, "hubs", "personal")
        os.makedirs(os.path.dirname(odd))
        os.rename(self.istate, odd)
        self.istate = odd
        self.iunit()
        self.assertIsNone(appmove.find(odd, "linux", self.home))

    def test_linux_move_and_undo_touch_only_this_hub(self):
        self.unit()   # the main hub from its clone: its unit, its commands, its settings stay as they are
        self.iunit()
        # the one-line install's mark the line wrote: an undo takes it away (the hub is the clone's again)
        os.makedirs(os.path.join(self.istate, "layers"))
        before = self.snap()
        rec, ran = self.move("linux", self.shell())
        with open(os.path.join(self.istate, "layers", "oneline.json"), "w") as f:
            f.write('{"data": "/home/x/.local/share/agents-hub"}')
        self.assertEqual((rec["instance"], rec["config_path"]), ("personal", self.iconf))
        units = os.path.join(self.home, ".config", "systemd", "user")
        self.assertFalse(os.path.exists(os.path.join(units, "agents-hub-personal.service")))
        self.assertTrue(os.path.exists(os.path.join(units, "agents-hub.service")))
        self.assertIn(["systemctl", "--user", "disable", "agents-hub-personal.service"], ran.calls)
        self.assertNotIn(["systemctl", "--user", "disable", "agents-hub.service"], ran.calls)
        with open(os.path.join(self.home, ".local", "bin", "hub")) as f:
            self.assertIn(self.repo, f.read())   # the terminal's `hub` is the main hub's still
        saved = [s["path"] for s in rec["saved"]]
        self.assertNotIn(os.path.join(self.home, ".local", "bin", "hub"), saved)
        self.assertIn(os.path.join(self.home, ".local", "bin", "hub-personal"), saved)
        main, own = self.configs()
        self.assertEqual(main, self.settings)   # the main hub's settings: not rewritten
        self.assertNotEqual(own["browser"]["mcp"], self.isettings["browser"]["mcp"])   # the environment's now
        from core import instance
        anchor = instance.load(self.istate)   # adopted from its service, and its code is the layer now
        self.assertEqual((anchor["config_dir"], anchor["ports"]), (self.icfg, {"web": self.WEB, "files": self.FILES}))
        self.assertEqual(anchor["code"], {"kind": "layer", "path": os.path.join(self.istate, "code")})
        with open(self.registry()) as f:
            self.assertEqual(json.load(f)["code"]["kind"], "layer")
        appmove.done(self.istate, now=lambda: 1_800_000_100)
        ran2, stopped = Ran(), []
        rec = appmove.undo(self.istate, run=ran2, now=lambda: 1_800_000_200,
                           stopper=lambda state, unit, osname, run: stopped.append(unit) or True)
        self.assertEqual(rec["left"], [])
        self.assertEqual(stopped, [{"kind": "systemd", "id": "agents-hub-personal.service"}])
        self.assertEqual(self.snap(), before)   # no anchor, no registry entry: as before the move
        self.assertEqual(self.configs(), [self.settings, self.isettings])
        self.assertIn(["systemctl", "--user", "restart", "agents-hub-personal.service"], ran2.calls)
        self.assertFalse([c for c in ran2.calls if "agents-hub.service" in c])

    def inside(self, worktrees=True):
        """The clone where `hub instance new` puts it: <state>/code; with a worktree of it inside the clone and one beside."""
        code = os.path.join(self.istate, "code")
        shutil.move(self.irepo, code)
        self.irepo = code
        self.wt = []
        if worktrees:
            for where in (os.path.join(code, ".claude", "worktrees", "w1"), os.path.join(self.root, "beside")):
                g(code, "worktree", "add", "-q", "-b", "b-" + os.path.basename(where), where)
                self.wt.append(where)
        return code

    def test_a_clone_in_the_instances_code_folder_is_offered(self):
        self.inside()
        self.iunit()
        got = appmove.find(self.istate, "linux", self.home)
        self.assertEqual((got["repo"], got["instance"]), (self.irepo, "personal"))

    def test_a_clone_in_another_folder_of_the_state_or_an_app_layer_is_not_offered(self):
        other = os.path.join(self.istate, "elsewhere")
        shutil.move(self.irepo, other)
        self.irepo = other
        self.iunit()
        self.assertIsNone(appmove.find(self.istate, "linux", self.home))
        platforms.current.rmtree(os.path.join(self.home, ".config", "systemd"))
        code = os.path.join(self.istate, "code")
        os.makedirs(os.path.join(self.istate, "layers", "l1"))
        shutil.move(other, os.path.join(self.istate, "layers", "l1", "x"))
        winsafe.symlink(self, os.path.join(self.istate, "layers", "l1", "x"), code)   # the app's pointer, not a folder
        self.irepo = code
        self.iunit()
        self.assertIsNone(appmove.find(self.istate, "linux", self.home))

    def test_a_clone_in_the_code_folder_goes_aside_for_the_layer_and_undo_puts_it_back(self):
        code = self.inside()
        self.iunit()
        os.makedirs(os.path.join(self.istate, "layers"))
        before = self.snap()
        heads = [g(w, "rev-parse", "HEAD") for w in self.wt]
        rec, ran = self.move("linux", self.shell())
        aside = rec["aside"]["to"]
        # beside the state folder, a name of its own (realpath: the temporary folder of macOS is a link to /private/var)
        self.assertEqual(os.path.dirname(aside), os.path.realpath(self.home))
        self.assertFalse(os.path.lexists(code))               # <state>/code is free for the app's pointer
        self.assertEqual(appmove.where(rec), aside)           # the fact and "About the hub" name where the clone is
        self.assertTrue(os.path.isfile(os.path.join(aside, "bin", "hub-up")))
        moved = [os.path.join(aside, ".claude", "worktrees", "w1"), self.wt[1]]
        for w, head in zip(moved, heads):   # the worktrees follow the clone: git finds its folder from both
            self.assertEqual(g(w, "rev-parse", "HEAD"), head)
            self.assertEqual(os.path.realpath(g(w, "rev-parse", "--path-format=absolute", "--git-common-dir")),
                             os.path.realpath(os.path.join(aside, ".git")))
        from core import instance
        self.assertEqual(instance.load(self.istate)["code"], {"kind": "layer", "path": code})
        winsafe.symlink(self, aside, code)   # what the app's install makes there
        appmove.done(self.istate, now=lambda: 1_800_000_100)
        from core import appwatch
        self.assertEqual(appwatch.moved(self.istate, {})["repo"], aside)
        self.assertEqual(appmove.snapshot(self.istate, now=1_800_000_101)["repo"], aside)
        rec = appmove.undo(self.istate, run=Ran(), now=lambda: 1_800_000_200,
                           stopper=lambda state, unit, osname, run: True)
        self.assertEqual(rec["left"], [])
        self.assertFalse(os.path.islink(code))
        self.assertFalse(os.path.lexists(aside))
        for w, head in zip(self.wt, heads):
            self.assertEqual(g(w, "rev-parse", "HEAD"), head)
            self.assertEqual(os.path.realpath(g(w, "rev-parse", "--path-format=absolute", "--git-common-dir")),
                             os.path.realpath(os.path.join(code, ".git")))
        self.assertEqual(self.snap(), before)

    def test_a_name_beside_the_state_folder_that_is_taken_is_not_used(self):
        self.inside(worktrees=False)
        self.iunit()
        taken = os.path.join(self.home, "hub-personal-clone")
        os.makedirs(taken)
        rec, _ = self.move("linux", self.shell())
        self.assertNotEqual(rec["aside"]["to"], taken)
        self.assertTrue(os.path.isdir(taken))
        self.assertTrue(os.path.isfile(os.path.join(rec["aside"]["to"], "bin", "hub-up")))

    def test_an_undo_does_not_push_a_folder_of_the_app_aside_and_does_not_start_a_hub_without_its_code(self):
        code = self.inside(worktrees=False)
        self.iunit()
        rec, _ = self.move("linux", self.shell())
        os.makedirs(os.path.join(code, "layer-file-dir"))
        with open(os.path.join(code, "keep.txt"), "w") as f:
            f.write("x")
        ran = Ran()
        rec = appmove.undo(self.istate, run=ran, now=lambda: 1_800_000_200, stopper=lambda *a: True)
        self.assertEqual(rec["status"], "moving")   # left to undo again: the line's next run and `app undo` retry it
        self.assertTrue(rec["returning"])
        self.assertIn(rec["aside"]["to"], rec["left"][0])
        self.assertFalse([c for c in ran.calls if "restart" in c])   # the unit names <state>/code: nothing to start
        self.assertTrue(os.path.isfile(os.path.join(rec["aside"]["to"], "bin", "hub-up")))   # the clone is kept
        self.assertTrue(os.path.isfile(os.path.join(code, "keep.txt")))                    # what is there is not touched
        platforms.current.rmtree(code)
        rec = appmove.undo(self.istate, run=Ran(), now=lambda: 1_800_000_300, stopper=lambda *a: True)
        self.assertEqual((rec["status"], rec["left"]), ("undone", []))
        self.assertTrue(os.path.isfile(os.path.join(code, "bin", "hub-up")))

    def test_a_worktree_whose_folder_is_gone_is_said_and_the_move_goes_on(self):
        self.inside()
        self.iunit()
        platforms.current.rmtree(self.wt[1])
        rec, _ = self.move("linux", self.shell())
        self.assertEqual(len(rec["aside"]["repair"]), 1)
        self.assertIn(self.wt[1], rec["aside"]["repair"][0])

    def test_a_failure_after_the_clone_went_aside_puts_it_back(self):
        code = self.inside()
        self.iunit()
        before = self.snap()
        with mock.patch.object(appmove, "_linux_handover", side_effect=OSError("no systemctl")):
            with self.assertRaises(appmove.Refused):
                self.move("linux", self.shell())
        self.assertEqual(appmove.record(self.istate)["status"], "failed")   # the move did start, and gave everything back
        self.assertTrue(os.path.isfile(os.path.join(code, "bin", "hub-up")))
        self.assertEqual(self.snap(), before)

    def test_mac_move_and_undo_of_an_anchored_instance(self):
        self.plist()
        plist = self.iplist()
        self.anchor()
        with open(self.registry()) as f:
            entry = f.read()
        before = self.snap()
        rec, _ = self.move("mac", self.shell())
        self.assertEqual(rec["unit"]["id"], "com.hub-personal")
        self.assertFalse(os.path.exists(os.path.join(self.istate, "Agents Hub.app")))
        self.assertTrue(os.path.exists(os.path.join(self.state, "Agents Hub.app")))   # the main hub's bundle stays
        from core import instance
        self.assertEqual(instance.load(self.istate)["code"]["kind"], "layer")
        ran, stopped = Ran(), []
        appmove.undo(self.istate, failed=True, run=ran,
                     stopper=lambda state, unit, osname, run: stopped.append(unit) or True)
        self.assertEqual(stopped, [{"kind": "launchd", "id": "com.hub-personal"}])
        self.assertIn(["launchctl", "bootstrap", f"gui/{os.getuid()}", plist], ran.calls)
        self.assertEqual(self.snap(), before)
        with open(self.registry()) as f:
            self.assertEqual(f.read(), entry)

    def test_a_shell_that_puts_only_the_main_hub_does_not_move_it(self):
        self.iunit()
        before = self.snap()
        with self.assertRaises(appmove.Refused) as cm:   # the .deb's first run: its own service is the main hub's
            self.move("linux", self.deb())
        self.assertIn("--instance personal", str(cm.exception))
        self.assertEqual(self.snap(), before)
        self.assertIsNone(appmove.record(self.istate))

    def test_a_folder_named_as_an_instance_on_the_main_hubs_settings_is_not_offered(self):
        path = self.iunit()
        with open(path) as f:
            text = f.read()
        with open(path, "w") as f:
            f.write(text.replace(f"Environment=HUB_CONFIG_DIR={self.icfg}\n", ""))
        self.assertIsNone(appmove.find(self.istate, "linux", self.home))

    def test_windows_undo_runs_the_instances_task(self):
        self.anchor(kind="task", id_="Agents Hub (personal)")
        with open(os.path.join(self.istate, "hub-up.cmd"), "w", newline="") as f:
            f.write(f'@echo off\r\ncd /d "{self.irepo}"\r\n"C:\\py\\python.exe" "{self.irepo}/bin/hub-up" --scheduled\r\n')
        got = appmove.find(self.istate, "windows", self.home)
        self.assertEqual((got["instance"], got["unit"]["id"]), ("personal", "Agents Hub (personal)"))
        rec, _ = self.move("windows", self.shell())
        self.assertIn(os.path.join(self.istate, "cmd", "hub-personal.cmd"), [s["path"] for s in rec["saved"]])
        ran = Ran()
        appmove.undo(self.istate, failed=True, run=ran, osname="windows", stopper=lambda *a: True)
        self.assertIn(["schtasks", "/Run", "/TN", "Agents Hub (personal)"], ran.calls)
        self.assertNotIn(["schtasks", "/Run", "/TN", "Agents Hub"], ran.calls)


class UndoFromThePage(unittest.TestCase):
    def test_under_a_systemd_unit_the_undo_is_a_unit_of_its_own(self):
        env = {"INVOCATION_ID": "x", "PATH": "/usr/bin", "HUB_SESSION": "maps"}
        cmd = appmove.undo_command("/s", "py", "/c/bin/hub", "/s/logs/app-move.log", env=env, which=lambda n: "/bin/" + n,
                                   osname="linux")
        self.assertEqual(cmd[:2], ["systemd-run", "--user"])
        self.assertIn("--setenv=HUB_STATE_DIR=/s", cmd)
        self.assertEqual(cmd[-4:], ["py", "/c/bin/hub", "app", "undo"])
        self.assertFalse(any("HUB_SESSION" in c for c in cmd))

    def test_elsewhere_the_command_itself(self):
        for osname, env in (("mac", {"INVOCATION_ID": "x"}), ("windows", {}), ("linux", {})):
            self.assertEqual(appmove.undo_command("/s", "py", "/h", "/l", env=env, which=lambda n: "/bin/" + n,
                                                  osname=osname), ["py", "/h", "app", "undo"])


class Waiting(unittest.TestCase):
    def test_waits_while_sessions_are_busy_then_goes(self):
        looks = [["maps: a turn is running"], ["maps: a turn is running"], []]
        said, slept = [], []
        got = appmove.wait_free("/s", 20, say=said.append, clock=lambda: 0, sleep=slept.append, look=lambda s: looks.pop(0))
        self.assertEqual(got, [])
        self.assertEqual(len(said), 1)   # said once while the same sessions hold it
        self.assertEqual(len(slept), 2)

    def test_the_ceiling_ends_the_wait_with_who_holds_it(self):
        t = [0]

        def clock():
            t[0] += 400
            return t[0]
        got = appmove.wait_free("/s", 20, say=lambda x: None, clock=clock, sleep=lambda s: None,
                                look=lambda s: ["maps: a turn is running"])
        self.assertEqual(got, ["maps: a turn is running"])


class Fact(Base):
    def test_the_core_records_a_done_move_once_and_the_page_sees_it_for_the_days(self):
        self.assertIsNone(appwatch.moved(self.state, {}))
        self.unit()
        Move.move(self, "linux", self.deb())
        self.assertIsNone(appwatch.moved(self.state, {}))   # moving: not done yet
        appmove.done(self.state, now=lambda: 1_800_000_100)
        data = appwatch.moved(self.state, {})
        self.assertEqual(data["repo"], self.repo)
        st = reducer.empty()
        appwatch.fold(st, {"type": "app.moved", "ts": 1_800_000_101}, data)
        self.assertIsNone(appwatch.moved(self.state, st))   # recorded: not again
        view = appwatch.move_view(st, 1_800_000_200)
        self.assertEqual(view["until"], 1_800_000_100 + appwatch.MOVE_DAYS * 86400)
        self.assertIsNone(appwatch.move_view(st, view["until"] + 1))
        self.assertEqual(appwatch.MOVE_DAYS, appmove.UNDO_DAYS)
        self.assertEqual(appwatch.MOVE, os.path.join(appmove.DIR, appmove.RECORD))

    def test_the_fact_is_the_cores_own(self):
        self.assertIn("app.moved", appwatch.FACTS)
        from core import machines
        self.assertIn("app.moved", machines.FACTS)


class Cli(Base):
    def hub(self, *args, **env):
        e = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION",)}
        e.update(HUB_STATE_DIR=self.state, HUB_LANG="en", **env)
        return subprocess.run([sys.executable, os.path.join(REPO, "bin", "hub"), "app", *args], capture_output=True,
                              text=True, env=e, timeout=120, stdin=subprocess.DEVNULL)

    def test_find_without_a_clone_says_none_with_code_3(self):
        r = self.hub("find", "--json")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(json.loads(r.stdout), {})

    def test_a_session_may_not(self):
        r = self.hub("undo", HUB_SESSION="maps")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("owner", r.stderr)

    def test_undo_without_a_move_is_refused_in_words(self):
        r = self.hub("undo")
        self.assertEqual(r.returncode, 1)
        self.assertIn("No move", r.stderr)


if __name__ == "__main__":
    unittest.main()
