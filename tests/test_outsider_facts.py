"""A mailbox fact from a module or a plugin process does not speak for a session, the hub, the core or the owner:
the core sets the channel and the sender — plugin and mod:<name> / ext:<name>. The owner's word from a module handle
(channel mod:<name>, owner) stays as it was.

Failure (PR #187 review): a module put message.received with channel "session" and sender "hub" — the core gave it
the hub's rights (wake, delete sessions), and with the sender of a live session, that session's name."""
import tests  # noqa: F401 — hide this machine's settings
import os
import tempfile
import types
import unittest

from core import spool
from core.daemon import Hub
from tests.fakes import lang

OWNER = {"channel": "cli", "owner": True}


class Outsider(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="outsider-")
        self.box = os.path.join(self.state, "spool")
        self.hub = Hub(self.state, types.SimpleNamespace(poll=lambda: []))
        self.hub.execute = lambda a: None
        self.put("session.requested", "hub", {"cwd": "/w", "session_id": "s-hub", "kind": "hub", **OWNER}, src="cli")
        self.put("session.requested", "a", {"cwd": "/w", "session_id": "s-a", **OWNER}, src="cli")
        self.put("session.requested", "b", {"cwd": "/w", "session_id": "s-b", **OWNER}, src="cli")
        for name in ("hub", "a", "b"):
            self.put("session.started", name, {"pid": 1}, src="host")

    def put(self, kind, entity=None, data=None, src="module"):
        spool.put(self.box, kind, entity, data, src=src)
        self.hub.ingest()

    def last(self, kind):
        from core import journal
        evs = journal.read_tail(os.path.join(self.state, "journal.jsonl"))
        return [e for e in evs if e["type"] == kind][-1]

    def rejected(self, ev):
        from core import journal
        evs = journal.read_tail(os.path.join(self.state, "journal.jsonl"))
        return next((e["data"]["reason"] for e in evs if e["type"] == "core.rejected" and e["data"]["of"] == ev["seq"]),
                    None)

    def said(self, data, src="module", to="a"):
        self.put("message.received", None, {"to": to, "text": "hello", **data}, src=src)
        return self.last("message.received")

    # --- forgeries ---
    def test_a_module_cannot_speak_as_the_hub(self):
        ev = self.said({"channel": "session", "owner": False, "sender": "hub"}, to="a")
        self.assertEqual((ev["data"]["channel"], ev["data"]["owner"]), ("plugin", False))
        self.assertNotEqual(ev["data"]["sender"], "hub")
        self.assertIsNotNone(self.rejected(ev))   # a module without a name is rejected, not "from the hub"

    def test_hubmod_fact_shape_keeps_its_module_name_even_with_a_forged_channel(self):
        ev = self.said({"channel": "session", "owner": False, "sender": "mod:fake"})
        self.assertEqual((ev["data"]["channel"], ev["data"]["sender"], ev["data"]["owner"]), ("plugin", "mod:fake", False))
        self.assertIsNone(self.rejected(ev))
        msg = self.hub.state["messages"][f"m{ev['seq']}"]
        self.assertEqual(msg["from"], {"kind": "module", "name": "fake"})

    def test_an_extension_cannot_speak_as_a_live_session(self):
        ev = self.said({"channel": "session", "owner": False, "sender": "b"}, src="ext:hello")
        self.assertEqual((ev["data"]["channel"], ev["data"]["sender"]), ("plugin", "ext:hello"))
        msg = self.hub.state["messages"][f"m{ev['seq']}"]
        self.assertEqual(msg["from"], {"kind": "plugin", "name": "hello"})   # the signature has no prefix
        self.assertIn("from plugin hello", msg.get("origin") or "")

    def test_an_extension_named_like_the_hub_gets_no_hub_rights(self):
        # bin/hub from a plugin process: channel plugin, sender is the bare extension name (HUB_EXTENSION=hub)
        self.put("session.closed", "b", {"channel": "plugin", "owner": False, "sender": "hub"}, src="cli")
        ev = self.last("session.closed")
        self.assertEqual(ev["data"]["sender"], "ext:hub")
        self.assertIsNotNone(self.rejected(ev))
        self.assertNotEqual(self.hub.state["sessions"]["b"]["state"], "closed")

    def test_a_module_without_a_channel_is_not_the_core(self):
        # without a channel the fold treats the fact as the core's (guards.from_hub_or_owner) — a module may not
        self.put("session.removed", "b", {"sender": "mod:fake"})
        ev = self.last("session.removed")
        self.assertEqual(ev["data"]["channel"], "plugin")
        self.assertIsNotNone(self.rejected(ev))
        self.assertNotEqual(self.hub.state["sessions"]["b"]["state"], "removed")

    def test_a_module_cannot_claim_the_owner_on_his_channels(self):
        for ch in ("web", "telegram", "cli"):
            ev = self.said({"channel": ch, "owner": True, "sender": "mod:fake"})
            self.assertEqual((ev["data"]["channel"], ev["data"]["owner"]), ("plugin", False), ch)

    def test_an_extension_cannot_use_the_module_owner_word(self):
        ev = self.said({"channel": "mod:fake", "owner": True}, src="ext:hello")
        self.assertEqual((ev["data"]["channel"], ev["data"]["owner"], ev["data"]["sender"]),
                         ("plugin", False, "ext:hello"))

    def test_a_declared_module_with_the_default_src_is_still_a_module(self):
        # the module field (a module fact, EXTENDING "Core events") with the default src (cli): without core.write —
        # the core's stamp
        import json
        os.makedirs(self.box, exist_ok=True)
        with open(os.path.join(self.box, "99999999999999999999-1-a.json"), "w", encoding="utf-8") as f:
            json.dump({"type": "message.received", "entity": None, "module": "fake", "src": "cli", "ts": 0,
                       "data": {"to": "a", "text": "x", "channel": "session", "owner": False, "sender": "hub"}}, f)
        self.hub.ingest()
        ev = self.last("message.received")
        self.assertEqual((ev["data"]["channel"], ev["data"]["sender"]), ("plugin", "mod:fake"))

    def test_a_core_event_from_a_nameless_module_is_refused_as_a_core_event(self):
        self.put("core.snapshot", None, {"state": {}, "req": "r2"})
        ev = self.last("spool.refused")
        self.assertNotIn("without a name", self.rejected(ev))

    def test_the_refused_core_stub_carries_the_rewritten_sender(self):
        self.put("core.snapshot", None, {"channel": "session", "owner": False, "sender": "hub", "req": "r1"},
                 src="ext:hello")
        ev = self.last("spool.refused")
        self.assertEqual(ev["data"]["sender"], "ext:hello")

    # --- legitimate paths — as before ---
    def test_owner_word_from_a_module_handle_stays(self):
        ev = self.said({"channel": "mod:fake", "owner": True, "sender": "hub"})
        self.assertEqual((ev["data"]["channel"], ev["data"]["owner"]), ("mod:fake", True))
        self.assertNotIn("sender", ev["data"])   # the owner's word needs no sender — and a foreign one will not stick
        self.assertIsNone(self.rejected(ev))

    def test_a_session_and_the_owner_are_untouched(self):
        ev = self.said({"channel": "session", "owner": False, "sender": "b"}, src="session")
        self.assertEqual((ev["data"]["channel"], ev["data"]["sender"]), ("session", "b"))
        ev = self.said({"channel": "web", "owner": True}, src="web")
        self.assertEqual((ev["data"]["channel"], ev["data"]["owner"]), ("web", True))

    def test_module_state_and_extension_records_still_go_in(self):
        self.hub.record("module.ready", "fake", {"module": "fake", "version": "1.0.0", "points": [], "launch": "L",
                                                 "channel": "core"})
        self.put("mod.fake.set", None, {"key": "k", "value": 1})
        self.assertEqual(self.hub.state["mods"]["fake"]["k"]["value"], 1)
        self.put("ext.hello.stats", None, {"n": 1}, src="ext:hello")
        ev = self.last("ext.hello.stats")
        self.assertIsNone(self.rejected(ev))

    def test_a_module_failure_seen_by_the_page_is_still_written(self):
        # the page's modrun.Modules and hub <module command> are core code: their module.error is not a module fact
        from core import modrun
        self.hub.record("module.ready", "fake", {"module": "fake", "version": "1.0.0", "points": [], "launch": "L",
                                                 "channel": "core"})
        modrun.Modules(self.state, records=lambda: {}).fail("fake", "http", "timeout", "slow", launch="L")
        self.hub.ingest()
        ev = self.last("module.error")
        # no channel (channel core in a mailbox file is a core forgery, core/admit.py) and not a module fact: no sender
        self.assertNotIn("channel", ev["data"])
        self.assertNotIn("sender", ev["data"])
        self.assertIsNone(self.rejected(ev))
        self.assertEqual(self.hub.state["modules"]["fake"]["last"]["kind"], "timeout")

    def test_a_module_cannot_report_a_failure_of_another_module(self):
        # another module's failures add up toward its stop: a module's module.error may only be about itself
        err = {"hook": "call", "kind": "timeout", "error": "slow"}
        shapes = (("mod:fake", {}), ("ext:hello", {}), ("cli", {"channel": "plugin", "sender": "mod:fake"}),
                  ("cli", {"channel": "plugin", "sender": "hello"}))   # the last one is bin/hub from a plugin process
        for src, extra in shapes:
            for _ in range(5):
                self.put("module.error", "victim", {"module": "victim", **err, **extra}, src=src)
                self.assertIn("only about itself", self.rejected(self.last("module.error")) or "", (src, extra))
            self.assertNotIn("victim", self.hub.state.get("modules") or {}, src)
        self.put("module.error", "victim", {**err}, src="mod:fake")   # without data.module — not about itself either
        self.assertIsNotNone(self.rejected(self.last("module.error")))

    def test_a_forged_module_field_does_not_pass_a_foreign_failure(self):
        # the module field in a mailbox file names victim as the "sender": but the core rejects module.* without
        # core.write earlier
        import json
        os.makedirs(self.box, exist_ok=True)
        with open(os.path.join(self.box, "99999999999999999999-2-a.json"), "w", encoding="utf-8") as f:
            json.dump({"type": "module.error", "entity": "victim", "module": "victim", "src": "mod:fake", "ts": 0,
                       "data": {"module": "victim", "hook": "call", "kind": "timeout", "error": "x"}}, f)
        self.hub.ingest()
        self.assertIsNotNone(self.rejected(self.last("module.error")))
        self.assertNotIn("victim", self.hub.state.get("modules") or {})

    def test_a_module_reports_its_own_failure(self):
        self.put("module.error", "fake", {"module": "fake", "hook": "call", "kind": "timeout", "error": "slow"},
                 src="mod:fake")
        ev = self.last("module.error")
        self.assertIsNone(self.rejected(ev))
        self.assertEqual(self.hub.state["modules"]["fake"]["last"]["kind"], "timeout")

    def test_the_feed_row_names_the_module_not_the_owner(self):
        # the page draws a line without sender as the owner's bubble ("You · plugin")
        from core import views
        ev = self.said({"channel": "plugin", "owner": False, "sender": "mod:fake"})
        (_, row), = views._items(self.hub.state, ev)
        self.assertEqual((row["sender"], row["channel"]), ("mod:fake", "plugin"))
        old = {**ev, "data": {**ev["data"], "sender": "agent-twitter"}}   # an old plugin fact: a bare name
        self.assertEqual(views._items(self.hub.state, old)[0][1]["sender"], "ext:agent-twitter")
        ev = self.said({"channel": "web", "owner": True}, src="web")
        self.assertIsNone(views._items(self.hub.state, ev)[0][1]["sender"])

    def test_a_plugin_post_keeps_its_byline(self):
        self.put("ext.post", None, {"plugin": "desk", "text": "post", "channel": "plugin", "owner": False,
                                    "sender": "desk"}, src="ext:desk")
        self.assertEqual(self.hub.state["feeds"]["desk"][-1]["by"], lang.legacy("post_by")["plugin"].format(name="desk"))


class Rule(unittest.TestCase):
    """spool.outsider: whose fact it is and which channel to write it with."""

    def test_not_an_outsider(self):
        for item in ({"src": "cli", "data": OWNER}, {"src": "session", "data": {"channel": "session", "sender": "a"}},
                     {"src": "hook", "data": {}}, {"src": "web", "data": {"channel": "web", "owner": True}}):
            self.assertEqual(spool.outsider(item), (None, None), item)

    def test_identity(self):
        cases = [({"src": "ext:hello", "data": {"sender": "hub"}}, "ext:hello"),
                 ({"src": "cli", "data": {"channel": "plugin", "sender": "desk"}}, "ext:desk"),
                 ({"src": "cli", "data": {"channel": "plugin", "sender": "mod:fake"}}, "mod:fake"),
                 ({"src": "plugin", "data": {"channel": "plugin", "sender": "ext:x"}}, "ext:x"),
                 ({"src": "module", "type": "mod.fake.set", "data": {"key": "k"}}, "mod:fake"),
                 ({"src": "module", "module": "fake", "type": "session.said", "data": {}}, "mod:fake"),
                 ({"src": "cli", "module": "fake", "type": "session.said", "data": {"sender": "hub"}}, "mod:fake"),
                 ({"src": "mod:fake", "type": "session.said", "data": {}}, "mod:fake")]
        for item, who in cases:
            data, why = spool.outsider(item)
            self.assertEqual((data["sender"], data["channel"], data["owner"], why), (who, "plugin", False, None), item)

    def test_a_module_without_a_name_is_refused(self):
        for d in ({"sender": "hub"}, {"channel": "mod:fake", "owner": False}):   # the mod: channel is only for the owner's word
            data, why = spool.outsider({"src": "module", "type": "message.received", "data": d})
            self.assertEqual((data["channel"], data["owner"], data["sender"]), ("plugin", False, "mod:?"))
            self.assertTrue(why)

    def test_another_modules_state_is_refused(self):
        # a plugin process via bin/hub (bare name) and a module via hubmod.fact write mod.<other>.* — not their own
        for item in ({"src": "cli", "type": "mod.other.set", "data": {"channel": "plugin", "sender": "desk"}},
                     {"src": "module", "type": "mod.other.set", "data": {"sender": "mod:fake"}}):
            data, why = spool.outsider(item)
            self.assertNotEqual(data["sender"], "mod:other")
            self.assertTrue(why, item)
        self.assertEqual(spool.outsider({"src": "module", "type": "mod.fake.set", "data": {}})[1], None)


if __name__ == "__main__":
    unittest.main()
