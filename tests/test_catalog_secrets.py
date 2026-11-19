"""MCP keys stay out of the journal: the catalog event carries servers without env and headers, an old journal that has
them folds without them, and a session gets them in mcp.json readable only by its user — from the catalog hubd keeps in
memory. Importing the owner's MCP writes hub.json 0600 from the first byte. All values are made up."""
import tests  # noqa: F401 — keep the machine's settings out (tests/__init__.py)
import json
import os
import shutil
import stat
import tempfile
import types
import unittest
from unittest import mock

from core import catalog, mine, reducer
from core.daemon import Hub

KEY = "tracker-" + "s3cr3t" * 4
HEADER = "Bearer " + "h3ad3r" * 4
CONFIG = {"mcp": {"tracker": {"server": {"command": "npx", "args": ["-y", "mcp-tracker"], "env": {"TRACKER_TOKEN": KEY}},
                              "description": "Tasks", "risk": ["owner-data"]},
                  "remote": {"server": {"type": "http", "url": "https://mcp.example.com/mcp",
                                        "headers": {"Authorization": HEADER}}, "description": "Remote"}}}


def private(path):
    return stat.S_IMODE(os.stat(path).st_mode) & 0o077 == 0


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="secrets-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.cfg = os.path.join(self.dir, "hub.json")
        self.write(CONFIG)
        patch = mock.patch.dict(os.environ, {"HUB_CONFIG": self.cfg})
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, cfg):
        with open(self.cfg, "w") as f:
            json.dump(cfg, f)

    def journal_text(self):
        with open(os.path.join(self.dir, "journal.jsonl"), encoding="utf-8") as f:
            return f.read()

    def servers(self, h, tools=("tracker", "remote")):
        flags = h.host.tool_flags(name="w1", tools=list(tools), kind="work", cwd=self.dir, project="all")
        path = flags[flags.index("--mcp-config", flags.index("--strict-mcp-config")) + 1]
        with open(path, encoding="utf-8") as f:
            return json.load(f)["mcpServers"], path


class CatalogWithoutSecrets(Base):
    def test_the_catalog_event_has_no_env_or_headers(self):
        h = Hub(self.dir, types.SimpleNamespace())
        h.load_catalog()
        h.snapshot("test")
        h.save_state()
        self.assertIn("tracker", h.state["catalog"])
        self.assertEqual(h.state["catalog"]["tracker"]["server"]["args"], ["-y", "mcp-tracker"])
        self.assertEqual(h.state["catalog"]["remote"]["server"]["url"], "https://mcp.example.com/mcp")
        for text in (self.journal_text(), json.dumps(h.state)):
            self.assertNotIn(KEY, text)
            self.assertNotIn("h3ad3r", text)
        with open(os.path.join(self.dir, "state.json"), encoding="utf-8") as f:
            self.assertNotIn(KEY, f.read())

    def test_a_session_still_gets_the_keys_in_a_private_mcp_json(self):
        h = Hub(self.dir, types.SimpleNamespace())
        h.load_catalog()
        servers, path = self.servers(h)
        self.assertEqual(servers["tracker"]["env"], {"TRACKER_TOKEN": KEY})
        self.assertEqual(servers["remote"]["headers"], {"Authorization": HEADER})
        if os.name == "posix":
            self.assertTrue(private(path), oct(os.stat(path).st_mode))

    def test_a_new_key_reaches_the_next_launch_without_an_event(self):
        h = Hub(self.dir, types.SimpleNamespace())
        h.load_catalog()
        before = self.journal_text()
        fresh = "tracker-" + "n3w" * 8
        cfg = json.loads(json.dumps(CONFIG))
        cfg["mcp"]["tracker"]["server"]["env"]["TRACKER_TOKEN"] = fresh
        self.write(cfg)
        h.load_catalog()
        self.assertEqual(self.journal_text(), before)   # the public view is the same — no event
        self.assertEqual(self.servers(h)[0]["tracker"]["env"], {"TRACKER_TOKEN": fresh})

    def test_a_broken_hub_json_keeps_the_keys_read_before(self):
        h = Hub(self.dir, types.SimpleNamespace())
        h.load_catalog()
        with open(self.cfg, "w") as f:
            f.write("{not json")
        h.load_catalog()
        self.assertEqual(self.servers(h)[0]["tracker"]["env"], {"TRACKER_TOKEN": KEY})

    def test_settings_read_at_start_give_the_keys_when_the_next_read_fails(self):
        # hubd read hub.json at startup, but by the first load_catalog the file is already broken: the journal catalog
        # has no keys, and a launch takes servers from the settings read at startup
        h = Hub(self.dir, types.SimpleNamespace())
        with open(self.cfg, "w") as f:
            f.write("{not json")
        h.load_catalog()
        self.assertEqual(self.servers(h)[0]["tracker"]["env"], {"TRACKER_TOKEN": KEY})
        self.assertNotIn(KEY, self.journal_text())


class OldJournalReplay(unittest.TestCase):
    def test_a_journal_written_before_carries_no_keys_into_the_state(self):
        # a journal from an earlier hub: catalog.loaded with the whole server, env included
        full = catalog.build(CONFIG)
        events = [{"seq": 1, "ts": 1.0, "src": "core", "type": "catalog.loaded", "entity": None,
                   "data": {"tools": full}}]
        st = reducer.replay(events)
        self.assertEqual(st["catalog"]["tracker"]["server"]["command"], "npx")
        self.assertNotIn(KEY, json.dumps(st))
        self.assertNotIn("h3ad3r", json.dumps(st))
        # and a snapshot from an earlier hub that had them
        snap = [{"seq": 1, "ts": 1.0, "src": "core", "type": "core.snapshot", "entity": None,
                 "data": {"why": "stop", "state": {**reducer.empty(), "catalog": full, "seq": 1}}}]
        self.assertNotIn(KEY, json.dumps(reducer.replay(snap)))

    def test_the_public_view_keeps_everything_else(self):
        from core import redact
        full = catalog.build(CONFIG)
        public = redact.public_catalog(full)
        self.assertEqual(set(public), set(full))
        for tid, t in full.items():
            want = {**t, "server": {k: v for k, v in t["server"].items() if k not in ("env", "headers")}} \
                if t.get("kind") == "mcp" else t
            self.assertEqual(public[tid], want, tid)


class ImportedMcpPrivate(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX file permissions")
    def test_hub_json_is_private_from_the_first_byte(self):
        home = tempfile.mkdtemp(prefix="mine-home-")
        self.addCleanup(shutil.rmtree, home, True)
        with open(os.path.join(home, ".claude.json"), "w") as f:
            json.dump({"mcpServers": {"tracker": CONFIG["mcp"]["tracker"]["server"]}}, f)
        cfg = os.path.join(home, "hub.json")
        with open(cfg, "w") as f:
            json.dump({}, f)
        os.chmod(cfg, 0o644)
        seen = []
        real = os.replace

        def replace(src, dst):
            if dst == cfg:
                seen.append(stat.S_IMODE(os.stat(src).st_mode))   # the mode the file with the key will have when it lands
            return real(src, dst)
        with mock.patch("os.replace", replace):
            mine.import_mcp(home, "tracker", "Tasks", ["owner-data"], env={"HOME": home, "HUB_CONFIG": cfg})
        self.assertEqual(seen, [0o600])
        self.assertEqual(stat.S_IMODE(os.stat(cfg).st_mode), 0o600)
        with open(cfg, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["mcp"]["tracker"]["server"]["env"], {"TRACKER_TOKEN": KEY})


if __name__ == "__main__":
    unittest.main()
