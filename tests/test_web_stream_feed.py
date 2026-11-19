"""Plugin channel posts in /api/stream (?feed=<plugin>, feature `stream.feed`): a client that kept polling /api/feed for the
Inbox gets each new post as an event of the stream it already reads. A stream without the parameter is unchanged."""
import tests  # noqa: F401
import http.client
import json
import unittest

from core import views
from tests.test_web_stream_shared import StreamShared
from modules.web import stream_feed

OWNER = {"owner": True, "channel": "web"}


class Parse(unittest.TestCase):
    def test_query(self):
        self.assertEqual(stream_feed.parse("/api/stream"), ({}, None))
        self.assertEqual(stream_feed.parse("/api/stream?delta=2"), ({}, None))
        self.assertEqual(stream_feed.parse("/api/stream?feed=inbox,teams&feed=inbox&feed_since=7"), ({"inbox": None, "teams": None}, 7))
        # a name that is no plugin channel name, too many of them, a bad number: ignored, never an error
        self.assertEqual(stream_feed.parse("/api/stream?feed=../x,a%20b,,ok&feed_since=-3"), ({"ok": None}, None))
        # a digit that is no number (int() refuses it, the stream must not die after its headers), a name the core never
        # takes a post for: ignored too
        self.assertEqual(stream_feed.parse("/api/stream?feed=Inbox,a.b,inbox&feed_since=%C2%B2"), ({"inbox": None}, None))
        self.assertEqual(len(stream_feed.parse("/api/stream?feed=" + ",".join(f"p{i}" for i in range(30)))[0]), stream_feed.MAX_PLUGINS)

    def test_start_and_steps(self):
        posts = {"inbox": [{"id": "p3"}, {"id": "p9"}], "teams": [{"id": "p4"}]}
        cur = stream_feed.start({"inbox": None, "teams": None, "none": None}, posts, None)
        self.assertEqual(cur, {"inbox": 9, "teams": 4, "none": 0})   # from now on: the history is /api/feed
        self.assertEqual(stream_feed.start({"inbox": None}, posts, 3), {"inbox": 3})   # from the client's own cursor
        posts["inbox"].append({"id": "p12"})
        out = stream_feed.step(cur, posts)
        self.assertEqual([(e["plugin"], [p["id"] for p in e["posts"]]) for e in out], [("inbox", ["p12"])])
        self.assertEqual(cur["inbox"], 12)
        self.assertEqual(stream_feed.step(cur, posts), [])


class FeedStream(StreamShared):
    def open(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(c.close)
        c.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        self.addCleanup(r.close)
        self.assertEqual(r.status, 200)
        return r

    def event(self, r, want=None):
        kind = None
        for _ in range(200):
            line = r.fp.readline().decode()
            self.assertTrue(line, "stream closed before an event")
            if line.startswith("event:"):
                kind = line[6:].strip()
            elif line.startswith("data:") and want in (None, kind):
                return kind, json.loads(line[5:])
        self.fail(f"no {want} event")

    def post(self, plugin, text):
        self.j.append("ext.post", None, {"plugin": plugin, "text": text, **OWNER})
        self.web.tail.poll()

    def test_new_posts_of_the_asked_channels_only(self):
        self.post("inbox", "old")
        new, plain = self.open("/api/stream?feed=inbox"), self.open("/api/stream")
        kind, snap = self.event(new)
        self.assertEqual(kind, "snapshot")
        self.assertIn("stream.feed", snap["features"])
        self.assertEqual(self.event(plain)[0], "snapshot")
        self.post("teams", "not asked")
        self.post("inbox", "fresh")
        _, body = self.event(new, "feed")
        self.assertEqual(body["plugin"], "inbox")
        self.assertEqual([p["text"] for p in body["posts"]], ["fresh"])   # not "old": it was there at the connection
        self.post("inbox", "second")
        self.assertEqual([p["text"] for p in self.event(new, "feed")[1]["posts"]], ["second"])
        self.assertEqual(self.event(plain)[0], "patch")   # the stream without the parameter: the same patches, no feed events

    def test_feed_since_catches_up_what_came_between_the_snapshot_and_the_stream(self):
        self.post("inbox", "one")
        cursor = self.web.tail.snapshot["feeds"]["inbox"]
        self.post("inbox", "two")
        r = self.open(f"/api/stream?feed=inbox&feed_since={cursor}")
        _, body = self.event(r, "feed")
        self.assertEqual([p["text"] for p in body["posts"]], ["two"])
        self.assertEqual(views.posts_since(self.web.tail.feed_posts["inbox"], cursor), body["posts"])

    def test_item_deltas_and_channel_posts_in_one_stream(self):
        # ?delta=2 (numbered patches) and ?feed= together: the posts keep their place before the patch, the numbers run on
        r = self.open("/api/stream?delta=2&feed=inbox")
        self.assertEqual(self.event(r)[0], "snapshot")
        numbers, seen = [], []
        for text in ("one", "two"):
            self.post("inbox", text)
            for _ in range(50):
                kind, body = self.event(r)
                seen.append((kind, body))
                if kind == "patch":
                    numbers.append(body["_n"])
                    if "feeds" in body:
                        break
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))
        order = [("feed", [p["text"] for p in b["posts"]]) if k == "feed" else (k, "feeds" in b) for k, b in seen]
        self.assertLess(order.index(("feed", ["one"])), order.index(("patch", True)))
        self.assertIn(("feed", ["two"]), order)

    def test_a_post_comes_before_the_patch_that_brings_its_number(self):
        # a client that drops right after a patch and comes back with its seq (feed_since) must already hold the posts
        # up to that seq: so the feed event goes first
        r = self.open("/api/stream?feed=inbox")
        self.assertEqual(self.event(r)[0], "snapshot")
        self.post("inbox", "between")
        seen = []
        for _ in range(50):
            kind, body = self.event(r)
            seen.append((kind, body))
            if kind == "patch" and "feeds" in body:
                break
        number = body["feeds"]["inbox"]
        self.assertIn(("feed", ["p%d" % number]), [(k, [p["id"] for p in b["posts"]]) for k, b in seen if k == "feed"])

    def test_a_stream_opened_before_the_journal_is_read_does_not_take_the_window_for_new_posts(self):
        # after `hub restart` an app reconnects while the page has not read the journal yet: the channels are empty then,
        # and the posts the journal already holds are the history (/api/feed), not new posts
        self.post("inbox", "old")
        with self.web.tail.reading, self.web.tail.changed:   # the page's own poll thread may be publishing that post just now
            self.web.tail.feed_posts, self.web.tail.published = {}, False
        r = self.open("/api/stream?feed=inbox")
        self.assertEqual(self.event(r)[0], "snapshot")
        self.post("inbox", "read with the journal")   # the first publication: what it holds is the stream's start
        for _ in range(50):   # the stream has taken it (its patch carries the channel's number) before the next post
            kind, body = self.event(r)
            if kind == "patch" and "feeds" in body:
                break
        self.assertIn("feeds", body)
        self.post("inbox", "new")
        self.assertEqual([p["text"] for p in self.event(r, "feed")[1]["posts"]], ["new"])


def load_tests(loader, standard, pattern):
    """Only our own: StreamShared already runs in test_web_stream_shared."""
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(Parse))
    suite.addTests(FeedStream(n) for n in FeedStream.__dict__ if n.startswith("test"))
    return suite


if __name__ == "__main__":
    unittest.main()
