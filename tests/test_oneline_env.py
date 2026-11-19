"""A new environment of the one-line install (spec §6.2–6.3): the environment is a folder, <data>/shells/<runtime>-<flavor>,
and the hub runs through its pointer <state>/app. The old environment's own tools/package/shellswap.py is the watchdog:
it moves the pointer at the full restart (core/shellnext.py, hub-up), the service starts the hub again through it —
or the watchdog does where no service would (the scheduler task held, a hub with no service) — and it puts the pointer
back when the core does not come up. The environments no hub needs any more go (core/envs.py).

The systems' programs (launchctl, systemctl, schtasks) are fakes here; the live stands of the slice run the real ones."""
import tests  # noqa: F401 - keeps the machine's settings out
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import platforms
from core import envs, shellnext
from tests import test_app_shell as shell_tests
from tests import test_app_update as base

REPO = base.REPO
PKG = os.path.join(REPO, "tools", "package")
POSIX = platforms.current.APP_OS != "windows"


def make_env(path, runtime, platform="linux-x64", flavor="team", sealed=True):
    """An environment folder as the install leaves it: the shell's own files, PACKAGED, the install's mark, its Python."""
    os.makedirs(path)
    for n in ("launch.py", "shellswap.py", "install.py", "start.py"):
        shutil.copy2(os.path.join(PKG, n), path)
    shutil.copy2(os.path.join(REPO, "core", "appcode.py"), path)
    base.write(os.path.join(path, "PACKAGED"), json.dumps({"format": "app", "layout": "layer", "version": "1.2.3",
                                                           "flavor": flavor, "runtime": runtime, "platform": platform,
                                                           "swaps": ["folder"]}))
    base.write(os.path.join(path, ".agents-hub-shell.json"), json.dumps({"platform": platform, "flavor": flavor,
                                                                         "runtime": runtime}))
    if sealed:
        base.write(os.path.join(path, ".agents-hub-sealed"), "1\n")
    py = shellnext.python_of(path)
    os.makedirs(os.path.dirname(py))
    if POSIX:
        os.symlink(sys.executable, py)
    else:
        shutil.copy2(sys.executable, py)
    return path


@unittest.skipUnless(POSIX, "the pointer here is a link; the Windows junction is the winstand stand's")
class FolderSwap(shell_tests.Swap):
    PLATFORM = "linux-x64"

    def setUp(self):
        super().setUp()
        self.root, self.state = os.path.realpath(self.root), os.path.realpath(self.state)   # macOS: /var is /private/var
        self.shells = os.path.join(self.root, "data", "shells")
        self.old = make_env(os.path.join(self.shells, "1-team"), 1)
        self.new = make_env(os.path.join(self.shells, "2-team"), 2)
        self.link = os.path.join(self.state, "app")
        os.symlink(os.path.relpath(self.old, self.state), self.link)
        base.write(os.path.join(self.state, "layers", "shell.json"), json.dumps({
            "launch": os.path.join(self.link, "launch.py"),
            "python": os.path.join(self.link, "runtime", "python", "bin", "python3")}))
        self.started = []
        go = mock.patch.object(self.sw.launch, "detached", self.started.append)
        go.start()
        self.addCleanup(go.stop)

    def fake(self, args, **kw):
        self.calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    def prepare(self, new=None, need=2):
        return self.sw.prepare(self.state, new or self.new, need, run=self.fake, now=self.now)

    def swap(self, **kw):
        return self.sw.swap(self.state, run=self.fake, clock=self.clock, sleep=self.sleep, now=self.now,
                            alive=lambda pid: False, **kw)

    def points_at(self):
        return os.path.realpath(self.link)

    def service(self, kind, ident=""):
        self.sw.update(self.state, service={"kind": kind, "id": ident})

    def test_prepare_takes_the_new_folder_next_to_the_old_one_with_the_old_ones_watchdog(self):
        rec = self.prepare()
        self.assertEqual((rec["status"], rec["folder"], rec["runtime"], rec["window"]), ("ready", True, 2, 300))
        self.assertEqual((rec["app"], rec["old"], rec["new"]), (self.link, self.old, self.new))
        # by real paths: the pointer will lead to the new one while the watchdog runs
        self.assertEqual(rec["watcher"], [os.path.realpath(shellnext.python_of(self.old)),
                                          os.path.join(self.old, "shellswap.py")])
        self.assertEqual(self.points_at(), self.old)   # nothing moved yet
        self.assertIn([shellnext.python_of(self.new), "-I", "-B", "-c", "import ssl, sqlite3"], self.calls)

    def test_a_folder_not_finished_elsewhere_of_another_build_or_too_old_is_refused(self):
        other = make_env(os.path.join(self.root, "elsewhere", "2-team"), 2)
        unsealed = make_env(os.path.join(self.shells, "2-team-1"), 2, sealed=False)
        arm = make_env(os.path.join(self.shells, "2-team-2"), 2, platform="linux-arm64")
        opened = make_env(os.path.join(self.shells, "2-open"), 2, flavor="open")
        for new, need, why in ((other, 2, "next to"), (unsealed, 2, "finished"), (arm, 2, "linux-arm64"),
                               (opened, 2, "open"), (self.new, 3, "with 3"), (self.old, 1, "next to")):
            with self.subTest(why):
                with self.assertRaises(self.sw.Refused) as e:
                    self.prepare(new, need)
                self.assertIn(why, str(e.exception))
        self.assertEqual(self.points_at(), self.old)

    def test_a_new_environment_whose_python_does_not_start_is_refused(self):
        self.fake = lambda args, **kw: subprocess.CompletedProcess(args, 1, "", "Fatal Python error")
        with self.assertRaises(self.sw.Refused) as e:
            self.prepare()
        self.assertIn("Fatal Python error", str(e.exception))

    def test_the_swap_moves_the_pointer_and_the_core_coming_up_ends_it(self):
        self.prepare()
        self.service("systemd", "agents-hub.service")
        self.comes_up()
        self.assertEqual(self.swap(), "done")
        self.assertEqual(self.points_at(), self.new)
        self.assertEqual(self.started, [])   # systemd starts the unit again on hub-up's exit code
        self.assertTrue(os.path.isdir(self.old))   # the way back stays until the next replacement

    def test_a_state_folder_reached_through_a_link_still_gets_a_pointer_that_leads_there(self):
        # macOS: TMPDIR and /var are /private/var — the state folder spelled one way, the environment the other; the
        # relative link made of the two spellings led to /private/private/var (the Mac bridge)
        alias = os.path.join(self.root, "alias")
        os.symlink(self.root, alias)
        state = os.path.join(alias, "state")
        self.sw.point_to(state, self.new)
        self.assertEqual(os.path.realpath(os.path.join(state, "app")), self.new)

    def test_a_junction_not_made_again_is_put_back(self):
        real = self.sw.launch.appcode().point
        calls = []

        def broken(link, parent, name):   # Windows: the junction removed, CreateJunction refused
            calls.append(name)
            if len(calls) == 1:
                os.unlink(link)
                raise OSError(5, "Access is denied")
            real(link, parent, name)
        with mock.patch.object(self.sw.launch.appcode(), "point", broken):
            with self.assertRaises(self.sw.Refused):
                self.sw.point_to(self.state, self.new)
        self.assertEqual(self.points_at(), self.old)

    def test_a_pointer_that_does_not_lead_there_is_put_back(self):
        real = self.sw.launch.appcode().point
        calls = []

        def wrong(link, parent, name):
            calls.append(name)
            real(link, parent, "1-team" if len(calls) == 1 else name)   # the first one lands elsewhere
        with mock.patch.object(self.sw.launch.appcode(), "point", wrong):
            os.unlink(self.link)
            os.symlink(self.new, self.link)   # it led to the new one; point_to(old) goes wrong once
            with self.assertRaises(self.sw.Refused):
                self.sw.point_to(self.state, os.path.join(self.shells, "3-team-missing"))
        self.assertEqual(self.points_at(), self.new)   # back where it led

    def test_linux_needs_no_package_manager_for_a_folder(self):
        rec = self.prepare()
        self.assertEqual(rec["platform"], "linux-x64")
        self.assertEqual(self.swap(), "returned")   # it ran at all (an app on Linux never does)

    def test_a_new_environment_whose_core_never_comes_goes_back(self):
        self.prepare()
        self.service("systemd", "agents-hub-x.service")
        self.assertEqual(self.swap(), "returned")
        self.assertEqual(self.points_at(), self.old)
        rec = self.record()
        self.assertEqual((rec["status"], rec["why"], rec["back"]), ("returned", "timeout", True))
        self.assertIn(["systemctl", "--user", "restart", "agents-hub-x.service"], self.calls)
        self.assertGreaterEqual(self.t[0], 300)

    def test_a_mac_agent_is_kicked_after_the_way_back(self):
        self.prepare()
        self.service("launchd", "com.hub-core")
        with mock.patch.dict(os.environ, {"HUB_SHELL_KICK": ""}):
            os.environ.pop("HUB_SHELL_KICK")
            self.assertEqual(self.swap(), "returned")
        self.assertIn(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.hub-core"], self.calls)

    def test_a_hub_with_no_service_is_started_by_the_watchdog_both_ways(self):
        self.prepare()
        self.service("none")
        self.assertEqual(self.swap(), "returned")
        start = [shellnext.python_of(self.link), "-I", "-B", os.path.join(self.link, "start.py")]
        self.assertEqual(self.started, [start, start])   # on the new one, then back on the old one

    def test_a_service_whose_file_was_not_found_starts_it_itself(self):
        self.prepare()
        self.service("service")
        self.comes_up()
        self.assertEqual(self.swap(), "done")
        self.assertEqual(self.started, [])

    def test_a_reboot_after_the_pointer_moved_watches_again_and_one_before_leaves_the_old(self):
        self.prepare()
        self.sw.update(self.state, status="swapping", since=self.wall[0])
        self.assertEqual(self.swap(resume=True), "failed")
        self.assertEqual(self.points_at(), self.old)
        self.prepare()
        self.sw.update(self.state, status="swapping", since=self.wall[0])
        self.sw.point_to(self.state, self.new)
        self.comes_up()
        self.assertEqual(self.swap(resume=True), "done")

    def test_the_launcher_resumes_a_folder_watchdog_on_linux_too(self):
        self.prepare()
        started = []
        self.sw.update(self.state, status="swapped", since=self.wall[0], pid=None)
        self.assertTrue(self.sw.resume(self.state, run=started.append))
        self.assertEqual(started[0][:4], [os.path.realpath(shellnext.python_of(self.old)), "-I", "-B",
                                          os.path.join(self.old, "shellswap.py")])

    def test_hub_up_names_the_service_for_the_watchdog_and_exits_when_the_pointer_moved(self):
        self.prepare()
        unit = {"kind": "systemd", "id": "agents-hub.service", "path": "/x", "port": None, "files_port": None}
        spawned = []

        def spawn(cmd):
            spawned.append(cmd)
            self.sw.update(self.state, status="swapped")
        with mock.patch.object(platforms.current, "service_unit", lambda state: unit), \
                mock.patch.dict(os.environ, {"INVOCATION_ID": "x"}):
            got = shellnext.at_restart(self.state, self.state, lambda text: None, spawn=spawn, clock=self.clock,
                                       sleep=self.sleep)
        self.assertEqual(got, platforms.current.APP_SWAP_EXIT)
        self.assertEqual(self.record()["service"], {"kind": "systemd", "id": "agents-hub.service"})
        self.assertEqual(spawned[0][-2:], ["swap", self.state])

    def test_a_hub_up_not_started_by_its_service_is_started_by_the_watchdog(self):
        # the unit file is there, but this hub-up was started by hand or in the background: nothing restarts it
        unit = {"kind": "systemd", "id": "agents-hub.service", "path": "/x", "port": None, "files_port": None}
        with mock.patch.object(platforms.current, "service_unit", lambda state: unit), \
                mock.patch.dict(os.environ, {"INVOCATION_ID": "", "HUB_SERVICE": ""}):
            self.assertEqual(shellnext._service_of(self.state), {"kind": "none", "id": ""})

    def test_a_hub_that_does_not_start_on_the_new_one_is_still_watched_and_given_back(self):
        self.prepare()
        self.service("none")
        boom = [True]

        def started(cmd):
            if boom:
                boom.clear()
                raise OSError(8, "Exec format error")
            self.started.append(cmd)
        with mock.patch.object(self.sw.launch, "detached", started):
            self.assertEqual(self.swap(), "returned")
        self.assertEqual(self.points_at(), self.old)
        self.assertEqual(len(self.started), 1)   # back on the old one
        with open(os.path.join(self.state, "logs", "shell-update.log")) as f:
            self.assertIn("the hub did not start on the new environment", f.read())

    def test_no_service_found_under_systemd_is_a_service_still(self):
        with mock.patch.object(platforms.current, "service_unit", lambda state: None), \
                mock.patch.dict(os.environ, {"INVOCATION_ID": "x"}):
            self.assertEqual(shellnext._service_of(self.state), {"kind": "service", "id": ""})
        with mock.patch.object(platforms.current, "service_unit", lambda state: None), \
                mock.patch.dict(os.environ, {"INVOCATION_ID": "", "HUB_SERVICE": ""}):
            self.assertEqual(shellnext._service_of(self.state), {"kind": "none", "id": ""})


def env_archive(path, top, runtime, platform="linux-x64", bad=False, flavor="team"):
    """The factory's environment archive, as small as it goes: the shell's own files, PACKAGED, a Python (a wrapper of
    this one through a link inside), paths.json."""
    import io
    import tarfile

    def add(t, name, data=b"", mode=0o644, link=None, kind=None):
        ti = tarfile.TarInfo(f"{top}/{name}" if name else top)
        ti.mtime, ti.mode = 1, mode
        if kind == "dir":
            ti.type = tarfile.DIRTYPE
            t.addfile(ti)
        elif link:
            ti.type, ti.linkname = tarfile.SYMTYPE, link
            t.addfile(ti)
        else:
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    with tarfile.open(path, "w:gz") as t:
        add(t, "", kind="dir", mode=0o755)
        for d in ("runtime", "runtime/python", "runtime/python/bin"):
            add(t, d, kind="dir", mode=0o755)
        add(t, "runtime/python/bin/python-wrapper", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n'.encode(), 0o755)
        add(t, "runtime/python/bin/python3", link="../../../.." if bad else "python-wrapper")
        for n in ("launch.py", "shellswap.py", "install.py", "start.py"):
            with open(os.path.join(PKG, n), "rb") as f:
                add(t, n, f.read())
        with open(os.path.join(REPO, "core", "appcode.py"), "rb") as f:
            add(t, "appcode.py", f.read())
        add(t, "PACKAGED", json.dumps({"format": "app", "layout": "layer", "version": "1.3.0", "flavor": flavor,
                                       "runtime": runtime, "platform": platform, "swaps": ["folder"]}).encode())
        add(t, "paths.json", b"{}")
    return path


@unittest.skipUnless(POSIX, "the pointer here is a link; the Windows junction is the winstand stand's")
class Offer(base.Stand):
    """The hub's side for a folder (modules/update/layer.py): runtime.shells offered, the archive downloaded and
    unpacked next to the current environment by deploy/oneline/unpack.py, its installer's steps done, the old
    environment's watchdog given it — the real programs, nothing faked but the server."""

    PLATFORM = "linux-x64"

    def setUp(self):
        super().setUp()
        self.root = os.path.realpath(self.root)   # macOS: /var is /private/var
        self.addCleanup(lambda: envs.remove(os.path.join(self.root, "data")))
        with open(os.path.join(REPO, "deploy", "oneline", "unpack.py"), encoding="utf-8") as f:
            unpack = f.read()
        tree = {"deploy/oneline/unpack.py": unpack}
        self.release("v1.2.4", tree)
        self.start(self.built["v1.2.4"]["seed"])
        self.release("v1.3.0", {**tree, "core/version.py": "RUNTIME_MIN = 2\n"})
        self.publish("v1.3.0", runtime_min=2)
        self.shells = os.path.join(self.root, "data", "shells")
        self.old = make_env(os.path.join(self.shells, "1-team"), 1)
        self.link = os.path.join(self.state, "app")
        os.symlink(self.old, self.link)
        base.write(os.path.join(self.codes(), "shell.json"), json.dumps({
            "launch": os.path.join(self.link, "launch.py"), "python": shellnext.python_of(self.link)}))
        self.archive = env_archive(os.path.join(self.root, "agents-hub-v1.3.0-linux-x64.tar.gz"),
                                   "agents-hub-v1.3.0-linux-x64", 2)
        self.publish_shell(self.archive, 2)

    def publish_shell(self, path, runtime):
        import contextlib
        import io
        send = sys.modules["deliver_send"] = base.load("deliver_send", os.path.join(PKG, "deliver_send.py"))
        self.addCleanup(sys.modules.pop, "deliver_send", None)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = send.main([self.url, "--key-file", self.key_file, "shell", path, "--platform", self.PLATFORM,
                              "--runtime", str(runtime), "--send"])
        self.assertEqual(code, 0)

    def updater(self, claude="9.9.9", run=subprocess.run, **update):
        from modules.update import layer
        cfg = {"update": {"enabled": True, "channel": "stable", "app": {"runtime": 1, "layer": True,
                                                                         "platform": self.PLATFORM}, **update},
               "dev": {"name": "hub-dev"}}
        return layer.LayerUpdater(os.path.join(self.codes(), self.current()), self.state, cfg, claude=lambda: claude,
                                  spawn=lambda *a: self.spawned.append(a), now=lambda: self.clock[0], run=run)

    def checked(self):
        return self.events("update.checked")[-1]["data"]

    def test_the_new_environment_is_offered_unpacked_next_to_the_old_one_and_laid_out(self):
        u = self.updater()
        self.assertEqual(u.check(apply=False, manual=True), "needs-runtime")
        self.assertEqual(self.checked()["shell"], {"runtime": 2, "size": os.path.getsize(self.archive), "auto": True})
        self.assertEqual(self.updater().check(apply=True, manual=True), "shell-ready")
        kinds = [e["type"] for e in self.events()]
        new = os.path.join(self.shells, "2-team")
        rec = shellnext.read(self.state)
        self.assertEqual((rec["status"], rec["folder"], rec["new"], rec["old"], rec["then"]),
                         ("ready", True, new, self.old, "1.3.0"))
        self.assertTrue(os.path.isfile(os.path.join(new, ".agents-hub-sealed")))       # its installer's steps
        self.assertTrue(os.path.isfile(os.path.join(new, ".agents-hub-shell.files")))  # its sheet: a run again checks it
        self.assertEqual(os.stat(os.path.join(new, "PACKAGED")).st_mode & 0o222, 0)    # read-only
        self.assertEqual(os.listdir(os.path.join(self.root, "data", "downloads")), [])
        self.assertEqual(os.path.realpath(self.link), self.old)   # the pointer moves at the restart, not now
        self.assertIn("hub.restart_requested", kinds)

    def test_a_broken_folder_of_that_name_gets_a_new_one_next_to_it(self):
        held = make_env(os.path.join(self.shells, "2-team"), 2)   # no sheet: not one the install finished whole
        with mock.patch.object(envs, "busy", lambda paths, run=None: {p for p in paths if p == held}):
            self.assertEqual(self.updater().check(apply=True, manual=True), "shell-ready")
        new = shellnext.read(self.state)["new"]
        self.assertRegex(os.path.basename(new), r"^2-team-\d+$")
        self.assertTrue(os.path.isdir(held))   # a program of it runs: it stays

    def test_a_broken_folder_nothing_holds_goes_before_the_new_one_takes_its_name(self):
        make_env(os.path.join(self.shells, "2-team"), 2)
        with mock.patch.object(envs, "busy", lambda paths, run=None: set()):
            self.assertEqual(self.updater().check(apply=True, manual=True), "shell-ready")
        self.assertEqual(shellnext.read(self.state)["new"], os.path.join(self.shells, "2-team"))
        self.assertTrue(os.path.isfile(os.path.join(self.shells, "2-team", ".agents-hub-shell.files")))

    def test_a_whole_one_the_line_put_there_is_taken_without_a_download(self):
        sys.path.insert(0, PKG)
        import install
        part, new = os.path.join(self.shells, "2-team-7.part"), os.path.join(self.shells, "2-team-7")   # a repair's name
        r = subprocess.run([sys.executable, "-I", os.path.join(REPO, "deploy", "oneline", "unpack.py"), "unpack",
                            self.archive, part, new], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIsNone(install.prepare(new))
        envs.keep_next(self.state, new, now=lambda: self.clock[0])   # the line leaves it to the update (setup.py)
        got = []
        u = self.updater()
        u.download = lambda *a, **kw: got.append(a)
        self.assertEqual(u.check(apply=True, manual=True), "shell-ready", shellnext.read(self.state).get("why"))
        self.assertEqual(got, [])
        self.assertEqual(shellnext.read(self.state)["new"], new)
        self.assertFalse(os.path.exists(os.path.join(self.codes(), envs.NEXT)))   # the record names it now

    def test_an_archive_that_does_not_unpack_whole_is_refused_and_leaves_nothing(self):
        bad = env_archive(os.path.join(self.root, "agents-hub-v1.3.1-linux-x64.tar.gz"), "agents-hub-v1.3.1-linux-x64",
                          3, bad=True)
        self.publish_shell(bad, 3)
        self.assertEqual(self.updater().check(apply=True, manual=True), "bad-shell")
        self.assertIn("reaches outside", self.checked()["error"])
        self.assertEqual(sorted(os.listdir(self.shells)), ["1-team"])
        self.assertEqual(shellnext.read(self.state)["status"], "failed")

    def test_an_old_watchdog_without_the_folder_branch_keeps_the_link(self):
        base.write(os.path.join(self.old, "PACKAGED"), json.dumps(   # built before slice O4: no "swaps"
            {"format": "app", "layout": "layer", "version": "1.2.3", "flavor": "team", "runtime": 1,
             "platform": "linux-x64"}))
        self.assertEqual(self.updater().check(apply=True, manual=True), "needs-runtime")
        self.assertFalse(self.checked()["shell"]["auto"])
        self.assertFalse(os.path.exists(os.path.join(self.shells, "2-team")))

    def test_the_one_the_line_left_is_kept_until_the_update_takes_it_or_a_month(self):
        later = make_env(os.path.join(self.shells, "2-team-5"), 2)
        envs.keep_next(self.state, later, now=lambda: self.clock[0])
        with mock.patch.object(envs, "busy", lambda paths, run=None: set()):
            self.assertEqual(envs.prune(self.state, now=lambda: self.clock[0]), [])
            self.assertEqual(envs.prune(self.state, now=lambda: self.clock[0] + 31 * 86400), ["2-team-5"])

    def test_after_the_replacement_the_environment_no_hub_needs_goes(self):
        older = make_env(os.path.join(self.shells, "0-team"), 0)
        shellnext.write(self.state, {"folder": True, "status": "done", "reported": True, "old": self.old,
                                     "new": self.old})
        with mock.patch.object(envs, "busy", lambda paths, run=None: set()):
            self.updater()._shell_prune()
        self.assertFalse(os.path.exists(older))
        self.assertTrue(os.path.isdir(self.old))

@unittest.skipUnless(POSIX, "the pointer here is a link; the Windows junction is the winstand stand's")
class OpenOffer(base.OpenStand):
    """The open build's new environment: runtime.shells of its pages names an asset of its GitHub release; github
    redirects it to the files' storage — downloaded without a key, checked, unpacked and laid out as the team's."""

    def setUp(self):
        super().setUp()
        self.addCleanup(lambda: envs.remove(os.path.join(self.root, "data")))
        with open(os.path.join(REPO, "deploy", "oneline", "unpack.py"), encoding="utf-8") as f:
            tree = {"deploy/oneline/unpack.py": f.read()}
        self.release("v1.2.4", tree)
        self.launch.ensure_layer(self.built["v1.2.4"]["seed"], self.mark, self.state, now=lambda: self.clock[0],
                                 run=lambda cmd: None, own_deliver=True)
        self.release("v1.3.0", {**tree, "core/version.py": "RUNTIME_MIN = 2\n"})
        self.shells_dir = os.path.join(self.root, "data", "shells")
        self.old = make_env(os.path.join(self.shells_dir, "1-open"), 1, flavor="open")
        self.link = os.path.join(self.state, "app")
        os.symlink(self.old, self.link)
        base.write(os.path.join(self.codes(), "shell.json"), json.dumps({
            "launch": os.path.join(self.link, "launch.py"), "python": shellnext.python_of(self.link)}))
        self.archive = env_archive(os.path.join(self.root, "agents-hub-v1.3.0-open-linux-x64.tar.gz"),
                                   "agents-hub-v1.3.0-open-linux-x64", 2, flavor="open")
        self.publish_shell(self.archive, 2, "v1.3.0")
        self.publish("v1.3.0", runtime_min=2)

    def test_the_new_environment_comes_from_the_release_without_a_key(self):
        self.assertEqual(self.updater().check(apply=True, manual=True), "shell-ready")
        new = os.path.join(self.shells_dir, "2-open")
        rec = shellnext.read(self.state)
        self.assertEqual((rec["status"], rec["new"], rec["old"]), ("ready", new, self.old))
        self.assertTrue(os.path.isfile(os.path.join(new, ".agents-hub-sealed")))
        places = [(where, path.split("?")[0]) for where, path, _ in self.asked]
        self.assertIn(("github", "/acme/agents-hub/releases/download/v1.3.0/agents-hub-v1.3.0-open-linux-x64.tar.gz"),
                      places)
        self.assertIn(("files", "/agents-hub-v1.3.0-open-linux-x64.tar.gz"), places)
        self.assertEqual(self.keys_seen(), {None})

    def test_a_redirect_off_the_hosts_of_the_releases_downloads_nothing(self):
        path = "/acme/agents-hub/releases/download/v1.3.0/agents-hub-v1.3.0-open-linux-x64.tar.gz"
        self.github.redirects[path] = "http://203.0.113.5/agents-hub-v1.3.0-open-linux-x64.tar.gz"
        self.assertEqual(self.updater().check(apply=True, manual=True), "broken")
        self.assertNotIn("files", {where for where, _, _ in self.asked})
        self.assertFalse(os.path.isdir(os.path.join(self.shells_dir, "2-open")))


class OutsideTheUnit(unittest.TestCase):
    """Under systemd hub-up's exit stops its unit and the unit's whole cgroup with it — the watchdog of a new
    environment too, in a session of its own (the live Ubuntu 24.04 stand: the pointer stayed on an environment that
    never came up). It goes as a transient unit of its own, with the hub's variables."""

    def test_a_unit_s_process_starts_the_watchdog_as_a_unit_of_its_own(self):
        from platforms import posix
        env = {"INVOCATION_ID": "x", "HUB_STATE_DIR": "/s", "HUB_SHELL_WINDOW": "90", "HUB_TREE": "a@/s",
               "PATH": "/usr/bin", "SECRET_OF_THE_UNIT": "no"}
        with mock.patch.object(posix.sys, "platform", "linux"):
            got = posix._outside_unit(["/py", "swap.py"], env, which=lambda name: "/usr/bin/" + name)
            self.assertEqual(got[:4], ["systemd-run", "--user", "--collect", "--quiet"])
            self.assertEqual(got[-3:], ["--", "/py", "swap.py"])
            self.assertEqual(sorted(got[4:-3]), ["--setenv=HUB_SHELL_WINDOW=90", "--setenv=HUB_STATE_DIR=/s",
                                                  "--setenv=PATH=/usr/bin"])
            self.assertIsNone(posix._outside_unit(["x"], {"PATH": "/usr/bin"}, which=lambda name: "/usr/bin/" + name))
            self.assertIsNone(posix._outside_unit(["x"], env, which=lambda name: None))
        with mock.patch.object(posix.sys, "platform", "darwin"):   # launchd leaves a session of its own alone
            self.assertIsNone(posix._outside_unit(["x"], env, which=lambda name: "/usr/bin/" + name))

    def test_the_two_lists_of_what_goes_along_are_one(self):
        from platforms import posix
        launch = base.load("hub_test_launch_outside2", os.path.join(PKG, "launch.py"))
        self.assertEqual(launch.OUTSIDE_ENV, posix._OUTSIDE_ENV)

    def test_the_environment_s_own_launcher_does_the_same(self):
        launch = base.load("hub_test_launch_outside", os.path.join(PKG, "launch.py"))
        ran = []
        with mock.patch.object(launch.sys, "platform", "linux"), mock.patch.object(launch, "WINDOWS", False), \
                mock.patch.dict(os.environ, {"INVOCATION_ID": "x", "HUB_STATE_DIR": "/s"}), \
                mock.patch.object(launch.shutil, "which", lambda name: "/usr/bin/" + name), \
                mock.patch.object(launch.subprocess, "run",
                                  lambda cmd, **kw: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0)), \
                mock.patch.object(launch.subprocess, "Popen", side_effect=AssertionError("not apart from the unit")):
            self.assertIsNone(launch.detached(["/py", "swap.py"]))
        self.assertEqual(ran[0][:4], ["systemd-run", "--user", "--collect", "--quiet"])
        self.assertIn("--setenv=HUB_STATE_DIR=/s", ran[0])

@unittest.skipUnless(os.path.isdir("/proc/self"), "Linux: /proc")
class Zombie(unittest.TestCase):
    def test_an_exited_process_nobody_reaped_is_not_alive_for_the_watchdog(self):
        # a container whose first process is `sleep infinity`: hub-up exited, nobody reaped it, and the watchdog waited
        # 180 s for it twice (the live Ubuntu 22.04 stand without systemd)
        launch = base.load("hub_test_launch_zombie", os.path.join(PKG, "launch.py"))
        p = subprocess.Popen([sys.executable, "-c", "pass"])  # portable: Linux /proc
        self.addCleanup(p.wait)
        for _ in range(100):
            with open(f"/proc/{p.pid}/stat") as f:
                if f.read().rsplit(")", 1)[-1].split()[0] == "Z":
                    break
            time.sleep(0.05)
        self.assertFalse(launch.alive(p.pid))
        self.assertTrue(launch.alive(os.getpid()))

@unittest.skipUnless(POSIX, "folder modes")
class ReadOnlySeed(unittest.TestCase):
    """The environment is read-only once installed (install.py): a layer installed from its seed — a new hub with no
    newer release on the server — takes folders its launcher can write PACKAGED into (the live Ubuntu stand, 08.11)."""

    def test_the_layer_of_a_read_only_seed_has_writable_folders_and_read_only_files(self):
        from core import appcode
        root = tempfile.mkdtemp(prefix="hub-seed-")
        self.addCleanup(lambda: envs.remove(root))
        seed = os.path.join(root, "seed")
        base.write(os.path.join(seed, "core", "x.py"), "X = 1\n")
        appcode.make(seed, "9.9.1", "team", "c0ffee", mtime=1700000000)
        sys.path.insert(0, PKG)
        import install
        install.read_only(seed)
        name, _ = appcode.install(seed, os.path.join(root, "layers"))
        layer = os.path.join(root, "layers", name)
        for d in (layer, os.path.join(layer, "core")):
            self.assertTrue(os.stat(d).st_mode & 0o200, d)
        self.assertFalse(os.stat(os.path.join(layer, "core", "x.py")).st_mode & 0o222)
        base.write(os.path.join(layer, "PACKAGED"), "{}")   # what the launcher writes there

@unittest.skipUnless(POSIX, "links and modes; Windows refuses the rename of a held folder (the winstand stand)")
class Prune(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="hub-envs-"))
        self.addCleanup(lambda: envs.remove(self.root))
        self.shells = os.path.join(self.root, "data", "shells")
        self.state = os.path.join(self.root, "state")
        os.makedirs(os.path.join(self.state, "layers"))
        self.now = time.time()   # a .part's age is by its real time on disk

    def env(self, name, read_only=True):
        path = make_env(os.path.join(self.shells, name), int(name.split("-")[0]))
        if read_only:
            sys.path.insert(0, PKG)
            import install
            install.read_only(path)
        return path

    def point(self, state, env):
        os.makedirs(state, exist_ok=True)
        os.symlink(env, os.path.join(state, "app"))

    def prune(self, busy=()):
        with mock.patch.object(envs, "busy", lambda paths, run=None: {p for p in paths if os.path.basename(p) in busy}), \
                mock.patch.object(envs, "hubs", lambda state: [state, os.path.join(self.root, "second")]):
            return envs.prune(self.state, now=lambda: self.now)

    def test_what_no_hub_points_at_goes_read_only_or_not(self):
        cur = self.env("2-team")
        self.env("1-team")
        self.env("2-team-1700000000", read_only=False)
        self.point(self.state, cur)
        self.assertEqual(self.prune(), ["1-team", "2-team-1700000000"])
        self.assertEqual(os.listdir(self.shells), ["2-team"])

    def test_a_second_hubs_environment_the_way_back_and_a_new_one_on_its_way_stay(self):
        cur, other, back, coming = (self.env(n) for n in ("2-team", "1-team-5", "1-team", "3-team"))
        self.point(self.state, cur)
        self.point(os.path.join(self.root, "second"), other)
        base.write(os.path.join(self.state, "layers", "shell-update.json"), json.dumps(
            {"folder": True, "status": "done", "old": back, "new": cur}))
        os.makedirs(os.path.join(self.root, "second", "layers"))
        base.write(os.path.join(self.root, "second", "layers", "shell-update.json"), json.dumps(
            {"status": "preparing", "file": coming}))
        self.assertEqual(self.prune(), [])
        base.write(os.path.join(self.root, "second", "layers", "shell-update.json"), json.dumps({}))
        self.assertEqual(self.prune(), ["3-team"])

    def test_a_new_one_that_did_not_come_up_goes(self):
        cur, bad = self.env("1-team"), self.env("2-team")
        self.point(self.state, cur)
        base.write(os.path.join(self.state, "layers", "shell-update.json"), json.dumps(
            {"folder": True, "status": "returned", "old": cur, "new": bad}))
        self.assertEqual(self.prune(), ["2-team"])

    def test_one_a_program_runs_from_a_young_part_and_foreign_names_stay(self):
        cur = self.env("2-team")
        self.point(self.state, cur)
        self.env("1-team")
        os.makedirs(os.path.join(self.shells, "3-team.part"))
        os.makedirs(os.path.join(self.shells, "notes"))
        self.assertEqual(self.prune(busy=("1-team",)), [])
        self.now += 2 * 86400
        self.assertEqual(sorted(self.prune()), ["1-team", "3-team.part"])
        self.assertEqual(sorted(os.listdir(self.shells)), ["2-team", "notes"])

    def test_a_removal_cut_short_is_finished_next_time(self):
        cur = self.env("2-team")
        self.point(self.state, cur)
        gone = self.env("1-team")
        os.chmod(gone, 0o755)   # macOS renames a folder only with its own write bit
        os.rename(gone, os.path.join(self.shells, ".1-team.gone-1"))
        self.assertEqual(self.prune(), [".1-team.gone-1"])
        self.assertEqual(os.listdir(self.shells), ["2-team"])

    def test_programs_of_this_user_tell_a_held_one(self):
        if sys.platform == "darwin":   # a copy of a system program is killed there; the live Mac probe of the slice ran
            self.skipTest("macOS kills a copied platform program (an environment's own Python is found: the Mac probe)")
        held, free = self.env("1-team", read_only=False), self.env("2-team", read_only=False)
        prog = os.path.join(held, "runtime", "sleep")
        shutil.copyfile(shutil.which("sleep"), prog)   # not copy2: macOS refuses the system file's flags on a copy
        os.chmod(prog, 0o755)
        p = subprocess.Popen([prog, "30"])  # portable: a copy of the system's sleep, POSIX only (the class)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)   # Popen returns once the program is exec'd: it runs from there already
        self.assertEqual(envs.busy([os.path.realpath(held), os.path.realpath(free)]), {os.path.realpath(held)})

if __name__ == "__main__":
    unittest.main()
