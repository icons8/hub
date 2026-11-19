"""The first frame of /api/stream is light (stream-lean): the page waits for it before the session list. The snapshot
carries what the lists and badges read; what only a screen needs on demand ("Updates": the notes of past updates, the
CHANGES.md entries, the unpublished list) is in /api/about."""
import tests  # noqa: F401
import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest

from core import journal, reducer, views
from modules.web import server
from modules.web.server import Web

OWNER = {"channel": "cli", "owner": True}
HEAVY_INFO = {"builtins": ["/cmd"] * 500, "plugins": ["p"] * 100, "cwd": "/x" * 50, "mcp": ["srv"] * 40, "mode": "bypass"}
KEPT_INFO = {"model": "opus", "effort": "high", "version": "2.1.310", "launcher": "claude", "harness": "claude"}


class Lean(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.started", "hub")
        self.ev("session.info", "hub", **KEPT_INFO, **HEAVY_INFO)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type, "entity": entity,
                                                   "data": data})
        return why

    def snap(self):
        r = views.Reader()
        r.state = self.st
        return r.snapshot()

    def test_session_info_keeps_only_what_clients_read(self):
        info = self.snap()["sessions"][0]["info"]
        for k, v in KEPT_INFO.items():
            self.assertEqual(info.get(k), v, k)
        for k in HEAVY_INFO:
            self.assertNotIn(k, info)

    def test_board_has_only_open_tasks(self):
        for i, state in enumerate(("working", "waiting-owner", "stopped", "replied", "done", "archived")):
            self.st["delegations"][f"d{i}"] = {"id": f"d{i}", "session": "hub", "for": "m1", "state": state, "since": 1,
                                              "words": "w", "context": "c"}
        self.assertEqual(sorted(d["state"] for d in self.snap()["delegations"]), ["stopped", "waiting-owner", "working"])

    def test_updates_without_notes(self):
        self.st["updates"] = [{"ts": 5, "seq": 9, "ok": True, "notes": ["x" * 1000], "commits": [], "why": None,
                               "files": [], "from": "a", "to": "b"}]
        (u,) = self.snap()["updates"]
        self.assertNotIn("notes", u)
        self.assertEqual((u["ts"], u["ok"], u["to"]), (5, True, "b"))   # what the "stale version" badge reads

    def test_lean_code_cuts_changes_and_unpublished_list(self):
        full = {"head": "a", "running": "a", "upstream": False, "changes": [{"d": "x"}] * 99,
                "unpublished": ["c1", "c2"], "unpublished_count": 2, "published_at": 1}
        lean = server.lean_code(full)
        self.assertNotIn("changes", lean)
        self.assertEqual(lean["unpublished"], [])   # the page tests "is there a list" — the list itself is in /api/about
        self.assertEqual((lean["unpublished_count"], lean["head"], lean["published_at"]), (2, "a", 1))
        self.assertEqual(len(full["changes"]), 99)   # the cached full one is not touched
        self.assertNotIn("unpublished", server.lean_code({"head": "a"}))   # no list — the "no list" meaning stays


class AboutServer(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="hubweb-")
        self.addCleanup(shutil.rmtree, self.d, True)
        j = journal.Journal(os.path.join(self.d, "journal.jsonl"))
        j.append("update.applied", None, {"from": "a", "to": "b", "notes": ["n1"], "channel": "cli", "owner": True})
        self.web = Web(self.d)
        self.addCleanup(self.web.stop.set)
        server = self.web.serve(port=0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.port = server.server_address[1]

    def raw(self, path="/api/about", **headers):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}", "X-Hub": "1", **headers})
        r = c.getresponse()
        got = r.status, dict(r.getheaders()), r.read()
        c.close()
        return got

    def about(self, path="/api/about"):
        return json.loads(self.raw(path)[2])


class About(AboutServer):

    def test_about_carries_updates_with_notes_and_full_code(self):
        self.web.tail.poll()
        got = self.about()
        self.assertEqual([u["notes"] for u in got["updates"]], [["n1"]])
        self.assertIn("changes", got["code"])

    def test_about_keeps_the_published_updates_while_the_journal_is_reread(self):
        # a re-read from the start (a language change, a rotation of the wrong file) holds the read lock with an empty
        # reader: /api/about answers without waiting for it, with the notes of the last published poll, not an empty history
        self.web.tail.poll()
        with self.web.tail.reading:
            self.web.tail.reader = views.Reader()
            got = self.about()
        self.assertEqual([u["notes"] for u in got["updates"]], [["n1"]])


class AboutLite(AboutServer):
    """?lite=1: the same keys without the two heavy parts (the notes of past updates, the CHANGES.md entries); without
    the parameter the answer is the full one, as before."""

    def setUp(self):
        super().setUp()
        j = journal.Journal(os.path.join(self.d, "journal.jsonl"))
        for i in range(20):
            j.append("update.applied", None, {"from": f"a{i}", "to": f"b{i}", "channel": "cli", "owner": True,
                                              "notes": [f"note {i} line {n} " + "x" * 400 for n in range(110)]})
        self.web.tail.poll()

    def test_without_the_parameter_the_answer_is_the_full_one(self):
        full = self.about()
        self.assertEqual(len(full["updates"]), 20)
        self.assertEqual(len(full["updates"][0]["notes"]), 110)
        self.assertGreater(len(self.raw()[2]), 900_000)
        self.assertNotIn("lite", full)
        self.assertEqual(full["code"]["changes"], self.web.code()["changes"])

    def test_lite_is_small_and_has_the_same_keys(self):
        full, (status, _, body) = self.about(), self.raw("/api/about?lite=1")
        lite = json.loads(body)
        self.assertEqual(status, 200)
        self.assertLess(len(body), 60_000)
        self.assertEqual(set(lite) - {"lite"}, set(full))
        self.assertEqual(set(lite["code"]), set(full["code"]))
        self.assertEqual(lite["lite"], True)
        self.assertEqual(lite["code"]["changes"], [])
        self.assertEqual(lite["code"]["head"], full["code"]["head"])
        self.assertEqual(len(lite["updates"]), 20)
        for u, f in zip(lite["updates"], full["updates"]):
            self.assertEqual(u["notes"], [])
            self.assertEqual(u["notes_count"], len(f["notes"]))
            self.assertEqual({k: v for k, v in u.items() if k not in ("notes", "notes_count")},
                             {k: v for k, v in f.items() if k != "notes"})

    def test_lite_code_card_is_the_snapshots_lean_one(self):
        # the unpublished commit list (up to 40 subjects) is most of a lite answer on a hub from git: the count stays
        code = {"head": "a", "changes": [{"date": "d", "items": ["x"]}], "unpublished": ["a1 one", "a2 two"],
                "unpublished_count": 2}
        lite = server.Web.about_lite({"code": code, "updates": []})["code"]
        self.assertEqual(lite, {"head": "a", "changes": [], "unpublished": [], "unpublished_count": 2})
        self.assertEqual(server.Web.about_lite({"code": {"head": "a"}, "updates": []})["code"],
                         {"head": "a", "changes": []})   # no list — none appears; `changes` is there always

    def test_lite_answers_304_to_its_etag(self):
        status, headers, _ = self.raw("/api/about?lite=1")
        tag = headers["ETag"]
        status, again, _ = self.raw("/api/about?lite=1", **{"If-None-Match": tag})
        self.assertEqual(status, 304)
        self.assertEqual((again["ETag"], again.get("Vary")), (tag, headers["Vary"]))   # the same headers as its 200
        self.assertNotIn("ETag", self.raw()[1])   # the full answer has no tag to revalidate

    def test_feature_flag(self):
        self.assertIn("about.lite", self.web.with_clock({})["features"])


if __name__ == "__main__":
    unittest.main()
