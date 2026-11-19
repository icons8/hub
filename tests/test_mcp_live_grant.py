"""An MCP tool granted to a session in the middle of its turn is connected at once, in that turn. Before, the grant
marked the session dirty and the tool came only with the restart after the turn (`_tools_apply`): a session that asked
for a tool and waited for it in the same turn could not use it.

Now the core asks the running Claude Code to add the new servers (`mcp_set_servers`, a control request, no model turn):
the model sees them on its next call. The restart after the turn stays as it was — the fallback and the source of
truth: a refusal (an older Claude Code) changes nothing but a fact in the journal. Codex sessions, plugins, "one for all"
tools and revoking keep the old way."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import tempfile
import unittest

from core import catalog, reducer
from hosts.streamjson import StreamHost, StreamProcess

OWNER = {"channel": "cli", "owner": True}
HUB = {"channel": "session", "owner": False, "sender": "hub"}


class Fold(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.acts = reducer.empty(), 0, []
        self.st["catalog"] = {"maps": {"kind": "mcp", "server": {"command": "maps-mcp"}, "risk": []},
                              "docs": {"kind": "mcp", "server": {"command": "docs-mcp"}, "risk": []},
                              "chat": {"kind": "mcp", "server": {"command": "chat-mcp"}, "risk": [],
                                       "exclusive": True},
                              "notes": {"kind": "plugin", "path": "/home/x/notes", "risk": []}}
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.started", "hub")
        self.ev("session.requested", "a", cwd="/w", session_id="s-a", **OWNER)
        self.ev("session.started", "a")
        self.ev("turn.started", "a", marker=None, prompt_head="x")
        self.ev("turn.result", "a", text="ok")

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.acts += acts
        return why

    def done(self, do):
        return [a for a in self.acts if a["do"] == do and a["session"] == "a"]

    def busy(self):
        self.ev("turn.started", "a", marker=None, prompt_head="works")
        self.assertEqual(self.st["sessions"]["a"]["state"], "busy")
        self.acts.clear()

    def test_a_grant_to_a_busy_session_connects_in_place(self):
        self.busy()
        self.assertIsNone(self.ev("tools.granted", "a", tool="maps", **HUB))
        self.assertEqual(self.done("mcp_set"), [{"do": "mcp_set", "session": "a", "tools": ["maps"], "live": ["maps"]}])
        self.assertEqual((self.done("release"), self.done("spawn")), ([], []))   # not mid-turn
        self.assertTrue(self.st["sessions"]["a"].get("dirty"))   # the restart after the turn stays
        self.assertNotIn("mcp_live", self.st["sessions"]["a"])
        self.ev("turn.result", "a", text="ok")
        self.assertEqual(len(self.done("spawn")), 1)
        self.assertEqual(self.done("spawn")[0]["tools"], ["maps"])
        self.assertEqual(len(self.done("mcp_set")), 1)   # not repeated by the restart

    def test_two_grants_in_one_turn_each_carry_the_whole_set(self):
        self.busy()
        self.ev("tools.granted", "a", tool="maps", **HUB)
        self.ev("tools.granted", "a", tool="docs", **HUB)
        self.assertEqual([a["live"] for a in self.done("mcp_set")], [["maps"], ["maps", "docs"]])

    def test_an_idle_session_restarts_as_before(self):
        self.acts.clear()
        self.ev("tools.granted", "a", tool="maps", **HUB)
        self.assertEqual(self.done("mcp_set"), [])
        self.assertEqual((len(self.done("release")), len(self.done("spawn"))), (1, 1))
        self.assertNotIn("mcp_live", self.st["sessions"]["a"])

    def test_a_dialog_is_mid_turn_too(self):
        self.busy()
        self.st["sessions"]["a"]["state"] = "dialog"
        self.ev("tools.granted", "a", tool="maps", **HUB)
        self.assertEqual(len(self.done("mcp_set")), 1)

    def test_codex_plugins_and_one_for_all_keep_the_old_way(self):
        self.busy()
        self.ev("tools.granted", "a", tool="notes", **HUB)   # a plugin: skills need a restart anyway
        self.ev("tools.granted", "a", tool="chat", **HUB)    # one for all: its holder may still be connected
        self.assertEqual(self.done("mcp_set"), [])
        self.st["codex"] = {"host": True, "whole": True}
        self.ev("tools.granted", "a", tool="maps", **HUB)
        self.assertEqual(self.done("mcp_set"), [])
        self.assertTrue(self.st["sessions"]["a"].get("dirty"))
        self.assertNotIn("mcp_live", self.st["sessions"]["a"])

    def test_a_later_grant_does_not_carry_a_plugin_or_a_one_for_all_one(self):
        # the action names what may go live apart from the whole set: a plugin and a "one for all" one granted earlier
        # in the turn do not ride along with the next MCP
        self.busy()
        self.ev("tools.granted", "a", tool="chat", **HUB)
        self.ev("tools.granted", "a", tool="notes", **HUB)
        self.ev("tools.granted", "a", tool="maps", **HUB)
        (act,) = self.done("mcp_set")
        self.assertEqual((act["tools"], act["live"]), (["chat", "notes", "maps"], ["maps"]))

    def test_a_revoke_is_not_applied_in_place(self):
        self.ev("tools.granted", "a", tool="maps", **HUB)
        self.ev("session.started", "a")
        self.busy()
        self.ev("tools.revoked", "a", tool="maps", **HUB)
        self.assertEqual(self.done("mcp_set"), [])
        self.assertTrue(self.st["sessions"]["a"].get("dirty"))

    def test_the_answer_is_a_fact_that_changes_nothing(self):
        self.busy()
        self.ev("tools.granted", "a", tool="maps", **HUB)
        before = json.dumps(self.st["sessions"]["a"], sort_keys=True)
        for data in ({"ok": True, "servers": ["maps"], "added": ["maps"]},
                     {"ok": False, "servers": ["maps"], "error": "Unsupported control request subtype: mcp_set_servers",
                      "unsupported": True}):
            self.assertIsNone(self.ev("session.mcp_set", "a", **data))
        self.assertIsNone(self.ev("host.done", "a", do="mcp_set", result="sent"))
        self.assertEqual(json.dumps(self.st["sessions"]["a"], sort_keys=True), before)


class Process(unittest.TestCase):
    """The host: the servers of the new set that the process was not started with, ${NAME} expanded as Claude Code
    expands --mcp-config, written to its stdin; the answer is a fact."""

    def setUp(self):
        self.host = StreamHost(tempfile.mkdtemp(prefix="ml-"))
        self.each = {"docs": {"command": "docs-mcp", "env": {"KEY": "${DOCS_KEY}"}},
                     "wiki": {"type": "http", "url": "https://wiki.example.test/mcp"}}
        self.asked = []

        def servers(**kw):   # maps is the base: every set has it
            self.asked.append(kw)
            return {"maps": {"command": "maps-mcp"}, **{t: self.each[t] for t in kw["tools"]}}
        self.host.mcp_servers = servers
        self.popen = FakePopen()
        proc = self.host.procs["a"] = StreamProcess(self.popen)
        proc.mcp_launched = frozenset({"maps"})
        proc.mcp_env = {"DOCS_KEY": "k-1"}
        proc.mcp_spawn = {"name": "a", "kind": "work", "cwd": "/home/x/w", "project": "all"}

    def answer(self, resp):
        rid = self.popen.lines[-1]["request_id"]
        self.host.inbox.put(("a", {"type": "control_response", "response": {"request_id": rid, **resp}}))
        return self.host.poll()

    def test_only_the_new_servers_go_expanded(self):
        self.assertEqual(self.host.mcp_set("a", tools=["docs"], live=["docs"]), "sent")
        (line,) = self.popen.lines
        self.assertEqual((line["type"], line["request"]["subtype"]), ("control_request", "mcp_set_servers"))
        # a server it was started with is not touched: set_servers would take it over and restart it on a change
        self.assertEqual(line["request"]["servers"], {"docs": {"command": "docs-mcp", "env": {"KEY": "k-1"}}})
        spawn = {"name": "a", "kind": "work", "cwd": "/home/x/w", "project": "all"}
        self.assertEqual(self.asked, [{"tools": ["docs"], **spawn}, {"tools": [], **spawn}])

    def test_a_tool_that_is_not_live_does_not_ride_along(self):
        # a "one for all" one granted earlier in the turn (not live) is in the whole set, not started with: it waits
        self.each["chat"] = {"command": "chat-mcp"}
        self.host.mcp_set("a", tools=["chat", "docs"], live=["docs"])
        self.assertEqual(sorted(self.popen.lines[-1]["request"]["servers"]), ["docs"])

    def test_names_come_from_the_whole_set_and_plugin_servers_wait(self):
        # an `as` that clashes with a plugin's server is renamed only in the whole set; a plugin's own server (one that
        # comes `with` an MCP too) waits for the restart with the plugin's skills
        whole = {"maps": {"command": "maps-mcp"}, "docs": {"command": "plugin-docs"}, "M": {"command": "m-mcp"},
                 "plugin:x:s": {"command": "x-mcp"}}
        self.host.mcp_servers = lambda **kw: whole if "M" in kw["tools"] else {"maps": {"command": "maps-mcp"},
                                                                               "docs": {"command": "plugin-docs"}}
        self.host.procs["a"].mcp_launched = frozenset({"maps", "docs"})
        self.host.mcp_set("a", tools=["P", "M"], live=["M"])
        self.assertEqual(self.popen.lines[-1]["request"]["servers"], {"M": {"command": "m-mcp"}})

    def test_the_answer_is_a_fact_without_values(self):
        self.host.mcp_set("a", tools=["docs"], live=["docs"])
        facts = self.answer({"subtype": "success", "response": {"added": ["docs"], "removed": [], "errors": {}}})
        self.assertEqual(facts, [("session.mcp_set", "a", {"ok": True, "servers": ["docs"], "added": ["docs"]})])
        self.assertNotIn("k-1", json.dumps(facts))

    def test_a_second_grant_keeps_the_first_server(self):
        self.host.mcp_set("a", tools=["docs"], live=["docs"])
        self.answer({"subtype": "success", "response": {"added": ["docs"], "removed": [], "errors": {}}})
        self.host.mcp_set("a", tools=["docs", "wiki"], live=["docs", "wiki"])
        # set_servers replaces what it set before: the earlier one goes again, or it would be dropped
        self.assertEqual(sorted(self.popen.lines[-1]["request"]["servers"]), ["docs", "wiki"])

    def test_nothing_new_writes_nothing(self):
        self.assertEqual(self.host.mcp_set("a", tools=[], live=[]), "unchanged")
        self.host.mcp_set("a", tools=["docs"], live=["docs"])
        self.answer({"subtype": "success", "response": {"added": ["docs"], "removed": [], "errors": {}}})
        self.assertEqual(self.host.mcp_set("a", tools=["docs"], live=["docs"]), "unchanged")
        self.assertEqual(len(self.popen.lines), 1)

    def test_a_server_that_fails_is_named_without_its_error(self):
        # the error may repeat the expanded address or arguments of the server (a key): only its name goes to the journal
        self.host.mcp_set("a", tools=["docs"], live=["docs"])
        (fact,) = self.answer({"subtype": "success", "response": {"added": ["docs"], "removed": [],
                                                                  "errors": {"docs": "failed: docs-mcp --key k-1"}}})
        self.assertEqual(fact[2]["failed"], ["docs"])
        self.assertNotIn("k-1", json.dumps(fact))

    def test_a_late_answer_does_not_mark_the_restarted_process(self):
        self.host.mcp_set("a", tools=["docs"], live=["docs"])
        old = self.popen.lines[-1]["request_id"]
        fresh = self.host.procs["a"] = StreamProcess(FakePopen())   # the restart after the turn came first
        fresh.mcp_launched = frozenset({"maps", "docs"})
        fresh.popen.pid = 2
        self.host.inbox.put(("a", {"type": "control_response", "response": {
            "request_id": old, "subtype": "success", "response": {"added": ["docs"], "removed": [], "errors": {}}}}))
        self.assertEqual(self.host.poll(), [])   # not a fact of the new process either
        self.assertEqual(fresh.mcp_pushed, {})

    def test_an_older_claude_refuses_and_nothing_breaks(self):
        self.host.mcp_set("a", tools=["docs"], live=["docs"])
        (fact,) = self.answer({"subtype": "error", "error": "Unsupported control request subtype: mcp_set_servers"})
        self.assertEqual(fact[:2], ("session.mcp_set", "a"))
        self.assertEqual((fact[2]["ok"], fact[2]["unsupported"]), (False, True))
        # not taken as set: the next grant sends it again (and is refused again — the restart brings it either way)
        self.assertEqual(self.host.mcp_set("a", tools=["docs"], live=["docs"]), "sent")

    def test_no_process(self):
        self.host.procs.pop("a")
        with self.assertRaises(RuntimeError):
            self.host.mcp_set("a", tools=["docs"], live=["docs"])

    def test_a_new_process_starts_with_its_set_and_pushes_nothing(self):
        # the restart after the turn: the new process has every server from --mcp-config, set_servers is not needed
        d = tempfile.mkdtemp(prefix="ml-")
        own, project = os.path.join(d, "mcp.json"), os.path.join(d, "project.json")
        for path, names in ((own, ("maps", "docs")), (project, ("local",))):
            with open(path, "w") as f:
                json.dump({"mcpServers": {n: {"command": n} for n in names}}, f)
        launched = StreamHost.launched_servers(["--plugin-dir", "/home/x/p", "--mcp-config", project,
                                                "--mcp-config", own, "--mcp-config", os.path.join(d, "gone.json"),
                                                "--strict-mcp-config"])
        self.assertEqual(launched, frozenset({"maps", "docs", "local"}))
        # a module's: JSON, several after one flag, the = form
        self.assertEqual(StreamHost.launched_servers(["--mcp-config", '{"mcpServers": {"x": {}}}', project,
                                                      "--mcp-config=" + own, "--verbose"]),
                         frozenset({"x", "local", "maps", "docs"}))
        self.host.procs["a"].mcp_launched = launched
        self.assertEqual(self.host.mcp_set("a", tools=["docs"], live=["docs"]), "unchanged")
        self.assertEqual(self.popen.lines, [])


class Servers(unittest.TestCase):
    def test_the_set_is_what_launch_flags_writes(self):
        cat = catalog.build({"mcp": {"maps": {"server": {"command": "maps-mcp"}},
                                     "docs": {"server": {"command": "docs-mcp"}, "as": "wiki"}}})
        cfg = {"base": ["maps"]}
        d = tempfile.mkdtemp(prefix="ml-")
        flags = catalog.launch_flags(["docs"], cfg, cat, "work", None, "all", d, "a")
        with open(flags[flags.index("--mcp-config") + 1]) as f:
            written = json.load(f)["mcpServers"]
        self.assertEqual(catalog.session_servers(["docs"], cfg, cat, "work", name="a", cwd=None, project="all"),
                         written)
        self.assertEqual(sorted(written), ["maps", "wiki"])


class FakePopen:
    pid = 1

    def __init__(self):
        self.lines = []
        self.stdin = self

    def poll(self):
        return None

    def write(self, line):
        self.lines.append(json.loads(line))

    def flush(self):
        pass


if __name__ == "__main__":
    unittest.main()
