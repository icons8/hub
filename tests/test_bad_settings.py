"""A typo in hub.json or a broken marketplace.json of a skill set does not bring hubd down. hub-up restarted it after a
pause, Hub() failed the same way, and the hub stayed down until the file was fixed by hand."""
import tests  # noqa: F401 — ignore this machine's settings
import json
import os
import tempfile
import types
import unittest
from unittest import mock

from core import catalog, journal
from core.daemon import Hub
from tests.fakes import cut

BAD = ['{"hub": null}', '{"hub": []}', '{"modules": "x"}', '{"modules": {"paths": [5]}}', '{"sets": [{"name": "s"}]}',
       '{"sets": "abc"}', '{"sets": [5]}', '{"mcp": {"a": "b"}}', '{"mcp": []}', '{"projects": []}', '[]',
       '{"rules": "x"}', '{"base": "x"}', '{"journal": 5}', '{"executors": []}', '{"compact_idle": "x"}',
       '{"update": null}', '{"dev": "x"}', '{"browsers": []}', '{"extensions": []}']


class BadSettings(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="bad-settings-")
        self.cfg = os.path.join(self.state, "hub.json")
        env = mock.patch.dict(os.environ, {"HUB_CONFIG": self.cfg})
        env.start()
        self.addCleanup(env.stop)

    def write(self, body):
        with open(self.cfg, "w", encoding="utf-8") as f:
            f.write(body)

    def hub(self):
        h = Hub(self.state, types.SimpleNamespace(), clock=lambda: 1000.0)
        h.execute = lambda a: None
        return h

    def notes(self, h):
        return [e for e in journal.read(h.journal.path) if e["type"] == "owner.notify"]

    def test_a_section_of_the_wrong_shape_is_ignored(self):
        for body in BAD:
            with self.subTest(body=body):
                self.write(body)
                h = self.hub()
                h.load_catalog()
                h.ensure_hub()
                h.ensure_jobs()
                h.limit()
                self.assertIn("core:hub-base", h.state["catalog"])

    def test_a_file_that_is_not_json_keeps_the_last_settings(self):
        self.write(json.dumps({"mcp": {"tracker": {"server": {"type": "http", "url": "https://example.com/mcp"}}},
                               "timezone": "Europe/Paris"}))
        h = self.hub()
        h.load_catalog()
        self.write('{"mcp": {')
        h.load_catalog()
        h.load_catalog()
        self.assertIn("tracker", h.state["catalog"])   # a typo does not take the session tools away
        self.assertIn("tracker", h.config["mcp"])
        self.assertEqual(h.config["timezone"], "Europe/Paris")
        notes = self.notes(h)
        self.assertEqual(len(notes), 1)                # once, not every tick
        self.assertIn(self.cfg, notes[0]["data"]["text"])
        self.assertIn("n:core:config", h.state["notices"])
        self.write(json.dumps({"mcp": {}}))
        h.load_catalog()
        self.assertNotIn("tracker", h.state["catalog"])
        # the cause is gone — the core withdraws its notice (push spec, §2.6): after a hubd restart too
        self.assertNotIn("n:core:config", h.state.get("notices") or {})
        self.write('{"mcp": ')                         # a new typo after the fix — one message again
        h.load_catalog()
        self.assertEqual(len(self.notes(h)), 2)

    def test_a_start_with_a_file_that_is_not_json(self):
        self.write(json.dumps({"mcp": {"tracker": {"server": {"type": "http", "url": "https://example.com/mcp"}}}}))
        self.hub().load_catalog()
        self.write('{"mcp": {')
        h = self.hub()                                 # Hub() itself used to fail
        h.load_catalog()
        h.ensure_hub()
        h.ensure_jobs()
        self.assertIn("tracker", h.state["catalog"])   # the journal's catalog stays until the file is fixed
        self.assertEqual(len(self.notes(h)), 1)
        self.write(json.dumps({"mcp": {}}))
        h.load_catalog()
        self.assertNotIn("tracker", h.state["catalog"])
        # the cause is gone — the core withdraws its notice (push spec, §2.6): after a hubd restart too
        self.assertNotIn("n:core:config", h.state.get("notices") or {})

    def test_a_session_starts_while_the_file_is_broken(self):
        # hub.json is unreadable: sessions still come up instead of a host.error on each; the hub was already told about the file error (notify)
        from hosts.streamjson import StreamHost
        self.write(json.dumps({"rules": ["A hub rule."], "permissions": {"auto_allow": True}}))
        host = StreamHost(self.state, claude=("echo",))
        h = Hub(self.state, host, clock=lambda: 1000.0)
        h.load_catalog()
        self.write('{"rules": [')
        h.load_catalog()
        cmd = host.command("worker", "s-w")                   # the previous settings the core holds: the rules are in place
        with open(cmd[cmd.index("--append-system-prompt-file") + 1], encoding="utf-8") as f:
            self.assertIn("A hub rule.", f.read())
        self.assertTrue(host.auto_allow_on())
        self.assertEqual(len(self.notes(h)), 1)

    def test_a_host_without_the_core_starts_on_defaults_while_the_file_is_broken(self):
        from hosts.streamjson import StreamHost
        self.write('{"rules": [')
        host = StreamHost(self.state, claude=("echo",))
        cmd = host.command("worker", "s-w")
        with open(cmd[cmd.index("--append-system-prompt-file") + 1], encoding="utf-8") as f:
            self.assertIn("in the owner's language", f.read())
        self.assertFalse(host.auto_allow_on())
        self.assertTrue(os.path.exists(host.agent_file("helper", "helper")))   # the agent file stub — too

    def test_readers_in_other_processes_hold_what_they_read_and_start_on_defaults(self):
        # one rule for those who must not fail over the file: the previously read value, or the default if there is none; stderr gets one
        # line per error text (the page did not serve a snapshot on an unreadable hub.json, modules crashed in a loop)
        from modules.files.server import Files
        from modules.directions import direction
        from modules.web.server import Web
        web, files = Web(self.state), Files(state_dir=self.state)
        self.write('{"title": "My team"')
        with mock.patch("sys.stderr") as err:   # nothing to hold — the default
            self.assertEqual(web.config(), catalog.defaults())
            web.config()
        self.assertEqual(err.write.call_count, 2)   # one line (the text and the newline), not a line per query
        web.with_clock({})   # the page snapshot is built
        self.write(json.dumps({"title": "My team", "directions": {"root": "/srv/boards"}}))
        self.assertEqual(web.config()["title"], "My team")
        self.write('{"title": "My')
        self.assertEqual(web.config()["title"], "My team")   # the previous one
        self.assertEqual(files.config()["title"], "My team")
        web.config()["title"] = "someone else's edit"                     # a copy: the previous value is not held by reference
        self.assertEqual(web.config()["title"], "My team")
        # the page reads the boards from the previous value (an absolute path of this OS: on Windows C:\srv\boards)
        self.assertEqual(web.directions_root(), os.path.abspath("/srv/boards"))
        # while the CLI that writes the boards refuses on an unreadable file instead of falling back to the default folder
        with self.assertRaises(ValueError):
            direction.root()
        # and those who act on settings (access, gateway, self-update) still do not guess for the owner
        with self.assertRaises(ValueError):
            catalog.load_config(self.state)

    def test_a_start_with_a_broken_file_raises_no_system_agents_until_it_reads(self):
        # nothing was read: the default names may differ from the owner's — do not create any until the file is readable
        self.write('{"dev": {"name": "my-dev"},')
        with mock.patch.dict(os.environ, {"HUB_NO_SYSTEM_AGENTS": ""}):
            h = self.hub()
            h.load_catalog()
            h.ensure_dev()
            h.ensure_jobs()
            self.assertEqual(h.state["sessions"], {})
            self.assertEqual(h.state["schedule"], {})
            self.write(json.dumps({"dev": {"name": "my-dev"}}))
            h.load_catalog()
        self.assertIn("my-dev", h.state["sessions"])

    def test_a_start_with_a_broken_file_raises_no_hub_session_until_it_reads(self):
        self.write('{"hub": {"enabled": true, "name": "boss"},')
        h = self.hub()
        h.load_catalog()
        h.ensure_hub()
        self.assertNotIn("hub", h.state["sessions"])
        self.write(json.dumps({"hub": {"enabled": True, "name": "boss"}}))
        h.load_catalog()
        self.assertIn("boss", h.state["sessions"])
        self.assertNotIn("hub", h.state["sessions"])

    def test_a_broken_marketplace_skips_only_what_is_broken(self):
        root = tempfile.mkdtemp(prefix="set-")
        os.makedirs(os.path.join(root, ".claude-plugin"))
        os.makedirs(os.path.join(root, "good"))
        with open(os.path.join(root, ".claude-plugin", "marketplace.json"), "w", encoding="utf-8") as f:
            json.dump({"plugins": [{"source": "./x"}, "junk", {"name": "out", "source": "../../.."},
                                   {"name": "good", "source": "./good"}]}, f)
        tools = catalog.build({**catalog.DEFAULTS, "sets": [{"name": "s", "path": root}, {"path": root}, 5],
                               "mcp": {"a": "b", "ok": {"server": {"command": "x"}}}})
        self.assertIn("good@s", tools)
        self.assertNotIn("out@s", tools)   # a source outside the set folder
        self.assertIn("ok", tools)
        self.assertNotIn("a", tools)

    def test_a_marketplace_that_is_not_an_object(self):
        root = tempfile.mkdtemp(prefix="set-")
        os.makedirs(os.path.join(root, ".claude-plugin"))
        for body in ("[1]", '{"plugins": 5}', '{"plugins": [{"name": "p", "source": 5}]}'):
            with self.subTest(body=body):
                with open(os.path.join(root, ".claude-plugin", "marketplace.json"), "w", encoding="utf-8") as f:
                    f.write(body)
                tools = catalog.build({**catalog.DEFAULTS, "sets": [{"name": "s", "path": root}]})
                self.assertIn("core:hub-base", tools)


class SettingsWriters(unittest.TestCase):
    """hub.json has several writers (the page — a thread per request, `hub tools import`, `hub accounts gateway`): each
    has its own temporary file and they share a lock; permissions are the old file's, a new one is 0600 (hub.json may hold tokens)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="writers-")
        self.path = os.path.join(self.dir, "hub.json")

    def test_two_writers_of_one_process_do_not_share_a_temporary_file(self):
        # the page is multithreaded: the threads share one pid and a common "hub.json.<pid>.tmp" — the second replace did not find the file
        real, nested = json.dump, []

        def dump(obj, f, **kw):
            if not nested:
                nested.append(True)
                catalog.write_settings(self.path, {"timezone": "Europe/Paris"})   # another thread — in the middle of a write
            return real(obj, f, **kw)
        with mock.patch.object(catalog.json, "dump", dump):
            catalog.write_settings(self.path, {"timezone": "UTC"})
        self.assertEqual(catalog.read_settings(self.path), {"timezone": "UTC"})
        self.assertEqual(os.listdir(self.dir), ["hub.json"])

    def test_a_failed_write_leaves_the_old_file_and_no_temporary(self):
        catalog.write_settings(self.path, {"timezone": "UTC"})
        with self.assertRaises(TypeError):
            catalog.write_settings(self.path, {"x": object()})
        self.assertEqual(catalog.read_settings(self.path), {"timezone": "UTC"})
        self.assertEqual(os.listdir(self.dir), ["hub.json"])

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_modes(self):
        catalog.write_settings(self.path, {})
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        os.chmod(self.path, 0o640)
        catalog.write_settings(self.path, {"timezone": "UTC"})
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o640)

    def edits(self):
        calls, real = [], catalog.edit_settings

        def spy(path):
            calls.append(path)
            return real(path)
        return calls, mock.patch.object(catalog, "edit_settings", spy)

    @cut.needs("gateway")
    def test_the_gateway_switch_writes_under_the_lock(self):
        from modules.gateway import gateway
        catalog.write_settings(self.path, {"timezone": "UTC"})
        calls, spy = self.edits()
        with mock.patch.dict(os.environ, {"HUB_CONFIG": self.path}), spy:
            gateway.switch(self.dir, False)
        self.assertEqual(calls, [self.path])
        self.assertEqual(catalog.read_settings(self.path), {"timezone": "UTC", "gateway": {"enabled": False}})

    @cut.needs("gateway")
    def test_the_gateway_switch_on_a_hub_without_a_settings_folder(self):
        from modules.gateway import gateway
        path = os.path.join(self.dir, "config", "hub.json")
        with mock.patch.dict(os.environ, {"HUB_CONFIG": path}):
            gateway.switch(self.dir, False)
        self.assertEqual(catalog.read_settings(path), {"gateway": {"enabled": False}})


if __name__ == "__main__":
    unittest.main()
