"""The words of another hub in a session's feed (the page draws a thread with that hub, js/busthread.js): the fields of
the rows come from the fold — an `in` row of a core message has hubbus {contact, body, agent?, thread?, direct?} (the
words alone, out of the core's frame), and what a session handed the liaison for another hub is a `bus` row in the
asker's own timeline. The page does not take the words out of the frame text."""
import tests  # noqa: F401 — keeps the machine's settings out
import unittest

from core import reducer, views
from tests.fakes import lang

FRAME = "From another hub: contact bee, bot is writing there.\n<<<bus-1a2b3c4d\nThe export works\nbus-1a2b3c4d>>>"


def state():
    st = reducer.empty()
    for n, seq in (("hub", 1), ("api", 3), ("liaison", 5)):
        for typ, data in (("session.requested", {"cwd": "/w", "session_id": "s-" + n, "channel": "web", "owner": True,
                                                  **({"kind": "hub"} if n == "hub" else {})}), ("session.started", {})):
            ev = {"seq": seq, "ts": 1000 + seq, "src": "test", "type": typ, "entity": n, "data": data}
            st, _, why = reducer.reduce(st, ev)
            assert why is None, why
            seq += 1
    return st


class Fields(unittest.TestCase):
    def inbox(self, st, **extra):
        st.setdefault("bus", {}).setdefault("inbox", {})["b9"] = {"contact": "bee", "agent_from": "bot", "thread": "t1", "text": "The export works",
                                                                  **extra}

    def msg(self, **extra):
        return {"id": "m9", "session": "api", "text": FRAME, "hubbus": {"inbox": "b9", "contact": "bee", "passed": True}, **extra}

    def test_the_words_come_without_the_frame_and_with_who_wrote_them(self):
        st = state()
        self.inbox(st, direct=True)
        self.assertEqual(views._hubbus(st, self.msg()), {"contact": "bee", "body": "The export works", "agent": "bot", "thread": "t1", "direct": True})

    def test_an_agent_name_the_other_hub_chose_is_checked_like_a_modules_one(self):
        st = state()
        for bad in ("bot\u202egpj", "x" * 41, "", None, 7):
            self.inbox(st)
            st["bus"]["inbox"]["b9"]["agent_from"] = bad
            self.assertNotIn("agent", views._hubbus(st, self.msg()))

    def test_no_inbox_record_or_not_a_hub_message_is_a_plain_row(self):
        st = state()
        self.assertIsNone(views._hubbus(st, self.msg()))
        self.assertIsNone(views._hubbus(st, {"id": "m1", "session": "api", "text": "x"}))
        self.inbox(st)
        self.assertIsNone(views._hubbus(st, self.msg(hubbus={"inbox": "b9"})))   # no contact: not ours

    def test_the_row_of_words_the_liaison_passed_carries_them(self):
        # the feed row of a core message is built in one place (views.core_row) for the live feed and an archive's:
        # the words of another hub go with it, or the page draws the frame instead of the thread
        st = state()
        self.inbox(st, status="ok", marker="bus-1a2b3c4d")
        st["bus"]["contacts"] = {"bee": {"status": "known", "verified": True}}
        r = views.Reader()
        r.state = st
        r.feed_all([{"seq": 20, "ts": 1020, "src": "core", "type": "core.bus_passed", "entity": None,
                     "data": {"inbox": "b9", "to": "api", "channel": "session", "sender": "liaison"}}])
        rows = [it for it in r.timelines["api"] if it.get("kind") == "in"]
        self.assertEqual([it.get("hubbus") for it in rows],
                         [{"contact": "bee", "body": "The export works", "agent": "bot", "thread": "t1"}])

    def test_a_request_for_another_hub_is_a_row_of_its_asker_not_only_of_the_liaison(self):
        st = state()
        st["messages"]["m20"] = {"id": "m20", "session": "liaison", "to": "liaison", "text": "Check the export",
                                 "origin": f" {lang.legacy('from')} api",
                                 "bus_to": "bee", "state": "queued", "attempt": 0, "created": 1, "since": 1}
        ev = {"seq": 20, "ts": 1020, "src": "session", "type": "message.received", "entity": None,
              "data": {"to": "liaison", "text": "Check the export", "bus_to": "bee", "channel": "session", "owner": False, "sender": "api"}}
        by = dict(views._items(st, ev))
        self.assertEqual(by["liaison"]["kind"], "in")
        self.assertEqual({k: by["api"][k] for k in ("kind", "dir", "contact", "text", "id")},
                         {"kind": "bus", "dir": "out", "contact": "bee", "text": "Check the export", "id": "m20"})
        st["messages"]["m20"].pop("bus_to")   # an ordinary message to the liaison: nothing for its asker
        self.assertNotIn("api", dict(views._items(st, ev)))


class Rendering(unittest.TestCase):
    """The words of a thread with another hub are drawn by the feed's own renderer (markdown, links, paths, the fold of
    a long message), not as a plain text node; the page-side wiring is read from the sources (no browser here)."""

    def src(self, name):
        import pathlib
        return (pathlib.Path(__file__).resolve().parent.parent / "modules/web/static" / name).read_text(encoding="utf-8")

    def test_both_sides_of_the_thread_go_through_the_feed_renderer(self):
        bus, feed = self.src("js/busthread.js"), self.src("js/feed.js")
        self.assertIn('words(req.text, "bo" + req.id)', bus)
        self.assertIn('words(answer.hubbus.body, "bi" + answer.id)', bus)
        self.assertIn("clamped({ id, text }, mdIn(text, S.cur?.name, id))", feed)   # the fold of any long message of the feed
        self.assertEqual(feed.count("busRow(it, hubWords)"), 2)   # an answer row and a request row

    def test_folding_is_its_own_checkbox_on_by_default(self):
        state, feed = self.src("js/state.js"), self.src("js/feed.js")
        self.assertIn('{ key: "hubfold", on: true,', state)
        self.assertIn("S.brief.hubfold ? clamped(", feed)

    def test_the_text_is_not_pre_wrapped_markdown_does_the_breaks(self):
        css = self.src("css/feed.css")
        line = next(l for l in css.splitlines() if l.startswith(".hubth-txt"))
        self.assertNotIn("pre-wrap", line)


if __name__ == "__main__":
    unittest.main()
