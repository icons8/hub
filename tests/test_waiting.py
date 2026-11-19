"""What a session is waiting for (owner decision 2026-10-03: directions end only on the owner's say-so, but if there is
something to do they must signal that they are waiting for something; a "free" label on all of them says nothing useful).
The session itself reports the fact `hub waiting "<whom/what>"` (session.waiting); the page shows it instead of
"free", with the time since. Another session does not say it for her; the hub and the owner can."""
import tests  # noqa: F401 — hide the machine's settings
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from core import forms, journal, reducer, spool, views

OWNER = {"channel": "cli", "owner": True}
HUB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")


class Waiting(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.requested", "maps", cwd="/g", session_id="s-g", **OWNER)
        self.ev("session.requested", "other", cwd="/o", session_id="s-o", **OWNER)

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 100 + self.seq, "type": type,
                                                   "entity": entity, "data": data})
        return why

    def mine(self, **who):
        return {"channel": "session", "owner": False, **who}

    def test_session_says_what_it_waits_for_and_clears_it(self):
        self.assertIsNone(self.ev("session.waiting", "maps", ts=500, text="a reply from support on ticket 42", **self.mine(sender="maps")))
        self.assertEqual(self.st["sessions"]["maps"]["waiting"], {"text": "a reply from support on ticket 42", "ts": 500})
        self.assertIsNone(self.ev("session.waiting", "maps", clear=True, **self.mine(sender="maps")))
        self.assertNotIn("waiting", self.st["sessions"]["maps"])

    def test_only_the_session_itself_or_hub_or_owner(self):
        self.assertIsNotNone(self.ev("session.waiting", "maps", text="x", **self.mine(sender="other")))
        self.assertIsNone(self.ev("session.waiting", "maps", text="the hub knows", **self.mine(sender="hub")))
        self.assertIsNone(self.ev("session.waiting", "maps", text="the owner knows", **OWNER))
        self.assertIsNotNone(self.ev("session.waiting", "maps", text="", **OWNER))      # empty is not a fact
        self.assertIsNotNone(self.ev("session.waiting", "nobody", text="x", **OWNER))

    def test_a_message_after_the_label_makes_it_stale_and_saying_it_again_freshens_it(self):
        # 2026-10-13 (issue #118): the label is the session's word on why she is free; if someone wrote to her after it
        # (the owner, another
        # session) the word is stale (snapshot: waited, the page shows a gray "waited: ... until"), it is not erased.
        # The fact is the
        # message itself: whether she read it in a turn, by insertion or not yet does not matter
        maps = lambda: self.st["sessions"]["maps"]
        self.ev("session.waiting", "maps", ts=210, text="MR review", **self.mine(sender="maps"))
        self.ev("turn.started", "maps", ts=230)                         # the end of background work: a turn without the word
        self.ev("turn.result", "maps", ts=235, text="still waiting", turns=1)
        self.assertNotIn("stale", maps()["waiting"])
        for who in (OWNER, self.mine(sender="other")):
            self.ev("session.waiting", "maps", ts=240, text="MR review", **self.mine(sender="maps"))
            self.assertIsNone(self.ev("message.received", None, ts=300, to="maps", text="word", **who))
            self.assertEqual(maps()["waiting"], {"text": "MR review", "ts": 240, "stale": 300}, who)
            self.assertIsNone(reducer.tools.waiting_now(maps()))
            self.ev("message.received", None, ts=320, to="maps", text="more", **who)
            self.assertEqual(maps()["waiting"]["stale"], 300)           # the staleness time is the first one
        self.ev("session.waiting", "maps", ts=410, text="MR review", **self.mine(sender="maps"))
        self.assertEqual(maps()["waiting"], {"text": "MR review", "ts": 410})

    def test_a_message_redirected_from_a_sleeping_executor_stales_the_receivers_label(self):
        # review 2026-10-14: a message to a sleeping executor that handed in goes to the one who brought it up; the
        # label of the
        # recipient goes stale, not the executor's, who did not see the message
        self.ev("session.waiting", "maps", ts=210, text="MR review", **self.mine(sender="maps"))
        self.ev("session.waiting", "other", ts=211, text="the client's reply", **self.mine(sender="other"))
        self.st["sessions"]["maps"].update(role="executor", raised_by="other", state="retired")
        self.assertTrue(reducer.executors.dormant(self.st["sessions"]["maps"]))
        reducer.new_message(self.st, "m903", "maps", "how is it going?", 300, origin=forms.ORIGIN_HUB)
        self.assertEqual(self.st["messages"]["m903"]["session"], "other")
        self.assertNotIn("stale", self.st["sessions"]["maps"]["waiting"])
        self.assertEqual(self.st["sessions"]["other"]["waiting"]["stale"], 300)

    def test_a_message_redirected_to_a_sleeping_raiser_wakes_the_raiser_not_the_executor(self):
        # review 2026-10-14: a letter to a sleeping executor that handed in goes to the one who brought it up; if he
        # sleeps too, wake him, not the executor
        self.st["sessions"]["maps"].update(role="executor", raised_by="other", state="retired")
        self.st["sessions"]["other"]["state"] = "asleep"
        reducer.new_message(self.st, "m904", "maps", "how is it going?", 300, origin=forms.ORIGIN_HUB)
        self.assertEqual((self.st["_wake"], self.st["sessions"]["maps"]["state"], self.st["sessions"]["other"]["state"]),
                         (["other"], "retired", "starting"))

    def test_core_and_schedule_messages_do_not_make_it_stale(self):
        # a core reminder, the continuation of an interrupted turn, a `hub wait` check are not a new word
        self.ev("session.waiting", "maps", ts=210, text="MR review", **self.mine(sender="maps"))
        reducer.new_message(self.st, "m901", "maps", "check", 300, origin=forms.ORIGIN_CORE)
        reducer.new_message(self.st, "m902", "maps", "check", 300, origin=forms.ORIGIN_SCHEDULE, schedule="w1")
        self.assertNotIn("stale", self.st["sessions"]["maps"]["waiting"])

    def test_snapshot_gives_stale_label_as_waited(self):
        r = views.Reader()
        for i, (t, e, d) in enumerate([("session.requested", "maps", {"cwd": "/g", "session_id": "s-g", **OWNER}),
                                       ("session.waiting", "maps", {"text": "a review from a colleague", **OWNER})]):
            r.feed({"seq": i + 1, "ts": 100 + i, "type": t, "entity": e, "data": d})
        r.state["sessions"]["maps"]["waiting"]["stale"] = 150
        (s,) = [s for s in r.snapshot()["sessions"] if s["name"] == "maps"]
        self.assertIsNone(s["waiting"])
        self.assertEqual(s["waited"]["text"], "a review from a colleague")

    def test_snapshot_shows_it(self):
        r = views.Reader()
        for i, (t, e, d) in enumerate([("session.requested", "maps", {"cwd": "/g", "session_id": "s-g", **OWNER}),
                                       ("session.waiting", "maps", {"text": "a review from a colleague", **OWNER})]):
            r.feed({"seq": i + 1, "ts": 100 + i, "type": t, "entity": e, "data": d})
        (s,) = [s for s in r.snapshot()["sessions"] if s["name"] == "maps"]
        self.assertEqual(s["waiting"]["text"], "a review from a colleague")

    def test_cli_from_a_session(self):
        env = {**os.environ, "HUB_SESSION": "maps"}
        for args in (["waiting"], ["waiting", ""]):
            r = subprocess.run([sys.executable, HUB, *args], capture_output=True, text=True, env=env)
            self.assertNotEqual(r.returncode, 0, args)                                 # without text: a hint
        r = subprocess.run([sys.executable, HUB, "waiting", "reply", "Google"], capture_output=True, text=True,
                           env={**env, "HUB_CLI_WAIT": "0"})
        spool = os.path.join(os.environ["HUB_STATE_DIR"], "spool")
        items = []
        for n in sorted(os.listdir(spool)) if os.path.isdir(spool) else []:
            with open(os.path.join(spool, n)) as f:
                items.append(json.load(f))
            os.unlink(os.path.join(spool, n))
        got = [(i["type"], i["entity"], i["data"].get("text"), i["data"].get("sender")) for i in items
               if i["type"] == "session.waiting"]
        self.assertEqual(got, [("session.waiting", "maps", "reply Google", "maps")], r.stdout + r.stderr)

    def cli(self, me, *args):
        """`hub waiting ...` from the session me in a hub with the sessions hub (kind hub), maps and qa: what landed in the spool."""
        state = tempfile.mkdtemp(prefix="waiting-")
        self.addCleanup(shutil.rmtree, state, True)
        j = journal.Journal(os.path.join(state, "journal.jsonl"))
        j.append("session.requested", "hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", **OWNER})
        for n in ("maps", "qa"):
            j.append("session.requested", n, {"cwd": "/" + n, "session_id": "s-" + n, **OWNER})
        env = {**os.environ, "HUB_STATE_DIR": state, "HUB_SESSION": me, "HUB_CLI_WAIT": "0"}
        r = subprocess.run([sys.executable, HUB, "waiting", *args], capture_output=True, text=True, env=env)
        return [(e["entity"], e["data"].get("clear"), e["data"].get("text"))
                for _, e in spool.take(os.path.join(state, "spool")) if e["type"] == "session.waiting"], r

    def test_the_hub_clears_another_sessions_waiting(self):
        # the hub's `hub waiting maps --clear` from its own session: the CLI took the name from HUB_SESSION, and the hub
        # itself got
        # "waiting: maps --clear" while maps kept its label
        got, r = self.cli("hub", "maps", "--clear")
        self.assertEqual(got, [("maps", True, None)], r.stdout + r.stderr)

    def test_a_session_naming_another_in_its_text_says_its_own_waiting(self):
        # the session name as the first word is her own words ("waiting: qa will recheck"), not a label for qa
        got, r = self.cli("maps", "qa", "rechecks", "stage")
        self.assertEqual(got, [("maps", None, "qa rechecks stage")], r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
