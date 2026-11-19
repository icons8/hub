"""The catalog and session tools as a whole: a catalog built from core plugins and a skill set, launch flags, a grant
with a restart through --resume, the owner's "yes", the hub session, a message without an address, a command from a
session."""
import tests  # noqa: F401 — ignore the machine's settings (~/.config/hub-core), see tests/__init__.py
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from core import catalog, i18n, journal, spool
from core.daemon import Hub
from hosts.streamjson import StreamHost
from tests.fakes import lang

HERE = os.path.dirname(__file__)
FAKE = os.path.join(HERE, "fakes", "fake_claude_stream.py")
HUB_CLI = os.path.join(HERE, "..", "bin", "hub")


def make_set(root):
    """A small skill set in marketplace format: jira (no risk) and stripe (money)."""
    os.makedirs(os.path.join(root, ".claude-plugin"))
    for name, kw in (("jira", []), ("stripe", ["risk:money"])):
        d = os.path.join(root, "plugins", name, ".claude-plugin")
        os.makedirs(d)
        with open(os.path.join(d, "plugin.json"), "w") as f:
            json.dump({"name": name, "version": "0.1.0"}, f)
    with open(os.path.join(root, ".claude-plugin", "marketplace.json"), "w") as f:
        json.dump({"name": "hub-skills", "owner": {"name": "t"}, "plugins": [
            {"name": "jira", "source": "./plugins/jira", "description": "Jira"},
            {"name": "stripe", "source": "./plugins/stripe", "description": "Stripe", "keywords": ["risk:money"]}]}, f)


class HubCase(unittest.TestCase):
    """A core with a hub session, a skill set, MCP and a project, running a fake claude."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubt-")
        self.set = os.path.join(self.state, "set")
        make_set(self.set)
        self.project = os.path.join(self.state, "proj")
        os.makedirs(self.project)
        with open(os.path.join(self.project, ".mcp.json"), "w") as f:
            json.dump({"mcpServers": {"projmcp": {"command": "true"}}}, f)
        self.hub_home = os.path.join(self.state, "hub-home")
        os.makedirs(self.hub_home)
        with open(os.path.join(self.state, "hub.json"), "w") as f:
            json.dump({"hub": {"enabled": True, "name": "hub", "cwd": self.hub_home},
                       "sets": [{"name": "hub-skills", "path": self.set}],
                       "mcp": {"demo-mcp": {"server": {"command": "true"}, "description": "demo", "risk": []}}}, f)
        self.argv_dir = os.path.join(self.state, "argv")
        self.old_config = os.environ.get("HUB_CONFIG")
        os.environ["HUB_CONFIG"] = os.path.join(self.state, "hub.json")  # do not use the real ~/.config of the machine
        # here the hub is like a person's: with system agents (core/system.py); e2e tests write on behalf of one
        self.no_system = os.environ.pop("HUB_NO_SYSTEM_AGENTS", None)
        self.hub = self.new_hub()
        self.hub.start()

    def tearDown(self):
        self.hub.host.kill_server()
        time.sleep(0.2)
        shutil.rmtree(self.state, ignore_errors=True)
        os.environ.pop("HUB_CONFIG", None)
        if self.old_config is not None:
            os.environ["HUB_CONFIG"] = self.old_config
        if self.no_system is not None:
            os.environ["HUB_NO_SYSTEM_AGENTS"] = self.no_system

    def new_hub(self):
        host = StreamHost(self.state, claude=(sys.executable, FAKE),
                          env={"FAKE_TURN_S": "0.2", "FAKE_ARGV_DIR": self.argv_dir})
        return Hub(self.state, host, registry_dir=os.path.join(self.state, "registry"))

    def put(self, type, entity=None, **data):
        spool.put(os.path.join(self.state, "spool"), type, entity, data)

    def until(self, pred, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            self.hub.tick()
            if pred():
                return
            time.sleep(0.03)
        self.fail(f"timed out; {json.dumps(self.hub.state['sessions'], ensure_ascii=False)[:1500]}")

    def sess(self, name):
        return self.hub.state["sessions"].get(name, {})

    def argvs(self, name):
        return [json.load(open(p)) for p in sorted(glob.glob(os.path.join(self.argv_dir, f"{name}-*.json")))]

    def plugin_dirs(self, argv):
        return [argv[i + 1] for i, a in enumerate(argv) if a == "--plugin-dir"]


class ToolsTest(HubCase):
    def test_catalog_has_core_plugins_set_and_mcp(self):
        cat = self.hub.state["catalog"]
        self.assertEqual(cat["core:hub-base"]["kind"], "plugin")
        self.assertIn("core:hub-coordinator", cat)
        self.assertEqual(cat["stripe@hub-skills"]["risk"], ["money"])
        self.assertEqual(cat["jira@hub-skills"]["path"], os.path.join(self.set, "plugins", "jira"))
        self.assertEqual(cat["demo-mcp"]["kind"], "mcp")

    def test_new_skill_in_set_reaches_catalog_without_restart(self):
        d = os.path.join(self.set, "plugins", "figma", ".claude-plugin")
        os.makedirs(d)
        with open(os.path.join(d, "plugin.json"), "w") as f:
            json.dump({"name": "figma", "version": "0.1.0"}, f)
        m = os.path.join(self.set, ".claude-plugin", "marketplace.json")
        market = json.load(open(m))
        market["plugins"].append({"name": "figma", "source": "./plugins/figma", "description": "Figma"})
        json.dump(market, open(m, "w"))
        self.hub.next_catalog = 0  # time for the reconciliation has come
        self.hub.tick()
        self.assertIn("figma@hub-skills", self.hub.state["catalog"])
        loads = [e for e in journal.read(self.hub.journal.path) if e["type"] == "catalog.loaded"]
        self.hub.next_catalog = 0
        self.hub.tick()  # nothing changed: no new entry
        self.assertEqual(len([e for e in journal.read(self.hub.journal.path) if e["type"] == "catalog.loaded"]),
                         len(loads))

    def test_new_skill_in_set_reaches_catalog_without_restart(self):
        d = os.path.join(self.set, "plugins", "figma", ".claude-plugin")
        os.makedirs(d)
        with open(os.path.join(d, "plugin.json"), "w") as f:
            json.dump({"name": "figma", "version": "0.1.0"}, f)
        m = os.path.join(self.set, ".claude-plugin", "marketplace.json")
        market = json.load(open(m))
        market["plugins"].append({"name": "figma", "source": "./plugins/figma", "description": "Figma"})
        json.dump(market, open(m, "w"))
        self.hub.next_catalog = 0  # time for the reconciliation has come
        self.hub.tick()
        self.assertIn("figma@hub-skills", self.hub.state["catalog"])
        count = lambda: len([e for e in journal.read(self.hub.journal.path) if e["type"] == "catalog.loaded"])  # noqa
        before = count()
        self.hub.next_catalog = 0
        self.hub.tick()  # nothing changed: no new entry
        self.assertEqual(count(), before)

    def test_hub_session_guaranteed_with_its_tools_and_gets_unaddressed_messages(self):
        self.until(lambda: self.sess("hub").get("state") == "idle")
        argv = self.argvs("hub")[0]
        dirs = self.plugin_dirs(argv)
        self.assertTrue(any(d.endswith(os.path.join("plugins", "hub-base")) for d in dirs))
        self.assertTrue(any(d.endswith(os.path.join("plugins", "hub-coordinator")) for d in dirs))
        self.assertIn("--strict-mcp-config", argv)  # account connectors are off
        self.put("session.close_requested", "hub")
        self.put("message.received", None, text="hello")
        self.until(lambda: any(m["session"] == "hub" and m["state"] == "delivered"
                               for m in self.hub.state["messages"].values()))
        self.until(lambda: self.sess("hub")["state"] == "idle")
        # the hub was not closed: rejected by the core
        self.assertTrue(any(e["type"] == "core.rejected" and e["data"]["type"] == "session.close_requested"
                            for e in journal.read(self.hub.journal.path)))

    def test_work_session_project_mcp_and_grant_restarts_with_plugin(self):
        self.put("session.requested", "a", cwd=self.project, session_id="sid-a")
        self.until(lambda: self.sess("a").get("state") == "idle")
        first = self.argvs("a")[0]
        self.assertIn(os.path.join(self.project, ".mcp.json"), first)  # the project's MCP is passed explicitly
        i = first.index("--setting-sources")  # without the user's settings: skills, plugins, hooks (Mac 30.09); Claude Code
        # still reads their ~/.claude/CLAUDE.md as a parent-folder CLAUDE.md for a session whose folder is inside home
        # (checked 05.10)
        self.assertEqual(first[i + 1], "project,local")
        # sessions write to each other only through the hub (owner decision 2026-10-01): Claude Code's built-in
        # sockets bypass the journal
        i = first.index("--disallowedTools")
        self.assertEqual(first[i + 1:i + 3], ["SendMessage", "ListAgents"])
        self.assertNotIn("core:hub-coordinator", " ".join(first))
        self.assertFalse(any(d.endswith("hub-coordinator") for d in self.plugin_dirs(first)))
        self.put("message.received", None, to="a", text="hello")  # one turn: the conversation is on disk, then --resume
        self.until(lambda: any(m["state"] == "delivered" for m in self.hub.state["messages"].values()
                               if m["session"] == "a") and self.sess("a")["state"] == "idle")
        self.put("tools.requested", "a", tool="jira@hub-skills", why="ticket")
        self.put("tools.granted", "a", tool="jira@hub-skills", channel="session", owner=False)
        self.put("tools.granted", "a", tool="demo-mcp", channel="session", owner=False)
        self.until(lambda: len(self.argvs("a")) >= 2 and self.sess("a").get("state") == "idle"
                   and not self.sess("a").get("dirty"))
        last = self.argvs("a")[-1]
        self.assertIn("--resume", last)
        self.assertIn(os.path.join(self.set, "plugins", "jira"), self.plugin_dirs(last))
        mcp_file = os.path.join(self.state, "sessions", "a", "mcp.json")
        self.assertIn(mcp_file, last)
        self.assertIn("demo-mcp", json.load(open(mcp_file))["mcpServers"])

    def test_mcp_granted_mid_turn_is_asked_of_the_running_process_and_the_restart_stays(self):
        # a tool granted mid-turn came only with the restart after the turn: now the core asks the running Claude Code
        # to add it (mcp_set_servers); a refusal (an older one: so does the fake) leaves the restart as it was
        self.put("session.requested", "a", cwd=self.project, session_id="sid-a")
        self.until(lambda: self.sess("a").get("state") == "idle")
        self.put("message.received", None, to="a", text="/slow")
        self.until(lambda: self.sess("a").get("state") == "busy")
        self.put("tools.granted", "a", tool="demo-mcp", channel="session", owner=False)
        events = lambda: list(journal.read(self.hub.journal.path))  # noqa: E731
        self.until(lambda: any(e["type"] == "session.mcp_set" for e in events()))
        (fact,) = [e["data"] for e in events() if e["type"] == "session.mcp_set"]
        self.assertEqual((fact["ok"], fact.get("unsupported"), fact["servers"]), (False, True, ["demo-mcp"]))
        self.assertTrue(any(e["type"] == "host.done" and e["data"].get("do") == "mcp_set" and
                            e["data"].get("result") == "sent" for e in events()))
        self.assertFalse([e for e in events() if e["type"] == "host.error"])
        self.assertEqual((len(self.argvs("a")), self.sess("a")["state"]), (1, "busy"))   # the turn goes on
        self.hub.host.interrupt("a")
        self.until(lambda: len(self.argvs("a")) >= 2 and self.sess("a").get("state") == "idle"
                   and not self.sess("a").get("dirty"))
        mcp_file = os.path.join(self.state, "sessions", "a", "mcp.json")
        self.assertIn(mcp_file, self.argvs("a")[-1])
        self.assertIn("demo-mcp", json.load(open(mcp_file))["mcpServers"])

    def test_request_reaches_the_hub(self):
        # 30.09: a session asked for research@hub-skills; the request went to the journal but nobody told the hub
        self.put("session.requested", "a", cwd=self.project, session_id="sid-a")
        self.until(lambda: self.sess("a").get("state") == "idle")
        self.put("tools.requested", "a", tool="jira@hub-skills", why="ticket", sender="a")
        self.until(lambda: any(m["session"] == "hub" and "hub tools grant a jira@hub-skills" in m["text"]
                               for m in self.hub.state["messages"].values()))
        told = [m for m in self.hub.state["messages"].values() if "jira@hub-skills" in m["text"]]
        self.assertEqual(len(told), 1)
        self.assertIn("ticket", told[0]["text"])
        # the hub asks for itself: it does not write to itself
        self.put("tools.requested", "hub", tool="stripe@hub-skills", why="invoice", sender="hub")
        self.until(lambda: self.sess("hub")["requests"])
        self.assertFalse(any("stripe@hub-skills" in m["text"] for m in self.hub.state["messages"].values()))

    def test_risky_tool_waits_for_owner(self):
        self.put("session.requested", "a", cwd=self.project, session_id="sid-a")
        self.until(lambda: self.sess("a").get("state") == "idle")
        self.put("tools.granted", "a", tool="stripe@hub-skills", channel="session", owner=False)
        self.until(lambda: any(r["state"] == "needs-owner" for r in self.sess("a")["requests"]))
        self.assertEqual(self.sess("a")["tools"], [])
        self.put("tools.approved", "a", tool="stripe@hub-skills", channel="web", owner=True)
        self.until(lambda: "stripe@hub-skills" in self.sess("a")["tools"] and self.sess("a")["state"] == "idle"
                   and len(self.argvs("a")) >= 2)

    def test_owner_decides_a_sleeping_sessions_request(self):
        # 17.10: for a sleeping session the "waiting for the owner" card never closed: "yes" and a denial got "no live
        # session", and the "yes" was lost. The decision is recorded at once, no process is started for it; the tool
        # arrives with the next wake-up
        self.put("session.requested", "a", cwd=self.project, session_id="sid-a")
        self.until(lambda: self.sess("a").get("state") == "idle")
        self.put("tools.granted", "a", tool="stripe@hub-skills", channel="session", owner=False)
        self.put("tools.granted", "a", tool="jira@hub-skills", channel="session", owner=False)
        self.until(lambda: len(self.argvs("a")) >= 2 and self.sess("a").get("state") == "idle"
                   and not self.sess("a").get("dirty"))   # jira needs no "yes": it comes with the wake-up, stripe waits for the owner
        self.hub.record("session.parked", "a", {"channel": "core", "why": "idle"})
        self.until(lambda: self.sess("a")["state"] == "parked")
        runs, notes = len(self.argvs("a")), len(self.hub.state["messages"])
        self.put("tools.approved", "a", tool="stripe@hub-skills", channel="session", owner=False, sender="b")
        self.put("tools.approved", "a", tool="stripe@hub-skills", channel="web", owner=True)
        self.until(lambda: "stripe@hub-skills" in self.sess("a")["tools"])
        (req,) = [r for r in self.sess("a")["requests"] if r["tool"] == "stripe@hub-skills"]
        self.assertEqual(req["state"], "granted")
        for _ in range(20):
            self.hub.tick()
            time.sleep(0.03)
        self.assertEqual((self.sess("a")["state"], len(self.argvs("a"))), ("parked", runs))   # not woken up for the grant
        self.assertNotIn("new_tools", self.sess("a"))
        self.assertTrue(any(m["session"] == "hub" and "is not up now" in m["text"]
                            for m in list(self.hub.state["messages"].values())[notes:]))
        self.put("message.received", None, to="a", text="hello")   # the wake-up comes with the tool, once
        self.until(lambda: len(self.argvs("a")) > runs and self.sess("a")["state"] == "idle")
        self.assertIn(os.path.join(self.set, "plugins", "stripe"), self.plugin_dirs(self.argvs("a")[-1]))
        for _ in range(20):
            self.hub.tick()
            time.sleep(0.03)
        self.assertEqual(len(self.argvs("a")), runs + 1)
        self.assertFalse(any(m["session"] == "a" and "Connected" in str(m["text"])
                             for m in self.hub.state["messages"].values()))
        (note,) = [m for m in self.hub.state["messages"].values() if m["session"] == "a" and m.get("fyi")]
        self.assertIn("stripe@hub-skills", note["text"])
        self.assertEqual(note["state"], "delivered")   # "for your information": delivered with the message that woke it

    def test_owner_denies_a_sleeping_sessions_request(self):
        self.put("session.requested", "a", cwd=self.project, session_id="sid-a")
        self.until(lambda: self.sess("a").get("state") == "idle")
        self.put("tools.granted", "a", tool="stripe@hub-skills", channel="session", owner=False)
        self.until(lambda: any(r["state"] == "needs-owner" for r in self.sess("a")["requests"]))
        self.hub.record("session.parked", "a", {"channel": "core", "why": "idle"})
        self.until(lambda: self.sess("a")["state"] == "parked")
        runs, notes = len(self.argvs("a")), len(self.hub.state["messages"])
        self.put("tools.denied", "a", tool="stripe@hub-skills", channel="session", owner=False, sender="hub")
        self.put("tools.denied", "a", tool="stripe@hub-skills", reason="not now", channel="web", owner=True)
        self.until(lambda: self.sess("a")["requests"][-1]["state"] == "denied")
        self.assertEqual(self.sess("a")["requests"][-1]["reason"], "not now")   # the owner's denial, not the hub's
        for _ in range(20):
            self.hub.tick()
            time.sleep(0.03)
        self.assertEqual((self.sess("a")["state"], len(self.argvs("a"))), ("parked", runs))
        mine = [m for m in list(self.hub.state["messages"].values())[notes:] if m["session"] == "a"]
        self.assertEqual([(m["state"], "not now" in m["text"]) for m in mine], [("aside", True)])   # does not wake it
        self.put("message.received", None, to="a", text="hello")
        self.until(lambda: len(self.argvs("a")) > runs and self.sess("a")["state"] == "idle")
        self.assertNotIn(os.path.join(self.set, "plugins", "stripe"), self.plugin_dirs(self.argvs("a")[-1]))

    def test_a_denial_reaches_the_session_with_its_reason(self):
        # 14.10: the hub denied a tool with a reason ("there is no such tool, take another"), but the session never
        # learned of it, kept writing "waiting for the browser", and the watcher raised it to the owner as his decision
        self.put("session.requested", "a", cwd=self.project, session_id="sid-a")
        self.until(lambda: self.sess("a").get("state") == "idle")
        self.put("tools.requested", "a", tool="browser:stage", why="check stage", channel="session", owner=False,
                 sender="a")
        self.until(lambda: self.sess("a")["requests"])
        self.put("tools.denied", "a", tool="browser:stage", reason="no such tool, take browser:agents",
                 channel="session", owner=False, sender="hub")
        self.until(lambda: any(m["session"] == "a" and "browser:stage" in m["text"]
                               for m in self.hub.state["messages"].values()))
        (told,) = [m for m in self.hub.state["messages"].values() if m["session"] == "a" and "browser:stage" in m["text"]]
        self.assertIn("no such tool, take browser:agents", told["text"])
        self.assertIn("hub", told["text"])

    def test_a_denial_continues_the_task_and_does_not_wake_a_sleeper(self):
        # review 14.10: the denial note was an "owner message", so the turn did not continue the task and the result
        # never reached whoever set it; and a denial to an executor that had already handed in its work (an old
        # request) woke it
        from core import reducer
        st, seq = reducer.empty(), 0

        def ev(type, entity=None, **data):
            nonlocal st, seq
            seq += 1
            st, _, _ = reducer.reduce(st, {"seq": seq, "ts": 100 + seq, "type": type, "entity": entity, "data": data})
        hub = {"channel": "session", "owner": False, "sender": "hub"}
        for n, kind in (("hub", "hub"), ("x", None), ("up", None), ("old", None)):
            ev("session.requested", n, cwd="/w", session_id=f"s-{n}", kind=kind, channel="web", owner=True)
            ev("session.started", n)
        ev("delegation.created", None, session="x", for_session="up", context="T-2", **hub)
        did = f"d{seq}"
        ev("turn.started", "x", marker=f"m{seq}")
        ev("tools.requested", "x", tool="browser:y", why="needed", channel="session", owner=False, sender="x")
        ev("turn.result", "x", text="waiting for the browser")
        ev("turn.stopped", "x")
        ev("tools.denied", "x", tool="browser:y", reason="take another", **hub)
        note = st["messages"][f"m{seq}"]
        self.assertEqual(note["origin"], " from core")
        ev("turn.started", "x", marker=note["id"])
        ev("turn.result", "x", text="done without the browser")
        ev("turn.stopped", "x")
        self.assertEqual([m["text"] for m in st["messages"].values() if m["session"] == "up"][-1], "done without the browser")
        self.assertEqual(st["delegations"][did]["reply"], "done without the browser")
        ev("tools.requested", "old", tool="browser:z", why="needed", channel="session", owner=False, sender="old")
        st["sessions"]["old"].update(state="asleep", role="executor", retired=True)   # handed in its work, the request remained
        n = len(st["messages"])
        ev("tools.denied", "old", tool="browser:z", channel="web", owner=True)
        self.assertEqual(len(st["messages"]), n)
        self.assertEqual(st["sessions"]["old"]["state"], "asleep")
        ev("tools.requested", "hub", tool="browser:h", why="needed", channel="session", owner=False, sender="hub")
        ev("tools.denied", lang.word("words", "hub"), tool="browser:h", channel="web", owner=True)   # the hub by its alias
        self.assertIn("refused browser:h", list(st["messages"].values())[-1]["text"])

    def test_cli_reports_rejection_from_core(self):
        """The CLI waits until the core records the fact and prints the outcome: a "yes" from a session is
        "rejected" with a reason."""
        import threading
        self.put("session.requested", "a", cwd=self.project, session_id="sid-a")
        self.until(lambda: self.sess("a").get("state") == "idle")
        self.put("tools.granted", "a", tool="stripe@hub-skills", channel="session", owner=False)
        self.until(lambda: any(r["state"] == "needs-owner" for r in self.sess("a")["requests"]))
        stop = threading.Event()
        t = threading.Thread(target=lambda: [self.hub.tick() or time.sleep(0.05) for _ in iter(stop.is_set, True)])
        t.start()
        try:
            env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_SESSION": "a"}
            out = subprocess.run([sys.executable, HUB_CLI, "tools", "approve", "a", "stripe@hub-skills"], env=env,
                                 capture_output=True, text=True, timeout=30).stdout
        finally:
            stop.set()
            t.join()
        self.assertIn("rejected: tools.approved a — only the owner approves", out)   # a session's command: in English

    def test_hub_grants_risky_by_owner_word_in_chat(self):
        """Owner decision 2026-09-30 (option 1): the owner's "yes" to the hub in chat is the permission
        (`grant --for mN`); without it the CLI honestly says "not connected yet" (the Russian CLI text)."""
        import threading
        self.until(lambda: self.sess("hub").get("state") == "idle")
        self.put("session.requested", "a", cwd=self.project, session_id="sid-a")
        self.until(lambda: self.sess("a").get("state") == "idle")
        self.put("message.received", None, text="yes, connect stripe", channel="web", owner=True)
        self.until(lambda: any(m.get("from_owner") for m in self.hub.state["messages"].values()))
        mid = next(m["id"] for m in self.hub.state["messages"].values() if m.get("from_owner"))
        stop = threading.Event()
        t = threading.Thread(target=lambda: [self.hub.tick() or time.sleep(0.05) for _ in iter(stop.is_set, True)])
        t.start()
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_SESSION": "hub"}
        run = lambda *a: subprocess.run([sys.executable, HUB_CLI, *a], env=env, capture_output=True, text=True,  # noqa: E731
                                        timeout=30).stdout
        try:
            plain = run("tools", "grant", "a", "stripe@hub-skills")
            bad = run("tools", "grant", "a", "stripe@hub-skills", "--for", "m1")
            good = run("tools", "grant", "a", "stripe@hub-skills", "--for", mid)
        finally:
            stop.set()
            t.join()
        self.assertIn("is NOT connected yet: waiting for the owner's \"yes\"", plain)   # the hub's command: in English
        self.assertIn("rejected", bad)
        self.assertIn(i18n.tr("en", "cli.tools.grant_done", tool="stripe@hub-skills")
                      + i18n.tr("en", "cli.tools.grant_by_word", message=mid, text="yes, connect stripe"), good)
        self.until(lambda: "stripe@hub-skills" in self.sess("a")["tools"])

    def test_grant_of_a_tool_the_session_has_says_so(self):
        """17.10: a grant for an already connected tool is refused by the core with "already has it", while the CLI
        printed "not connected yet" (the Russian CLI text)."""
        import threading
        self.until(lambda: self.sess("hub").get("state") == "idle")
        self.put("session.requested", "a", cwd=self.project, session_id="sid-a", tools=["demo-mcp"])
        self.until(lambda: self.sess("a").get("state") == "idle")
        stop = threading.Event()
        t = threading.Thread(target=lambda: [self.hub.tick() or time.sleep(0.05) for _ in iter(stop.is_set, True)])
        t.start()
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_SESSION": "hub"}
        try:
            out = subprocess.run([sys.executable, HUB_CLI, "tools", "grant", "a", "demo-mcp"], env=env,
                                 capture_output=True, text=True, timeout=30).stdout
        finally:
            stop.set()
            t.join()
        self.assertIn("already has it", out)
        self.assertIn("demo-mcp is already connected", out)   # the hub's command: in English
        self.assertNotIn("NOT connected", out)

    def test_grant_by_short_name_writes_full_id(self):
        """19.10: "hub tools grant a jira" records "jira@hub-skills" (the only one with that name) in the journal, and
        the CLI speaks about it."""
        import threading
        self.until(lambda: self.sess("hub").get("state") == "idle")
        self.put("session.requested", "a", cwd=self.project, session_id="sid-a")
        self.until(lambda: self.sess("a").get("state") == "idle")
        stop = threading.Event()
        t = threading.Thread(target=lambda: [self.hub.tick() or time.sleep(0.05) for _ in iter(stop.is_set, True)])
        t.start()
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_SESSION": "hub"}
        try:
            out = subprocess.run([sys.executable, HUB_CLI, "tools", "grant", "a", "jira"], env=env,
                                 capture_output=True, text=True, timeout=30).stdout
        finally:
            stop.set()
            t.join()
        self.assertIn("connected: jira@hub-skills", out)   # the hub's command: in English
        self.assertIn("jira@hub-skills", self.sess("a")["tools"])
        grants = [e for e in journal.read(self.hub.journal.path) if e["type"] == "tools.granted"]
        self.assertEqual(grants[-1]["data"]["tool"], "jira@hub-skills")

    def test_grant_to_a_sleeping_session_says_what_is_true(self):
        """17.10: a grant to a sleeping session is refused with "no live session", while the CLI printed "waiting for
        the owner's yes" for the pending request.
        19.10: a sleeping session is granted like a live one, without a wake-up: the pending request really does wait
        for the owner's "yes"."""
        import threading
        self.until(lambda: self.sess("hub").get("state") == "idle")
        self.put("session.requested", "a", cwd=self.project, session_id="sid-a", tools=["demo-mcp"])
        self.until(lambda: self.sess("a").get("state") == "idle")
        self.put("tools.granted", "a", tool="stripe@hub-skills", channel="session", owner=False)
        self.until(lambda: any(r["state"] == "needs-owner" for r in self.sess("a")["requests"]))
        self.hub.record("session.parked", "a", {"channel": "core", "why": "idle"})   # asleep, like a parked one
        self.until(lambda: self.sess("a")["state"] == "parked")
        stop = threading.Event()
        t = threading.Thread(target=lambda: [self.hub.tick() or time.sleep(0.05) for _ in iter(stop.is_set, True)])
        t.start()
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_SESSION": "hub"}
        run = lambda *a: subprocess.run([sys.executable, HUB_CLI, *a], env=env, capture_output=True, text=True,  # noqa: E731
                                        timeout=30).stdout
        try:
            risky, had = run("tools", "grant", "a", "stripe@hub-skills"), run("tools", "grant", "a", "demo-mcp")
            plain, listed = run("tools", "grant", "a", "jira@hub-skills"), run("tools", "list", "a")
        finally:
            stop.set()
            t.join()
        # 19.10: a sleeping session is granted like a live one; risky tools wait for the owner's "yes" (recorded for
        # a sleeping session without a wake-up)
        self.assertNotIn("no live session", risky)
        # the hub's commands: in English
        self.assertIn("stripe@hub-skills is NOT connected yet: waiting for the owner's \"yes\"", risky)
        self.assertIn("demo-mcp is already connected", had)     # the sleeping one has it too, not "not started"
        self.assertIn("connected: jira@hub-skills;", plain)   # then core/tools.grant_note: in the set when it wakes up
        self.assertIn(i18n.tr("en", "cli.tools.granted_later", tool="jira@hub-skills"), listed)
        self.assertEqual(self.sess("a")["state"], "parked")

    def test_cli_inside_session_is_not_owner(self):
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_SESSION": "a", "HUB_CLI_WAIT": "0"}
        subprocess.run([sys.executable, HUB_CLI, "tools", "approve", "a", "stripe@hub-skills"], env=env,
                       check=True, capture_output=True)
        subprocess.run([sys.executable, HUB_CLI, "tools", "request", "jira@hub-skills", "need", "ticket"], env=env,
                       check=True, capture_output=True)
        items = sorted((e for _, e in spool.take(os.path.join(self.state, "spool")) if e["type"].startswith("tools.")),
                       key=lambda e: e["type"])
        self.assertEqual([(e["type"], e["entity"], e["data"]["owner"], e["data"]["channel"]) for e in items],
                         [("tools.approved", "a", False, "session"), ("tools.requested", "a", False, "session")])
        self.assertEqual(items[1]["data"]["why"], "need ticket")


if __name__ == "__main__":
    unittest.main()


class ConfigTest(unittest.TestCase):
    def test_config_location_standard_then_state_dir(self):
        d = tempfile.mkdtemp()
        state, xdg = os.path.join(d, "state"), os.path.join(d, "xdg")
        os.makedirs(state)
        old = {k: os.environ.pop(k, None) for k in ("HUB_CONFIG", "XDG_CONFIG_HOME", "HUB_CONFIG_DIR", "APPDATA")}
        os.environ["XDG_CONFIG_HOME"] = xdg   # the default location, without a second hub's own folder (HUB_CONFIG_DIR)
        os.environ["APPDATA"] = xdg           # the same on Windows (platforms/windows.config_dir)
        try:
            self.assertEqual(catalog.config_path(state), os.path.join(xdg, "hub-core", "hub.json"))
            with open(os.path.join(state, "hub.json"), "w") as f:
                json.dump({"projects": {"old": {"path": "/o"}}}, f)
            self.assertEqual(catalog.load_config(state)["projects"], {"old": {"path": "/o"}})  # the old location
            os.makedirs(os.path.join(xdg, "hub-core"))
            with open(os.path.join(xdg, "hub-core", "hub.json"), "w") as f:
                json.dump({"projects": {"sandbox": {"path": "/s"}}}, f)
            self.assertEqual(catalog.load_config(state)["projects"], {"sandbox": {"path": "/s"}})
        finally:
            for k, v in old.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v
            shutil.rmtree(d, ignore_errors=True)


class DelegationE2E(HubCase):
    """A task as a whole: the hub hands out a task on the owner's message, the executor replies to the owner; the
    reply is in the hub timeline under his message, the hub gets REPLIED; "Done" only explicitly."""

    def test_delegation_reply_reaches_owner_thread_and_hub(self):
        from core import views
        self.until(lambda: self.sess("hub").get("state") == "idle")
        self.put("session.requested", "pay", cwd=self.project, session_id="sid-pay")
        self.until(lambda: self.sess("pay").get("state") == "idle")
        self.put("message.received", None, text="Count the payouts", channel="web", owner=True)
        self.until(lambda: any(m["session"] == "hub" and m["state"] == "delivered"
                               for m in self.hub.state["messages"].values()))
        owner_mid = next(m["id"] for m in self.hub.state["messages"].values() if m["text"] == "Count the payouts")
        self.put("delegation.created", None, session="pay", context="for September", channel="session", owner=False,
                 sender="hub", **{"for": owner_mid})
        self.until(lambda: any(d["state"] == "replied" for d in self.hub.state["delegations"].values()))
        (d,) = self.hub.state["delegations"].values()
        self.assertTrue(d["reply"].startswith("ok: Task from the owner"))
        self.until(lambda: any(m["session"] == "hub" and m["text"].startswith("REPLIED") and m["state"] == "delivered"
                               for m in self.hub.state["messages"].values()))
        reader = views.Reader()
        for ev in journal.read(self.hub.journal.path):
            reader.feed(ev)
        reply = [i for i in reader.timelines["hub"] if i["kind"] == "reply"]
        self.assertEqual((reply[0]["from"], reply[0]["for"]), ("pay", owner_mid))
        self.assertEqual(d["state"], "replied")  # not "done" by itself
        self.put("delegation.done", d["id"], channel="session", owner=False, sender="hub")
        self.until(lambda: self.hub.state["delegations"][d["id"]]["state"] == "done")
