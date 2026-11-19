"""The app's update by the button on a code layer (app spec §4, slice A3): the hub reads releases.json from the
distribution server with the build key, shows a release at once (no delay: the count of hubs standing on it is the
caution, slice A3n), and on "Update"
downloads the layer, checks it against the sum and its manifest, keeps the archive and leaves the layer for the next
full restart — hub-up moves the pointer <state>/code while no part runs and starts again through it. The previous
release's watchdog puts the pointer back when the hub does not come up, and the layer is not offered again; "Return"
takes the previous layer; the first start installs the latest without a button. Real layers from the factory
(tools/package/layer.py) and the real delivery server (deliver/server.py) on a free port of 127.0.0.1."""
import tests  # noqa: F401 - keeps the machine's settings out
import hashlib
import http.server
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

import platforms
from core import appcode, spool
from tests.fakes import cut, stands

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_KEY = "build-key-of-the-test-0001"
RELEASE_KEY = "release-key-of-the-test-01"
HOUR = 3600


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git(cwd, *args):
    import subprocess
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args], cwd=cwd, check=True,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


TREE = {"core/__init__.py": "", "core/version.py": "RUNTIME_MIN = 1\n", "core/x.py": "X = 1\n",
        "bin/hub-up": "print('up')\n", "modules/__init__.py": "", "README.md": "readme\n"}


@cut.needs_deliver()   # its subclasses are skipped with it (the open flavor has no deliver/)
class Stand(unittest.TestCase):
    """A repo with releases, their layers built by the factory, the delivery server over a temporary folder, and an
    app's state folder whose launcher installed the v1.2.3 seed (its shell names the server and the build key)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="hub-app-update-")
        self.addCleanup(platforms.current.rmtree, self.root)
        self.layer = load("hub_test_layer", os.path.join(REPO, "tools", "package", "layer.py"))
        self.launch = load("hub_test_launch", os.path.join(REPO, "tools", "package", "launch.py"))
        self.work = os.path.join(self.root, "repo")
        self.chain = stands.Chain(self.root, "app-update", sys.version_info[:2], stands.source_of(
            os.path.join(REPO, "tools", "package", "layer.py"), os.path.join(REPO, "core", "appcode.py")))
        self.built = {}
        self.release("v1.2.3")
        # the delivery server
        server = load("hub_test_deliver", os.path.join(REPO, "deliver", "server.py"))
        self.srv = server.make(os.path.join(self.root, "deliver"), "127.0.0.1", 0, read=[sha(BUILD_KEY)],
                               write=[sha(RELEASE_KEY)], say=lambda line: None)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()   # a quick shutdown: the default poll is 0.5 s
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.key_file = os.path.join(self.root, "release.key")
        write(self.key_file, RELEASE_KEY)
        os.chmod(self.key_file, 0o600)
        # the shell: PACKAGED and deliver.json next to it
        self.shell = os.path.join(self.root, "shell")
        self.mark = os.path.join(self.shell, "PACKAGED")
        write(self.mark, json.dumps({"format": "app", "layout": "layer", "flavor": "team", "version": "1.2.3",
                                     "runtime": 1}))
        write(os.path.join(self.shell, "deliver.json"), json.dumps({"url": self.url, "key": BUILD_KEY}))
        self.state = os.path.join(self.root, "state")
        os.makedirs(os.path.join(self.state, "spool"))
        self.clock = [1_800_000_000.0]
        self.start(self.built["v1.2.3"]["seed"])
        self.spawned = []

    # --- the factory and the server

    def release(self, tag, tree=None):
        files = {**TREE, "core/x.py": f"X = {tag!r}\n", **(tree or {})}
        out = os.path.join(self.root, "out")
        seed = os.path.join(self.root, f"seed-{tag}")

        def build():   # the repo of the releases, one commit and one tag each, and the layer the factory makes of it
            if not os.path.isdir(os.path.join(self.work, ".git")):
                git(self.root, "init", "-q", "-b", "main", self.work)
            for rel, text in files.items():
                write(os.path.join(self.work, rel), text)
            git(self.work, "add", "-A")
            git(self.work, "commit", "-qm", tag, "--allow-empty")
            git(self.work, "tag", tag)
            return {"sheet": self.layer.build(self.work, tag, out, sys.executable, keep=seed)}
        sheet = self.chain.step(["release", tag, files], ["repo", "out", f"seed-{tag}"], build)["sheet"]
        self.built[tag] = {"sheet": sheet, "seed": seed, "archive": os.path.join(out, sheet["archive"]["name"])}
        return self.built[tag]

    def publish(self, tag, channel="stable", runtime_min=1, notes="- what is new\n"):
        sheet = self.built[tag]["sheet"]
        send = sys.modules["deliver_send"] = load("deliver_send", os.path.join(REPO, "tools", "package", "deliver_send.py"))
        self.addCleanup(sys.modules.pop, "deliver_send", None)
        notes_file = os.path.join(self.root, "notes.txt")
        write(notes_file, notes)
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = send.main([self.url, "--key-file", self.key_file, "layer", self.built[tag]["archive"], "--channel",
                              channel, "--version", sheet["version"], "--commit", sheet["commit"], "--runtime-min",
                              str(runtime_min), "--notes", notes_file, "--send"])
        self.assertEqual(code, 0)
        return sheet

    # --- the app

    def start(self, seed):
        return self.launch.ensure_layer(seed, self.mark, self.state, now=lambda: self.clock[0], run=lambda cmd: None)

    def codes(self):
        return os.path.join(self.state, "layers")

    def current(self):
        return appcode.current(os.path.join(self.state, "code"), self.codes())

    def updater(self, claude="9.9.9", **update):
        from modules.update import layer
        cfg = {"update": {"enabled": True, "channel": "stable", "app": {"runtime": 1, "layer": True}, **update},
               "dev": {"name": "hub-dev"}}
        return layer.LayerUpdater(os.path.join(self.codes(), self.current()), self.state, cfg, claude=lambda: claude,
                                  spawn=lambda *a: self.spawned.append(a), now=lambda: self.clock[0])

    def events(self, kind=None):
        out = []
        for path, item in spool.take(os.path.join(self.state, "spool")):
            os.unlink(path)
            out.append(item)
        return [e for e in out if kind is None or e["type"] == kind]

    def read(self, *rel):
        with open(os.path.join(self.state, *rel)) as f:
            return json.load(f)

    def exists(self, *rel):
        return os.path.exists(os.path.join(self.state, *rel))


class Check(Stand):
    def test_a_stable_release_is_shown_at_once_with_no_delay_in_the_app(self):
        self.release("v1.3.0")
        self.publish("v1.3.0", notes="- a new thing\n- another\n")
        # the schedule, the hub has only just seen it, and the delay setting of a hub from git (24 h) says nothing here
        self.assertEqual(self.updater(delay_hours=48).check(apply=False), "available")
        data = self.events("update.checked")[-1]["data"]
        self.assertTrue(data["app"])
        self.assertNotIn("install_after", data)
        self.assertNotIn("ripe", data)
        self.assertFalse(self.exists("layers", "seen.json"))   # the hub no longer keeps when it first saw a release
        self.assertEqual((data["version"], data["have_version"], data["update_channel"]), ("1.3.0", "1.2.3", "stable"))
        self.assertEqual(data["notes"], ["a new thing", "another"])
        self.assertFalse(self.exists("layers", "1.3.0-team"))   # looking downloads nothing

    def test_check_by_the_owner_shows_the_newest_at_once(self):
        self.release("v1.3.0")
        self.publish("v1.3.0")
        self.assertEqual(self.updater().check(apply=False, manual=True), "available")

    def test_one_channel_even_for_a_hub_set_to_main(self):
        # the owner's decision of 2026-11-09 (m1917882, "yes, only one channel"): every package is a release; a hub moved
        # from git with update.channel main does not wait for nightly packages that never come
        self.release("v1.3.0")
        self.publish("v1.3.0", channel="nightly")
        self.assertEqual(self.updater(channel="main").check(apply=False), "no-release")   # nightly is not read
        self.release("v1.3.1")
        self.publish("v1.3.1")
        self.assertEqual(self.updater(channel="main").check(apply=False), "available")
        data = self.events("update.checked")[-1]["data"]
        self.assertEqual((data["version"], data["update_channel"]), ("1.3.1", "stable"))

    def test_never_down(self):
        self.release("v1.3.0")
        self.start(self.built["v1.3.0"]["seed"])   # a newer installer over the app
        self.assertEqual(self.current(), "1.3.0-team")
        self.publish("v1.2.3")
        self.clock[0] += 48 * HOUR
        self.assertEqual(self.updater().check(apply=False, manual=True), "current")

    def test_a_release_that_needs_a_newer_shell_waits_and_downloads_nothing(self):
        self.release("v1.3.0")
        self.publish("v1.3.0", runtime_min=2)
        self.assertEqual(self.updater().check(apply=True, manual=True), "needs-runtime")
        data = self.events("update.checked")[-1]["data"]
        self.assertEqual((data["need_runtime"], data["have_runtime"]), (2, 1))
        self.assertFalse(os.path.isdir(os.path.join(self.codes(), "archives")) and os.listdir(
            os.path.join(self.codes(), "archives")))

    def test_without_the_server_the_hub_is_offline_not_broken(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.assertEqual(self.updater().check(apply=False), "offline")

    def test_a_shell_without_a_server_has_nothing_to_look_at(self):
        os.unlink(os.path.join(self.codes(), "deliver.json"))
        self.assertEqual(self.updater().check(apply=False), "no-source")

    def test_without_the_build_key_the_server_refuses_and_the_page_keeps_the_apps_buttons(self):
        write(os.path.join(self.codes(), "deliver.json"), json.dumps({"url": self.url, "key": "another-key-0000000"}))
        write(os.path.join(self.codes(), "history.json"), json.dumps([{"name": "1.2.2-team", "version": "1.2.2"}]))
        self.release("v1.3.0")
        self.publish("v1.3.0")
        self.assertEqual(self.updater().check(apply=False, manual=True), "error")
        data = self.events("update.checked")[-1]["data"]
        self.assertIn("401", data["error"])
        self.assertNotIn("another-key", data["error"])
        self.assertEqual((data["app"], data["layer"]), (True, True))   # the page keeps the app's texts and buttons

    def test_a_version_whose_folder_name_would_be_too_long_is_refused_up_front(self):
        u = self.updater()
        u.small = lambda url, private=None: (200, json.dumps({"channels": {"stable": {
            "version": "1." + "9" * 60, "layer": {"url": "/v1/layer/x.tar.gz", "sha256": "0" * 64, "size": 10}}}}).encode())
        self.assertEqual(u.check(apply=True, manual=True), "error")
        self.assertFalse(self.exists("layers", "next.json"))



class Install(Stand):
    def setUp(self):
        super().setUp()
        self.release("v1.3.0")
        self.publish("v1.3.0")

    def test_prune_keeps_the_newest_copy_of_edits_not_the_last_by_name(self):
        # release review 2026-11-09: sorted by name, "1.10.0-…" comes before "1.9.3-…" and today's copy went
        old, new = "1.9.3-team-modified-20261101-120000", "1.10.0-team-modified-20261108-120000"
        for n in (old, new):
            os.makedirs(os.path.join(self.codes(), n, "core"))
        self.updater().prune(self.current())
        self.assertTrue(os.path.isdir(os.path.join(self.codes(), new)))
        self.assertFalse(os.path.isdir(os.path.join(self.codes(), old)))

    def test_prune_keeps_the_way_back_of_a_waiting_trial(self):
        # release review 2026-11-09: a release installed over a build of the person's edits — the trial goes back to the
        # build, which history does not name; a pass of the new release removed it and a rollback pointed at nothing
        back = "1.2.3+local.f25083b97-team"
        os.makedirs(os.path.join(self.codes(), back, "core"))
        with open(os.path.join(self.codes(), back, appcode.MANIFEST), "w") as f:
            json.dump({"version": "1.2.3+local.f25083b97"}, f)
        from modules.update import update as base_mod
        os.makedirs(os.path.dirname(os.path.join(self.state, base_mod.TRIAL)), exist_ok=True)
        with open(os.path.join(self.state, base_mod.TRIAL), "w") as f:
            json.dump({"status": "waiting", "layer": {"from": back, "to": self.current()}}, f)
        self.updater().prune(self.current())
        self.assertTrue(os.path.isdir(os.path.join(self.codes(), back)))
        with open(os.path.join(self.state, base_mod.TRIAL), "w") as f:
            json.dump({"status": "ok", "layer": {"from": back, "to": self.current()}}, f)
        self.updater().prune(self.current())
        self.assertFalse(os.path.isdir(os.path.join(self.codes(), back)))   # the trial is over: an old build goes

    def test_update_downloads_checks_and_leaves_the_layer_for_the_restart(self):
        self.assertEqual(self.updater().check(apply=True, manual=True), "applied")
        self.assertTrue(os.path.isfile(os.path.join(self.codes(), "1.3.0-team", appcode.MANIFEST)))
        self.assertEqual(appcode.diff(os.path.join(self.codes(), "1.3.0-team"),
                                      appcode.load(os.path.join(self.codes(), "1.3.0-team")), full=True), [])
        self.assertTrue(os.path.isfile(os.path.join(self.codes(), "archives", "code-1.3.0-team.tar.gz")))   # for repairs
        self.assertEqual(self.current(), "1.2.3-team")      # the running hub keeps its layer until the restart
        self.assertEqual(self.read("layers", "next.json")["name"], "1.3.0-team")
        trial = self.read("update", "trial.json")
        self.assertEqual((trial["status"], trial["version"], trial["previous"]), ("waiting", "1.3.0", "1.2.3"))
        self.assertEqual((trial["layer"]["from"], trial["layer"]["to"]), ("1.2.3-team", "1.3.0-team"))
        self.assertEqual(len(self.spawned), 1)              # the previous release's watchdog
        self.assertTrue(os.path.isfile(os.path.join(self.state, "update", "trial-run.py")))
        kinds = [e["type"] for e in self.events()]
        self.assertIn("update.applied", kinds)
        self.assertIn("hub.restart_requested", kinds)
        self.assertEqual(self.read("layers", "history.json"), [{"name": "1.2.3-team", "version": "1.2.3"}])
        # the shell's PACKAGED goes along (the launcher writes it only into the layer of its own start): the new code
        # knows it is the app, its environment number and the download address
        self.assertEqual(self.read("layers", "1.3.0-team", "PACKAGED"), self.read("layers", "1.2.3-team", "PACKAGED"))
        # the restart: hub-up moves the pointer while no part runs
        self.assertEqual(appcode.take_next(self.state), "1.3.0-team")
        self.assertEqual(self.current(), "1.3.0-team")
        self.assertFalse(self.exists("layers", "next.json"))

    def test_the_band_sees_the_download_and_the_check_and_nothing_after(self):
        # app spec §4a.2: "Updating to X: downloading 6 of 11 MB" → "checking"; the file goes whatever the end
        from core import fetch
        from modules.update import layer
        seen, real = [], fetch.download
        path = os.path.join(self.codes(), layer.PROGRESS)

        def download(*a, progress=None, **kw):
            def shown(done, total):
                progress(done, total)
                with open(path) as f:
                    seen.append(json.load(f)["phase"])
            return real(*a, progress=shown, **kw)
        up = self.updater()
        up.download = download
        real_unpack = appcode.unpack

        def unpack(*a, **kw):
            with open(path) as f:
                seen.append(json.load(f)["phase"])
            return real_unpack(*a, **kw)
        with mock.patch.object(appcode, "unpack", unpack):
            self.assertEqual(up.check(apply=True, manual=True), "applied")
        self.assertEqual((seen[0], seen[-1]), ("download", "check"), seen)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(layer.Progress(self.codes(), "1.3.0", 10, False).base["first"])

    def test_a_broken_download_leaves_no_progress(self):
        from modules.update import layer
        stored = os.path.join(self.root, "deliver", "files", "layer", "code-1.3.0-team.tar.gz")
        os.chmod(stored, 0o644)
        with open(stored, "r+b") as f:
            f.seek(100)
            f.write(b"\0" * 64)
        self.assertEqual(self.updater().check(apply=True, manual=True), "bad-archive")
        self.assertFalse(os.path.exists(os.path.join(self.codes(), layer.PROGRESS)))

    def test_the_build_key_never_lands_in_a_layer_or_a_record(self):
        self.updater().check(apply=True, manual=True)
        for top in ("update", os.path.join("layers", "1.3.0-team"), os.path.join("layers", "next.json"),
                    os.path.join("layers", "history.json")):
            path = os.path.join(self.state, top)
            for d, _, names in os.walk(path) if os.path.isdir(path) else [(os.path.dirname(path), [], [top.split(os.sep)[-1]])]:
                for n in names:
                    with open(os.path.join(d, n), "rb") as f:
                        self.assertNotIn(BUILD_KEY.encode(), f.read(), os.path.join(d, n))

    def test_a_damaged_download_is_not_installed(self):
        stored = os.path.join(self.root, "deliver", "files", "layer", "code-1.3.0-team.tar.gz")
        os.chmod(stored, 0o644)
        with open(stored, "r+b") as f:
            f.seek(100)
            f.write(b"\0" * 64)
        self.assertEqual(self.updater().check(apply=True, manual=True), "bad-archive")
        self.assertFalse(self.exists("layers", "next.json"))
        self.assertFalse(self.exists("layers", "1.3.0-team"))
        self.assertFalse(self.exists("layers", "archives", "code-1.3.0-team.tar.gz"))
        self.assertEqual(self.current(), "1.2.3-team")

    def own(self, ver):
        """The release's copy as core/ownclaude leaves it in the hub's folder. -> its path."""
        from core import ownclaude
        path = ownclaude.path(self.state, ver)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
        return path

    def test_a_layers_claude_is_downloaded_into_the_hubs_folder_first(self):
        # owner 2026-11-13 (m2261407): the update downloads the Claude Code its release runs, without a model, before the
        # switch; whatever claude the person has is not asked
        self.release("v1.4.0", {"core/version.py": 'RUNTIME_MIN = 1\nCLAUDE_TESTED = "3.0.0"\nMIN_CLAUDE = CLAUDE_TESTED\n'})
        self.publish("v1.4.0")
        put = []
        u = self.updater(claude="9.9.9")
        u.claude_install = lambda state, need: put.append((state, need, self.exists("layers", "next.json"))) or self.own(need)
        self.assertEqual(u.check(apply=True, manual=True), "applied")
        self.assertEqual(put, [(self.state, "3.0.0", False)])   # before the switch
        self.assertEqual(self.events("update.checked")[-1]["data"]["claude_installed"], "3.0.0")

    def test_a_release_before_the_folder_downloads_nothing(self):
        # a layer whose core/version.py names MIN_CLAUDE only runs claude from PATH: nothing to download for it
        self.release("v1.4.0", {"core/version.py": 'RUNTIME_MIN = 1\nMIN_CLAUDE = "3.0.0"\n'})
        self.publish("v1.4.0")
        u = self.updater(claude="2.1.0")
        u.claude_install = lambda *a: self.fail("downloaded")
        self.assertEqual(u.check(apply=True, manual=True), "applied")

    def test_a_hub_on_its_layer_downloads_its_claude(self):
        # the layer is current and its own copy is not there (an updater of before the folder took it, or the download
        # failed): the check downloads it, as a hub from git does (modules/update/update.py _claude_own)
        self.release("v1.4.0", {"core/version.py": 'RUNTIME_MIN = 1\nCLAUDE_TESTED = "3.0.0"\nMIN_CLAUDE = CLAUDE_TESTED\n'})
        self.publish("v1.4.0")
        u = self.updater(claude=None)
        u.claude_install = lambda state, need: self.own(need)
        self.assertEqual(u.check(apply=True, manual=True), "applied")
        self.assertEqual(appcode.take_next(self.state), "1.4.0-team")   # the restart
        import platforms
        platforms.current.rmtree(os.path.join(self.state, "claude-code"))
        put = []
        u = self.updater(claude="2.1.0")
        u.claude_install = lambda state, need: put.append(need) or self.own(need)
        self.assertEqual(u.check(), "current")
        self.assertEqual(put, ["3.0.0"])
        self.assertEqual(self.events("update.checked")[-1]["data"]["claude_installed"], "3.0.0")

    def test_a_failed_claude_download_holds_the_layer(self):
        # spec d2263524, decision R3: a release's sessions run its own copy — the layer waits, unpacked, for the next pass
        from core import claudeinstall
        from modules.update import update as mod
        self.release("v1.4.0", {"core/version.py": 'RUNTIME_MIN = 1\nCLAUDE_TESTED = "3.0.0"\nMIN_CLAUDE = CLAUDE_TESTED\n'})
        self.publish("v1.4.0")
        u = self.updater(claude="2.1.0")
        u.claude_install = mock.Mock(side_effect=claudeinstall.Failed("signature", "the manifest's signature is not valid"))
        self.assertEqual(u.check(apply=True, manual=True), "claude-wait")
        self.assertFalse(self.exists("layers", "next.json"))
        self.assertEqual(self.current(), "1.2.3-team")
        evs = self.events()
        (line,) = [e for e in evs if e["data"].get("id") == mod.CLAUDE_NOTICE]
        self.assertIn("signature", line["data"]["text"])
        self.assertIn("signature", [e for e in evs if e["type"] == "update.checked"][-1]["data"]["claude_failed"])
        u = self.updater(claude="2.1.0")   # `hub update` again, the download goes through: the layer goes in
        u.claude_install = lambda state, need: self.own(need)
        self.assertEqual(u.check(apply=True, manual=True), "applied")
        self.assertTrue(self.exists("layers", "next.json"))

    def test_the_next_update_runs_on_the_updated_layer(self):
        self.updater().check(apply=True, manual=True)
        appcode.take_next(self.state)
        self.release("v1.3.1")
        self.publish("v1.3.1")
        from core import packaged
        from unittest import mock
        with mock.patch.object(packaged, "MARK", os.path.join(self.codes(), "1.3.0-team", "PACKAGED")):
            cfg = packaged.apply({"update": {}})
        from modules.update import layer
        u = layer.LayerUpdater(os.path.join(self.codes(), "1.3.0-team"), self.state, cfg, claude=lambda: "9.9.9",
                               spawn=lambda *a: None, now=lambda: self.clock[0])
        self.assertEqual(u.check(apply=True, manual=True), "applied")

    def test_a_background_install_asks_the_person_to_restart_and_restarts_nothing(self):
        # the owner's decision of 2026-11-09 (m1917410): the schedule installs, the person is told "restart the hub"
        self.assertEqual(self.updater().check(apply=True, manual=False), "applied")
        self.assertEqual(self.read("layers", "next.json")["name"], "1.3.0-team")
        self.assertEqual(len(self.spawned), 1)                     # the previous release's watchdog, as for a press
        got = self.events()
        self.assertNotIn("hub.restart_requested", [e["type"] for e in got])
        ready = [e["data"] for e in got if e["type"] == "owner.notify" and e["data"].get("ready")]
        self.assertEqual(len(ready), 1)
        self.assertEqual((ready[0]["id"], ready[0]["ready"]), ("update-ready", {"version": "1.3.0"}))
        self.assertIn("restart the hub", ready[0]["text"])
        self.assertIn("what is new", ready[0]["text"])
        checked = [e["data"] for e in got if e["type"] == "update.checked"]
        self.assertIs(checked[-1].get("ask_restart"), True)          # the page's "Restart"
        self.assertEqual(self.updater().check(apply=True, manual=False), "restarting")   # waits: nothing again
        self.assertIs(self.events("update.checked")[-1]["data"].get("ask_restart"), True)
        self.release("v1.3.1")                                     # a newer one while it waits: it takes its place
        self.publish("v1.3.1")
        self.assertEqual(self.updater().check(apply=True, manual=False), "applied")
        self.assertEqual(self.read("layers", "next.json")["name"], "1.3.1-team")
        self.assertEqual(self.read("layers", "history.json"), [{"name": "1.2.3-team", "version": "1.2.3"}])
        ready = [e["data"] for e in self.events("owner.notify") if e["data"].get("ready")]
        # the same notice id: Telegram edits the message instead of a second one
        self.assertEqual([(r["id"], r["ready"]["version"]) for r in ready], [("update-ready", "1.3.1")])
        self.assertEqual(appcode.take_next(self.state), "1.3.1-team")

    def test_the_owners_own_install_restarts_by_itself(self):
        self.assertEqual(self.updater().check(apply=True, manual=True, restart=True), "applied")
        got = self.events()
        self.assertIn("hub.restart_requested", [e["type"] for e in got])
        self.assertFalse([e for e in got if e["type"] == "owner.notify" and e["data"].get("ready")])
        self.assertNotIn("ask_restart", [e for e in got if e["type"] == "update.checked"][-1]["data"])   # queued already
        self.assertEqual(self.updater().check(apply=True, manual=False), "restarting")
        self.assertNotIn("ask_restart", self.events("update.checked")[-1]["data"])

    def test_check_does_not_install_a_release_the_owner_returned(self):
        # review: "Check" on a layer installs as the schedule does — a returned release stays out (failed.json, owner)
        write(os.path.join(self.codes(), "failed.json"), json.dumps({"1.3.0-team": {"version": "1.3.0", "why": "owner"}}))
        from modules.update import update as mod
        apply, asks = mod.plan("check", False, True, app=True, layered=True)
        self.assertEqual(self.updater().check(apply=apply, manual=asks), "current")
        self.assertFalse(self.exists("layers", "next.json"))

    def test_update_manual_the_schedule_and_check_only_look_and_the_button_installs(self):
        # update.manual: a pass of the schedule names the version and installs nothing; "Update" / `hub update` installs
        from modules.update import update as mod

        def run(what, pressed, looked, **update):
            u = self.updater(**update)
            apply, asks = mod.plan(what, pressed, looked, app=True, layered=True, button=mod.button_only(u.cfg))
            return u.check(apply=apply, manual=asks, restart=pressed)

        self.assertEqual(run("install", False, False, manual=True), "available")   # the schedule
        self.assertEqual(run("check", False, True, manual=True), "available")      # "Check for updates"
        self.assertFalse(self.exists("layers", "next.json"))
        self.assertEqual(self.events("update.checked")[-1]["data"]["version"], "1.3.0")
        self.assertEqual(run("install", True, False, manual=True), "applied")      # the button
        self.assertEqual(self.read("layers", "next.json")["name"], "1.3.0-team")
        self.assertTrue(self.events("hub.restart_requested"))

    def test_without_update_manual_the_schedule_installs_in_the_background(self):
        from modules.update import update as mod
        for given in ({}, {"manual": False}, {"manual": "true"}, {"manual": 1}):   # only a literal true holds back
            u = self.updater(**given)
            apply, asks = mod.plan("install", False, False, app=True, layered=True, button=mod.button_only(u.cfg))
            self.assertEqual((apply, asks), (True, False), given)

    def test_the_owners_install_of_a_release_waiting_in_the_background_queues_its_restart(self):
        self.assertEqual(self.updater().check(apply=True, manual=False), "applied")
        self.events()
        self.assertEqual(self.updater().check(apply=True, manual=True, restart=True), "restarting")
        self.assertEqual(len(self.events("hub.restart_requested")), 1)
        self.assertNotIn("ask", self.read("layers", "next.json"))

    def test_a_second_press_before_the_restart_does_not_install_again(self):
        self.updater().check(apply=True, manual=True)
        self.spawned.clear()
        self.assertEqual(self.updater().check(apply=True, manual=True), "restarting")
        self.assertEqual(self.spawned, [])

    def test_a_layer_that_did_not_come_up_is_not_offered_again_and_a_newer_one_is(self):
        write(os.path.join(self.codes(), "failed.json"), json.dumps({"1.3.0-team": {"version": "1.3.0", "why": "timeout"}}))
        self.clock[0] += 48 * HOUR
        self.assertEqual(self.updater().check(apply=False), "current")
        self.assertEqual(self.updater().check(apply=True, manual=True), "current")   # no button for it, also on "Check"
        write(os.path.join(self.codes(), "failed.json"), json.dumps({"1.3.0-team": {"version": "1.3.0", "why": "owner"}}))
        self.assertEqual(self.updater().check(apply=False), "current")               # returned: not by the schedule
        self.assertEqual(self.updater().check(apply=False, manual=True), "available")   # his own press still may
        self.assertEqual(self.events("update.checked")[-1]["data"].get("version"), "1.3.0")
        self.release("v1.3.1")
        self.publish("v1.3.1")
        self.assertEqual(self.updater().check(apply=False), "available")   # a newer one: at once, with its own count

    def test_old_layers_and_their_archives_go_the_history_stays(self):
        for tag in ("v1.3.1", "v1.3.2"):
            self.updater().check(apply=True, manual=True)
            appcode.take_next(self.state)
            self.release(tag)
            self.publish(tag)
        self.updater().check(apply=True, manual=True)
        appcode.take_next(self.state)
        self.updater().check(apply=False)   # a pass prunes
        names = sorted(n for n in os.listdir(self.codes()) if os.path.isfile(os.path.join(self.codes(), n, appcode.MANIFEST)))
        self.assertEqual(names, ["1.3.0-team", "1.3.1-team", "1.3.2-team"])   # current + two previous
        archives = sorted(os.listdir(os.path.join(self.codes(), "archives")))
        self.assertEqual(archives, ["code-1.3.0-team.tar.gz", "code-1.3.1-team.tar.gz", "code-1.3.2-team.tar.gz"])


class Restart(Stand):
    """hub-up at its full restart: the pointer onto the layer left for it, and starting again through the pointer."""

    def setUp(self):
        super().setUp()
        self.release("v1.3.0")
        self.publish("v1.3.0")
        self.updater().check(apply=True, manual=True)

    def test_hub_up_starts_again_through_the_pointer_not_its_own_folder(self):
        old = os.path.join(self.codes(), "1.2.3-team")
        got = appcode.restart_script(self.state, old, os.path.join(old, "bin", "hub-up"))
        self.assertEqual(got, os.path.join(self.state, "code", "bin", "hub-up"))
        appcode.take_next(self.state)
        self.assertEqual(os.path.realpath(got), os.path.realpath(os.path.join(self.codes(), "1.3.0-team", "bin", "hub-up")))

    def test_a_hub_from_git_starts_again_by_its_own_file(self):
        script = os.path.join(REPO, "bin", "hub-up")
        self.assertEqual(appcode.restart_script(self.state, REPO, script), script)

    def test_hub_up_switches_the_layer_and_asks_for_the_migration(self):
        up = self.hub_up()
        up.next_layer()
        self.assertEqual(self.current(), "1.3.0-team")
        self.assertTrue(self.exists("migrate.pending"))
        self.assertEqual(up.self_script(), os.path.join(self.state, "code", "bin", "hub-up"))

    def test_hub_up_starts_the_watchdog_of_a_switch_nobody_watches(self):
        # review of the audit fix: the watchdog left a switch untaken for a month — the restart that takes it starts
        # one before any code of the new release runs (a new hub-up that crashes never gets to the update process)
        up = self.hub_up()
        up.next_layer()
        self.assertEqual([c[-2:] for c in self.watchers], [[self.state, "--resume"]])
        self.assertTrue(self.watchers[0][1].endswith(os.path.join("update", "trial-run.py")))

    def test_hub_up_leaves_a_watched_switch_and_another_layers_trial_alone(self):
        t = self.read("update", "trial.json")
        write(os.path.join(self.state, "update", "trial.json"), json.dumps({**t, "pid": os.getpid()}))
        self.hub_up().next_layer()                                     # its watchdog is alive
        nxt = os.path.join(self.codes(), "next.json")
        write(nxt, json.dumps({"name": "1.2.3-team", "why": "return"}))
        write(os.path.join(self.state, "update", "trial.json"), json.dumps({**t, "pid": None}))
        self.hub_up().next_layer()                                     # a return: the trial is of another layer
        self.assertEqual(self.watchers, [])

    def hub_up(self):
        """bin/hub-up loaded as a module of a layer of this state folder (it sets umask, os.environ, sys.path)."""
        mask = os.umask(0o077)
        os.umask(mask)
        env, path = dict(os.environ), list(sys.path)
        self.addCleanup(os.umask, mask)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(env)))
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), path))
        from importlib.machinery import SourceFileLoader
        loader = SourceFileLoader("hub_test_hub_up", os.path.join(REPO, "bin", "hub-up"))
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("hub_test_hub_up", loader))
        loader.exec_module(mod)
        layer = os.path.join(self.codes(), "1.2.3-team")
        mod.REPO, mod.__file__ = layer, os.path.join(layer, "bin", "hub-up")
        mod.default_state_dir = lambda: self.state
        mod.say = lambda text: None
        self.watchers = []
        mod.spawn_watchdog = self.watchers.append
        return mod

    def test_the_launcher_takes_a_switch_left_by_a_restart_that_did_not_happen(self):
        self.assertEqual(self.current(), "1.2.3-team")
        self.start(self.built["v1.2.3"]["seed"])   # the machine rebooted before the restart
        self.assertEqual(self.current(), "1.3.0-team")

    def test_the_launchers_switch_of_an_update_asks_for_the_migration(self):
        self.start(self.built["v1.2.3"]["seed"])
        self.assertTrue(self.exists("migrate.pending"))

    def test_the_launcher_beside_a_running_hub_leaves_the_switch_to_its_restart(self):
        write(os.path.join(self.state, "hub-up.json"), json.dumps({"core": os.getpid()}))
        self.start(self.built["v1.2.3"]["seed"])
        self.assertEqual(self.current(), "1.2.3-team")
        self.assertTrue(self.exists("layers", "next.json"))


class Watchdog(Stand):
    """The previous release's watchdog (modules/update/trial.py) on a layer."""

    def setUp(self):
        super().setUp()
        self.release("v1.3.0")
        self.publish("v1.3.0")
        self.updater().check(apply=True, manual=True)
        self.events()
        from modules.update import trial
        self.trial = trial

    def test_the_hub_is_up_when_the_page_answers_from_the_new_layer(self):
        t = self.read("update", "trial.json")
        later = t["started"] + 5
        old = {"core_started": later, "web_started": later, "code": {"running": t["from"][:9], "layer": "1.2.3-team"}}
        new = {"core_started": later, "web_started": later, "code": {"running": t["to"][:9], "layer": "1.3.0-team"}}
        self.assertEqual(self.trial.decide(t, old, None, later), "wait")
        self.assertEqual(self.trial.decide(t, new, None, later), "ok")

    def test_a_layer_that_does_not_come_up_goes_back_and_is_written_up_by_the_previous_code(self):
        appcode.take_next(self.state)            # the restart happened, the new layer does not come up
        t = self.read("update", "trial.json")
        self.assertEqual(self.trial.roll_layer(self.state, t, "timeout"), "rolled-back")
        self.assertEqual(self.current(), "1.2.3-team")
        self.assertIn("1.3.0-team", self.read("layers", "failed.json"))
        self.assertEqual(self.read("update", "trial.json")["status"], "rolled-back")
        u = self.updater()
        u.recover()                               # the previous code's update process at its start
        failed = self.events("update.failed")
        self.assertEqual((failed[-1]["data"]["why"], failed[-1]["data"]["by"]), ("rolled-back", "timeout"))
        self.assertFalse(self.exists("update", "rolledback.json"))
        self.clock[0] += 48 * HOUR
        self.assertEqual(self.updater().check(apply=False), "current")   # not offered again

    def run_watchdog(self, about, at=None):
        """The watchdog's loop on the test's clock: each sleep moves it; about — the page's answer at that moment;
        at — {seconds after the install: what happens then} (the restart that takes the switch, say)."""
        from unittest import mock
        start = self.read("update", "trial.json")["started"]
        clock, todo = [start], dict(at or {})

        def sleep(s):
            clock[0] += s
            for when in sorted(t for t in todo if clock[0] >= start + t):
                todo.pop(when)()
        with mock.patch.object(self.trial, "restart_hub", lambda *a, **k: "stopped"):
            return self.trial.run(self.state, now=lambda: clock[0], sleep=sleep, about=lambda port: about(clock[0]))

    def without_core(self):
        t = self.read("update", "trial.json")
        t["core_pid"] = None   # hub-up.json named no core at the install (review 02.11)
        write(os.path.join(self.state, "update", "trial.json"), json.dumps(t))
        return t

    def answers(self, t, layer, after):
        page = {"core_started": t["started"] + after, "web_started": t["started"] + after,
                "code": {"running": t["to" if layer == "1.3.0-team" else "from"][:9], "layer": layer}}
        return lambda now: page if now >= t["started"] + after else None

    def test_a_switch_waiting_for_the_persons_restart_is_never_counted_or_given_up(self):
        # a background install: three days without the restart, a core that restarts on the running release (a crash,
        # `hub restart core`) — no clock, no give-up (review: it took back releases that never ran); the restart takes
        # the switch, the hub comes up on the new layer: "ok"
        t = self.read("update", "trial.json")
        old = {"core_started": t["started"] + 60, "web_started": t["started"] + 60,
               "code": {"running": t["from"][:9], "layer": "1.2.3-team"}}
        new = self.answers(t, "1.3.0-team", 3 * 86400 + 20)
        self.assertEqual(self.run_watchdog(lambda now: new(now) or old,
                                           at={3 * 86400: lambda: appcode.take_next(self.state)}), "ok")
        self.assertEqual(self.read("update", "trial.json")["status"], "ok")

    def test_a_switch_taken_after_the_watchdog_left_still_gets_its_trial(self):
        # audit: the switch waited past SWITCH_GIVE_UP — the watchdog leaves, the trial stays open; the restart that
        # takes the switch at last gets a watchdog again (the update process at its start) and a release that does not
        # come up goes back
        from unittest import mock
        t = self.read("update", "trial.json")
        old = self.answers(t, "1.2.3-team", 10)
        self.run_watchdog(old)
        self.assertEqual(self.read("update", "trial.json")["status"], "waiting")
        appcode.take_next(self.state)              # the restart, a month on
        self.spawned.clear()
        self.updater().recover()
        self.assertEqual(self.spawned[-1][-1], "--resume")
        clock = [self.clock[0]]
        with mock.patch.object(self.trial, "restart_hub", lambda *a, **k: "stopped"):
            got = self.trial.run(self.state, resume=True, now=lambda: clock[0],
                                 sleep=lambda s: clock.__setitem__(0, clock[0] + s), about=lambda port: None)
        self.assertEqual(got, "rolled-back")
        self.assertEqual(self.current(), "1.2.3-team")

    def test_a_switch_that_goes_without_being_taken_ends_the_watchdog_with_no_rollback(self):
        # review: next.json gone (a return, a switch hub-up refused) while the pointer stays — nothing new ran
        t = self.without_core()
        old = self.answers(t, "1.2.3-team", 10)   # the core restarts on the running release meanwhile
        nxt = os.path.join(self.codes(), "next.json")
        self.assertEqual(self.run_watchdog(old, at={60: lambda: os.unlink(nxt)}), "dropped")
        self.assertFalse(self.exists("layers", "failed.json") and "1.3.0-team" in self.read("layers", "failed.json"))
        self.assertEqual(self.current(), "1.2.3-team")

    def test_with_no_core_known_a_restart_that_does_not_come_up_on_the_new_layer_is_taken_back(self):
        t = self.without_core()
        old = self.answers(t, "1.2.3-team", 10)
        self.assertEqual(self.run_watchdog(old, at={5: lambda: appcode.take_next(self.state)}), "rolled-back")
        self.assertIn("1.3.0-team", self.read("layers", "failed.json"))

    def test_on_windows_a_rollback_with_no_hub_up_left_runs_the_hubs_task(self):
        # a new hub-up that crashes at once: the scheduler task does not start it again by itself (edge of slice A3)
        from unittest import mock
        ran = []
        trial = {"task": "Agents Hub"}
        run = lambda argv, **kw: ran.append(argv) or mock.Mock(returncode=0)   # noqa: E731
        with mock.patch.object(self.trial, "WINDOWS", True):
            self.assertTrue(self.trial.start_hub(trial, run=run))
            self.assertFalse(self.trial.start_hub({}, run=run))                    # no task named: nothing to run
        with mock.patch.object(self.trial, "WINDOWS", False):
            self.assertFalse(self.trial.start_hub(trial, run=run))                 # a service brings hub-up up itself
        self.assertEqual(ran, [["schtasks", "/Run", "/TN", "Agents Hub"]])

    def test_a_restart_that_never_switched_is_taken_back_too(self):
        t = self.read("update", "trial.json")      # the pointer still on the previous layer, the switch left over
        self.assertEqual(self.trial.roll_layer(self.state, t, "timeout"), "rolled-back")
        self.assertFalse(self.exists("layers", "next.json"))
        self.assertEqual(self.current(), "1.2.3-team")

    def test_a_pointer_someone_else_moved_is_left_alone(self):
        self.release("v1.4.0")
        appcode.install(self.built["v1.4.0"]["seed"], self.codes())
        appcode.point(os.path.join(self.state, "code"), self.codes(), "1.4.0-team")
        t = self.read("update", "trial.json")
        self.assertEqual(self.trial.roll_layer(self.state, t, "timeout"), "moved")
        self.assertEqual(self.current(), "1.4.0-team")


class Return(Stand):
    def test_return_takes_the_previous_layer_at_the_restart_and_the_returned_one_is_not_offered(self):
        self.release("v1.3.0")
        self.publish("v1.3.0")
        self.updater().check(apply=True, manual=True)
        appcode.take_next(self.state)
        self.events()
        u = self.updater()
        u.check(apply=False)
        self.assertEqual(self.events("update.checked")[-1]["data"]["previous"], "1.2.3")
        self.assertEqual(self.updater().roll_back("1.2.3"), "rolled-back")
        self.assertEqual(self.read("layers", "next.json")["name"], "1.2.3-team")
        kinds = [e["type"] for e in self.events()]
        self.assertIn("update.failed", kinds)
        self.assertIn("hub.restart_requested", kinds)
        appcode.take_next(self.state)
        self.assertEqual(self.current(), "1.2.3-team")
        self.clock[0] += 48 * HOUR
        self.assertEqual(self.updater().check(apply=False), "current")

    def test_a_return_whose_switch_cannot_be_written_leaves_the_watchdog_on_guard(self):
        # review 02.11: the trial was marked "rolled-back" before the switch back was written — a failed write left the
        # hub on the release it was leaving with no watchdog
        self.release("v1.3.0")
        self.publish("v1.3.0")
        self.updater().check(apply=True, manual=True)
        appcode.take_next(self.state)
        u = self.updater()
        real = u._layers_write

        def write_next(name, data):
            if name == appcode.NEXT:
                raise OSError("disk full")
            return real(name, data)
        u._layers_write = write_next
        with self.assertRaises(OSError):
            u.roll_back("1.2.3")
        self.assertEqual(self.read("update", "trial.json")["status"], "waiting")

    def test_return_before_the_restart_cancels_the_switch_and_keeps_the_running_layer(self):
        self.release("v1.3.0")
        self.publish("v1.3.0")
        self.updater().check(apply=True, manual=True)
        self.events()
        self.updater().check(apply=False)   # the page while the restart waits: no "Return" to the running layer itself
        self.assertNotIn("previous", self.events("update.checked")[-1]["data"])
        self.assertEqual(self.updater().roll_back(""), "cancelled")
        self.assertFalse(self.exists("layers", "next.json"))
        self.assertEqual(self.current(), "1.2.3-team")
        failed = self.read("layers", "failed.json")
        self.assertNotIn("1.2.3-team", failed)               # the running layer is not "did not come up"
        self.assertIn("1.3.0-team", failed)                  # the cancelled one is not offered again by the schedule
        self.assertEqual(self.read("update", "trial.json")["status"], "rolled-back")   # its watchdog leaves
        self.assertEqual(self.read("layers", "history.json"), [])
        self.assertTrue(self.events("owner.notify"))
        self.assertEqual(appcode.take_next(self.state), None)

    def test_a_cancel_keeps_the_history_the_install_trimmed_and_names_another_version_nowhere(self):
        full = [{"name": "1.2.1-team", "version": "1.2.1"}, {"name": "1.2.2-team", "version": "1.2.2"}]
        write(os.path.join(self.codes(), "history.json"), json.dumps(full))
        self.release("v1.3.0")
        self.publish("v1.3.0")
        self.updater().check(apply=True, manual=True)
        self.assertEqual(self.updater().roll_back("1.2.2"), "moved")   # an old tab's "Return 1.2.2": not a cancel
        self.assertTrue(self.exists("layers", "next.json"))
        self.assertEqual(self.updater().roll_back("1.2.3"), "cancelled")
        self.assertEqual(self.read("layers", "history.json"), full)
        data = [e for e in self.events("update.checked")][-1]["data"]
        self.assertEqual((data["have_version"], data["update_channel"]), ("1.2.3", "stable"))

    def test_a_switch_carries_the_shells_packaged_to_a_layer_without_one(self):
        self.release("v1.3.0")
        appcode.install(self.built["v1.3.0"]["seed"], self.codes())
        write(os.path.join(self.codes(), "next.json"), json.dumps({"name": "1.3.0-team", "why": "return"}))
        appcode.take_next(self.state)
        self.assertEqual(self.read("layers", "1.3.0-team", "PACKAGED"), self.read("layers", "1.2.3-team", "PACKAGED"))

    def test_return_with_a_v_as_the_help_writes_it(self):
        self.release("v1.3.0")
        self.publish("v1.3.0")
        self.updater().check(apply=True, manual=True)
        appcode.take_next(self.state)
        self.assertEqual(self.updater().roll_back("v1.2.3"), "rolled-back")

    def test_a_return_does_not_run_the_older_codes_migration(self):
        self.release("v1.3.0")
        self.publish("v1.3.0")
        self.updater().check(apply=True, manual=True)
        self.assertEqual(appcode.pending(self.state)["why"], "update")
        appcode.take_next(self.state)
        self.updater().roll_back("1.2.3")
        self.assertEqual(appcode.pending(self.state)["why"], "return")

    def test_return_of_another_version_than_the_button_named_goes_nowhere(self):
        self.release("v1.3.0")
        self.publish("v1.3.0")
        self.updater().check(apply=True, manual=True)
        appcode.take_next(self.state)
        self.assertEqual(self.updater().roll_back("1.1.0"), "moved")
        self.assertFalse(self.exists("layers", "next.json"))


class FirstStart(Stand):
    def test_the_first_start_installs_the_latest_layer_without_a_button(self):
        from modules.update import layer
        self.assertTrue(layer.fresh(self.state))   # the launcher's mark of the very first start
        self.release("v1.3.0")
        self.publish("v1.3.0")
        self.assertEqual(layer.first_start(self.updater()), "applied")
        self.assertFalse(layer.fresh(self.state))
        self.assertEqual(self.read("layers", "next.json")["name"], "1.3.0-team")

    def test_without_the_server_the_first_start_tries_again(self):
        from modules.update import layer
        self.srv.shutdown()
        self.srv.server_close()
        self.assertEqual(layer.first_start(self.updater()), "offline")
        self.assertTrue(layer.fresh(self.state))

    def test_the_first_starts_tries_space_out(self):
        from modules.update import layer
        self.assertEqual([layer.first_wait(n) for n in (0, 1, 2, 3, 20)], [0, 60, 120, 240, 6 * HOUR])

    def test_a_later_start_is_not_a_first_one(self):
        from modules.update import layer
        os.unlink(os.path.join(self.codes(), "fresh.json"))
        self.start(self.built["v1.2.3"]["seed"])
        self.assertFalse(layer.fresh(self.state))


class Key(unittest.TestCase):
    """core/fetch: the build key goes to the distribution server's host only, never after a redirect elsewhere."""

    def serve(self, handler):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    def test_a_redirect_to_another_host_goes_without_the_key(self):
        seen = []

        class Other(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        other = self.serve(Other)

        class Ours(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(302)
                self.send_header("Location", other + "/x")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        ours = self.serve(Ours)
        from core import fetch
        status, _ = fetch.small(ours + "/v1/releases.json", private={"Authorization": "Bearer " + BUILD_KEY})
        self.assertEqual(status, 200)
        self.assertEqual(seen, ["Bearer " + BUILD_KEY, None])


    def test_a_post_carries_the_json_and_the_key_and_a_redirect_is_not_followed(self):
        seen = []

        class Ours(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, self.headers.get("Authorization"), self.headers.get("Content-Type"),
                             self.rfile.read(int(self.headers["Content-Length"]))))
                code = 307 if self.path == "/moved" else 200
                self.send_response(code)
                self.send_header("Location", "http://127.0.0.1:9/elsewhere")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        ours = self.serve(Ours)
        from core import fetch
        private = {"Authorization": "Bearer " + BUILD_KEY}
        self.assertEqual(fetch.post(ours + "/v1/stand", {"id": "a" * 32, "version": None}, private=private), (200, b"{}"))
        self.assertEqual(fetch.post(ours + "/moved", {}, private=private)[0], 307)   # an answer, not followed
        self.assertEqual(seen[0], ("/v1/stand", "Bearer " + BUILD_KEY, "application/json",
                                   json.dumps({"id": "a" * 32, "version": None}).encode()))
        self.assertEqual(len(seen), 2)
        with self.assertRaises(fetch.Failed) as caught:
            fetch.post("http://127.0.0.1:9/x", {}, private=private)
        self.assertEqual(caught.exception.why, "network")


class OpenStand(unittest.TestCase):
    """The open build: its releases.json and layers on static pages read with no key (tools/package/pages.py writes
    them), its environments assets of its GitHub releases behind a redirect to the files' storage (tests/fakes/pages.py:
    pages, github, files on 127.0.0.1 — a stand: the rule of core/fetch.asset_ok lets http to this machine there)."""

    PLATFORM = "linux-x64"

    def setUp(self):
        from tests.fakes import pages as places
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="hub-open-update-"))
        self.addCleanup(platforms.current.rmtree, self.root)
        self.layer = load("hub_test_layer_o", os.path.join(REPO, "tools", "package", "layer.py"))
        self.launch = load("hub_test_launch_o", os.path.join(REPO, "tools", "package", "launch.py"))
        self.pages_mod = load("hub_test_pages_o", os.path.join(REPO, "tools", "package", "pages.py"))
        self.work = os.path.join(self.root, "repo")
        self.out = os.path.join(self.root, "out")
        self.built = {}
        self.pages_root = os.path.join(self.root, "pages")
        self.files_root = os.path.join(self.root, "files")
        os.makedirs(self.files_root)
        self.pages, self.github, self.files, self.asked = places.places(self, self.pages_root, self.files_root)
        self.url = self.pages.url + "/agents-hub"
        self.shells = {}
        self.release("v1.2.3")
        self.shell = os.path.join(self.root, "shell")
        self.mark = os.path.join(self.shell, "PACKAGED")
        write(self.mark, json.dumps({"format": "app", "layout": "layer", "flavor": "open", "version": "1.2.3",
                                     "runtime": 1, "platform": self.PLATFORM}))
        self.state = os.path.join(self.root, "state")
        os.makedirs(os.path.join(self.state, "spool"))
        self.clock = [1_800_000_000.0]
        self.launch.ensure_layer(self.built["v1.2.3"]["seed"], self.mark, self.state, now=lambda: self.clock[0],
                                 run=lambda cmd: None, own_deliver=True)
        # what the open line's setup.py keeps: the pages' address, no key
        write(os.path.join(self.codes(), "deliver.json"), json.dumps({"url": self.url}))
        self.spawned = []

    def release(self, tag, tree=None):
        files = {**TREE, "core/x.py": f"X = {tag!r}\n", **(tree or {})}
        if not os.path.isdir(os.path.join(self.work, ".git")):
            git(self.root, "init", "-q", "-b", "main", self.work)
        for rel, text in files.items():
            write(os.path.join(self.work, rel), text)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", tag, "--allow-empty")
        git(self.work, "tag", tag)
        seed = os.path.join(self.root, f"seed-{tag}")
        sheet = self.layer.build(self.work, tag, self.out, sys.executable, flavor="open", keep=seed)
        self.built[tag] = {"sheet": sheet, "seed": seed, "archive": os.path.join(self.out, sheet["archive"]["name"])}
        return self.built[tag]

    def publish(self, tag, runtime_min=1, notes="- what is new\n"):
        sheet = dict(self.built[tag]["sheet"], runtime_min=runtime_min)
        dst = os.path.join(self.pages_root, "v1", "layer", sheet["archive"]["name"])
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        import shutil
        shutil.copyfile(self.built[tag]["archive"], dst)
        self.doc = self.pages_mod.releases(sheet, self.shells, self.github.url + "/acme/agents-hub/releases/download/"
                                           + tag, notes, now=self.clock[0])
        write(os.path.join(self.pages_root, "v1", "releases.json"), json.dumps(self.doc, indent=1))
        return sheet

    def publish_shell(self, path, runtime, tag):
        """An environment archive as an asset of the release of tag: github redirects its address to the files."""
        import shutil
        name = os.path.basename(path)
        shutil.copyfile(path, os.path.join(self.files_root, name))
        self.github.redirects[f"/acme/agents-hub/releases/download/{tag}/{name}"] = \
            f"{self.files.url}/{name}?sig=a%2Bb&se=2026-11-11T10%3A00%3A00Z"
        self.shells[self.PLATFORM] = {"archive": {"name": name, "sha256": appcode.sha256(path),
                                                  "size": os.path.getsize(path)}, "runtime": runtime}

    def codes(self):
        return os.path.join(self.state, "layers")

    def current(self):
        return appcode.current(os.path.join(self.state, "code"), self.codes())

    def updater(self, claude="9.9.9", run=None, **update):
        import subprocess
        from modules.update import layer
        cfg = {"update": {"enabled": True, "channel": "stable", "app": {"runtime": 1, "layer": True, "flavor": "open",
                                                                         "platform": self.PLATFORM}, **update},
               "dev": {"name": "hub-dev"}}
        return layer.LayerUpdater(os.path.join(self.codes(), self.current()), self.state, cfg, claude=lambda: claude,
                                  spawn=lambda *a: self.spawned.append(a), now=lambda: self.clock[0],
                                  run=run or subprocess.run)

    def events(self, kind=None):
        out = []
        for path, item in spool.take(os.path.join(self.state, "spool")):
            os.unlink(path)
            out.append(item)
        return [e for e in out if kind is None or e["type"] == kind]

    def keys_seen(self):
        return {auth for _, _, auth in self.asked}


class OpenUpdate(OpenStand):
    def test_the_release_is_read_and_installed_without_a_key(self):
        self.release("v1.3.0")
        self.publish("v1.3.0")
        self.assertEqual(self.updater().check(apply=True, manual=True), "applied")
        self.assertTrue(os.path.isfile(os.path.join(self.codes(), "1.3.0-open", appcode.MANIFEST)))
        with open(os.path.join(self.codes(), "next.json")) as f:
            self.assertEqual(json.load(f)["name"], "1.3.0-open")
        asked = [path for _, path, _ in self.asked]
        for path in ("/agents-hub/min.json", "/agents-hub/v1/releases.json", "/agents-hub/v1/layer/code-1.3.0-open.tar.gz"):
            self.assertIn(path, asked)
        self.assertEqual(self.keys_seen(), {None})

    def test_an_open_hub_with_an_unreadable_manifest_is_an_error_not_a_hub_without_a_source(self):
        man = os.path.join(self.codes(), self.current(), appcode.MANIFEST)
        os.chmod(man, 0o600)
        with open(man, "w") as f:
            f.write("{")
        self.assertEqual(self.updater().check(apply=False), "error")

    def test_only_the_open_build_reads_its_source_without_a_key(self):
        from modules.update import layer
        self.assertEqual(layer.source(self.state, "open"), {"url": self.url, "key": None})
        self.assertIsNone(layer.source(self.state, "team"))   # the team build without its key: nothing, as before
        self.assertIsNone(layer.source(self.state))
        write(os.path.join(self.codes(), "deliver.json"), json.dumps({"url": "http://203.0.113.5/agents-hub"}))
        self.assertIsNone(layer.source(self.state, "open"))   # http only to this machine
        write(os.path.join(self.codes(), "deliver.json"), json.dumps({"url": "https://pages.example.test/x",
                                                                      "key": "not a key"}))
        self.assertIsNone(layer.source(self.state, "open"))
        write(os.path.join(self.codes(), "deliver.json"), json.dumps({"url": "https://pages.example.test/x",
                                                                      "key": BUILD_KEY}))
        self.assertEqual(layer.source(self.state, "team"), {"url": "https://pages.example.test/x", "key": BUILD_KEY})

    def test_a_team_hub_without_its_key_still_has_nothing_to_look_at(self):
        man = os.path.join(self.codes(), self.current(), appcode.MANIFEST)
        with open(man) as f:
            got = json.load(f)
        os.chmod(man, 0o600)
        with open(man, "w") as f:
            json.dump({**got, "flavor": "team"}, f)
        self.assertEqual(self.updater().check(apply=False), "no-source")
        self.assertEqual(self.asked, [])

    def test_an_environment_off_the_pages_and_the_releases_hosts_is_refused(self):
        from modules.update import layer
        u = self.updater()
        src = {"url": "https://pages.example.test/agents-hub", "key": None}
        for url, ok in (("https://github.com/acme/agents-hub/releases/download/v1.3.0/a-open-linux-x64.tar.gz", True),
                        ("https://pages.example.test/agents-hub/v1/app/a.tar.gz", True),
                        ("/v1/app/a.tar.gz", True),
                        ("https://downloads.example.test/a-open-linux-x64.tar.gz", False),
                        ("http://github.com/acme/a.tar.gz", False)):
            offer = {"url": url, "sha256": "0" * 64, "size": 10, "runtime": 2, "name": "a.tar.gz"}
            with mock.patch.object(u, "_folder", lambda: None), mock.patch.object(u, "download", side_effect=OSError):
                if ok:
                    with self.assertRaises(OSError):   # it got as far as the download
                        u._install_shell(src, offer, {"version": "1.3.0"}, 2, False)
                else:
                    with self.assertRaises(layer.Refused):
                        u._install_shell(src, offer, {"version": "1.3.0"}, 2, False)
        team = {"url": "https://deliver.example.test", "key": BUILD_KEY}   # the team build: its own server only
        offer = {"url": "https://github.com/acme/a.tar.gz", "sha256": "0" * 64, "size": 10, "runtime": 2, "name": "a"}
        with self.assertRaises(layer.Refused):
            u._install_shell(team, offer, {"version": "1.3.0"}, 2, False)


class Assets(unittest.TestCase):
    """core/fetch: the open build downloads only from its pages and the hosts of its releases' files, every redirect
    checked before it is asked (the same table as the install scripts')."""

    def test_the_address_rule(self):
        from core import fetch
        from tests.fakes import pages
        for url, server, ok in pages.ASSET_CASES:
            self.assertEqual(fetch.asset_ok(url, server), ok, url)

    def test_a_redirect_is_followed_only_where_the_rule_allows_and_never_asked_elsewhere(self):
        from core import fetch
        from tests.fakes import pages
        root = tempfile.mkdtemp(prefix="hub-assets-")
        self.addCleanup(platforms.current.rmtree, root)
        write(os.path.join(root, "x.tar.gz"), "the bytes of x\n")
        page, github, files, asked = pages.places(self, root, root)
        server = page.url + "/agents-hub"
        github.redirects["/r/x.tar.gz"] = files.url + "/x.tar.gz?sig=a%2Bb"
        github.redirects["/r/away.tar.gz"] = "http://203.0.113.5/x.tar.gz"
        github.redirects["/r/odd.tar.gz"] = "https://downloads.example.test/x.tar.gz"
        allow = lambda url: fetch.asset_ok(url, server)  # noqa: E731
        dest = os.path.join(root, "got")
        data = b"the bytes of x\n"
        fetch.download(github.url + "/r/x.tar.gz", dest, hashlib.sha256(data).hexdigest(), len(data), "x", "again",
                       pause=0, allow=allow)
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), data)
        for away in ("/r/away.tar.gz", "/r/odd.tar.gz"):
            asked.clear()
            with self.assertRaises(fetch.Failed):
                fetch.download(github.url + away, dest + "2", "0" * 64, 5, "x", "again", pause=0, allow=allow)
            self.assertEqual([p for _, p, _ in asked], [away])   # never further, and not asked again: a refusal
        with self.assertRaises(fetch.Failed):   # the first address too
            fetch.download("http://203.0.113.5/x.tar.gz", dest + "3", "0" * 64, 5, "x", "again", pause=0, allow=allow)


class Telegram(unittest.TestCase):
    """The bot: one message with "Update" per new stable version of an app on a layer; the press is the page's flag."""

    def setUp(self):
        from core import journal
        from modules.telegram import bot as tg
        from tests.test_telegram import OWNER, FakeBotApi
        self.dir = tempfile.mkdtemp(prefix="hub-app-tg-")
        self.addCleanup(platforms.current.rmtree, self.dir)
        self.state = os.path.join(self.dir, "state")
        old = os.environ.get("HUB_TELEGRAM_CONFIG")
        os.environ["HUB_TELEGRAM_CONFIG"] = os.path.join(self.dir, "telegram.json")
        self.addCleanup(lambda: os.environ.__setitem__("HUB_TELEGRAM_CONFIG", old) if old is not None
                        else os.environ.pop("HUB_TELEGRAM_CONFIG", None))
        tg.save_config({"token": "1:x", "owners": [OWNER]})
        self.fake = FakeBotApi()
        self.addCleanup(self.fake.server.server_close)
        self.addCleanup(self.fake.server.shutdown)
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        self.j.append("session.requested", "hub", {"cwd": "/w", "session_id": "h", "kind": "hub"})
        self.bot = tg.Bot(self.state, tg.Api("1:x", base=self.fake.base))
        self.bot.catch_up()
        self.owner = OWNER

    def checked(self, **data):
        self.j.append("update.checked", None, {"result": "available", "app": True, "layer": True,
                                               "update_channel": "stable", "version": "1.3.0", "notes": ["- new"], **data},
                      src="update")
        self.bot.tick()

    def buttons(self):
        return [b for b in self.fake.sent() if b.get("reply_markup", {}).get("inline_keyboard")]

    def test_a_new_version_is_one_message_with_the_button_and_the_press_asks_the_update(self):
        self.checked()
        self.checked()                       # the next check of the same version: no second message
        got = self.buttons()
        self.assertEqual(len(got), 1)
        self.assertIn("1.3.0", got[0]["text"])
        data = got[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        cq = {"id": "q1", "data": data, "from": {"id": self.owner}, "message": {"chat": {"id": self.owner, "type": "private"}}}
        self.bot.on_callback(cq)
        from modules.update.update import INSTALL_FLAG
        self.assertTrue(os.path.exists(os.path.join(self.state, INSTALL_FLAG)))
        os.unlink(os.path.join(self.state, INSTALL_FLAG))
        self.bot.on_callback({**cq, "id": "q2"})   # one press, one install
        self.assertFalse(os.path.exists(os.path.join(self.state, INSTALL_FLAG)))
        self.checked(version="1.3.1")        # a newer one is a new message
        self.assertEqual(len(self.buttons()), 2)

    def test_an_installed_release_waiting_for_the_restart_has_a_restart_button(self):
        from core import spool
        self.j.append("owner.notify", None, {"title": "Hub update", "id": "update-ready", "ready": {"version": "1.3.0"},
                                             "text": "The hub is updated to 1.3.0 — restart the hub"}, src="update")
        self.bot.tick()
        got = self.buttons()
        self.assertEqual(len(got), 1)
        self.assertIn("1.3.0", got[0]["text"])
        key = got[0]["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(key["text"], "Restart")
        cq = {"id": "q1", "data": key["callback_data"], "from": {"id": self.owner},
              "message": {"chat": {"id": self.owner, "type": "private"}}}
        os.makedirs(os.path.join(self.state, "layers"), exist_ok=True)
        nxt = os.path.join(self.state, "layers", "next.json")
        with open(nxt, "w") as f:
            json.dump({"name": "1.3.0-team", "why": "update", "ask": True}, f)   # the switch waits for the restart
        self.bot.on_callback(cq)
        self.bot.on_callback({**cq, "id": "q2"})              # one press, one request
        taken = spool.take(os.path.join(self.state, "spool"))
        for path, _ in taken:
            os.unlink(path)
        asked = [i for _, i in taken if i["type"] == "hub.restart_requested"]
        self.assertEqual(len(asked), 1)
        self.assertEqual((asked[0]["data"]["parts"], asked[0]["data"]["owner"]), ([], True))
        os.unlink(nxt)                                        # restarted (or "Return" before it): the old button asks nothing
        cq2 = {**cq, "id": "q3", "data": self.buttons()[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]}
        self.bot.on_callback(cq2)
        self.assertFalse([i for _, i in spool.take(os.path.join(self.state, "spool"))
                          if i["type"] == "hub.restart_requested"])
        # a session's own notice with the same fields gets no button: only the update process restarts the hub this way
        self.j.append("owner.notify", "maps", {"title": "x", "id": "y", "ready": {"version": "9"}, "text": "restart?"},
                      src="session")
        self.bot.tick()
        self.assertEqual(len(self.buttons()), 1)

    def test_nightly_a_git_hub_and_a_look_without_news_send_nothing(self):
        self.checked(update_channel="main")
        self.checked(layer=None)
        self.checked(result="current")
        self.assertEqual(self.buttons(), [])


class Fold(unittest.TestCase):
    def test_the_page_keeps_ask_restart_only_as_true(self):
        from core import reducer
        for given, kept in ((True, True), (1, None), ("true", None), (None, None)):
            st, _, why = reducer.reduce(reducer.empty(), {"seq": 1, "ts": 5, "type": "update.checked", "entity": None,
                                                         "data": {"result": "applied", "app": True, "layer": True,
                                                                  "version": "1.3.0", "ask_restart": given,
                                                                  "stands": 4}})
            self.assertIsNone(why)
            self.assertEqual(st["update_check"].get("ask_restart"), kept, given)
            self.assertNotIn("stands", st["update_check"])   # the count of hubs is gone (09.11)


class Settings(unittest.TestCase):
    def test_one_channel_for_the_layer_and_the_app_from_git(self):
        from core import packaged
        from unittest import mock
        for layout, want in (("layer", "stable"), (None, "stable")):   # the owner's decision of 2026-11-09 (m1917882)
            data = {"format": "app", "runtime": 1, **({"layout": layout} if layout else {})}
            with mock.patch.object(packaged, "info", lambda d=data: d):
                got = packaged.apply({"update": {"channel": "main"}})
            self.assertEqual(got["update"]["channel"], want)
            self.assertEqual(got["update"]["app"].get("layer"), True if layout else None)

    def test_update_manual_is_not_overwritten_by_the_packaged_settings(self):
        from core import packaged
        from unittest import mock
        with mock.patch.object(packaged, "info", lambda: {"format": "app", "runtime": 1, "layout": "layer"}):
            got = packaged.apply({"update": {"manual": True, "enabled": False}})
        self.assertEqual((got["update"]["manual"], got["update"]["enabled"]), (True, True))


class Every(unittest.TestCase):
    """How often the update module looks (review of 2026-11-09): a hub on a code layer asks its delivery server every 6 h,
    a hub from git its remote every hour; update.every in hub.json overrides both. The settings' defaults filled in
    update.every for every hub, so a layer's 6 h never applied."""

    def config(self, update=None):
        from core import catalog
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        path = os.path.join(d, "hub.json")
        with open(path, "w") as f:
            json.dump({"update": update} if update else {}, f)
        with mock.patch.dict(os.environ, {"HUB_CONFIG": path}):
            return catalog.load_config(d)

    def test_a_layer_looks_every_six_hours_and_git_every_hour(self):
        from modules.update import update as mod
        self.assertEqual(mod.every(self.config(), layered=True), 6 * 3600)
        self.assertEqual(mod.every(self.config(), layered=False), 3600)

    def test_hub_json_sets_it_for_both(self):
        from modules.update import update as mod
        self.assertEqual(mod.every(self.config({"every": 900}), layered=True), 900)
        self.assertEqual(mod.every(self.config({"every": 900}), layered=False), 900)

    def test_a_bad_value_is_the_default(self):
        from modules.update import update as mod
        for bad in ("6h", -1, None, [1], True):
            self.assertEqual(mod.every({"update": {"every": bad}}, layered=True), 6 * 3600, bad)


if __name__ == "__main__":
    unittest.main()
