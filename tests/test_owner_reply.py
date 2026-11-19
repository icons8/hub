"""owner_reply: the result addressed strictly to the owner — a turn on the owner's message, a reply to the owner's task, or
the continuation of such a turn with no marker (background work ended). Service traffic of the core and of sessions
(REPLIED, HANDOFF, "X is busy", the watcher, "push it" from another session) does not set it, though it sets owner_out. The
page hands it to plugins through hub.addUnread."""
import tests  # noqa: F401
import os
import unittest

from core import reducer, views
from tests.fakes import lang

OWNER = {"channel": "web", "owner": True}
KERNEL = {"channel": "session", "owner": False, "sender": lang.legacy("who")["core"]}
PEER = {"channel": "session", "owner": False, "sender": "agent-dev"}
HUB = {"channel": "session", "owner": False, "sender": "hub"}


class OwnerReply(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        for n, kind in (("hub", "hub"), ("w", None)):
            self.ev("session.requested", n, cwd="/w", session_id=f"s-{n}", kind=kind, **OWNER)
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, _ = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                 "entity": entity, "data": data})
        return self.seq

    def turn(self, name, marker, text):
        self.ev("turn.started", name, **({"marker": f"m{marker}"} if marker else {}))
        return self.ev("turn.result", name, text=text)

    def reply(self, name):
        return self.st["sessions"][name].get("owner_reply") or 0

    def test_owner_message_sets_it(self):
        m = self.ev("message.received", None, to="w", text="what is there?", **OWNER)
        r = self.turn("w", m, "all is going")
        self.assertEqual(self.reply("w"), r)

    def test_kernel_and_peer_messages_do_not(self):
        for who in (KERNEL, PEER):
            m = self.ev("message.received", None, to="w", text="REPLIED d1 / push abc", **who)
            self.turn("w", m, "pushed")
        self.assertTrue(self.st["sessions"]["w"].get("owner_out"))   # the old badge would light up
        self.assertEqual(self.reply("w"), 0)

    def test_owner_task_answer_sets_it(self):
        m = self.ev("message.received", None, to="hub", text="fix the map", **OWNER)
        d = self.ev("delegation.created", None, session="w", **{"for": f"m{m}"}, **HUB)
        r = self.turn("w", d, "done")
        self.assertEqual(self.reply("w"), r)

    def test_background_wake_continues_owner_turn(self):
        m = self.ev("message.received", None, to="w", text="run the tests", **OWNER)
        self.turn("w", m, "started, will write the result")
        r = self.turn("w", None, "tests are green")      # Claude Code woke it: background work ended
        self.assertEqual(self.reply("w"), r)
        k = self.ev("message.received", None, to="w", text="watcher: background is silent", **KERNEL)
        self.turn("w", k, "stopped")
        self.turn("w", None, "background stopped")          # continuation of a service turn — not to the owner
        self.assertEqual(self.reply("w"), r)

    def test_continuing_the_owners_turn_with_a_reply_to_the_hubs_task_does_not(self):
        # the hub inserted its task into the owner's turn (--now), the turn ended empty; a continuation without a marker
        # (background work ended)
        # closed the hub's task: that is a reply to the hub, so neither owner_out nor owner_reply
        m = self.ev("message.received", None, to="w", text="run the tests", **OWNER)
        self.ev("turn.started", "w", marker=f"m{m}")
        d = self.ev("delegation.created", None, session="w", for_session="hub", context="collect the log", now=True, **HUB)
        self.ev("turn.started", "w", marker=f"m{d}")
        self.ev("turn.result", "w", text="")
        self.ev("turn.started", "w")
        r = self.ev("turn.result", "w", text="log collected")
        self.assertEqual(self.st["delegations"][f"d{d}"].get("replied_seq"), r)
        self.assertNotEqual(self.st["sessions"]["w"].get("owner_out"), r)
        self.assertNotEqual(self.reply("w"), r)

    def test_the_start_of_the_answer_survives_a_later_result(self):
        m = self.ev("message.received", None, to="w", text="what is there?", **OWNER)
        r = self.turn("w", m, "all is going")
        p = self.ev("message.received", None, to="w", text="push it", **PEER)
        self.turn("w", p, "pushed")
        s = self.st["sessions"]["w"]
        self.assertEqual((s["out_head"], s["reply_head"], s["reply_ts"]), ("pushed", "all is going", 100 + r))
        rd = views.Reader()
        rd.state = self.st
        row = {x["name"]: x for x in rd.snapshot(now=500)["sessions"]}["w"]
        self.assertEqual((row["reply_head"], row["reply_ts"], row["out_head"]), ("all is going", 100 + r, "pushed"))

    def test_snapshot_has_owner_reply_and_seen(self):
        r = views.Reader()
        r.state = self.st
        m = self.ev("message.received", None, to="w", text="what is there?", **OWNER)
        out = self.turn("w", m, "all is going")
        r.state = self.st
        row = {s["name"]: s for s in r.snapshot(now=500)["sessions"]}["w"]
        self.assertEqual(row["owner_reply"], out)
        self.assertLess(row["seen"], out)   # owner wrote to it (seen = that message), the result came later — unread


class Page(unittest.TestCase):
    JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "modules", "web", "static", "js")

    def read(self, name):
        with open(os.path.join(self.JS, name), encoding="utf-8") as f:
            return f.read()

    def test_plugins_start_from_the_cores_value_and_seen_follows_the_core(self):
        # review: a plugin's previous answer was fed back to it (a second addUnread, a patch without sessions) as a core
        # value, and
        # the plugin's cleared "new" did not give /api/seen: the core's seen stuck
        api = self.read("api.js")
        apply = api[api.index("export function applyUnread"):api.index("export function connect")]
        self.assertIn("s.core_unread ??= !!s.unread", apply)
        self.assertIn("s.unread = s.core_unread", apply)
        self.assertIn("if (!S.unread.length || applying) return;", apply)   # hub.redraw from addUnread: no recursion
        main = self.read("main.js")
        self.assertIn("const isUnread = (s) => s.core_unread ?? s.unread;", main)       # one helper: the core's value, not the plugin's
        self.assertIn("if (!isUnread(s)", main[main.index("function markSeen"):])
        ext = self.read("ext.js")
        self.assertIn("  redraw,\n", ext)                 # hub.redraw recounts the plugins' "new"
        self.assertIn("applyUnread();", ext[ext.index("function redraw()"):])


if __name__ == "__main__":
    unittest.main()
