"""The gist of a session — one line in "Now" (owner requirement: write the essence itself, within 160 characters or 3–4
lines; give all sessions a hub command to update their own gist). The session reports the fact itself with
`hub gist "<gist>"` (session.gist): what it has done, what it needs from the owner, what it is doing now. The core keeps
it on the session and cuts anything longer than 160 characters at a word. A gist is stale, and the snapshot does not
return it (the page shows the first phrase of the result), when a result for the owner without a gist line of its own
came after it: in a later turn, or later in the same turn unless the session waits or runs a background job that is
not a service one. A turn without a result for the owner ("—", a built-in command) changes nothing. Another session
cannot speak for it; the hub and the owner can."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import subprocess
import sys
import unittest

from core import reducer, tools, views

OWNER = {"channel": "cli", "owner": True}
HUB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")


class Gist(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.requested", "maps", cwd="/g", session_id="s-g", **OWNER)
        self.ev("session.requested", "other", cwd="/o", session_id="s-o", **OWNER)
        for n in ("hub", "maps", "other"):
            self.ev("session.started", n)

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 100 + self.seq, "type": type,
                                                   "entity": entity, "data": data})
        return why

    def mine(self, who="maps"):
        return {"channel": "session", "owner": False, "sender": who}

    def snap(self, name="maps"):
        r = views.Reader()
        r.state = self.st
        return next(s for s in r.snapshot(now=10 ** 4)["sessions"] if s["name"] == name)

    def owner_turn(self, result, gist=None):
        """A turn on an owner message — as the host and bin/hub write it: the gist is a command mid-turn, the result is at the end."""
        m = self.seq + 1
        self.ev("message.received", None, to="maps", text="how is the map?", channel="web", owner=True)
        self.ev("turn.started", "maps", marker=f"m{m}", prompt_head="how is the map?")
        if gist:
            self.assertIsNone(self.ev("session.gist", "maps", text=gist, **self.mine()))
        self.ev("turn.result", "maps", text=result, turns=3)
        self.ev("turn.stopped", "maps")

    def test_session_sets_its_gist_and_clears_it(self):
        self.assertIsNone(self.ev("session.gist", "maps", ts=500, text="The Europe map is ready, waiting for a 'yes' to publish",
                                  **self.mine()))
        self.assertEqual(self.st["sessions"]["maps"]["gist"]["text"], "The Europe map is ready, waiting for a 'yes' to publish")
        self.assertEqual(self.snap()["gist"], {"text": "The Europe map is ready, waiting for a 'yes' to publish", "ts": 500})
        self.assertIsNone(self.ev("session.gist", "maps", clear=True, **self.mine()))
        self.assertNotIn("gist", self.st["sessions"]["maps"])
        self.assertIsNone(self.snap()["gist"])

    def test_only_the_session_itself_or_hub_or_owner(self):
        self.assertIsNotNone(self.ev("session.gist", "maps", text="x", **self.mine("other")))
        self.assertIsNone(self.ev("session.gist", "maps", text="hub knows", **self.mine("hub")))
        self.assertIsNone(self.ev("session.gist", "maps", text="owner knows", **OWNER))
        self.assertIsNotNone(self.ev("session.gist", "maps", text="  ", **OWNER))     # empty — not a fact
        self.assertIsNotNone(self.ev("session.gist", "nobody", text="x", **OWNER))
        # a plugin process writes its own facts, not the session's words (review, high)
        self.assertIsNotNone(self.ev("session.gist", "maps", text="from the plugin", channel="plugin", owner=False))
        self.assertEqual(self.st["sessions"]["maps"]["gist"]["text"], "owner knows")

    def test_longer_than_160_is_cut_by_word(self):
        long = "Fixed the feed loading on the page: " + "checked in three browsers and on a phone, " * 6
        self.ev("session.gist", "maps", text=long + "\n\nsecond paragraph", **self.mine())
        text = self.st["sessions"]["maps"]["gist"]["text"]
        self.assertLessEqual(len(text), reducer.GIST)
        self.assertEqual(text, reducer.one_line(long, word=True))
        self.assertTrue(text.endswith("…"), text)
        self.assertTrue(long.startswith(text[:-1].rstrip(" ,")), text)   # at a word: not mid-word
        self.assertNotIn("\n", text)
        # a word ending exactly at the boundary is not lost
        exact = "a" * 149 + " bcdefghij" + " end"
        self.assertEqual(reducer.one_line(exact, word=True), "a" * 149 + " bcdefghij…")
        self.ev("session.gist", "maps", text="Exactly   short\nline", **self.mine())
        self.assertEqual(self.st["sessions"]["maps"]["gist"]["text"], "Exactly short line")

    def test_first_line_of_the_last_owner_result_is_current(self):
        self.owner_turn("The Europe map is built, waiting for a 'yes' to publish.\n" + "Details. " * 30)
        self.assertEqual(self.snap()["gist"]["text"], "The Europe map is built, waiting for a 'yes' to publish.")
        # a turn for another session (the result is not for the owner) does not make it stale: the last result to the owner is the same
        d = self.seq + 1
        self.ev("delegation.created", None, session="maps", for_session="hub", context="collect the log", **self.mine("hub"))
        self.ev("turn.started", "maps", marker=f"m{d}")
        self.ev("turn.result", "maps", text="log collected")
        self.ev("turn.stopped", "maps")
        self.assertEqual(self.snap()["gist"]["text"], "The Europe map is built, waiting for a 'yes' to publish.")

    def test_a_new_owner_result_without_a_new_gist_makes_it_stale(self):
        self.owner_turn("Done: the Europe map is built. " * 10)
        self.ev("session.gist", "maps", text="The Europe map awaits review", **OWNER)
        self.assertEqual(self.snap()["gist"]["text"], "The Europe map awaits review")
        self.owner_turn("Africa is done too. Details below. " * 10)   # the gist was not updated — the old one is not about this result
        self.assertEqual(self.snap()["gist"]["text"], "Africa is done too. Details below.")   # a short first sentence takes the next one; the list row says what was done, not the old state
        self.assertIn("gist", self.st["sessions"]["maps"])                # the old one stays in the core, the snapshot does not return it

    def test_gist_set_between_turns_belongs_to_the_last_turn(self):
        # the owner or the hub fixed the gist while the session is idle — it is about the last turn, not stale
        self.owner_turn("Done: the Europe map is built. " * 10)
        self.assertEqual(self.snap()["gist"]["text"], "Done: the Europe map is built.")   # no gist said: the result itself
        self.ev("session.gist", "maps", text="The Europe map awaits review", **OWNER)
        self.assertEqual(self.snap()["gist"]["text"], "The Europe map awaits review")

    def test_a_mid_turn_gist_is_not_the_caption_after_the_turn_ended(self):
        # `hub gist` mid-turn says what the session is doing now; the result closes that: a finished session's line
        # shows the result (its first line), not "searching the order..." (the Done section)
        self.owner_turn("The surcharge for order 27.10 is filed and checked, the receipt is attached. " * 6, gist="Preparing the surcharge for the order")
        self.assertEqual(self.snap()["gist"]["text"], "The surcharge for order 27.10 is filed and checked, the receipt is attached.")   # the result closes the mid-turn gist
        self.assertTrue(self.snap()["out_head"].startswith("The surcharge for order"))
        # a turn that ended without a result for the owner ("—") does not close it: the job may go on (review, item 3)
        d = self.seq + 1
        self.ev("turn.started", "maps", marker=f"m{d}")
        self.ev("session.gist", "maps", text="Looking for the order in the mail", **self.mine())
        self.assertEqual(self.snap()["gist"]["text"], "Looking for the order in the mail")           # the turn is running: it is "now"
        self.ev("turn.result", "maps", text="—")
        self.ev("turn.stopped", "maps")
        self.assertEqual(self.snap()["gist"]["text"], "Looking for the order in the mail")
        # the first line of the result itself is the gist of that turn, as before
        self.owner_turn("The surcharge is filed, the receipt is in the mail.\n" + "Details. " * 30, gist="Looking for the receipt")
        self.assertEqual(self.snap()["gist"]["text"], "The surcharge is filed, the receipt is in the mail.")

    def test_a_list_row_without_a_gist_says_what_was_done_not_nothing(self):
        # 2026-11-04: with no gist line (a result opening with a heading or a table, a gist never said) the row was
        # empty and a client fell back to the goal or the words of whoever wrote; now it is the first sentence of the result
        self.assertIsNone(self.snap()["gist"])                              # nothing was done yet: no status, no fallback
        self.owner_turn("## Summary\n\nBuilt the Europe map and checked the borders. Captions are left.\n\n| a | b |")
        self.assertIsNone(self.st["sessions"]["maps"].get("gist"))          # the core wrote no gist: the heading is not one
        self.assertEqual(self.snap()["gist"]["text"], "Summary. Built the Europe map and checked the borders. Captions are left.")   # a short result whole: its tail is often the state; the table's rows are not in it
        self.owner_turn("## Summary\n\nBuilt the Europe map and checked the borders. Captions are left.\n\n" + "Details. " * 30)   # a long one: the first sentence
        self.assertEqual(self.snap()["gist"]["text"], "Summary. Built the Europe map and checked the borders.")

    def test_result_lead(self):
        self.assertEqual(tools.result_lead("Done. The Europe map is built, the borders are checked."),
                         "Done. The Europe map is built, the borders are checked.")    # a bare "Done." takes the next sentence
        self.assertEqual(tools.result_lead("The Europe map is built. Waiting for a reply."), "The Europe map is built. Waiting for a reply.")   # short: whole
        cut = "The Europe map is built. " + "Checking the borders. " * 10
        self.assertEqual(tools.result_lead(cut[:159] + "…"), "The Europe map is built.")                              # cut by the core: the first sentence
        self.assertEqual(tools.result_lead("No sentence end in this line"), "No sentence end in this line")
        self.assertEqual(tools.result_lead("—"), "")
        self.assertEqual(tools.result_lead(None), "")
        self.assertEqual(tools.result_lead("Yes"), "Yes")                                # a short answer is still a result
        self.assertEqual(tools.result_lead("?"), "")                                   # a mark alone says nothing
        long = ("words " * 40)[:reducer.OUT_HEAD] + "…"                                  # cut by the core, no sentence end
        lead = tools.result_lead(long)
        self.assertLessEqual(len(lead), reducer.GIST)
        self.assertTrue(lead.endswith("words…"), lead)                                   # at a word, within the gist limit

    def test_the_result_start_is_marked_older_than_a_running_turn_and_not_an_old_result(self):
        self.owner_turn("## Summary\n\nThe Europe map is built. Waiting for a reply.")
        self.assertEqual(self.snap()["gist"], {"text": "Summary. The Europe map is built. Waiting for a reply.", "ts": 100 + self.seq - 1, "lead": True})
        # a turn for another session: its latest output is not for the owner — the result to the owner is an old one
        d = self.seq + 1
        self.ev("delegation.created", None, session="maps", for_session="hub", context="collect the log", **self.mine("hub"))
        self.ev("turn.started", "maps", marker=f"m{d}")
        self.ev("turn.result", "maps", text="log collected")
        self.ev("turn.stopped", "maps")
        self.assertIsNone(self.snap()["gist"])
        # the owner's result again: the row is its start
        self.owner_turn("## Africa\n\nBuilt. Checking the borders.")
        self.assertEqual(self.snap()["gist"]["text"], "Africa. Built. Checking the borders.")
        # a result ends its turn: in the next one (a background job's notice) it is older than the turn — the page
        # takes a gist over the watchdog's recap only from ts >= since (sidebar gistAfter)
        self.ev("turn.started", "maps", marker=None, prompt_head="background task notice")
        snap = self.snap()
        self.assertEqual((snap["state"], snap["gist"]["text"]), ("busy", "Africa. Built. Checking the borders."))
        self.assertLess(snap["gist"]["ts"], snap["since"])

    def test_a_gist_survives_the_result_while_it_waits_or_runs_a_background_job(self):
        # a result for the owner without a gist line closes the gist said earlier in the turn...
        self.owner_turn("Build started. Waiting for a green CI. " * 10, gist="Waiting for the build on CI")
        self.assertEqual(self.snap()["gist"]["text"], "Build started. Waiting for a green CI.")
        # ...unless the session still waits for something: then the gist is what is going on
        self.ev("session.waiting", "maps", text="CI build", **self.mine())
        self.assertEqual(self.snap()["gist"]["text"], "Waiting for the build on CI")
        self.ev("session.waiting", "maps", clear=True, **self.mine())
        self.assertEqual(self.snap()["gist"]["text"], "Build started. Waiting for a green CI.")
        # ...or runs a background job of its own (started recently, the snapshot is at 10**4)
        self.ev("session.background", "maps", ts=9500, tasks=[{"id": "b1", "kind": "bash", "title": "ci watch"}])
        self.assertEqual(self.snap()["gist"]["text"], "Waiting for the build on CI")

    def test_a_service_background_job_does_not_keep_the_gist(self):
        # a long quiet job (a monitor, a server) is a service one, not the work the gist is about (review, item 4)
        self.owner_turn("Build started. Waiting for a green CI. " * 10, gist="Waiting for the build on CI")
        self.ev("session.background", "maps", ts=200, tasks=[{"id": "b1", "kind": "bash", "title": "dev server"}])
        self.assertEqual(self.snap()["gist"]["text"], "Build started. Waiting for a green CI.")                              # since 200, silent: at 10**4 a service
        self.ev("session.background_output", "maps", id="b1", at=9900, line="GET / 200", channel="core")
        self.assertEqual(self.snap()["gist"]["text"], "Waiting for the build on CI")    # it speaks: work again

    def test_an_idle_gist_survives_a_builtin_command_and_a_silent_turn(self):
        # the owner set the gist between turns; /compact (turns == 0) and a check turn with "—" are no result for the
        # owner and do not hide it (review, item 2)
        self.owner_turn("Done: the Europe map is built. " * 10)
        self.ev("session.gist", "maps", text="The Europe map awaits review", **OWNER)
        self.ev("turn.started", "maps", marker=None, prompt_head="/compact")
        self.ev("turn.result", "maps", text="", turns=0)
        self.ev("turn.stopped", "maps")
        self.assertEqual(self.snap()["gist"]["text"], "The Europe map awaits review")
        self.ev("turn.started", "maps", marker=None, prompt_head="check")
        self.ev("turn.result", "maps", text="—", turns=1)
        self.ev("turn.stopped", "maps")
        self.assertEqual(self.snap()["gist"]["text"], "The Europe map awaits review")

    def test_a_turn_injected_mid_turn_is_the_same_turn(self):
        # an insertion mid-turn (watchdog, --now) is the same turn: the gist given before it is still "now" until the result
        m = self.seq + 1
        self.ev("message.received", None, to="maps", text="build the map", channel="web", owner=True)
        self.ev("turn.started", "maps", marker=f"m{m}", prompt_head="x")
        self.ev("session.gist", "maps", text="Building the Europe map", **self.mine())
        self.ev("turn.started", "maps", marker=None, prompt_head="insertion")
        self.assertEqual(self.snap()["gist"]["text"], "Building the Europe map")
        self.ev("turn.result", "maps", text="The Europe map is built. Checking the borders. " * 10)
        self.ev("turn.stopped", "maps")
        self.assertEqual(self.snap()["gist"]["text"], "The Europe map is built.")

    def test_cli_from_a_session(self):
        env = {**os.environ, "HUB_SESSION": "maps"}
        for args in (["gist"], ["gist", ""]):
            r = subprocess.run([sys.executable, HUB, *args], capture_output=True, text=True, env=env)
            self.assertNotEqual(r.returncode, 0, args)                                 # no text — a usage hint
        r = subprocess.run([sys.executable, HUB, "gist", "Map", "ready"], capture_output=True, text=True,
                           env={**env, "HUB_CLI_WAIT": "0"})
        spool = os.path.join(os.environ["HUB_STATE_DIR"], "spool")
        items = []
        for n in sorted(os.listdir(spool)) if os.path.isdir(spool) else []:
            with open(os.path.join(spool, n)) as f:
                items.append(json.load(f))
            os.unlink(os.path.join(spool, n))
        got = [(i["type"], i["entity"], i["data"].get("text"), i["data"].get("sender")) for i in items
               if i["type"] == "session.gist"]
        self.assertEqual(got, [("session.gist", "maps", "Map ready", "maps")], r.stdout + r.stderr)

    def test_cli_hub_sets_the_gist_of_another_session(self):
        import tempfile
        state = tempfile.mkdtemp(prefix="gist-cli-")
        with open(os.path.join(state, "journal.jsonl"), "w") as f:
            for i, (n, kind) in enumerate((("hub", "hub"), ("maps", None)), 1):
                f.write(json.dumps({"seq": i, "ts": i, "type": "session.requested", "entity": n, "src": "t",
                                    "data": {"cwd": "/w", "session_id": f"s-{n}", "kind": kind, **OWNER}}) + "\n")
        def run(me, *args):
            subprocess.run([sys.executable, HUB, "gist", *args], capture_output=True, text=True,
                           env={**os.environ, "HUB_SESSION": me, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0"})
            spool, got = os.path.join(state, "spool"), []
            for n in sorted(os.listdir(spool)) if os.path.isdir(spool) else []:
                with open(os.path.join(spool, n)) as f:
                    got.append(json.load(f))
                os.unlink(os.path.join(spool, n))
            return [(i["entity"], i["data"].get("text")) for i in got if i["type"] == "session.gist"]
        self.assertEqual(run("hub", "maps", "Map awaits review"), [("maps", "Map awaits review")])
        self.assertEqual(run("maps", "hub", "told", "me"), [("maps", "hub told me")])   # not the hub — about itself
        self.assertEqual(run("hub", "maps", "built,", "waiting"), [("hub", "maps built, waiting")])   # its own gist without quotes


if __name__ == "__main__":
    unittest.main()
