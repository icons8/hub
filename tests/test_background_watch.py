"""The watchdog also watches sessions' background work (owner request 2026-10-03: the watchdog should also watch for
hung background processes; live case: a local server started in the background, 100 min without output, held up a hub
restart). The host remembers the output file of a background command; the watchdog checks when it last changed and
reports the last line to the core (the page shows what is running in the background and what it wrote last); one that
runs long and has been silent for long raises an alarm to the hub: which task, how long it has run, since when, and
what it printed last."""
import tests  # noqa: F401 — keeps the machine's settings out
import os
import shutil
import tempfile
import unittest

from core import reducer, spool, views
from hosts.streamjson import StreamHost
from modules.watch import watch

T0 = 1_000_000.0


class HostKeepsTheOutputFile(unittest.TestCase):
    def test_launch_reply_names_the_output_file(self):
        host = StreamHost(tempfile.mkdtemp(prefix="bgw-"))
        host.inbox.put(("a", {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": "npm run console", "description": "Start console", "run_in_background": True}}]}}))
        host.inbox.put(("a", {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "Command running in background with ID: b1x. "
                                                                    "Output is being written to: /tmp/claude/tasks/b1x.output. You will be notified"}]}}))
        (fact,) = [f for f in host.poll() if f[0] == "session.background"]
        self.assertEqual(fact[2], {"id": "b1x", "kind": "bash", "title": "Start console",
                                   "output": "/tmp/claude/tasks/b1x.output"})


class CoreKeepsTheLastOutput(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "ux", cwd="/w", session_id="s", channel="cli", owner=True)
        self.ev("session.started", "ux")
        self.ev("session.background", "ux", ts=100, id="b1", kind="bash", title="Start console", output="/tmp/b1.output")

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or self.seq, "type": type,
                                                   "entity": entity, "data": data})
        return why

    def test_output_fact_and_the_full_list_keep_it(self):
        self.assertIsNone(self.ev("session.background_output", "ux", id="b1", at=150.0,
                                  line="[uxr] owner console: http://127.0.0.1:7070/admin", channel="cli", owner=True))
        b = self.st["sessions"]["ux"]["background"]["b1"]
        self.assertEqual((b["output"], b["out_at"], b["out"]), ("/tmp/b1.output", 150.0,
                                                                 "[uxr] owner console: http://127.0.0.1:7070/admin"))
        # Claude Code sends the whole list: the file and the last output are not lost
        self.ev("session.background", "ux", tasks=[{"id": "b1", "kind": "bash", "title": "Start console"}])
        self.assertEqual(self.st["sessions"]["ux"]["background"]["b1"]["out_at"], 150.0)
        self.assertIsNotNone(self.ev("session.background_output", "ux", id="nope", at=1.0, line="x",
                                     channel="cli", owner=True))
        self.assertIsNotNone(self.ev("session.background_output", "ux", id="b1", at=1.0, line="x",
                                     channel="session", owner=False, sender="ux"))   # written by the watchdog, not by a session

    def test_snapshot_shows_since_and_the_last_output(self):
        r = views.Reader()
        for i, (t, e, d) in enumerate([
                ("session.requested", "ux", {"cwd": "/w", "session_id": "s", "channel": "cli", "owner": True}),
                ("session.started", "ux", {}),
                ("session.background", "ux", {"id": "b1", "kind": "bash", "title": "Start console", "output": "/o"}),
                ("session.background_output", "ux", {"id": "b1", "at": 150.0, "line": "listening on 7070",
                                                     "channel": "cli", "owner": True})]):
            r.feed({"seq": i + 1, "ts": 100 + i, "type": t, "entity": e, "data": d})
        (s,) = [s for s in r.snapshot()["sessions"] if s["name"] == "ux"]
        (b,) = s["background"]
        self.assertEqual((b["title"], b["since"], b["out"], b["out_at"]), ("Start console", 102, "listening on 7070", 150.0))


class WatcherLooksAtTheBackground(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bgw-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.w = watch.Watcher(self.dir, cfg={"off": True})
        self.w.started = True
        self.out = os.path.join(self.dir, "b1.output")
        with open(self.out, "w") as f:
            f.write("> console\n[uxr] session runtime on http://127.0.0.1:7070\n\n")
        os.utime(self.out, (T0 + 60, T0 + 60))
        self.seq = 0
        self.feed(0, "session.requested", "ux", cwd="/w", session_id="s", channel="cli", owner=True)
        self.feed(1, "session.started", "ux")
        self.feed(30, "session.background", "ux", id="b1", kind="bash", title="Restart local console", output=self.out)

    def feed(self, t, type, entity=None, **data):
        self.seq += 1
        self.w.feed({"seq": self.seq, "ts": T0 + t, "type": type, "entity": entity, "data": data}, T0 + t)

    def spooled(self, type):
        d = os.path.join(self.dir, "spool")
        out = [e for _, e in spool.take(d)]
        for n in os.listdir(d) if os.path.isdir(d) else []:
            os.unlink(os.path.join(d, n))
        return [e for e in out if e["type"] == type]

    def test_last_output_goes_to_the_core_once_per_change(self):
        self.w.background(T0 + 90)
        (o,) = self.spooled("session.background_output")
        self.assertEqual((o["entity"], o["data"]["id"], o["data"]["at"], o["data"]["line"]),
                         ("ux", "b1", T0 + 60, "[uxr] session runtime on http://127.0.0.1:7070"))
        self.w.background(T0 + 120)
        self.assertEqual(self.spooled("session.background_output"), [])          # the file did not change: stay silent

    def test_long_and_silent_background_is_an_alert_once(self):
        self.w.background(T0 + 20 * 60)
        self.assertEqual(self.spooled("watch.alert"), [])                         # 20 min: too early
        self.w.background(T0 + watch.BG_LONG + 60)
        (a,) = self.spooled("watch.alert")
        self.assertEqual((a["entity"], a["data"]["kind"]), ("ux", "background"))
        self.assertIn("Restart local console", a["data"]["reason"])
        self.assertIn("127.0.0.1:7070", a["data"]["reason"])                      # what it printed last
        self.w.background(T0 + 3 * watch.BG_LONG)
        self.assertEqual(self.spooled("watch.alert"), [])                         # once per task

    def test_a_silent_command_is_not_an_alert_for_two_hours(self):
        # live case: night builds, local CI and a pre-commit lock under load stay silent for an hour or two and still work
        self.w.background(T0 + 2 * 3600)
        self.assertEqual(self.spooled("watch.alert"), [])                         # 2 h without output: still working
        self.w.background(T0 + 3 * 3600 + 60)
        (a,) = self.spooled("watch.alert")                                        # 3 h and silent: hung
        self.assertEqual((a["entity"], a["data"]["kind"]), ("ux", "background"))

    def test_background_that_keeps_writing_is_not_an_alert(self):
        os.utime(self.out, (T0 + watch.BG_LONG, T0 + watch.BG_LONG))              # it writes, so it is working
        self.w.background(T0 + watch.BG_LONG + 60)
        self.assertEqual(self.spooled("watch.alert"), [])

    def test_agent_without_a_file_is_judged_by_age(self):
        self.feed(40, "session.background", "ux", id="ag1", kind="agent", title="Map billing")
        self.w.background(T0 + watch.BG_AGENT + 60)
        kinds = [(a["data"]["kind"], "Map billing" in a["data"]["reason"]) for a in self.spooled("watch.alert")]
        self.assertIn(("background", True), kinds)

    def test_core_takes_the_alert(self):
        st, why = reducer.empty(), None
        for i, (t, e, d) in enumerate([
                ("session.requested", "ux", {"cwd": "/w", "session_id": "s", "channel": "cli", "owner": True}),
                ("watch.alert", "ux", {"kind": "background", "reason": "2 h in the background", "channel": "cli", "owner": True})]):
            st, _, why = reducer.reduce(st, {"seq": i + 1, "ts": 100 + i, "type": t, "entity": e, "data": d})
        self.assertIsNone(why)
        self.assertIn("ux:background", st["watch"])          # its own shelf: does not mute alarms about the session's behavior


if __name__ == "__main__":
    unittest.main()
