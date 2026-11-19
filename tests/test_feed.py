"""A plugin channel, API 1.2 (request: a plugin where the hub and agents keep their own social feed, so
agents post on their own in free time, behave as personalities and comment on each other). A post is a journal
fact: `hub post <plugin> "<text>" [--reply pN]`; the author is whoever sent it (a session, the owner, a plugin
process) and cannot be forged; `hub feed <plugin>` reads it; the page uses /api/feed."""
import tests  # noqa: F401 — keep this machine's settings out
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from core import journal, langdata, reducer, version, views

OWNER = {"channel": "web", "owner": True}
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO, "bin", "hub")


class Feed(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        for n, kind in (("hub-core", "hub"), ("devops", None), ("seo", None)):
            self.ev("session.requested", n, cwd="/w", session_id=f"s-{n}", **({"kind": kind} if kind else {}), **OWNER)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                   "entity": entity, "data": data})
        return why

    def post(self, who, text, **extra):
        data = {"plugin": "agent-twitter", "text": text, **extra}
        data.update({"channel": "session", "owner": False, "sender": who} if who != "owner" else OWNER)
        return self.ev("ext.post", None, **data)

    def feed(self):
        return self.st["feeds"]["agent-twitter"]

    def test_sessions_post_and_reply_as_themselves(self):
        self.assertEqual(version.compat("^1.2")[0], "ok")
        self.assertIsNone(self.post("devops", "Three services were rolled out today without a single rollback"))
        first = self.feed()[0]
        self.assertEqual((first["by"], first["text"], first["reply_to"]), ("devops", "Three services were rolled out today without a single rollback", None))
        self.assertIsNone(self.post("seo", "And did you not forget robots.txt? 🙂", reply_to=first["id"]))
        self.assertIsNone(self.post("owner", "Well done"))
        self.assertEqual([(p["by"], p["reply_to"]) for p in self.feed()], [("devops", None), ("seo", first["id"]), ("owner", None)])

    def test_bad_posts_are_refused(self):
        self.assertIsNotNone(self.post("devops", ""))
        self.assertIsNotNone(self.post("devops", "x" * 20001))
        self.assertIsNotNone(self.post("devops", "answer", reply_to="p999"))
        self.assertIsNotNone(self.ev("ext.post", None, plugin="../evil", text="x", **OWNER))
        self.assertIsNotNone(self.ev("ext.post", None, plugin="agent-twitter", text="x", channel="session", owner=False,
                                     sender="nobody"))                       # from a nonexistent session: rejected

    def test_feed_is_capped(self):
        for i in range(520):
            self.post("devops", f"post {i}")
        self.assertEqual(len(self.feed()), 500)
        self.assertEqual(self.feed()[-1]["text"], "post 519")

    def test_reader_gives_the_feed_since(self):
        r = views.Reader()
        evs = [("session.requested", "devops", {"cwd": "/w", "session_id": "s", **OWNER}),
               ("ext.post", None, {"plugin": "agent-twitter", "text": "one", "channel": "session", "owner": False, "sender": "devops"}),
               ("ext.post", None, {"plugin": "agent-twitter", "text": "two", **OWNER})]
        for i, (t, e, d) in enumerate(evs):
            r.feed({"seq": i + 1, "ts": 100 + i, "type": t, "entity": e, "data": d})
        self.assertEqual([p["text"] for p in r.feed_since("agent-twitter", 0)], ["one", "two"])
        self.assertEqual([p["text"] for p in r.feed_since("agent-twitter", 2)], ["two"])
        self.assertEqual(r.snapshot()["feeds"], {"agent-twitter": 3})     # the snapshot holds only the last number


class Cli(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="feed-")
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        j.append("session.requested", "devops", {"cwd": "/w", "session_id": "s", **OWNER})
        j.append("ext.post", None, {"plugin": "agent-twitter", "text": "hello, fleet", "channel": "session",
                                    "owner": False, "sender": "devops"})
        self.env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CLI_WAIT": "0"}
        self.env.pop("HUB_SESSION", None)
        self.env.pop("HUB_EXTENSION", None)

    def run_hub(self, *args, **env):
        return subprocess.run([sys.executable, HUB, *args], capture_output=True, text=True, env={**self.env, **env})

    def spooled(self):
        d, out = os.path.join(self.state, "spool"), []
        for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            with open(os.path.join(d, n)) as f:
                out.append(json.load(f))
        return out

    def test_post_feed_and_plugin_sender(self):
        r = self.run_hub("feed", "agent-twitter")
        self.assertIn("p2 · devops", r.stdout)
        self.assertIn("hello, fleet", r.stdout)
        self.run_hub("post", "agent-twitter", "and to you", "--reply", "p2", HUB_SESSION="seo")
        self.run_hub("send", "devops", "break", HUB_EXTENSION="agent-twitter")
        posts = [(e["type"], e["data"].get("sender"), e["data"].get("reply_to"), e["data"].get("channel"), e["data"].get("owner"))
                 for e in self.spooled()]
        self.assertEqual(posts, [("ext.post", "seo", "p2", "session", False),
                                 ("message.received", "agent-twitter", None, "plugin", False)])   # not "owner"

    def test_a_plugin_process_that_inherited_a_session_speaks_as_the_plugin(self):
        # a plugin process started from a session: it inherited HUB_SESSION, yet it is still a plugin
        self.run_hub("send", "devops", "break", HUB_EXTENSION="agent-twitter", HUB_SESSION="hub")
        (e,) = self.spooled()
        self.assertEqual((e["data"]["channel"], e["data"]["sender"], e["src"]), ("plugin", "agent-twitter", "cli"))
        r = self.run_hub("close-ready", HUB_EXTENSION="agent-twitter", HUB_SESSION="hub")   # and the CLI checks treat it as a plugin, not a session
        self.assertNotEqual(r.returncode, 0)


class OldPosts(unittest.TestCase):
    """A post that left the channel's window (the last FEED_MAX) is still in the journal: /api/feed?post=pN reads it
    from there by its number, without a scan of the archives."""

    def line(self, seq, text="x", plugin="inbox"):
        data = {"plugin": plugin, "text": text, "owner": True, "channel": "web"}
        return json.dumps({"seq": seq, "ts": 1000 + seq, "src": "web", "type": "ext.post", "entity": None, "data": data},
                          separators=(",", ":")) + "\n"

    def test_event_at_finds_the_archive_by_name_and_then_the_live_journal(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, journal.ARCHIVE))
            for name, seqs in (("journal-1-4.jsonl", (1, 2, 3, 4)), ("journal-2-8.jsonl", (2, 5, 6, 7, 8))):   # 2: carried
                with open(os.path.join(d, journal.ARCHIVE, name), "w") as f:
                    f.writelines(self.line(n, f"in {name}") for n in seqs)
            with open(os.path.join(d, "journal.jsonl"), "w") as f:
                f.writelines(self.line(n, "live") for n in (9, 10))
            self.assertEqual(journal.event_at(d, 3)["data"]["text"], "in journal-1-4.jsonl")
            self.assertEqual(journal.event_at(d, 7)["data"]["text"], "in journal-2-8.jsonl")
            self.assertEqual(journal.event_at(d, 10)["data"]["text"], "live")
            self.assertEqual([journal.event_at(d, n) for n in (0, 11, "3", None)], [None] * 4)
            self.assertIsNone(journal.event_at(os.path.join(d, "nothing"), 3))

    def test_event_at_reads_only_the_archive_the_event_was_written_to(self):
        # every archive name starts at the same carried number: an old seq must not read every archive
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, journal.ARCHIVE))
            for last in (4, 8, 12, 16):
                with open(os.path.join(d, journal.ARCHIVE, f"journal-1-{last}.jsonl"), "w") as f:
                    f.writelines(self.line(n, f"in {last}") for n in [1, *range(last - 3, last + 1)])
            with open(os.path.join(d, "journal.jsonl"), "w") as f:
                f.writelines(self.line(n) for n in (1, 17))
            self.assertEqual(list(journal._files(d, 6)), [os.path.join(d, journal.ARCHIVE, "journal-1-8.jsonl"),
                                                         os.path.join(d, "journal.jsonl")])
            self.assertEqual(journal.event_at(d, 6)["data"]["text"], "in 8")
            self.assertEqual(list(journal._files(d, 17)), [os.path.join(d, "journal.jsonl")])

    def test_old_posts_skip_what_the_core_rejected_and_do_not_fall_on_a_bad_one(self):
        rejected = lambda of: json.dumps({"seq": of + 1, "ts": 1, "type": "core.rejected", "entity": None,
                                          "data": {"of": of, "reason": "no post p1"}}) + "\n"
        post = lambda seq, **data: json.dumps({"seq": seq, "ts": 1, "type": "ext.post", "entity": None, "data": {
            "plugin": "inbox", "text": "hi", "channel": "plugin", "sender": "ext:teams", **data}}) + "\n"
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "journal.jsonl"), "w") as f:
                f.writelines([post(2, plugin="Bad Name"), post(4, reply_to="p1"), rejected(4), post(6), post(8)])
            window = [{"id": "p8", "text": "hi"}]
            self.assertEqual([p["id"] for p in views.old_posts(d, "inbox", ["p2", "p4", "p6", "p8"], window)], ["p6", "p8"])
            self.assertIsNone(journal.event_at(d, 4, accepted=True))
            self.assertEqual(journal.event_at(d, 4)["seq"], 4)   # hub show still prints it
            with mock.patch.object(journal, "events_at", side_effect=AssertionError("read")):   # in the window range: no read
                self.assertEqual(views.old_posts(d, "inbox", ["p9"], window), [])

    def test_several_numbers_read_each_file_once(self):
        # 20 posts asked from one 85 MB archive: one pass over it, not 20; what the archive lacks — one pass over the live
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, journal.ARCHIVE))
            with open(os.path.join(d, journal.ARCHIVE, "journal-1-8.jsonl"), "w") as f:
                f.writelines(self.line(n, "old") for n in range(1, 9))
            with open(os.path.join(d, "journal.jsonl"), "w") as f:
                f.writelines(self.line(n, "live") for n in (9, 10, 11))
            opened = []
            real = open
            with mock.patch("builtins.open", side_effect=lambda path, *a, **k: opened.append(os.path.basename(path)) or real(path, *a, **k)):
                got = journal.events_at(d, [2, 3, 4, 5, 6, 7, 10, 11, 42], accepted=True)
            self.assertEqual(sorted(got), [2, 3, 4, 5, 6, 7, 10, 11])
            self.assertEqual(sorted(opened), ["journal-1-8.jsonl", "journal.jsonl"])

    def test_a_wanted_event_right_after_another_is_not_skipped(self):
        # the line after a found event is looked at for core.rejected: when it is itself asked for, it is still found
        rejected = lambda of: json.dumps({"seq": of + 1, "ts": 1, "type": "core.rejected", "entity": None,
                                          "data": {"of": of, "reason": "x"}}, separators=(",", ":")) + "\n"
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "journal.jsonl"), "w") as f:
                f.writelines([self.line(1), self.line(2), self.line(3), rejected(3), self.line(5)])
            self.assertEqual(sorted(journal.events_at(d, [1, 2, 3, 5], accepted=True)), [1, 2, 5])
            self.assertEqual(sorted(journal.events_at(d, [1, 2, 3, 4, 5])), [1, 2, 3, 4, 5])
        # rejected in the archive it was written to: the carried copy in the live journal (no rejection next to it) is
        # not served either
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, journal.ARCHIVE))
            with open(os.path.join(d, journal.ARCHIVE, "journal-1-4.jsonl"), "w") as f:
                f.writelines([self.line(1), self.line(2), rejected(2), self.line(4)])
            with open(os.path.join(d, "journal.jsonl"), "w") as f:
                f.writelines([self.line(2), self.line(5)])
            self.assertEqual(sorted(journal.events_at(d, [2, 5], accepted=True)), [5])

    def test_event_at_ignores_a_number_quoted_inside_another_line(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "journal.jsonl"), "w") as f:
                f.write(self.line(1, '{"seq":2,"ts":1}'))
                f.write(self.line(3))
            self.assertIsNone(journal.event_at(d, 2))
            self.assertEqual(journal.event_at(d, 3)["seq"], 3)

    def test_the_row_of_an_old_post_is_the_one_the_reducer_made(self):
        ev = {"seq": 7, "ts": 1007, "type": "ext.post", "entity": None,
              "data": {"plugin": "inbox", "text": " hi ", "channel": "plugin", "sender": "ext:teams", "reply_to": "p3"}}
        plugin, row = reducer.post_row(ev, ev["data"])
        by = langdata.legacy("post_by")["plugin"].format(name="teams")   # a protocol value the core still writes
        self.assertEqual((plugin, row), ("inbox", {"id": "p7", "by": by, "text": "hi", "reply_to": "p3", "ts": 1007}))
        st = reducer.empty()
        st["feeds"] = {"inbox": [{"id": "p3"}]}
        reducer._ext_post(st, ev, ev["data"])
        self.assertEqual(st["feeds"]["inbox"][1], row)
        with self.assertRaises(reducer.Rejected):   # a gone session is refused by the reducer, but not when read back
            reducer.post_row(ev, {**ev["data"], "channel": "session", "sender": "gone"}, {})
        self.assertEqual(reducer.post_row(ev, {**ev["data"], "channel": "session", "sender": "gone"})[1]["by"], "gone")
        with self.assertRaises(reducer.Rejected):
            reducer.post_row(ev, {"plugin": "inbox", "text": "", "channel": "web", "owner": True})


class Web(unittest.TestCase):
    def test_page_reads_and_owner_posts(self):
        from tests import test_web
        h = test_web.WebTest("test_page_and_snapshot")
        h.setUp()
        try:
            h.j.append("session.requested", "devops", {"cwd": "/w", "session_id": "s", **OWNER})
            h.j.append("ext.post", None, {"plugin": "agent-twitter", "text": "one", "channel": "session", "owner": False,
                                          "sender": "devops"})
            h.settle()
            got = json.loads(h.req("GET", "/api/feed?plugin=agent-twitter&since=0")[1])["posts"]
            self.assertEqual([(p["by"], p["text"]) for p in got], [("devops", "one")])
            self.assertEqual(json.loads(h.req("GET", "/api/snapshot")[1])["feeds"], {"agent-twitter": 2})
            self.assertEqual(h.req("POST", "/api/feed/post", {"plugin": "agent-twitter", "text": "Well done", "reply_to": "p2"})[0], 202)
            (e,) = [x for x in h.spooled() if x["type"] == "ext.post"]
            self.assertEqual((e["data"]["text"], e["data"]["reply_to"], e["data"]["owner"]), ("Well done", "p2", True))
        finally:
            h.tearDown()

    def test_a_post_outside_the_window_is_read_from_the_journal_by_its_number(self):
        from tests import test_web
        h = test_web.WebTest("test_page_and_snapshot")
        h.setUp()
        try:
            h.j.append("session.requested", "devops", {"cwd": "/w", "session_id": "s", **OWNER})
            old = h.j.append("ext.post", None, {"plugin": "agent-twitter", "text": "old", "channel": "session",
                                                "owner": False, "sender": "devops"})
            other = h.j.append("ext.post", None, {"plugin": "other", "text": "foreign", "channel": "session",
                                                  "owner": False, "sender": "devops"})
            with mock.patch.object(reducer, "FEED_MAX", 1):
                h.j.append("ext.post", None, {"plugin": "agent-twitter", "text": "new", "channel": "session",
                                              "owner": False, "sender": "devops"})
                h.settle()
            feed = lambda q: json.loads(h.req("GET", "/api/feed?plugin=agent-twitter&" + q)[1])["posts"]
            self.assertEqual([p["text"] for p in feed("since=0")], ["new"])   # the window
            self.assertEqual([(p["id"], p["by"], p["text"]) for p in feed(f"post=p{old['seq']}")],
                             [(f"p{old['seq']}", "devops", "old")])
            self.assertEqual([p["text"] for p in feed(f"post=p{old['seq']}&post=p{old['seq'] + 2}")], ["old", "new"])
            self.assertEqual(feed(f"post=p{other['seq']}"), [])   # another channel's post is not served under this plugin
            self.assertEqual(feed("post=p9999"), [])
            self.assertEqual(h.req("GET", "/api/feed?plugin=agent-twitter&post=oops")[0], 400)
            # p007 would be read as post 7, but the page asked for "p007" and drops what it did not ask for
            self.assertEqual(h.req("GET", f"/api/feed?plugin=agent-twitter&post=p0{old['seq']}")[0], 400)
            self.assertIn("feed.post_by_id", json.loads(h.req("GET", "/api/snapshot")[1])["features"])
        finally:
            h.tearDown()


if __name__ == "__main__":
    unittest.main()
