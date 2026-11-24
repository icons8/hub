"""Spec mode (owner decisions 2026-11-08): a person turns specs on for a session with /spec on in its composer; that
session may raise an executor with --spec. The executor writes only its spec file (a line in its prompt and a hook on
the file-editing tools); its turn results are interim reports to whoever raised it, and the task stays open — the
executor does not hand in, does not sleep, is not parked or nudged — until a person accepts the spec ("Accept") or drops
it ("Drop"); whoever raised it may do that only with the owner's word (--for mN). After "Accept" the same executor does
the work in the same conversation. The model is not called."""
import tests  # noqa: F401 — hide the machine's settings
import json
import os
import tempfile
import unittest

from core import executors, parking, reducer, specmode
from tests import winsafe
from tests.test_minihub import OWNER, Delegating

WEB = {"channel": "web", "owner": True}
CLI = {"channel": "cli", "owner": True}


def by(name):
    return {"channel": "session", "owner": False, "sender": name}


class Core(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **CLI)
        self.ev("session.requested", "maps", cwd="/w", session_id="s-maps", role="agent", about="Maps", **CLI)
        for n in ("hub", "maps"):
            self.ev("session.started", n)

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        self.acts = acts
        return why

    def to(self, who):
        return [m for m in self.st["messages"].values() if m["session"] == who]

    def raised(self, name="maps-1", **extra):
        """maps (specs on) raises an executor with --spec; like the daemon: the session and its task."""
        self.assertIsNone(self.ev("session.spec_set", "maps", on=True, **WEB))
        self.assertIsNone(self.ev("task.run_requested", name, task="design the export", spec=True, **by("maps"),
                                  **extra))
        for type, entity, data in executors.events_for(self.st, name, self.st["runs"][name], cwd="/w/x", worktree=None):
            self.assertIsNone(self.ev(type, entity, **data), (type, data))
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == name]
        self.ev("session.started", name)
        return d["id"]

    def turn(self, name, marker, text):
        self.ev("turn.started", name, marker=marker)
        self.ev("turn.result", name, text=text)
        self.ev("turn.stopped", name)

    # --- /spec on|off ---------------------------------------------------------------------------------------------

    def test_specs_are_turned_on_only_by_a_person_in_the_composer(self):
        refusals = [self.ev("session.spec_set", "maps", on=True, **who) for who in (by("maps"), by("hub"), CLI)]
        self.assertTrue(all(refusals), refusals)
        self.assertNotIn("spec", self.st["sessions"]["maps"])
        self.assertIsNone(self.ev("session.spec_set", "maps", on=True, **WEB))
        self.assertIs(self.st["sessions"]["maps"]["spec"], True)
        self.assertIsNone(self.ev("session.spec_set", "maps", on=False, channel="telegram", owner=True))
        self.assertNotIn("spec", self.st["sessions"]["maps"])

    def test_an_executor_has_no_specs_of_its_own(self):
        self.raised()
        self.assertIn("executor", self.ev("session.spec_set", "maps-1", on=True, **WEB))

    def test_spec_flag_needs_specs_on_at_whoever_raises(self):
        why = self.ev("task.run_requested", "maps-1", task="x", spec=True, **by("maps"))
        self.assertIn("/spec on", why)
        self.assertNotIn("maps-1", self.st.get("runs") or {})

    def test_a_folder_name_for_the_spec(self):
        self.ev("session.spec_set", "maps", on=True, **WEB)
        self.assertTrue(self.ev("task.run_requested", "maps-1", task="x", spec="../etc", **by("maps")))
        self.assertIsNone(self.ev("task.run_requested", "maps-2", task="x", spec="export-v2", **by("maps")))
        for type, entity, data in executors.events_for(self.st, "maps-2", self.st["runs"]["maps-2"], cwd="/w/x",
                                                       worktree=None):
            self.ev(type, entity, **data)
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "maps-2"]
        self.assertEqual(d["spec"]["path"], "specs/export-v2/spec.md")
        # the same folder while that spec is open: refused
        self.assertIn("export-v2", self.ev("task.run_requested", "maps-3", task="y", spec="export-v2", **by("maps")))

    # --- the task stays open -------------------------------------------------------------------------------------

    def test_the_task_carries_its_spec_and_the_brief_says_spec_mode(self):
        did = self.raised()
        d = self.st["delegations"][did]
        self.assertEqual(d["spec"], {"path": f"specs/{did}/spec.md", "state": "open"})
        brief = self.st["messages"][d["message"]]["text"]
        self.assertIn("Spec mode", brief)
        self.assertIn(f"specs/{did}/spec.md", brief)

    def test_a_result_with_an_open_spec_is_interim_and_the_task_stays_open(self):
        did = self.raised()
        d = self.st["delegations"][did]
        before = len(self.to("hub"))
        self.turn("maps-1", d["message"], "The spec is ready: three slices, two questions.")
        self.assertEqual(self.st["delegations"][did]["state"], "spec")
        (got,) = [m for m in self.to("maps") if "three slices" in m["text"]]
        self.assertIn(did, got["text"])
        self.assertFalse(got["text"].startswith("REPLIED"))
        self.assertFalse([m for m in self.to("hub")[before:] if m["text"].startswith("REPLIED")])
        self.assertTrue(self.st["delegations"][did]["spec"].get("ready"))
        # a later message from maps and its result: interim again, still open
        self.ev("message.received", None, to="maps-1", text="add the rollback case", **by("maps"))
        (m,) = [m for m in self.to("maps-1") if m["text"] == "add the rollback case"]
        self.turn("maps-1", m["id"], "Added the rollback case.")
        self.assertEqual(self.st["delegations"][did]["state"], "spec")
        self.assertTrue([m for m in self.to("maps") if "rollback case" in m["text"]])

    def test_the_spec_waits_for_the_owner_only_between_the_executors_turns(self):
        """Owner decision 2026-11-09: while the discussion goes on, the card "Spec dN is ready" must not hang."""
        from core import views
        did = self.raised()
        def ready():
            r = views.Reader()
            r.state = self.st
            row = {s["name"]: s for s in r.snapshot(now=1e6)["sessions"]}["maps-1"]
            return row["spec"]["ready"], row["status"]
        self.assertFalse(ready()[0])   # it has not reported yet
        self.turn("maps-1", self.st["delegations"][did]["message"], "The spec is ready.")
        self.assertEqual(ready(), (True, "waiting"))
        # the owner writes to it: its turn is the discussion — no card until it has answered
        self.assertIsNone(self.ev("message.received", None, to="maps-1", text="why two slices?", **WEB))
        (m,) = [m for m in self.to("maps-1") if m["text"] == "why two slices?"]
        self.ev("turn.started", "maps-1", marker=m["id"])
        self.assertEqual(ready(), (False, "working"))
        self.assertEqual(self.st["delegations"][did]["state"], "spec")   # the task itself still waits for "Accept"
        self.ev("turn.result", "maps-1", text="Because of the migration.")
        self.ev("turn.stopped", "maps-1")
        self.assertEqual(ready(), (True, "waiting"))

    def test_the_rule_of_the_card(self):
        waits = {"state": "spec"}
        for sess, want in (({"state": "idle"}, True), ({"state": "parked"}, True), ({"state": "busy"}, False),
                           ({"state": "starting"}, False), ({"state": "dialog"}, False),
                           ({"state": "idle", "background": {"b1": {}}}, False),   # a job of its own is still running
                           (None, True)):   # the executor's row is gone: nothing writes the spec, it can be decided
            self.assertIs(specmode.awaiting(waits, sess), want, sess)
        self.assertFalse(specmode.awaiting({"state": "working"}, {"state": "idle"}))
        self.assertFalse(specmode.awaiting(None, {"state": "idle"}))

    def test_an_open_spec_holds_the_executor_awake_and_unparked(self):
        did = self.raised()
        self.turn("maps-1", self.st["delegations"][did]["message"], "The spec is ready.")
        s = self.st["sessions"]["maps-1"]
        self.assertIsNotNone(__import__("core.guards", fromlist=["x"]).closable(self.st, s))
        self.assertEqual([x for x in executors.retiring(self.st, 10 ** 9) if x[1] == "maps-1"], [])
        self.assertFalse(parking.parkable(self.st, s))
        self.assertEqual([p for p in __import__("core.delegations", fromlist=["x"]).pending(self.st, 10 ** 9)
                          if p[1] == did], [])   # no reminders, no 90-minute timeout

    def test_an_open_spec_is_open_work_and_a_mini_hubs_task_waits_for_it(self):
        did = self.raised()
        self.turn("maps-1", self.st["delegations"][did]["message"], "The spec is ready.")
        self.assertTrue(executors._open_work(self.st, "maps-1", 10 ** 9))
        d = self.st["delegations"][did]
        up = {"id": "d1", "session": "maps"}
        d.update(parent="d1", minihub=True)
        self.assertEqual(__import__("core.minihub", fromlist=["x"]).waiting_on(self.st, up), ["maps-1"])

    def test_done_is_refused_while_the_spec_is_open(self):
        did = self.raised()
        self.turn("maps-1", self.st["delegations"][did]["message"], "The spec is ready.")
        self.assertIn("Accept", self.ev("delegation.done", did, **by("maps")))
        self.assertEqual(self.st["delegations"][did]["state"], "spec")

    def test_the_executor_is_raised_with_the_spec_mode_and_its_raiser_with_the_rule(self):
        did = self.raised()
        first = [a for a in self.actions if a["do"] == "spawn" and a["session"] == "maps-1"][0]
        self.assertTrue(first["spec"]["open"])   # the hook's settings from the very first launch
        self.assertIn({"do": "spec_mode", "session": "maps-1", "task": did, "path": f"specs/{did}/spec.md",
                       "boss": "maps", "open": True}, self.actions)   # the projection comes with the task
        again = reducer.spawn_action(self.st["sessions"]["maps-1"], st=self.st)
        self.assertEqual(again["spec"], {"task": did, "path": f"specs/{did}/spec.md", "boss": "maps", "open": True})
        self.assertTrue(reducer.spawn_action(self.st["sessions"]["maps"], st=self.st).get("spec_boss"))
        self.assertNotIn("spec_boss", reducer.spawn_action(self.st["sessions"]["hub"], st=self.st))

    # --- Accept and Drop -----------------------------------------------------------------------------------------

    def test_the_owner_accepts_and_the_executor_does_the_work(self):
        did = self.raised()
        self.turn("maps-1", self.st["delegations"][did]["message"], "The spec is ready.")
        self.assertIsNone(self.ev("delegation.spec_accepted", did, sha256="ab" * 32,
                                  pending=f"specs/{did}/.accepted-abababababab-00000000.md", **WEB))
        d = self.st["delegations"][did]
        self.assertEqual((d["state"], d["spec"]["state"]), ("working", "accepted"))
        self.assertIn({"do": "spec_mode", "session": "maps-1", "task": did, "path": f"specs/{did}/spec.md",
                       "boss": "maps", "open": False, "accepted_from": f"specs/{did}/.accepted-abababababab-00000000.md"},
                      self.acts)
        (go,) = [m for m in self.to("maps-1") if m.get("for_delegation") == did]
        self.assertIn("accepted", go["text"])
        self.assertIn("spec.accepted.md", go["text"])
        self.assertIn("accepted", self.ev("delegation.spec_accepted", did, sha256="ab" * 32, pending=f"specs/{did}/.accepted-abababababab-00000000.md", **WEB))
        # its next result is the usual hand-in
        self.turn("maps-1", go["id"], "Done: branch task/maps-1, tests green.")
        self.assertEqual(self.st["delegations"][did]["state"], "replied")
        self.assertIsNone(self.ev("delegation.done", did, **by("maps")))

    def test_accept_in_the_middle_of_the_first_turn(self):
        did = self.raised()
        self.ev("turn.started", "maps-1", marker=self.st["delegations"][did]["message"])
        self.assertIsNone(self.ev("delegation.spec_accepted", did, sha256="cd" * 32, pending=f"specs/{did}/.accepted-cdcdcdcdcdcd-00000000.md", channel="telegram",
                                  owner=True))
        self.ev("turn.result", "maps-1", text="Spec written.")
        self.ev("turn.stopped", "maps-1")
        d = self.st["delegations"][did]
        self.assertEqual(d["state"], "working")   # that turn's result is still the spec's report, not the hand-in
        self.assertTrue([m for m in self.to("maps") if "already accepted" in m["text"]])
        self.assertFalse([m for m in self.to("hub") if m["text"].startswith("REPLIED")])
        self.turn("maps-1", d["spec"]["go"], "Done by the spec.")
        self.assertEqual(self.st["delegations"][did]["state"], "replied")

    def test_a_broken_turn_keeps_the_spec_waiting(self):
        did = self.raised()
        self.turn("maps-1", self.st["delegations"][did]["message"], "The spec is ready.")
        self.ev("message.received", None, to="maps-1", text="one more case", **by("maps"))
        (m,) = [m for m in self.to("maps-1") if m["text"] == "one more case"]
        self.ev("turn.started", "maps-1", marker=m["id"])
        self.assertIsNone(self.ev("turn.result", "maps-1", text="", is_error=True, subtype="error_during_execution"))
        self.assertEqual(self.st["delegations"][did]["state"], "spec")

    def test_a_cut_off_task_with_an_open_spec_comes_back_to_waiting(self):
        did = self.raised()
        self.ev("turn.started", "maps-1", marker=self.st["delegations"][did]["message"])
        self.ev("turn.result", "maps-1", text="", is_error=True, subtype="error_during_execution")
        self.ev("turn.stopped", "maps-1")
        self.assertEqual(self.st["delegations"][did]["state"], "stopped")
        self.assertIsNotNone(__import__("core.guards", fromlist=["x"]).closable(self.st, self.st["sessions"]["maps-1"]))
        self.ev("message.received", None, to="maps-1", text="go on", **by("maps"))
        (m,) = [m for m in self.to("maps-1") if m["text"] == "go on"]
        self.turn("maps-1", m["id"], "The spec is ready now.")
        self.assertEqual(self.st["delegations"][did]["state"], "spec")
        self.assertIsNone(self.ev("delegation.spec_dropped", did, **WEB))

    def test_a_folder_is_taken_by_a_queued_request_too_and_dn_is_not_a_name(self):
        self.ev("session.spec_set", "maps", on=True, **WEB)
        self.assertIsNone(self.ev("task.run_requested", "maps-1", task="x", spec="auth", **by("maps")))
        self.assertIn("maps-1", self.ev("task.run_requested", "maps-2", task="y", spec="auth", **by("maps")))
        self.assertTrue(self.ev("task.run_requested", "maps-3", task="y", spec="d57", **by("maps")))

    def test_a_flag_without_its_task_does_not_stick(self):
        self.ev("session.spec_set", "maps", on=True, **WEB)
        self.ev("task.run_requested", "maps-1", task="x", spec=True, **by("maps"))
        (sess_ev,) = [e for e in executors.events_for(self.st, "maps-1", self.st["runs"]["maps-1"], cwd="/w/x",
                                                       worktree=None) if e[0] == "session.requested"]
        self.ev(*sess_ev[:2], **sess_ev[2])   # its task never came
        self.ev("session.started", "maps-1")
        self.ev("message.received", None, to="maps-1", text="hello", **CLI)
        (m,) = [m for m in self.to("maps-1") if m["text"] == "hello"]
        self.ev("turn.started", "maps-1", marker=m["id"])
        self.assertNotIn("task_spec", self.st["sessions"]["maps-1"])

    def test_whoever_raised_it_accepts_only_with_the_owners_word(self):
        did = self.raised()
        self.turn("maps-1", self.st["delegations"][did]["message"], "The spec is ready.")
        self.assertIn("copy", self.ev("delegation.spec_accepted", did, sha256="ab" * 32, **WEB))   # no copy made
        self.assertIn("--for", self.ev("delegation.spec_accepted", did, sha256="ab" * 32, pending=f"specs/{did}/.accepted-abababababab-00000000.md", **by("maps")))
        self.assertTrue(self.ev("delegation.spec_accepted", did, sha256="ab" * 32, pending=f"specs/{did}/.accepted-abababababab-00000000.md", **by("maps-1")))
        self.ev("message.received", None, to="hub", text="accept it", **CLI)
        to_hub = f"m{self.seq}"
        # the owner's message to another session is not his word to maps
        self.assertTrue(self.ev("delegation.spec_accepted", did, sha256="ab" * 32, pending=f"specs/{did}/.accepted-abababababab-00000000.md", **{"for": to_hub},
                                **by("maps")))
        self.ev("message.received", None, to="maps", text="the spec is fine, accept", **WEB)
        word = f"m{self.seq}"
        self.assertIsNone(self.ev("delegation.spec_accepted", did, sha256="ab" * 32, pending=f"specs/{did}/.accepted-abababababab-00000000.md", **{"for": word},
                                  **by("maps")))
        self.assertEqual(self.st["delegations"][did]["spec"]["by"], "maps")

    def reply_to_card(self, did, to="maps-1", text="all yes, except the second slice", who=None, card=None):
        """The owner answers the spec's card in Telegram with words, not a button: the bot names the card's task
        (spec_card) and sends them to the executor, quoting the card. -> the message id."""
        self.assertIsNone(self.ev("message.received", None, to=to, text=text, quote=f"Spec {did}: accept — …",
                                  spec_card=card or did, **(who or {"channel": "telegram", "owner": True})))
        return f"m{self.seq}"

    def accept(self, did, word, who="maps", type="delegation.spec_accepted"):
        pending = {"sha256": "ab" * 32, "pending": f"specs/{did}/.accepted-abababababab-00000000.md"} \
            if type == "delegation.spec_accepted" else {}
        return self.ev(type, did, **pending, **{"for": word}, **by(who))

    def test_the_owners_reply_to_the_spec_card_is_his_word_for_whoever_raised_it(self):
        # a colleague's hub: he answered the card "Spec dN" in Telegram with words, they went to the executor, and the
        # raiser's accept --for was refused as "the owner wrote it to the executor, not to you"
        did = self.raised()
        self.turn("maps-1", self.st["delegations"][did]["message"], "The spec is ready.")
        word = self.reply_to_card(did)
        self.assertEqual(self.st["messages"][word]["session"], "maps-1")   # the words still go to the executor
        self.assertEqual(self.st["messages"][word]["spec_card"], did)
        self.assertIsNone(self.accept(did, word))
        self.assertEqual(self.st["delegations"][did]["spec"]["by"], "maps")

    def test_the_hub_drops_by_the_reply_to_the_card_too(self):
        did = self.raised()
        self.turn("maps-1", self.st["delegations"][did]["message"], "The spec is ready.")
        word = self.reply_to_card(did, text="drop it")
        self.assertIsNone(self.accept(did, word, who="hub", type="delegation.spec_dropped"))
        self.assertEqual(self.st["delegations"][did]["spec"]["state"], "dropped")

    def test_what_a_reply_to_the_spec_card_does_not_open(self):
        did = self.raised()
        self.turn("maps-1", self.st["delegations"][did]["message"], "The spec is ready.")
        other = self.raised("maps-2")
        self.turn("maps-2", self.st["delegations"][other]["message"], "The spec is ready.")
        elsewhere = "the owner wrote it to maps-1, not to you"
        # his words to the executor without the card: as before, not his word to maps
        self.ev("message.received", None, to="maps-1", text="all yes", quote="Spec: accept — …",
                **{"channel": "telegram", "owner": True})
        self.assertIn(elsewhere, self.accept(did, f"m{self.seq}"))
        # a reply to the card of another spec counts only for that one
        self.assertIn("not to you", self.accept(did, self.reply_to_card(other, to="maps-2")))
        # a card named in words sent to another session is no link: the core keeps no spec_card
        word = self.reply_to_card(did, to="maps-2")
        self.assertNotIn("spec_card", self.st["messages"][word])
        self.assertIn("not to you", self.accept(did, word))
        # not the owner: words from Telegram not his (a member of the group), a module handle (from_owner, but not the
        # owner himself)
        word = self.reply_to_card(did, who={"channel": "telegram", "owner": False})
        self.assertNotIn("spec_card", self.st["messages"].get(word) or {})
        self.assertTrue(self.accept(did, word))
        word = self.reply_to_card(did, who={"channel": "mod:notes", "owner": True})
        self.assertNotIn("spec_card", self.st["messages"][word])
        self.assertTrue(self.accept(did, word))
        # a session that neither raised it nor is the hub, and the executor itself
        word = self.reply_to_card(did)
        self.ev("session.requested", "docs", cwd="/d", session_id="s-docs", role="agent", about="Docs", **CLI)
        self.ev("session.started", "docs")
        self.assertIn("accept", self.accept(did, word, who="docs"))
        self.assertTrue(self.accept(did, word, who="maps-1"))
        # and the same words are no "--for" of any other command: the reply opens only this spec
        self.assertIn(elsewhere, self.ev("message.received", None, to="maps-1", text="go", **{"for": word},
                                         **by("hub")) or "")
        self.assertEqual(self.st["delegations"][did]["spec"]["state"], "open")
        # a Claude Code command typed in reply to the card is an action on the session, not words about the spec
        word = self.reply_to_card(did, text="/compact")
        self.assertNotIn("spec_card", self.st["messages"][word])
        self.assertIn(elsewhere, self.accept(did, word))
        # nor is a remark to a person of the hub's group in reply to the card (fyi: it starts no turn)
        self.assertIsNone(self.ev("message.received", None, to="maps-1", text="@ann look", quote="Spec", spec_card=did,
                                  fyi=True, channel="telegram", owner=True))
        self.assertNotIn("spec_card", self.st["messages"][f"m{self.seq}"])
        # an accepted spec: a reply to its stale card links nothing
        self.assertIsNone(self.accept(did, self.reply_to_card(did)))
        late = self.reply_to_card(did)
        self.assertNotIn("spec_card", self.st["messages"][late])

    def test_drop_closes_the_task_and_the_executor_sleeps(self):
        did = self.raised()
        self.turn("maps-1", self.st["delegations"][did]["message"], "The spec is ready.")
        self.assertIsNone(self.ev("delegation.spec_dropped", did, **WEB))
        d = self.st["delegations"][did]
        self.assertEqual((d["state"], d["spec"]["state"]), ("done", "dropped"))
        self.assertTrue([a for a in self.acts if a["do"] == "spec_mode" and a.get("dropped")])
        self.assertTrue([m for m in self.to("maps") if "dropped" in m["text"]])
        self.assertNotEqual(self.st["sessions"]["maps-1"].get("closing", "none"), "none")   # it hands over and sleeps
        self.assertNotIn("spec", reducer.spawn_action(self.st["sessions"]["maps-1"], st=self.st))

    def test_specs_off_keeps_the_open_ones(self):
        did = self.raised()
        self.assertIsNone(self.ev("session.spec_set", "maps", on=False, **WEB))
        self.assertEqual(self.st["delegations"][did]["spec"]["state"], "open")
        self.assertTrue([m for m in self.to("maps") if did in m["text"] and "Open specs" in m["text"]])


class OwnerWords(Delegating):
    """Owner decision 2026-11-09: while an executor's spec is open the owner's words in its feed go to the executor
    itself, the mini-hub gets a copy "for your information" (no turn). Outside the spec mode, and once the spec is
    accepted or dropped, they go through the mini-hub as before (the 15.10 rule)."""

    def setUp(self):
        super().setUp()
        self.minihub()
        self.assertIsNone(self.ev("session.spec_set", "notes", on=True, **WEB))
        self.assertIsNone(self.run_task("notes-ci", text="design the export", spec=True))
        self.start("notes-ci")
        (self.d,) = [d for d in self.st["delegations"].values() if d["session"] == "notes-ci"]
        self.assertEqual(self.st["sessions"]["notes-ci"].get("bus"), "notes")

    def words(self, text="and the rollback case", **extra):
        mark = self.seq
        self.assertIsNone(self.ev("message.received", None, to="notes-ci", text=text, **extra, **WEB))
        return mark

    def test_the_words_go_to_the_executor_and_a_copy_to_the_minihub(self):
        for extra in ({}, {"quote": "the spec is ready"}):
            mark = self.words(**extra)
            (mine,) = self.to("notes-ci", mark)
            self.assertEqual(mine["text"], "and the rollback case")
            self.assertTrue(mine["from_owner"])
            self.assertFalse(mine.get("redirected") or mine.get("relayed_for"))
            (copy,) = self.to("notes", mark)
            self.assertTrue(copy["fyi"])
            self.assertEqual(copy["state"], "aside")   # no turn, no wake: it rides with the mini-hub's next message
            self.assertIn("notes-ci", copy["text"])
            self.assertIn(self.d["id"], copy["text"])
            self.assertIn("and the rollback case", copy["text"])
            self.assertFalse(copy.get("from_owner"))   # not an answer to anything of the mini-hub's
        self.assertIn("owner_talks", self.st["sessions"]["notes-ci"])   # it is the owner's interlocutor now

    def test_only_the_owners_own_words(self):
        # the hub relaying them (--for), a session's message, a wake, a comment in "Files" — as before
        mark = self.seq
        self.ev("message.received", None, to="hub", text="tell notes-ci: test", **WEB)
        word = f"m{self.seq}"
        self.ev("message.received", None, to="notes-ci", text="test", **{"for": word}, **by("hub"))
        self.assertEqual(self.to("notes-ci", mark + 1), [])
        self.assertEqual(len([m for m in self.to("notes", mark + 1) if "The owner to executor notes-ci" in m["text"]]), 1)
        self.assertEqual([m for m in self.to("notes", mark + 1) if m.get("fyi")], [])   # and no copy

    def test_a_command_attachments_and_a_long_text(self):
        mark = self.words("/compact")                                       # an action on the session: no copy
        self.assertEqual([m["text"] for m in self.to("notes-ci", mark)], ["/compact"])
        self.assertEqual(self.to("notes", mark), [])
        mark = self.words("x" * 2500)
        (copy,) = self.to("notes", mark)
        self.assertTrue(copy["text"].endswith("x" * 2000 + "…"))            # cut, and the cut is visible
        self.assertEqual(self.to("notes-ci", mark)[0]["text"], "x" * 2500)  # the executor has all of it
        mark = self.words("see this", attachments=[{"path": "/tmp/a.png", "name": "a.png", "size": 2048}])
        (copy,) = self.to("notes", mark)
        self.assertIn("/tmp/a.png", copy["text"])

    def test_a_sleeping_executor_gets_no_direct_words(self):
        # its spec is still open (the task is "stopped"), but it is asleep: as before the words go to whoever raised it,
        # wrapped, once and with no copy next to them
        self.assertIsNone(self.ev("session.stop_requested", "notes-ci", **OWNER))
        self.assertTrue(reducer.executors.dormant(self.st["sessions"]["notes-ci"]))
        self.assertTrue(specmode.is_open(self.st["delegations"][self.d["id"]]))
        mark = self.words("still there?")
        self.assertEqual(self.to("notes-ci", mark), [])
        (msg,) = self.to("notes", mark)
        self.assertTrue(msg["text"].startswith("The owner to executor notes-ci: still there?"), msg["text"])
        self.assertNotIn("fyi", msg)

    def test_the_input_field_stops_saying_it_goes_through_the_minihub(self):
        from core import views
        def relay():
            r = views.Reader()
            r.state = self.st
            return {s["name"]: s.get("relay") for s in r.snapshot(now=self.events[-1]["ts"])["sessions"]}["notes-ci"]
        self.assertIsNone(relay())
        self.ev("turn.started", "notes-ci", marker=self.d["message"])
        self.ev("turn.result", "notes-ci", text="The spec is ready.")
        self.ev("turn.stopped", "notes-ci")
        self.assertIsNone(relay())
        self.assertIsNone(self.ev("delegation.spec_dropped", self.d["id"], **WEB))
        self.assertEqual(relay(), "notes")

    def test_without_a_spec_through_the_minihub_as_before(self):
        self.assertIsNone(self.ev("session.spec_set", "notes", on=False, **WEB))
        self.assertIsNone(self.run_task("notes-ab", text="fix CI"))
        self.start("notes-ab")
        mark = self.seq
        self.assertIsNone(self.ev("message.received", None, to="notes-ab", text="and finish", **WEB))
        self.assertEqual(self.to("notes-ab", mark), [])
        (msg,) = self.to("notes", mark)
        self.assertTrue(msg["text"].startswith("The owner to executor notes-ab: and finish"))
        self.assertNotIn("fyi", msg)

    def test_a_neighbour_without_a_spec_is_not_moved(self):
        # one mini-hub, two executors: only the one with the open spec is talked to directly
        self.assertIsNone(self.run_task("notes-ab", text="fix CI"))
        self.start("notes-ab")
        mark = self.seq
        self.assertIsNone(self.ev("message.received", None, to="notes-ab", text="and finish", **WEB))
        self.assertEqual(self.to("notes-ab", mark), [])
        self.assertEqual(len(self.to("notes", mark)), 1)

    def test_after_accept_and_after_drop_through_the_minihub_again(self):
        self.ev("turn.started", "notes-ci", marker=self.d["message"])
        self.ev("turn.result", "notes-ci", text="The spec is ready.")
        self.ev("turn.stopped", "notes-ci")
        self.assertEqual(self.st["delegations"][self.d["id"]]["state"], "spec")
        self.assertIsNone(self.ev("delegation.spec_accepted", self.d["id"], sha256="ab" * 32,
                                  pending=f"specs/{self.d['id']}/.accepted-abababababab-00000000.md", **WEB))
        mark = self.words("go on")
        self.assertEqual([m for m in self.to("notes-ci", mark) if m.get("from_owner")], [])
        (msg,) = self.to("notes", mark)
        self.assertTrue(msg["text"].startswith("The owner to executor notes-ci: go on"))

    def test_after_drop_through_the_minihub_again(self):
        self.ev("turn.started", "notes-ci", marker=self.d["message"])
        self.ev("turn.result", "notes-ci", text="The spec is ready.")
        self.ev("turn.stopped", "notes-ci")
        self.assertIsNone(self.ev("delegation.spec_dropped", self.d["id"], **WEB))
        mark = self.seq
        self.ev("message.received", None, to="notes-ci", text="never mind", **WEB)
        self.assertEqual([m for m in self.to("notes-ci", mark) if m.get("from_owner")], [])
        self.assertTrue([m for m in self.to("notes", mark) if m["text"].startswith("The owner to executor notes-ci")])


class Projection(unittest.TestCase):
    """The hook's file: <state>/sessions/<name>/spec-mode.json, written atomically by the daemon."""

    def test_write_and_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            specmode.write_projection(tmp, "maps-1", {"task": "d7", "path": "specs/d7/spec.md", "boss": "maps",
                                                      "open": True})
            path = os.path.join(tmp, "sessions", "maps-1", specmode.PROJECTION)
            with open(path, encoding="utf-8") as f:
                got = json.load(f)
            self.assertEqual(got["path"], os.path.join(tmp, "specs", "d7", "spec.md"))
            self.assertTrue(got["open"])
            self.assertTrue(os.path.isfile(got["path"]))   # the folder and an empty file are made before the launch

    def test_a_link_in_place_of_the_spec_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "specs", "d7"))
            winsafe.symlink(self, os.path.join(tmp, "x"), os.path.join(tmp, "specs", "d7", "spec.md"))
            with self.assertRaises(OSError):
                specmode.prepare(tmp, "specs/d7/spec.md")

    def test_accept_copies_the_file_as_it_is_and_gives_the_copys_sum(self):
        with tempfile.TemporaryDirectory() as tmp:
            specmode.prepare(tmp, "specs/d7/spec.md")
            path = os.path.join(tmp, "specs", "d7", "spec.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write("# Spec\n")
            pending, sha = specmode.accept_copy(tmp, "specs/d7/spec.md")
            self.assertEqual(sha, specmode.digest(path))
            self.assertTrue(specmode.pending_ok("specs/d7/spec.md", pending))
            self.assertFalse(specmode.pending_ok("specs/d7/spec.md", "specs/d7/spec.md"))
            self.assertFalse(specmode.pending_ok("specs/d7/spec.md", "specs/x/" + pending.rsplit("/", 1)[1]))
            with open(os.path.join(tmp, "specs", "d7", "spec.md"), "a", encoding="utf-8") as f:
                f.write("edited after the accept\n")
            specmode.settle_copy(tmp, "specs/d7/spec.md", pending)   # the core said yes
            with open(os.path.join(tmp, "specs", "d7", specmode.ACCEPTED), encoding="utf-8") as f:
                self.assertEqual(f.read(), "# Spec\n")   # what was there at the press
            os.remove(path)
            with self.assertRaises(OSError):   # no file — nothing to accept
                specmode.accept_copy(tmp, "specs/d7/spec.md")


class Hook(unittest.TestCase):
    """hooks/spec_guard.py: Edit, Write, MultiEdit, NotebookEdit only to the spec while it is open; the rest goes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="spec-hook-")
        self.spec = {"task": "d7", "path": "specs/d7/spec.md", "boss": "maps", "open": True}
        specmode.write_projection(self.tmp, "maps-1", self.spec)
        self.path = specmode.abs_path(self.tmp, "specs/d7/spec.md")

    def tearDown(self):
        import platforms
        platforms.current.rmtree(self.tmp)

    def why(self, tool, **inp):
        from hooks import spec_guard
        return spec_guard.reason(tool, inp, self.tmp, "maps-1", cwd="/w/x")

    def test_only_the_spec_is_edited(self):
        self.assertIsNone(self.why("Write", file_path=self.path, content="# Spec"))
        self.assertIsNone(self.why("Edit", file_path=self.path, old_string="a", new_string="b"))
        for tool, key in (("Write", "file_path"), ("Edit", "file_path"), ("MultiEdit", "file_path"),
                          ("NotebookEdit", "notebook_path")):
            why = self.why(tool, **{key: "/w/x/core/reducer.py"})
            self.assertIn("write only " + self.path, why)
            self.assertIn("hub send maps", why)
        self.assertTrue(self.why("Write", file_path="/tmp/draft.md"))
        self.assertTrue(self.why("Write", file_path=""))
        self.assertTrue(self.why("Write"))

    def test_other_tools_are_not_limited(self):
        for tool, inp in (("Bash", {"command": "python3 -c 1"}), ("mcp__jira__create", {}), ("Agent", {}),
                          ("Read", {"file_path": "/w/x/core/reducer.py"})):
            self.assertIsNone(self.why(tool, **inp), tool)

    def test_a_link_to_the_spec_or_from_it_is_refused(self):
        link = os.path.join(self.tmp, "link.md")
        winsafe.symlink(self, self.path, link)
        self.assertIsNone(self.why("Write", file_path=link))   # the same real file: it is the spec
        os.remove(self.path)
        winsafe.symlink(self, link, self.path)   # the spec itself turned into a link
        self.assertTrue(self.why("Write", file_path=self.path))

    def test_accepted_opens_the_edits_without_a_restart(self):
        specmode.write_projection(self.tmp, "maps-1", {**self.spec, "open": False})
        self.assertIsNone(self.why("Write", file_path="/w/x/core/reducer.py"))

    def test_no_projection_lets_it_go_a_broken_one_refuses(self):
        from hooks import spec_guard
        self.assertIsNone(spec_guard.reason("Write", {"file_path": "/x"}, self.tmp, "other"))
        with open(os.path.join(self.tmp, "sessions", "maps-1", specmode.PROJECTION), "w", encoding="utf-8") as f:
            f.write("{")
        self.assertIn("cannot be read", self.why("Write", file_path=self.path))

    def test_the_hook_as_claude_runs_it(self):
        import subprocess
        import sys
        hook = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks", "spec_guard.py")
        env = {**os.environ, "HUB_STATE_DIR": self.tmp, "HUB_SESSION": "maps-1"}
        run = lambda inp: subprocess.run([sys.executable, hook], input=json.dumps(inp), capture_output=True,   # noqa: E731
                                         text=True, encoding="utf-8", env=env, timeout=60)
        out = run({"tool_name": "Write", "tool_input": {"file_path": "/w/x/a.py"}, "cwd": "/w/x"})
        self.assertEqual(json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(run({"tool_name": "Write", "tool_input": {"file_path": self.path}}).stdout.strip(), "")


class Host(unittest.TestCase):
    """The executor in spec mode is launched with the hook's settings and the spec line; its raiser with the rule."""

    def test_the_command_line(self):
        from hosts.streamjson import StreamHost
        host = StreamHost(tempfile.mkdtemp(prefix="spec-host-"))

        def suffix(cmd):
            with open(cmd[cmd.index("--append-system-prompt-file") + 1], encoding="utf-8") as f:
                return f.read()
        cmd = host.command("maps-1", "sid", executor_of="maps",
                           spec={"task": "d7", "path": "specs/d7/spec.md", "boss": "maps", "open": True})
        self.assertIn("hooks-settings-spec.json", " ".join(cmd))
        text = suffix(cmd)
        self.assertIn("Spec mode (task d7)", text)
        self.assertIn(os.path.join(host.state_dir, "specs", "d7", "spec.md"), text)
        with open(os.path.join(host.state_dir, "hooks-settings-spec.json"), encoding="utf-8") as f:
            guard = json.load(f)["hooks"]["PreToolUse"][0]
        self.assertEqual(guard["matcher"], "Edit|Write|MultiEdit|NotebookEdit")
        self.assertIn("spec_guard.py", json.dumps(guard))
        # accepted: an ordinary launch
        cmd = host.command("maps-1", "sid", executor_of="maps",
                           spec={"task": "d7", "path": "specs/d7/spec.md", "boss": "maps", "open": False})
        self.assertNotIn("hooks-settings-spec.json", " ".join(cmd))
        self.assertNotIn("Spec mode", suffix(cmd))
        boss = suffix(host.command("maps", "sid2", spec_boss=True))
        self.assertIn("Specs are on for you", boss)
        self.assertIn("not a card (AskUserQuestion)", boss)   # an open spec's forks stay in the spec, not a form
        self.assertNotIn("Specs are on for you", suffix(host.command("maps", "sid2")))


if __name__ == "__main__":
    unittest.main()
