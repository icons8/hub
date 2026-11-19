"""The page without timelines in the stream (owner request 03.10: a 2.2 MB snapshot on every event, 90% of it the
timelines of all sessions): the stream carries a snapshot without timelines and at most once a second; the open
session's timeline comes from /api/timeline, only what changed after the since mark; a step row is updated in place
under the same key k; a new reader gets the whole timeline."""
import tests  # noqa: F401 — keep the machine's settings out
import http.client
import json
import threading
import time

from modules.web import server
from tests import test_web


class Timeline(test_web.WebTest):
    def tl(self, since=0, epoch="", session="a"):
        code, body = self.req("GET", f"/api/timeline?session={session}&since={since}&epoch={epoch}")
        self.assertEqual(code, 200)
        return json.loads(body)

    def tool(self, tid, cmd):
        self.j.append("turn.tool", "a", {"id": tid, "name": "Bash", "input": {"command": cmd}})

    def result(self, tid, text):
        self.j.append("turn.tool_result", "a", {"id": tid, "text": text, "error": False})

    def stream_events(self, seconds, during=None):
        """Snapshots from /api/stream over seconds seconds (during — what to do while the stream is being read)."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=seconds + 5)
        c.request("GET", "/api/stream", headers={"Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        out = []

        def read():
            event = None
            try:
                for line in r.fp:
                    if line.startswith(b"event: "):
                        event = line[7:].strip()
                    elif line.startswith(b"data: ") and event in (b"snapshot", b"patch"):
                        out.append((event.decode(), json.loads(line[6:])) if self.raw else json.loads(line[6:]))
            except (OSError, ValueError):
                pass
        threading.Thread(target=read, daemon=True).start()
        if during:
            threading.Thread(target=during, daemon=True).start()
        time.sleep(seconds)
        got = list(out)
        c.sock.shutdown(2) if c.sock else None
        c.close()
        return got

    raw = False

    def test_after_the_first_snapshot_the_stream_sends_only_what_changed(self):
        # 07.10, a speed investigation (owner decision): every second the tab was sent a 558 KB snapshot, while only
        # 1–108 KB of it changed (the number, timeline revisions, sessions) — the schedule, tasks and code repeated
        self.seed()
        self.raw = True
        self.j.append("schedule.set", "j1", {"session": "a", "every": "1h", "prompt": "check", "channel": "cli",
                                             "owner": True})
        self.settle()

        def later():
            time.sleep(1.5)
            self.tool("t1", "ls")
            self.settle()
        got = self.stream_events(4, during=later)
        (kind, first), (kind2, patch) = got[0], got[-1]
        self.assertEqual((kind, kind2), ("snapshot", "patch"))
        self.assertIn("schedule", first)
        self.assertNotIn("schedule", patch)                       # unchanged — not sent
        self.assertIn("timeline_rev", patch)
        self.assertGreater(patch["seq"], first["seq"])

    def test_stream_snapshot_has_no_timelines_but_their_revisions(self):
        self.seed()
        self.tool("t1", "ls")
        self.settle()
        (snap, *_) = self.stream_events(1.5)
        self.assertNotIn("timelines", snap)
        self.assertGreater(snap["timeline_rev"]["a"], 0)
        self.assertTrue(snap["epoch"])
        full = json.loads(self.req("GET", "/api/snapshot")[1])
        self.assertIn("a", full["timelines"])                       # the full snapshot, for scripts, is as it was

    def test_full_then_only_changes_and_in_place_update(self):
        self.seed()
        self.tool("t1", "pytest -q")
        self.settle()
        first = self.tl()
        self.assertTrue(first["full"])
        (step,) = [it for it in first["items"] if it.get("kind") == "tool"]
        self.assertEqual(step.get("status"), "running")
        self.result("t1", "5 passed")                               # the same step: "running" → "done"
        self.tool("t2", "git status")
        self.settle()
        delta = self.tl(first["rev"], first["epoch"])
        self.assertFalse(delta["full"])
        by_k = {it["k"]: it for it in delta["items"]}
        self.assertEqual(by_k[step["k"]]["status"], "done")
        self.assertEqual(len(delta["items"]), 2)                    # the corrected step and the new one — nothing else
        self.assertGreater(delta["rev"], first["rev"])
        self.assertEqual(self.tl(delta["rev"], delta["epoch"])["items"], [])   # nothing new — empty

    def test_says_whether_there_is_anything_earlier(self):
        # owner report 03.10: "Show earlier" on an empty history — a fresh session (a fork) has nothing earlier to show
        from core import views
        self.seed()
        self.tool("t1", "ls")
        self.settle()
        self.assertFalse(self.tl()["older"])                       # born in front of the reader, the timeline is whole
        for i in range(views.SNAPSHOT_TAIL + 5):                    # longer than is returned at once — there is something earlier
            self.tool(f"x{i}", "ls")
        self.settle()
        self.assertTrue(self.tl()["older"])
        from core import reducer
        st = reducer.empty()                                        # the reader caught the session midway: the journal
        for i, (t, d) in enumerate([("session.requested", {"cwd": "/w", "session_id": "s", "channel": "cli",
                                                           "owner": True}), ("session.started", {})]):   # starts with a snapshot
            st, _, _ = reducer.reduce(st, {"seq": i + 1, "ts": 1, "type": t, "entity": "old", "data": d})
        r = views.Reader()
        r.feed({"seq": 3, "ts": 2, "type": "core.snapshot", "entity": None, "data": {"state": st}})
        r.feed({"seq": 4, "ts": 3, "type": "turn.tool", "entity": "old", "data": {"id": "t", "name": "Bash", "input": {}}})
        self.assertTrue(r.timeline("old")["items"])
        self.assertTrue(r.timeline("old")["older"])
        self.assertTrue(r.timeline("nobody")["older"] is False)     # there is no timeline — and nothing earlier

    def test_another_reader_gets_the_whole_timeline(self):
        self.seed()
        self.tool("t1", "ls")
        self.settle()
        first = self.tl()
        again = self.tl(first["rev"], "old-reader")
        self.assertTrue(again["full"])
        self.assertEqual([it["k"] for it in again["items"]], [it["k"] for it in first["items"]])

    def test_stream_sends_at_most_once_a_second(self):
        self.seed()

        def busy():
            for i in range(20):                                     # 20 events in 2 s — like a fleet at work
                self.tool(f"x{i}", f"echo {i}")
                self.settle()
                time.sleep(0.1)
        snaps = self.stream_events(2.6, during=busy)
        self.assertLessEqual(len(snaps), 4)                         # the first at once, then at most once a second
        self.assertGreaterEqual(len(snaps), 2)
        self.assertEqual(server.STREAM_GAP, 1.0)


def load_tests(loader, standard, pattern):
    """Only our own tests: the class inherits WebTest, and its tests already run in test_web."""
    import unittest
    suite = unittest.TestSuite()
    for cls in (Timeline,):
        suite.addTests(cls(n) for n in cls.__dict__ if n.startswith("test"))
    return suite


if __name__ == "__main__":
    import unittest
    unittest.main()
