"""Waiting for a person's reply (owner 01.10: a session wrote in Teams and set a check for tomorrow, though the reply
came 2 minutes later; 30.09: wait for the reply instead of moving on).
`hub wait "<what we wait for and where to look>"` sets checks for the session itself: in 10 min and at the end of the
period (--for, 4 h); once the reply is there, `hub wait done` cancels the rest. A check with no reply ends with "—",
which is nothing new for the owner."""
import tests  # noqa: F401 — ignore the machine's settings
import os
import subprocess
import sys
import tempfile
import unittest

from core import reducer, spool, views

HUB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")


class Cli(unittest.TestCase):
    def run_hub(self, *args):
        state = tempfile.mkdtemp(prefix="wait-")
        env = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0", "HUB_SESSION": "gp"}
        r = subprocess.run([sys.executable, HUB, *args], env=env, capture_output=True, text=True)
        return r, [ev for _, ev in spool.take(os.path.join(state, "spool"))]

    def test_wait_sets_a_series_of_checks_to_itself(self):
        r, evs = self.run_hub("wait", "a reply from Sam in the chat, thread 19:abc", "--for", "4h")
        self.assertEqual(r.returncode, 0, r.stderr)
        jobs = [e for e in evs if e["type"] == "schedule.set"]
        # 16.10: of eight checks, 116 of 122 turns were "—"; replies were found on the 2nd (5 min) and the late ones
        # (1 h, 4 h); so two checks: in 10 min and at the end of the period; a late reply is found later but not lost
        self.assertEqual(len(jobs), 2)
        self.assertTrue(all(e["entity"].startswith("gp-wait-") and e["data"]["session"] == "gp" for e in jobs))
        ats = [e["data"]["at"] for e in jobs]
        self.assertEqual([round((a - ats[0]) / 60) for a in ats], [0, 230])
        self.assertIn("hub wait done", jobs[0]["data"]["prompt"])
        # the check ends its own wait only: the tag of its id is the label (another wait of the session stays)
        tag = jobs[0]["entity"].split("-")[2]
        self.assertTrue(all(f"hub wait done {tag}" in e["data"]["prompt"] for e in jobs))
        self.assertIn("a reply from Sam in the chat", jobs[0]["data"]["prompt"])
        r, evs = self.run_hub("wait", "x", "--for", "30m")
        self.assertEqual(len([e for e in evs if e["type"] == "schedule.set"]), 2)   # 10 and 30 min
        self.assertIn("10 min, 30 min", r.stdout)   # a session's command: in English
        r, evs = self.run_hub("wait", "x", "--for", "5m")
        self.assertEqual(len([e for e in evs if e["type"] == "schedule.set"]), 1)   # the period is shorter than the first check: one, at the end
        r, evs = self.run_hub("wait", "x", "--for", "0m")
        self.assertNotEqual(r.returncode, 0)   # no checks at all: not "waiting"

    def test_a_command_like_word_is_not_what_is_awaited(self):
        # another hub's report: `hub wait list` armed two checks with the text "list"
        for word in ("list", "status", "cancel"):
            r, evs = self.run_hub("wait", word)
            self.assertNotEqual(r.returncode, 0, word)
            self.assertIn("hub wait", r.stderr)
            self.assertEqual([e for e in evs if e["type"] == "schedule.set"], [], word)

    def test_wait_needs_a_session(self):
        state = tempfile.mkdtemp(prefix="wait-")
        env = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0"}
        env.pop("HUB_SESSION", None)
        r = subprocess.run([sys.executable, HUB, "wait", "x"], env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)


class FeedText(unittest.TestCase):
    """The timeline shows a check in human terms: "waiting for a reply — <what>", without the service tag and the
    instruction (PR #99). The core extracts what we wait for from the check text in the same form `hub wait` writes;
    if they diverge, the test fails."""
    def test_feed_item_carries_the_job_and_what_is_awaited(self):
        r, evs = Cli.run_hub(self, "wait", "a colleague's reply in the releases chat", "--for", "30m")
        self.assertEqual(r.returncode, 0, r.stderr)
        rd, seq = views.Reader(), [0]

        def ev(t, e=None, **d):
            seq[0] += 1
            rd.feed({"seq": seq[0], "ts": 1000 + seq[0], "type": t, "entity": e, "data": d})
        ev("session.requested", "gp", cwd="/w", session_id="s", channel="cli", owner=True)
        ev("session.started", "gp")
        job = next(e for e in evs if e["type"] == "schedule.set")
        ev("schedule.set", job["entity"], **{**job["data"], "at": 1004, "channel": "cli", "owner": False})
        ev("schedule.set", "gp-daily", session="gp", every="1d", prompt="daily summary", channel="cli", owner=True)
        ev("schedule.due", job["entity"], slot=1000 + seq[0], channel="core")
        ev("schedule.due", "gp-daily", slot=1000 + seq[0], channel="core")
        items = [x for x in rd.timelines["gp"] if x.get("channel") == "schedule"]
        self.assertEqual([(x["job"], x.get("waiting")) for x in items],
                         [(job["entity"], "a colleague's reply in the releases chat"), ("gp-daily", None)])


class Quiet(unittest.TestCase):
    def test_check_without_reply_is_not_news(self):
        st, seq = reducer.empty(), [0]

        def ev(t, e=None, **d):
            nonlocal st
            seq[0] += 1
            st, _, _ = reducer.reduce(st, {"seq": seq[0], "ts": seq[0], "type": t, "entity": e, "data": d})
        ev("session.requested", "gp", cwd="/w", session_id="s", channel="cli", owner=True)
        ev("session.started", "gp")
        ev("turn.started", "gp", marker=None, prompt_head="[⏰ schedule gp-wait-1]")
        ev("turn.result", "gp", text="—")
        r = views.Reader()
        r.state = st
        self.assertFalse(r.snapshot()["sessions"][0]["unread"])


if __name__ == "__main__":
    unittest.main()
