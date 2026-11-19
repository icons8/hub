"""Service timeline lines that nobody needs except a developer: the page, plugins and clients do not receive them
(/api/timeline, snapshot, /api/history), while `hub show` shows them, as it shows the whole timeline. The decision is
made by the record itself (views.quiet_line), so archive parses made before the rule are judged the same way."""
import json
import os
import shutil
import tempfile
import unittest

from core import history, langdata, views

# the job rows of the earlier core, without a code (archive parses made before it): "in the schedule: ", "removed from
# the schedule: ", "job " in its words
OLD_JOB_SET, _, OLD_JOB = langdata.legacy("job_line").split("|")


def events(raw):
    return [{"seq": i, "ts": r.get("t", 1000 + i), "src": r.get("src", "test"), "type": r["type"],
             "entity": r.get("entity"), "data": r.get("data", {})} for i, r in enumerate(raw, 1)]


OWNER = {"channel": "web", "owner": True}
WORK = [
    {"type": "session.requested", "entity": "a", "data": {"session_id": "s", "cwd": "/w"}},          # "session created"
    {"type": "session.started", "entity": "a", "data": {"session_id": "s"}},
    {"type": "message.received", "data": {"to": "a", "text": "check the rollout", **OWNER}},
    {"type": "turn.started", "entity": "a", "data": {"marker": "m3"}},
    {"type": "session.said", "entity": "a", "data": {"text": "The rollout went through, no errors."}},
    {"type": "session.said", "entity": "a", "data": {"text": "—"}},                                   # quiet result
    {"type": "turn.result", "entity": "a", "data": {"text": "—"}},
    {"type": "schedule.set", "entity": "a-wait-ab12-1", "data": {"session": "a", "at": 99999, "prompt": "p", **OWNER}},
    {"type": "schedule.set", "entity": "nightly", "data": {"session": "a", "at": 99999, "prompt": "report", **OWNER}},
    {"type": "schedule.remove_requested", "entity": "a-wait-ab12-1", "data": OWNER},
    {"type": "host.done", "entity": "a", "data": {"do": "compact", "result": "sent"}},                 # "compacting" without a request
    {"type": "session.compacted", "entity": "a", "data": {"ok": True, "before": 300000, "after": 40000}},
    {"type": "session.exited", "entity": "a", "data": {}},                                              # "process vanished"
    {"type": "session.started", "entity": "a", "data": {"session_id": "s", "source": "resume"}},       # started by the core
    {"type": "session.reload_requested", "entity": "a", "data": OWNER},                                 # the owner's /reload
    {"type": "session.started", "entity": "a", "data": {"session_id": "s", "source": "resume"}},
]


def texts(items):
    return [(it["kind"], it.get("text")) for it in items]


class QuietLines(unittest.TestCase):
    def reader(self, raw=WORK):
        r = views.Reader()
        r.feed_all(events(raw))
        return r

    def test_page_gets_no_developer_lines_but_keeps_what_tells_him_something(self):
        r = self.reader()
        shown = texts(r.timeline("a")["items"])
        everything = texts(r.timelines["a"])
        for gone in [("sys", "session created"), ("sys", "compacting the conversation (/compact)")]:
            self.assertIn(gone, everything)
            self.assertNotIn(gone, shown)
        self.assertFalse([t for k, t in shown if t and "a-wait-ab12-1" in t])        # hub wait steps
        self.assertTrue([t for k, t in everything if t and "a-wait-ab12-1" in t])
        # the quiet result "—" does reach the page: it tells the page the turn has no reply (feed.js), and the page
        # does not draw it itself
        for kept in [("out", "The rollout went through, no errors."), ("out", "—"), ("sys", "conversation compacted: 300k → 40k tokens"),
                     ("sys", "the process vanished, bringing it back up")]:
            self.assertIn(kept, shown)
        self.assertTrue([t for k, t in shown if t and t.startswith("in the schedule: nightly")])
        # a start by the core is not shown; the same start by the owner's /reload is his command, so it is visible
        resumed = [it for it in r.timeline("a")["items"] if it.get("text") == "brought up again (--resume)"]
        self.assertEqual([bool(it.get("command")) for it in resumed], [True])
        self.assertEqual(len([it for it in r.timelines["a"] if it.get("text") == "brought up again (--resume)"]), 2)

    def test_snapshot_timelines_are_the_same_as_the_page_feed(self):
        r = self.reader()
        self.assertEqual(texts(r.snapshot()["timelines"]["a"]), texts(r.timeline("a")["items"]))

    def test_hub_show_still_has_everything(self):
        lines = "\n".join(views.show(self.reader(), "a", n=100))
        self.assertIn("session created", lines)
        self.assertIn("a-wait-ab12-1", lines)

    def test_executor_handoff_and_removal_lines_are_not_shown(self):
        r = self.reader(WORK[:2] + [
            {"type": "session.close_requested", "entity": "a", "data": {"retire": True}},
            {"type": "session.remove_requested", "entity": "a", "data": OWNER}])
        every = [t for _, t in texts(r.timelines.get("a") or [])]
        self.assertIn("handing things over before sleep (HANDOFF-REQUEST)", every)
        # the core's incoming message to the session itself ("hand over your work") is its input, and showing it is
        # left alone; there are no service lines
        self.assertEqual([t for k, t in texts(r.timeline("a")["items"]) if k == "sys"], [])

    def test_errors_and_owner_commands_are_never_quiet(self):
        self.assertFalse(views.quiet_line({"kind": "out", "text": "—"}))
        self.assertTrue(views.quiet_line({"kind": "sys", "text": OLD_JOB + "a-wait-ab12-1 now goes to this session"}))
        self.assertFalse(views.quiet_line({"kind": "sys", "command": True, "text": "compacting the conversation (/compact)"}))
        self.assertFalse(views.quiet_line({"kind": "out", "text": "— done"}))
        self.assertFalse(views.quiet_line({"kind": "sys", "text": OLD_JOB_SET + "night-wait-report, every 2 h"}))

    def test_archived_feed_parsed_before_the_rule_is_filtered_too(self):
        # "Show earlier" takes the timeline from a parse of the journal archive, which an earlier version may have made,
        # without marks
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        arch = os.path.join(d, "journal-archive")
        os.makedirs(arch)
        name = "journal-1-16.jsonl"
        with open(os.path.join(arch, name), "w", encoding="utf-8") as f:
            for ev in events(WORK):
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        h = history.History(d)
        page = h.older("a", 10 ** 9, 100, [], keep=views.shown_line)
        got = texts(page["items"])
        self.assertIn(("out", "The rollout went through, no errors."), got)
        self.assertNotIn(("sys", "session created"), got)


if __name__ == "__main__":
    unittest.main()
