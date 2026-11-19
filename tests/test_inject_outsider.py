"""An insertion mid-turn (message.inject) and its urgency estimate (message.weighed) are the watchdog's or the owner's: a
module or a plugin cannot push its own message into a busy session with them.

Failure: the core refused message.inject only from channel session. A module (hubmod.fact) or a plugin (src ext:<name>)
sent an ordinary message to a busy session, then message.inject on its mN — and the message was typed mid-turn,
bypassing the queue and the rule that urgent (--now) is decided by the hub or the owner."""
import tests  # noqa: F401 — hide this machine's settings
import os
import tempfile
import types
import unittest
from unittest import mock

import platforms
from core import hubmod, journal, spool
from core.daemon import Hub

OWNER = {"channel": "cli", "owner": True}


class InjectFromOutside(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="inject-outsider-")
        self.addCleanup(platforms.current.rmtree, self.state)
        self.box = os.path.join(self.state, "spool")
        self.hub = Hub(self.state, types.SimpleNamespace(poll=lambda: []))
        self.typed = []
        self.hub.execute = lambda a: self.typed.append(a) if a.get("do") == "type" else None
        self.put("session.requested", "hub", {"cwd": "/w", "session_id": "s-hub", "kind": "hub", **OWNER}, src="cli")
        self.put("session.requested", "a", {"cwd": "/w", "session_id": "s-a", **OWNER}, src="cli")
        for name in ("hub", "a"):
            self.put("session.started", name, {"pid": 1}, src="host")
        self.put("turn.started", "a", {"marker": None, "prompt_head": "long work"}, src="hook")
        self.assertEqual(self.hub.state["sessions"]["a"]["state"], "busy")

    def put(self, kind, entity=None, data=None, src="module"):
        spool.put(self.box, kind, entity, data, src=src)
        self.hub.ingest()

    def last(self, kind):
        return [e for e in journal.read_tail(os.path.join(self.state, "journal.jsonl")) if e["type"] == kind][-1]

    def rejected(self, ev):
        evs = journal.read_tail(os.path.join(self.state, "journal.jsonl"))
        return next((e["data"]["reason"] for e in evs if e["type"] == "core.rejected" and e["data"]["of"] == ev["seq"]),
                    None)

    def module_fact(self, kind, entity=None, data=None):
        # the module's real path: hubmod.fact from a process the hub started (HUB_MODULE, HUB_STATE_DIR)
        with mock.patch.dict(os.environ, {"HUB_MODULE": "fake", "HUB_STATE_DIR": self.state}):
            hubmod.fact(kind, data, entity=entity)
        self.hub.ingest()
        return self.last(kind)

    def queued(self, mid):
        msg = self.hub.state["messages"][mid]
        self.assertEqual((msg["state"], msg.get("injected")), ("queued", None))
        self.assertEqual([a for a in self.typed if a.get("message") == mid], [])

    def test_a_module_cannot_insert_its_own_message_mid_turn(self):
        sent = self.module_fact("message.received", data={"to": "a", "text": "mine first"})
        mid = f"m{sent['seq']}"
        self.queued(mid)
        ev = self.module_fact("message.inject", mid)
        self.assertEqual((ev["data"]["channel"], ev["data"]["sender"]), ("plugin", "mod:fake"))
        self.assertIn("not by a module or a plugin", self.rejected(ev) or "")
        self.queued(mid)

    def test_a_plugin_cannot_insert_its_own_message_mid_turn(self):
        self.put("message.received", None, {"to": "a", "text": "mine first"}, src="ext:hello")
        mid = f"m{self.last('message.received')['seq']}"
        self.queued(mid)
        # whatever it says about itself, the core stamps it as the plugin
        self.put("message.inject", mid, {"channel": "cli", "owner": True}, src="ext:hello")
        ev = self.last("message.inject")
        self.assertEqual((ev["data"]["channel"], ev["data"]["sender"]), ("plugin", "ext:hello"))
        self.assertIn("not by a module or a plugin", self.rejected(ev) or "")
        self.queued(mid)

    def test_its_own_seen_at_does_not_pass_it_off_as_a_stale_watchdog_fact(self):
        # a stale watchdog fact is accepted quietly (overtaken); a plugin's is refused all the same
        self.put("message.received", None, {"to": "a", "text": "mine first"}, src="ext:hello")
        mid = f"m{self.last('message.received')['seq']}"
        self.put("turn.result", "a", {"text": "done"}, src="host")   # its message is typed at the end of the turn
        self.assertNotEqual(self.hub.state["messages"][mid]["state"], "queued")
        self.put("message.inject", mid, {"seen_at": 1}, src="ext:hello")
        self.assertIsNotNone(self.rejected(self.last("message.inject")))

    def test_neither_shows_the_watchdogs_urgency_on_its_message(self):
        sent = self.module_fact("message.received", data={"to": "a", "text": "mine first"})
        mid = f"m{sent['seq']}"
        ev = self.module_fact("message.weighed", mid, {"p": 1.0})
        self.assertIn("not by a module or a plugin", self.rejected(ev) or "")
        self.put("message.weighed", mid, {"p": 1.0, "channel": "cli", "owner": True}, src="ext:hello")
        self.assertIn("not by a module or a plugin", self.rejected(self.last("message.weighed")) or "")
        self.assertIsNone(self.hub.state["messages"][mid].get("urgency"))
        self.put("message.weighed", mid, {"p": 0.2, "channel": "cli", "owner": True}, src="watch")
        self.assertEqual(self.hub.state["messages"][mid].get("urgency"), 0.2)

    def test_the_watchdog_the_page_and_the_owner_word_from_a_module_still_insert(self):
        for data, src in (({"p": 0.9, "channel": "cli", "owner": True}, "watch"),   # modules/watch/watch.py insert
                          ({"channel": "web", "owner": True}, "web"),               # the page's "now" (/api/inject)
                          ({"channel": "telegram", "owner": True}, "telegram"),     # the owner's channels
                          ({"channel": "mod:fake", "owner": True}, "module")):      # hubmod.owner on the owner's request
            self.put("message.received", None, {"to": "a", "text": "now", **OWNER}, src="cli")
            mid = f"m{self.last('message.received')['seq']}"
            self.put("message.inject", mid, data, src=src)
            ev = self.last("message.inject")
            self.assertIsNone(self.rejected(ev), src)
            self.assertTrue(self.hub.state["messages"][mid].get("injected"), src)


if __name__ == "__main__":
    unittest.main()
