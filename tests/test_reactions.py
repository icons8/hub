"""Emoji reactions (as in Telegram): 👀 — a turn took the owner's message, 👍 — the session answered (same badges as the
bot); an aborted turn, a failure and a turn that ended with a question to the owner clear the badge. Badges live in
the snapshot's reactions key. The owner's reaction to a session row does not wake it with a turn: it goes out with his
next message that the session receives."""
import tests  # noqa: F401 — do not see the machine's settings
import unittest

from core import forms, reducer, views

OWNER = {"channel": "web", "owner": True}


class Reactions(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.typed = reducer.empty(), 0, {}
        self.add("a")

    def add(self, name):
        self.ev("session.requested", name, cwd="/w", session_id=f"s-{name}", **OWNER)
        self.ev("session.started", name)

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.st, actions, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq if ts is None else ts,
                                                         "type": type, "entity": entity, "data": data})
        for a in actions:
            if a.get("do") == "type":
                self.typed[a["message"]] = a["text"]   # what the session received as input
        return why

    def say(self, text="do it", to="a", **data):
        self.assertIsNone(self.ev("message.received", None, to=to, text=text, **{**OWNER, **data}))
        return f"m{self.seq}"

    def badge(self, key):
        return {**(self.st.get("badges") or {}), **(self.st.get("owner_badges") or {})}.get(key)

    def result(self, text="Done.", session="a", **data):
        self.ev("turn.result", session, text=text, **data)

    def turn(self, mid, text="Done.", session="a"):
        """A turn in journal order (hubd stamps the tick with the event time, 2026-10-13): start, Stop, result."""
        self.ev("turn.started", session, marker=mid)
        self.ev("turn.stopped", session)
        self.result(text, session)

    def form(self):
        """An open form in the middle of a turn (not a card: the owner's word answers it within the same turn)."""
        self.ev("permission.requested", "a", request_id="r1", tool="AskUserQuestion",
                input={"questions": [{"question": "Which one?", "options": [{"label": "One"}, {"label": "Two"}]}]})
        self.assertEqual(self.st["sessions"]["a"]["state"], "dialog")

    def resume(self, account="spare"):
        """Respawn on another account after a limit: the core writes a continuation (LIMIT_CUT) — its id."""
        self.ev("session.exited", "a")
        self.st["sessions"]["a"]["account"] = account   # this is how a respawn sets it (spawn_action)
        self.ev("session.started", "a")
        return f"m{self.seq}x5"

    # --- 👀 → 👍 ----------------------------------------------------------------------------------------------------
    def test_eyes_when_taken_and_thumb_when_the_turn_answered(self):
        mid = self.say()
        self.assertIsNone(self.badge(mid))
        self.ev("turn.started", "a", marker=mid)
        self.assertEqual(self.badge(mid), "👀")
        self.ev("turn.stopped", "a")           # Stop comes before the result — the badge waits for the result
        self.assertEqual(self.badge(mid), "👀")
        self.ev("turn.result", "a", text="Done.")
        self.assertEqual(self.badge(mid), "👍")

    def test_badges_are_in_the_reactions_of_the_snapshot_not_in_messages(self):
        """The badge in messages made the whole message map travel the stream on every 👍 — now it is a separate key."""
        r = views.Reader()
        n = 0
        for type, entity, data in (("session.requested", "a", {"cwd": "/w", "session_id": "s-a", **OWNER}),
                                   ("session.started", "a", {}),
                                   ("message.received", None, {"to": "a", "text": "do it", **OWNER}),
                                   ("turn.started", "a", {"marker": "m3"}), ("turn.result", "a", {"text": "Done."})):
            n += 1
            r.feed({"seq": n, "ts": 100 + n, "type": type, "entity": entity, "data": data})
        snap = r.snapshot()
        self.assertEqual(snap["reactions"], {"badges": {"m3": "👍"}, "taken": {"a": 4}})
        self.assertNotIn("reaction", snap["messages"]["m3"])

    def test_every_message_of_a_turn_gets_the_mark_and_the_answer(self):
        """The owner added a message mid-turn (second turn.started without Stop) and typed input taken with the turn
        (also)."""
        first = self.say("one")
        self.ev("turn.started", "a", marker=first)
        second = self.say("two")
        self.ev("turn.started", "a", marker=second)
        self.assertEqual([self.badge(m) for m in (first, second)], ["👀"] * 2)
        self.ev("turn.stopped", "a")
        self.result()
        self.assertEqual([self.badge(m) for m in (first, second)], ["👍"] * 2)
        third = self.say("three")
        self.assertEqual(self.st["messages"][third]["state"], "typing")
        self.ev("turn.started", "a", marker=None, also=[third])
        self.assertEqual(self.badge(third), "👀")
        self.result()
        self.assertEqual(self.badge(third), "👍")

    def test_interrupted_or_failed_turn_leaves_no_mark(self):
        mid = self.say()
        self.ev("turn.started", "a", marker=mid)
        self.assertEqual(self.badge(mid), "👀")
        self.ev("turn.result", "a", text="Interrupted", is_error=True)
        self.assertIsNone(self.badge(mid))

    def test_a_quiet_result_is_not_an_answer(self):
        mid = self.say()
        self.ev("turn.started", "a", marker=mid)
        self.assertEqual(self.badge(mid), "👀")
        self.ev("turn.result", "a", text="—")   # quiet result of a `hub wait` check
        self.assertIsNone(self.badge(mid))

    def test_a_builtin_command_result_is_not_the_answer(self):
        """/compact, /cost — a result without a turn (turns: 0): no badge, and nobody else's is cleared."""
        mid = self.say()
        self.ev("turn.started", "a", marker=mid)
        self.ev("turn.result", "a", text="", turns=0)
        self.assertEqual(self.badge(mid), "👀")
        self.ev("turn.result", "a", text="Done.")
        self.ev("turn.result", "a", text="", turns=0)
        self.assertEqual(self.badge(mid), "👍")

    # --- the thumb leads to the answer: reactions.links {session, seq}  ------------------------------------------
    def link(self, key):
        return (self.st.get("badge_links") or {}).get(key)

    def answered_turn(self, mid, text="Done.", session="a"):
        """A turn that answered in a timeline row (session.said); its event number is where the thumb leads."""
        self.ev("turn.started", session, marker=mid)
        self.ev("session.said", session, text="Looking.")
        self.ev("session.said", session, text=text)
        said = self.seq
        self.ev("turn.stopped", session)
        self.result(text, session)
        return said

    def test_the_thumb_links_to_the_last_row_the_turn_said(self):
        mid = self.say()
        said = self.answered_turn(mid)
        self.assertEqual(self.badge(mid), "👍")
        self.assertEqual(self.link(mid), {"session": "a", "seq": said})

    def test_the_link_is_in_the_reactions_of_the_snapshot(self):
        r = views.Reader()
        n = 0
        for type, entity, data in (("session.requested", "a", {"cwd": "/w", "session_id": "s-a", **OWNER}),
                                   ("session.started", "a", {}),
                                   ("message.received", None, {"to": "a", "text": "do it", **OWNER}),
                                   ("turn.started", "a", {"marker": "m3"}), ("session.said", "a", {"text": "Done."}),
                                   ("turn.result", "a", {"text": "Done."})):
            n += 1
            r.feed({"seq": n, "ts": 100 + n, "type": type, "entity": entity, "data": data})
        snap = r.snapshot()
        self.assertEqual(snap["reactions"]["links"], {"m3": {"session": "a", "seq": 5}})
        self.assertNotIn("reaction_link", snap["messages"]["m3"])

    def test_no_row_said_in_the_turn_means_no_link(self):
        """A row said by an earlier turn is not this turn's answer; a blank row is not shown in the timeline."""
        first = self.say("one")
        self.answered_turn(first)
        second = self.say("two")
        self.ev("turn.started", "a", marker=second)
        self.ev("session.said", "a", text="   ")
        self.ev("session.said", "a", text="A helper agent's step", parent="toolu_1")
        self.ev("turn.stopped", "a")
        self.result()
        self.assertEqual(self.badge(second), "👍")
        self.assertIsNone(self.link(second))
        self.assertIsNotNone(self.link(first))

    def test_no_thumb_no_link(self):
        """An interrupted turn gets neither; the answered one before it keeps its link."""
        first = self.say("one")
        self.answered_turn(first)
        self.assertIsNotNone(self.link(first))
        mid = self.say()
        self.ev("turn.started", "a", marker=mid)
        self.ev("session.said", "a", text="Half of it")
        self.ev("turn.result", "a", text="Interrupted", is_error=True)
        self.assertIsNone(self.badge(mid))
        self.assertIsNone(self.link(mid))
        self.assertIsNotNone(self.link(first))

    def test_the_owners_own_badge_drops_the_link(self):
        for emoji in ("", "❤️"):
            mid = self.say()
            self.answered_turn(mid)
            self.assertIsNotNone(self.link(mid))
            self.assertIsNone(self.ev("message.reacted", None, target=mid, emoji=emoji, **OWNER))
            self.assertIsNone(self.link(mid))

    def test_links_go_with_the_badges_they_belong_to(self):
        """The core keeps the last BADGES_KEEP badges; a link outlives its badge nowhere."""
        mids = []
        for _ in range(3):
            mids.append(self.say())
            self.answered_turn(mids[-1])
        keep, reducer.BADGES_KEEP = reducer.BADGES_KEEP, 2
        try:
            mids.append(self.say())
            self.answered_turn(mids[-1])
        finally:
            reducer.BADGES_KEEP = keep
        self.assertEqual(set(self.st["badge_links"]), set(self.st["badges"]))

    def test_a_renamed_session_keeps_the_link(self):
        mid = self.say()
        said = self.answered_turn(mid)
        self.assertIsNone(self.ev("session.renamed", "a", to="b", **OWNER))
        self.assertEqual(self.link(mid), {"session": "b", "seq": said})

    # --- 1: only the owner's own message  --------------------------------------------------------------------------
    def test_a_marker_quoted_in_another_sessions_turn_is_not_taken(self):
        """A foreign message label in another session's turn (background notice, quote) is not a take: no 👀 for a
        message queued at the addressee, no reopening of one already answered."""
        self.add("b")
        self.ev("turn.started", "a", marker=None)   # a is busy: the message waits in the queue
        queued = self.say("for you, a")
        self.ev("turn.started", "b", marker=queued)
        self.assertIsNone(self.badge(queued))
        self.assertEqual(self.st["messages"][queued]["state"], "queued")
        self.ev("turn.stopped", "a")
        self.ev("turn.result", "a", text="Did the previous one.")
        self.turn(queued)
        self.assertEqual(self.badge(queued), "👍")
        self.ev("turn.started", "b", marker=None, also=[queued])
        self.ev("turn.result", "b", text="Interrupted", is_error=True)
        self.assertEqual(self.badge(queued), "👍")

    def test_only_the_owners_own_words_get_badges(self):
        """A core service notice with origin " from the owner..." (a question he withdrew) is not his message: no
        badge."""
        self.st["sessions"]["a"].setdefault("asks", []).append(
            {"id": "q1", "since": 50, "request_id": "r", "tool": "AskUserQuestion", "input": {}})
        self.ev("question.withdrawn", "a", id="q1", why="no longer needed", **OWNER)
        notice = max(self.st["messages"], key=lambda k: int(k[1:].split("x")[0]))
        self.assertTrue(self.st["messages"][notice]["origin"].startswith(forms.ORIGIN_OWNER))
        self.assertFalse(self.st["messages"][notice].get("from_owner"))
        self.turn(notice)
        self.assertIsNone(self.badge(notice))
        mid = self.say()
        self.turn(mid)
        self.assertEqual(self.badge(mid), "👍")

    # --- 5: tmux sends no results  -----------------------------------------------------------------------------------
    def test_no_badges_on_a_host_without_results(self):
        mid = self.say()
        self.turn(mid)
        self.assertEqual(self.badge(mid), "👍")
        self.ev("core.started", None, pid=1, host="tmux")
        mid = self.say("more")
        self.ev("turn.started", "a", marker=mid)
        self.ev("turn.stopped", "a")
        self.assertIsNone(self.badge(mid))

    # --- 6: the facts of the turn decide, not automaton transitions  -------------------------------------------------
    def test_parked_on_stop_before_the_result_still_gets_the_thumb(self):
        """Parking exactly at Stop (updated plugin), the result arrives after it — 👍, not a cleared badge."""
        mid = self.say()
        self.ev("turn.started", "a", marker=mid)
        self.st["sessions"]["a"]["plugins_stale"] = ["x"]
        self.ev("turn.stopped", "a")
        self.assertEqual(self.st["sessions"]["a"]["state"], "parked")   # parked: no process
        self.assertEqual(self.badge(mid), "👀")
        self.result()
        self.assertEqual(self.badge(mid), "👍")

    # --- whose result: the current turn's (journal in event order, 2026-10-13)  -----------------------------------
    def test_each_turn_decides_its_own_messages(self):
        a = self.say("one")
        self.turn(a)
        b = self.say("two")
        self.ev("turn.started", "a", marker=b)
        self.assertEqual((self.badge(a), self.badge(b)), ("👍", "👀"))
        self.ev("turn.stopped", "a")
        self.result("Interrupted", is_error=True)
        self.assertEqual((self.badge(a), self.badge(b)), ("👍", None))

    def test_the_result_decides_the_turn_whatever_its_duration(self):
        """The result belongs to the current turn; duration_ms is not compared with the turn start (it used to be used
        to guess whose result it was)."""
        for dur in (10 ** 9, 1000):   # longer than the whole turn and shorter than it
            mid = self.say()
            self.ev("turn.started", "a", marker=mid)
            self.seq += 30
            self.ev("turn.stopped", "a")
            self.result(duration_ms=dur)
            self.assertEqual(self.badge(mid), "👍", dur)

    def test_a_second_result_of_the_same_turn_changes_nothing(self):
        """The first result of a turn decides; the second (without a new turn start) is no longer about its messages."""
        mid = self.say()
        self.ev("turn.started", "a", marker=mid)
        began = 100 + self.seq
        self.ev("turn.stopped", "a")
        self.result(duration_ms=(100 + self.seq + 1 - began) * 1000)
        self.assertEqual(self.badge(mid), "👍")
        self.result("Interrupted", is_error=True, duration_ms=(100 + self.seq + 1 - began) * 1000)
        self.assertEqual(self.badge(mid), "👍")

    def test_a_turn_without_a_result_leaves_no_mark_when_the_next_starts(self):
        """No result came (the output broke off) — 👀 from the latest turn start: a new turn clears the previous one,
        and its result concerns only its own messages."""
        a = self.say("one")
        self.ev("turn.started", "a", marker=a)
        self.ev("turn.stopped", "a")
        b = self.say("two")
        self.ev("turn.started", "a", marker=b)
        self.assertEqual((self.badge(a), self.badge(b)), (None, "👀"))
        self.ev("turn.stopped", "a")
        self.result()
        self.assertEqual((self.badge(a), self.badge(b)), (None, "👍"))

    def test_a_turn_that_asked_the_owner_is_not_done(self):
        """The turn asked the owner (a card) — no badge even though the result has text; the next turn stands on its
        own."""
        mid = self.say()
        self.ev("turn.started", "a", marker=mid)
        self.ev("permission.requested", "a", request_id="r1", tool="AskUserQuestion", interactive=True,
                input={"questions": [{"question": "Which one?", "options": [{"label": "One"}, {"label": "Two"}]}]})
        self.assertTrue(self.st["sessions"]["a"].get("asks"))
        self.ev("turn.stopped", "a")
        self.result("Asked the owner.")
        self.assertIsNone(self.badge(mid))
        nxt = self.say("and more")
        self.turn(nxt)
        self.assertEqual(self.badge(nxt), "👍")   # the previous turn's question is still open, but it is not this turn's

    def test_a_question_answered_within_the_turn_does_not_take_the_thumb(self):
        """From the 2026-10-13 journal: the turn asked with a card, the owner answered while the turn ran (the answer
        was read by the same turn), the turn ended with a reply — 👍 for both the question and the answer."""
        mid = self.say()
        self.ev("turn.started", "a", marker=mid)
        self.ev("permission.requested", "a", request_id="r1", tool="AskUserQuestion", interactive=True,
                input={"questions": [{"question": "Which one?", "options": [{"label": "One"}, {"label": "Two"}]}]})
        self.assertIsNone(self.ev("permission.answered", "a", request_id="r1", behavior="allow",
                                  answers={"Which one?": "One"}, **OWNER))
        self.assertFalse(self.st["sessions"]["a"].get("asks"))
        answer = f"m{self.seq}"
        self.assertTrue(self.st["messages"][answer].get("from_owner"))
        self.ev("message.inject", answer, **OWNER)
        self.ev("turn.started", "a", marker=answer)
        self.ev("turn.stopped", "a")
        self.result("Did the first.")
        self.assertEqual((self.badge(mid), self.badge(answer)), ("👍", "👍"))

    def test_an_old_open_question_does_not_block_the_mark(self):
        self.st["sessions"]["a"].setdefault("asks", []).append({"id": "q1", "since": 1, "request_id": "r"})
        mid = self.say()
        self.turn(mid)
        self.assertEqual(self.badge(mid), "👍")

    def test_a_busy_session_from_an_old_state(self):
        """Updated mid-turn: a busy session in state.json has no badge fields — the turn is completed, the core does
        not crash."""
        mid = self.say()
        self.ev("turn.started", "a", marker=mid)
        self.st["sessions"]["a"].pop("eyes", None)
        self.st.pop("badges", None)
        nxt = self.say("more")
        self.assertIsNone(self.ev("turn.started", "a", marker=nxt))
        self.assertIsNone(self.ev("turn.result", "a", text="Done."))
        self.assertEqual(self.badge(nxt), "👍")

    def test_a_journal_of_the_old_order_does_not_break(self):
        """Journal from before the update: within a tick hooks come first, then stdout — Stop A, start B, result A,
        Stop B, result B; the hook that reads an insertion comes after Stop. We keep no exact badges there; the core
        does not crash and no 👀 is left hanging."""
        a = self.say("one")
        self.ev("turn.started", "a", marker=a)
        self.ev("turn.stopped", "a")
        b = self.say("two")
        self.ev("turn.started", "a", marker=b)
        self.assertIsNone(self.ev("turn.result", "a", text="Did one.", duration_ms=5000))
        self.ev("turn.stopped", "a")
        self.assertIsNone(self.ev("turn.result", "a", text="Did two.", duration_ms=1000))
        self.ev("turn.started", "a", marker=None)
        c = self.say("three", now=True)
        self.ev("turn.stopped", "a")
        self.assertIsNone(self.ev("turn.started", "a", marker=c))
        self.ev("turn.stopped", "a")
        self.assertIsNone(self.ev("turn.result", "a", text="Did three."))
        self.assertNotIn("👀", [self.badge(m) for m in (a, b, c)])

    # --- insertion mid-turn: the badge comes from the turn that read it  ---------------------------------------------
    def test_an_insert_is_seen_when_the_turn_reads_it(self):
        """A watcher or --now inserted an owner message mid-turn: until Claude Code reads it — no badge; the turn
        with its marker (mid-turn) — 👀, the result of that turn — 👍."""
        self.ev("turn.started", "a", marker=None)
        mid = self.say("and one more thing", now=True)
        self.assertTrue(self.st["messages"][mid].get("injected"))
        self.assertIsNone(self.badge(mid))
        self.ev("turn.started", "a", marker=mid)
        self.assertEqual(self.badge(mid), "👀")
        self.ev("turn.stopped", "a")
        self.result()
        self.assertEqual(self.badge(mid), "👍")

    def test_an_insert_the_turn_did_not_read_is_answered_by_the_turn_that_read_it(self):
        """Inserted when the turn was already ending: that turn's result is not about it; the turn with its marker is
        new and it decides."""
        self.ev("turn.started", "a", marker=None)
        mid = self.say("and one more thing", now=True)
        self.ev("turn.stopped", "a")
        self.result("Did its own.")
        self.assertIsNone(self.badge(mid))
        self.ev("turn.started", "a", marker=mid)
        self.assertEqual(self.badge(mid), "👀")
        self.assertEqual(self.st["sessions"]["a"]["taken"], self.seq)   # started by its word — a fold
        self.result("Interrupted", is_error=True)
        self.assertIsNone(self.badge(mid))

    def test_an_insert_read_twice_is_decided_once(self):
        """The marker of an already-settled insertion in the next turn (also) is not a new take: 👍 stays."""
        self.ev("turn.started", "a", marker=None)
        mid = self.say("and one more thing", now=True)
        self.ev("turn.started", "a", marker=mid)
        self.ev("turn.stopped", "a")
        self.result()
        self.ev("turn.started", "a", marker=None, also=[mid])
        self.assertEqual(self.badge(mid), "👍")
        self.result("Interrupted", is_error=True)
        self.assertEqual(self.badge(mid), "👍")

    def test_an_insert_does_not_fold_the_turn_it_came_into(self):
        """A fold happens before the turn that took the owner's next word; an insertion mid-turn (and an answer to a
        form) is the same turn: its answers stay open. An insertion taken by a separate turn is already a new turn."""
        first = self.say()
        self.ev("turn.started", "a", marker=first)
        taken = self.seq
        mid = self.say("and one more thing", now=True)
        self.assertEqual(self.st["sessions"]["a"]["taken"], taken)
        self.ev("turn.started", "a", marker=mid)
        self.assertEqual(self.st["sessions"]["a"]["taken"], taken)
        self.ev("turn.stopped", "a")
        self.result()
        self.ev("turn.started", "a", marker=None)
        late = self.say("and more", now=True)
        self.ev("turn.stopped", "a")
        self.result()
        self.assertEqual(self.st["sessions"]["a"]["taken"], taken)
        self.ev("turn.started", "a", marker=late)
        self.assertEqual(self.st["sessions"]["a"]["taken"], self.seq)
        self.ev("turn.stopped", "a")
        self.result()
        self.ev("turn.started", "a", marker=None)
        own = self.say("and the last one", now=True)
        self.ev("message.reacted", None, target=own, emoji="❤️", **OWNER)   # has its own badge — not managed by the core
        self.ev("turn.stopped", "a")
        self.result()
        self.ev("turn.started", "a", marker=own)
        self.assertEqual(self.st["sessions"]["a"]["taken"], self.seq)

    def test_only_a_turn_started_by_the_owners_word_folds(self):
        """A fold is only a turn started by the owner's word: a turn from another session's message or a core word —
        not."""
        self.add("b")
        mid = self.say()
        self.turn(mid)
        taken = self.st["sessions"]["a"]["taken"]
        self.assertIsNone(self.ev("message.received", None, to="a", text="take a look", channel="session", sender="b"))
        self.turn(f"m{self.seq}")
        self.ev("turn.started", "a", marker=None)   # woke up on its own: the background ended
        self.ev("turn.stopped", "a")
        self.result()
        self.assertEqual(self.st["sessions"]["a"]["taken"], taken)

    def test_an_answer_to_a_form_is_taken_and_answered(self):
        """The owner's word that became an answer to an open form is also taken by a turn: 👀, 👍 on its result; the
        turn is the same, so it does not fold his answers."""
        self.ev("turn.started", "a", marker=None)
        self.form()
        answer = self.say("One")
        self.assertEqual(self.st["messages"][answer].get("answered_form"), "r1")
        self.assertEqual(self.badge(answer), "👀")
        self.assertIsNone(self.st["sessions"]["a"].get("taken"))
        self.ev("turn.stopped", "a")
        self.result("Did the first.")
        self.assertEqual(self.badge(answer), "👍")

    def test_an_answer_to_a_form_does_not_fold_in_a_session_from_an_old_state(self):
        """State from before the update: the start of the running turn is not recorded — an answer to a form still
        does not fold (a fold is set only by a turn started by the owner's word, not by a coincidence of time)."""
        self.ev("turn.started", "a", marker=None)
        self.form()
        for k in ("turn_began", "turn_asks"):
            self.st["sessions"]["a"].pop(k, None)
        answer = self.say("One")
        self.assertEqual(self.st["messages"][answer].get("answered_form"), "r1")
        self.assertIsNone(self.st["sessions"]["a"].get("taken"))

    # --- aborted turn: process, limit, close  ------------------------------------------------------------------------
    def test_a_cut_turn_is_answered_by_the_continuation(self):
        """The process died mid-turn: the turn is aborted — no badge; after the respawn the core says to continue, the
        continuation turn is 👀 again and its result decides."""
        mid = self.say()
        self.ev("turn.started", "a", marker=mid)
        self.ev("session.exited", "a")
        self.assertIsNone(self.badge(mid))
        self.ev("session.started", "a")
        go = f"m{self.seq}x3"
        self.assertIn(go, self.st["messages"])
        self.ev("turn.started", "a", marker=go)
        self.assertEqual(self.badge(mid), "👀")
        self.ev("turn.stopped", "a")
        self.result("Continued and finished.")
        self.assertEqual(self.badge(mid), "👍")

    def test_a_limit_without_a_continuation_leaves_no_mark(self):
        """An account limit cut the turn and there was no continuation (no other account): no badge, like a failure;
        the next turn's result is not about it."""
        mid = self.say()
        self.ev("turn.started", "a", marker=mid)
        self.result("You've hit your weekly limit · resets 5pm", is_error=True)
        self.assertIsNone(self.badge(mid))
        self.ev("turn.stopped", "a")
        nxt = self.say("more")
        self.turn(nxt)
        self.assertEqual((self.badge(mid), self.badge(nxt)), (None, "👍"))

    def test_the_continuation_after_a_limit_decides(self):
        """Limit, the core respawned it on another account and told it to continue: the continuation turn is 👀 again
        and decides."""
        mid = self.say()
        self.ev("turn.started", "a", marker=mid)
        self.result("You've hit your weekly limit · resets 5pm", is_error=True)
        self.ev("turn.stopped", "a")
        go = self.resume()
        self.assertIn(go, self.st["messages"])
        self.ev("turn.started", "a", marker=go)
        self.assertEqual(self.badge(mid), "👀")
        self.ev("turn.stopped", "a")
        self.result("Continued and finished.")
        self.assertEqual(self.badge(mid), "👍")

    def test_an_owner_message_queued_before_the_continuation_does_not_drop_the_cut_turn(self):
        """Review: the process died mid-turn and the owner's word waited in the queue ahead of the continuation — his
        turn goes first; the continuation turn brings back 👀 of the aborted turn, and it decides."""
        a = self.say("one")
        self.ev("turn.started", "a", marker=a)
        b = self.say("two")
        self.assertEqual(self.st["messages"][b]["state"], "queued")
        self.ev("session.exited", "a")
        self.ev("session.started", "a")
        go = f"m{self.seq}x3"
        self.assertIn(go, self.st["messages"])
        self.ev("turn.started", "a", marker=b)
        self.assertEqual((self.badge(a), self.badge(b)), (None, "👀"))
        self.ev("turn.stopped", "a")
        self.result("Finished two.")
        self.assertEqual((self.badge(a), self.badge(b)), (None, "👍"))
        self.ev("turn.started", "a", marker=go)
        self.ev("turn.stopped", "a")
        self.result("Finished one.")
        self.assertEqual(self.badge(a), "👍")

    def test_a_continuation_read_in_the_middle_of_a_turn_still_decides(self):
        """Second review: the continuation was inserted mid-turn by the owner's word (watcher) — taking it brings back
        👀 of the aborted turn, the result of that turn decides."""
        a = self.say("one")
        self.ev("turn.started", "a", marker=a)
        b = self.say("two")
        self.ev("session.exited", "a")
        self.ev("session.started", "a")
        go = f"m{self.seq}x3"
        self.ev("turn.started", "a", marker=b)
        self.assertIsNone(self.ev("message.inject", go, **OWNER))
        self.ev("turn.started", "a", marker=go)
        self.assertEqual((self.badge(a), self.badge(b)), ("👀", "👀"))
        self.ev("turn.stopped", "a")
        self.result("Did both.")
        self.assertEqual((self.badge(a), self.badge(b)), ("👍", "👍"))

    def test_a_continuation_that_never_runs_leaves_nothing_waiting(self):
        """Second review: the continuation was removed from the queue — the aborted turn stays without a badge,
        nothing hangs."""
        a = self.say("one")
        self.ev("turn.started", "a", marker=a)
        b = self.say("two")   # waits in the queue ahead of the continuation
        self.ev("session.exited", "a")
        self.ev("session.started", "a")
        go = f"m{self.seq}x3"
        self.assertEqual(self.st["sessions"]["a"].get("resume"), go)
        self.assertIsNone(self.ev("message.cancel", go, **OWNER))
        self.assertIsNone(self.badge(a))   # no 👀 hangs while it is busy with something else
        self.turn(b)
        self.assertEqual((self.badge(a), self.badge(b)), (None, "👍"))
        self.assertFalse(self.st["sessions"]["a"].get("eyes"))
        self.assertFalse(self.st["sessions"]["a"].get("cut_eyes"))

    def test_a_limit_with_the_same_account_drops_the_wait_on_the_next_turn(self):
        """Limit, respawned on the same account (no continuation) — the next turn without a continuation: the aborted
        one has no badge."""
        a = self.say("one")
        self.ev("turn.started", "a", marker=a)
        self.result("You've hit your weekly limit · resets 5pm", is_error=True)
        self.ev("turn.stopped", "a")
        self.ev("session.exited", "a")
        self.ev("session.started", "a")
        b = self.say("two")
        self.turn(b)
        self.assertFalse(self.st["sessions"]["a"].get("cut_eyes"))
        self.assertEqual((self.badge(a), self.badge(b)), (None, "👍"))

    def test_a_turn_released_midway_leaves_no_mark(self):
        """Review: an executor was stopped mid-turn (stop: interrupt and release) or the owner took it into the
        terminal -- there may be no result: clear 👀 at once."""
        self.st["sessions"]["a"]["role"] = "executor"   # an executor can be stopped at once
        for event in ("session.stop_requested", "session.take_requested"):
            mid = self.say()
            self.ev("turn.started", "a", marker=mid)
            self.ev(event, "a", **OWNER)
            self.assertIn(self.st["sessions"]["a"]["state"], ("asleep", "retired", "held"), event)
            self.assertIsNone(self.badge(mid), event)
            self.result("Interrupted", is_error=True)
            self.ev("session.wake_requested" if event == "session.stop_requested" else "session.give_requested", "a", **OWNER)
            self.ev("session.started", "a")

    def test_a_question_deferred_while_the_owner_is_away_is_open(self):
        """Review: the owner went "away" — the session's question is deferred until he returns, the turn went on: this
        turn's question is open -- no 👍."""
        mid = self.say()
        self.ev("turn.started", "a", marker=mid)
        self.ev("owner.away", None, **OWNER)   # stepped away while it works (his word would bring him back)
        self.ev("permission.requested", "a", request_id="r1", tool="AskUserQuestion", interactive=True,
                input={"questions": [{"question": "Which one?", "options": [{"label": "One"}, {"label": "Two"}]}]})
        self.assertTrue(self.st["sessions"]["a"]["asks"][0]["away"])   # the card waits for his return
        self.ev("turn.stopped", "a")
        self.result("Did the first, left the question.")
        self.assertIsNone(self.badge(mid))

    def test_a_question_left_open_stays_open_for_a_letter_read_without_a_stop_before_it(self):
        """The turn's question is deferred and open; the next message is read with no Stop between (the session is still
        busy): it is the same busy stretch, the open question is not forgotten, so no 👍 on the new word."""
        mid = self.say()
        self.ev("turn.started", "a", marker=mid)
        self.ev("owner.away", None, **OWNER)
        self.ev("permission.requested", "a", request_id="r1", tool="AskUserQuestion", interactive=True,
                input={"questions": [{"question": "Which one?", "options": [{"label": "One"}, {"label": "Two"}]}]})
        again = self.say("and now the second")
        self.ev("turn.started", "a", marker=again)
        self.ev("turn.stopped", "a")
        self.result("Did the second.")
        self.assertIsNone(self.badge(again))

    def test_a_limit_cut_is_not_continued_by_a_later_restart_for_background_work(self):
        """Third review: limit, respawned on the same account (no continuation); then a restart cut the background
        work -- its continuation is not about the message cut by the limit."""
        a = self.say("one")
        self.ev("turn.started", "a", marker=a)
        self.result("You've hit your weekly limit · resets 5pm", is_error=True)
        self.ev("turn.stopped", "a")
        self.ev("session.background", "a", tasks=[{"id": "t1", "kind": "bash", "title": "build"}])
        self.ev("session.exited", "a")
        self.ev("session.started", "a")
        go = f"m{self.seq}x3"
        self.assertIn(go, self.st["messages"])
        self.ev("turn.started", "a", marker=go)
        self.ev("turn.stopped", "a")
        self.result("Nothing to continue.")
        self.assertIsNone(self.badge(a))

    def test_a_continuation_moved_to_another_session_does_not_answer_for_the_cut_one(self):
        """Third review: the continuation went to another session (the queue moved) — its turn does not settle what
        was taken by the aborted turn of another session."""
        self.add("b")
        a = self.say("one")
        self.ev("turn.started", "a", marker=a)
        self.ev("session.exited", "a")
        self.ev("session.started", "a")
        go = f"m{self.seq}x3"
        self.st["messages"][go]["session"] = "b"   # the queue moved to another session
        self.ev("turn.started", "b", marker=go)
        self.ev("turn.stopped", "b")
        self.result("Did its own.", session="b")
        self.assertIsNone(self.badge(a))

    def test_a_badge_the_owner_set_is_not_taken_back_by_the_continuation(self):
        """Third review: the owner set his own badge while the message waited for the continuation — the continuation
        does not touch it, even if his reactions are pushed out by the limit."""
        a = self.say("one")
        self.ev("turn.started", "a", marker=a)
        self.ev("session.exited", "a")
        b = self.say("two")   # the continuation will stand behind it
        self.ev("session.started", "a")
        go = f"m{self.seq}x3"
        self.ev("message.reacted", None, target=a, emoji="❤️", **OWNER)
        self.st["owner_badges"].pop(a)   # pushed out by the OWN_KEEP limit
        self.turn(b)
        self.ev("turn.started", "a", marker=go)
        self.ev("turn.stopped", "a")
        self.result("Finished one.")
        self.assertNotIn(a, self.st.get("badges") or {})

    def test_two_cuts_in_a_row_keep_what_both_took(self):
        """Fourth review: the new process's turn started before its SessionStart and was also aborted — the
        continuation carries what both aborts took."""
        a = self.say("one")
        self.ev("turn.started", "a", marker=a)
        self.ev("session.exited", "a")
        b = self.say("two")
        self.ev("turn.started", "a", marker=b)   # from starting: the turn hook before SessionStart
        self.ev("session.exited", "a")
        self.ev("session.started", "a")
        go = f"m{self.seq}x3"
        self.ev("turn.started", "a", marker=go)
        self.ev("turn.stopped", "a")
        self.result("Finished both.")
        self.assertEqual((self.badge(a), self.badge(b)), ("👍", "👍"))

    def test_a_builtin_command_hitting_the_limit_keeps_the_cut_turn(self):
        """Fourth review: /compact hit the same limit (a result without a turn) — what the limit-aborted turn took is
        not lost."""
        a = self.say("one")
        self.ev("turn.started", "a", marker=a)
        self.result("You've hit your weekly limit · resets 5pm", is_error=True)
        self.ev("turn.stopped", "a")
        self.result("You've hit your weekly limit · resets 5pm", is_error=True, turns=0)
        go = self.resume()
        self.ev("turn.started", "a", marker=go)
        self.ev("turn.stopped", "a")
        self.result("Continued and finished.")
        self.assertEqual(self.badge(a), "👍")

    def test_a_new_conversation_before_the_continuation_forgets_the_cut_turn(self):
        """Sixth review: the conversation changed before the continuation turn (SessionStart with a new id — /clear):
        there is nothing to continue in an empty conversation — its answer is not a 👍 for the aborted one."""
        a = self.say("one")
        self.ev("turn.started", "a", marker=a)
        self.ev("session.exited", "a")
        self.ev("session.started", "a")
        go = f"m{self.seq}x3"
        self.ev("session.started", "a", session_id="s-a2")
        self.ev("turn.started", "a", marker=go)
        self.ev("turn.stopped", "a")
        self.result("I do not see what to continue.")
        self.assertIsNone(self.badge(a))

    def test_a_second_exit_with_background_work_keeps_the_cut_turn(self):
        """Fifth review: aborted turn; the new process started background work before SessionStart and also left (as
        free) -- the continuation is still about what the aborted turn took."""
        a = self.say("one")
        self.ev("turn.started", "a", marker=a)
        self.ev("session.exited", "a")
        self.ev("turn.started", "a", marker=None)   # from starting: the turn hook before SessionStart
        self.ev("session.background", "a", tasks=[{"id": "t1", "kind": "bash", "title": "build"}])
        self.ev("turn.stopped", "a")
        self.result("Started the build.")
        self.ev("session.exited", "a")
        self.ev("session.started", "a")
        go = f"m{self.seq}x3"
        self.ev("turn.started", "a", marker=go)
        self.ev("turn.stopped", "a")
        self.result("Finished one.")
        self.assertEqual(self.badge(a), "👍")

    def test_a_respawn_for_new_tools_keeps_the_cut_turn(self):
        """Sixth review: aborted turn; the new process started background work before SessionStart, then a restart for
        the sake of tools rewrote the abort record — the continuation is still about what the aborted turn took."""
        a = self.say("one")
        self.ev("turn.started", "a", marker=a)
        self.ev("session.exited", "a")
        self.ev("turn.started", "a", marker=None)   # from starting: the turn hook before SessionStart
        self.ev("session.background", "a", tasks=[{"id": "t1", "kind": "bash", "title": "build"}])
        self.ev("turn.stopped", "a")
        self.result("Started the build.")
        # a service by now (runs past restart.SERVICE_AFTER, silent): a relaunch for tools does not wait for it (11.11)
        self.st["sessions"]["a"]["background"]["t1"]["since"] = -10 ** 6
        self.st["sessions"]["a"]["dirty"] = True   # the tool set changed — restart of a free session
        self.ev("session.info", "a")
        self.assertTrue(self.st["sessions"]["a"]["cut_turn"].get("respawn"))
        self.ev("session.started", "a")
        go = f"m{self.seq}x3"
        self.ev("turn.started", "a", marker=go)
        self.ev("turn.stopped", "a")
        self.result("Finished one.")
        self.assertEqual(self.badge(a), "👍")

    def test_eyes_go_when_the_session_is_gone_for_good(self):
        """Died for good (respawns ran out) or closed — there will be no result: 👀 is cleared. Sleep and parking — no.
        """
        mid = self.say()
        self.ev("turn.started", "a", marker=mid)
        for _ in range(reducer.MAX_RESTARTS + 1):
            self.ev("session.exited", "a")
        self.assertEqual(self.st["sessions"]["a"]["state"], "failed")
        self.assertIsNone(self.badge(mid))
        self.assertFalse(self.st["sessions"]["a"].get("eyes"))

    def test_an_unread_insert_of_a_cut_turn_gets_no_mark(self):
        """The insertion was read by neither a turn nor the continuation after a respawn — no badge (neither 👀 nor
        someone else's 👍)."""
        self.ev("turn.started", "a", marker=None)
        mid = self.say("and one more thing", now=True)
        self.ev("session.exited", "a")
        self.ev("session.started", "a")
        self.ev("turn.started", "a", marker=f"m{self.seq}x3")
        self.ev("turn.stopped", "a")
        self.result("Continued and finished.")
        self.assertIsNone(self.badge(mid))

    # --- owner's badge  --------------------------------------------------------------------------------------------
    def test_the_owner_removed_the_eyes_and_the_turn_does_not_put_them_back(self):
        """A badge cleared by the owner is an empty string among his badges (the page uses it to show "Reaction" on
        his own message); the core does not bring it back."""
        mid = self.say()
        self.ev("turn.started", "a", marker=mid)
        self.assertEqual(self.badge(mid), "👀")
        self.ev("message.reacted", None, target=mid, emoji="", **OWNER)
        self.assertEqual(self.badge(mid), "")
        self.ev("turn.started", "a", marker=None, also=[mid])
        self.ev("turn.result", "a", text="Done.")
        self.assertEqual(self.badge(mid), "")

    def test_only_a_badge_the_owner_removed_is_marked_removed(self):
        """No badge by the core's decision (failure, question) and not yet taken — no key: there is no "Reaction" on
        his own message there."""
        failed = self.say()
        self.ev("turn.started", "a", marker=failed)
        self.result("Interrupted", is_error=True)
        waiting = self.say("more")
        merged = {**(self.st.get("badges") or {}), **(self.st.get("owner_badges") or {})}   # as in the snapshot
        self.assertNotIn(failed, merged)
        self.assertNotIn(waiting, merged)
        self.ev("message.reacted", None, target=failed, emoji="", **OWNER)
        self.assertEqual(self.badge(failed), "")

    def test_owner_changes_the_reaction_and_it_is_kept(self):
        mid = self.say()
        self.ev("turn.started", "a", marker=mid)
        self.ev("message.reacted", None, target=mid, emoji="❤️", **OWNER)
        self.assertEqual(self.badge(mid), "❤️")
        self.ev("turn.result", "a", text="Done.")
        self.assertEqual(self.badge(mid), "❤️")   # the owner set his own — the core does not overwrite it

    def test_a_session_cannot_react_for_the_owner(self):
        mid = self.say()
        self.assertIsNotNone(self.ev("message.reacted", None, target=mid, emoji="👍", session="a"))
        self.assertIsNone(self.badge(mid))
        self.ev("session.said", "a", text="Done.")
        self.assertIsNotNone(self.ev("message.reacted", None, target=str(self.seq), emoji="👍", session="a"))

    # --- reaction to a session row: a note appended to the input  -------------------------------------------------
    def react(self, emoji="👍", text="Done.", session="a"):
        self.ev("session.said", session, text=text)
        line = str(self.seq)
        self.assertIsNone(self.ev("message.reacted", None, target=line, emoji=emoji, session=session, quote=text, **OWNER))
        return line

    NOTE = "Before this the owner reacted to your messages"

    def test_a_reaction_to_a_session_line_does_not_wake_it_and_goes_with_the_next_message(self):
        line = self.react()
        self.assertEqual(self.badge(line), "👍")
        before = len(self.st["messages"])
        self.assertEqual(len(self.st["messages"]), before)   # there is no separate turn message
        mid = self.say("next")
        self.assertIn(self.NOTE + ": 👍 to «Done.»", self.typed[mid])
        self.assertTrue(self.typed[mid].startswith(reducer.MARKER.format(id=mid, origin=self.st["messages"][mid]["origin"])))
        self.assertNotIn("reacted to your", self.st["messages"][mid]["origin"])   # not in the label
        self.turn(mid)
        later = self.say("and more")
        self.assertNotIn("reacted to your", self.typed[later])   # once

    def test_a_reaction_changed_while_the_message_waits_goes_as_changed(self):
        """The message waits in the queue and the owner changed the reaction: the session gets the one in place when
        the message goes out."""
        self.ev("turn.started", "a", marker=None)
        line = self.react("👍")
        mid = self.say("next")
        self.ev("message.reacted", None, target=line, emoji="❤️", session="a", quote="Done.", **OWNER)
        self.ev("turn.stopped", "a")
        self.assertIn(self.NOTE + ": ❤️ to «Done.»", self.typed[mid])
        self.assertNotIn("👍", self.typed[mid])

    def test_a_reaction_removed_while_the_message_waits_is_not_sent(self):
        self.ev("turn.started", "a", marker=None)
        line = self.react("👍")
        mid = self.say("next")
        self.ev("message.reacted", None, target=line, emoji="", session="a", **OWNER)
        self.ev("turn.stopped", "a")
        self.assertIn(mid, self.typed)
        self.assertNotIn("reacted to your", self.typed[mid])
        self.assertIsNone(self.badge(line))

    def test_turning_the_module_off_while_the_message_waits_drops_the_reaction(self):
        self.ev("turn.started", "a", marker=None)
        self.react()
        mid = self.say("next")
        self.ev("settings.changed", None, what="reactions.enabled", old=True, new=False, **OWNER)
        self.ev("turn.stopped", "a")
        self.assertIn(mid, self.typed)
        self.assertNotIn("reacted to your", self.typed[mid])

    def test_turning_the_module_off_drops_the_badge_of_an_unsent_reaction(self):
        """Review: the module was switched off — a waiting reaction to a row will not go out, and its badge in the
        feed is cleared (otherwise after switching on a reaction the session never received would be visible)."""
        line = self.react()
        self.ev("settings.changed", None, what="reactions.enabled", old=True, new=False, **OWNER)
        self.assertIsNone(self.badge(line))

    def test_the_owner_changes_the_badge_of_a_pruned_message(self):
        """Review: core.pruned removed a message older than a day from state but its badge stayed in the feed — the
        owner changes it."""
        mid = self.say("here")
        self.turn(mid)
        self.ev("core.pruned", None, before=10_000)
        self.assertNotIn(mid, self.st["messages"])
        self.assertIsNone(self.ev("message.reacted", None, target=mid, emoji="", **OWNER))
        self.assertEqual(self.badge(mid), "")
        self.assertIsNotNone(self.ev("message.reacted", None, target="m999999", emoji="👍", **OWNER))

    def test_an_answer_to_a_form_does_not_eat_the_reaction(self):
        """The owner's word that became an answer to an open form does not go in as input — the reaction waits for the
        next one."""
        self.react()
        self.ev("turn.started", "a", marker=None)
        self.ev("permission.requested", "a", request_id="r1", tool="AskUserQuestion",
                input={"questions": [{"question": "Which one?", "options": [{"label": "One"}, {"label": "Two"}]}]})
        answer = self.say("One")
        self.assertNotIn(answer, self.typed)
        self.ev("turn.stopped", "a")
        mid = self.say("next")
        self.assertIn(self.NOTE + ": 👍", self.typed[mid])

    def test_a_builtin_or_skill_command_does_not_carry_the_reaction(self):
        """/compact is entered without a label; for a /command the end of the input is its arguments: the reaction
        waits for an ordinary message."""
        self.react()
        self.st["sessions"]["a"]["skills"] = ["ship"]
        for text in ("/compact", "/ship v2"):
            cmd = self.say(text)
            self.assertIn(cmd, self.typed, text)
            self.assertNotIn("reacted to your", self.typed[cmd], text)
            self.ev("turn.result", "a", text="", turns=0)
            self.ev("turn.stopped", "a")
        mid = self.say("next")
        self.assertIn(self.NOTE + ": 👍", self.typed[mid])

    def test_a_removed_message_does_not_eat_the_reaction(self):
        self.react()
        self.ev("turn.started", "a", marker=None)   # busy — the message waits in the queue
        first = self.say("next")
        self.ev("message.cancel", first, **OWNER)
        self.ev("turn.stopped", "a")
        self.assertNotIn(first, self.typed)
        mid = self.say("next, differently")
        self.assertIn(self.NOTE + ": 👍", self.typed[mid])

    def test_a_renamed_session_gets_its_reaction_and_a_new_one_with_the_old_name_does_not(self):
        self.react()
        self.assertIsNone(self.ev("session.renamed", "a", to="b", **OWNER))
        if self.st["sessions"]["b"]["state"] != "idle":   # the renamed one is brought up again
            self.ev("session.started", "b")
        self.add("a")   # another session took the freed name
        fresh = self.say("hi", to="a")
        self.assertIn(fresh, self.typed)
        self.assertNotIn("reacted to your", self.typed[fresh])
        mid = self.say("next", to="b")
        self.assertIn(self.NOTE + ": 👍", self.typed[mid])

    def test_a_redirected_message_carries_only_the_receivers_reactions(self):
        """An executor was stopped — the message from its queue goes to the one who brought it up: no reaction to the
        executor's row."""
        self.add("boss")
        self.ev("session.requested", "ex", cwd="/w", session_id="s-ex", executor=True, raised_by="boss", **OWNER)
        self.ev("session.started", "ex")
        self.react("👀", text="I can delete the old branches. Delete?", session="ex")
        self.ev("turn.started", "ex", marker=None)   # busy: the owner's next word waits in the queue
        mid = self.say("what about the tests?", to="ex")
        self.ev("session.stop_requested", "ex", **OWNER)
        self.assertEqual(self.st["messages"][mid]["session"], "boss")
        self.assertIn(mid, self.typed)
        self.assertNotIn("reacted to your", self.typed[mid])

    def test_a_message_to_another_session_carries_its_own_reactions(self):
        self.add("b")
        self.react("👍", text="Done at a.")
        self.react("❤️", text="Done at b.", session="b")
        mid = self.say("next", to="b")
        self.assertIn("❤️ to «Done at b.»", self.typed[mid])
        self.assertNotIn("Done at a", self.typed[mid])

    def test_every_waiting_reaction_goes(self):
        """More than three reactions — all go out, in the order they were set (a changed one — by the time of change).
        """
        lines = [self.react(e, text=f"Line {i}.") for i, e in enumerate(["👍", "✅", "👀", "❤️", "🙏"])]
        self.ev("message.reacted", None, target=lines[0], emoji="✅", session="a", quote="Line 0.", **OWNER)
        mid = self.say("next")
        note = self.typed[mid]
        for i in range(1, 5):
            self.assertIn(f"«Line {i}.»", note)
        self.assertLess(note.index("«Line 4.»"), note.index("✅ to «Line 0.»"))

    def test_the_quote_cannot_break_the_marker(self):
        """"]" in a quote broke the label (it is read up to "] "); the length is as for a reply quote (_quote)."""
        self.react(text="Plan:\n- [x] tests\n- [ ] release " + "words " * 60)
        mid = self.say("next")
        typed = self.typed[mid]
        note = typed[typed.index(self.NOTE):typed.index(".)", typed.index(self.NOTE))]
        self.assertNotIn("]", note)
        self.assertLessEqual(len(note), len(self.NOTE) + reducer.QUOTE_MAX + 20)

    def test_waiting_reactions_and_badges_survive_pruning(self):
        """core.pruned cleans old messages — a waiting reaction and the feed row badges remain."""
        line = self.react()
        mid = self.say("here")
        self.turn(mid)
        self.ev("turn.started", "a", marker=None)   # busy: the next one will wait
        self.ev("core.pruned", None, before=10_000)
        self.assertEqual((self.badge(line), self.badge(mid)), ("👍", "👍"))
        later = self.react("❤️", text="One more line.")
        self.ev("core.pruned", None, before=20_000)
        self.ev("turn.stopped", "a")
        nxt = self.say("next")
        self.assertIn("❤️ to «One more line.»", self.typed[nxt])
        self.assertEqual(self.badge(later), "❤️")

    def test_the_owners_reactions_are_not_pushed_out_by_the_fleets_badges(self):
        """Core badges are the latest BADGES_KEEP; the owner's reactions (to rows and to his own messages) are kept
        separately: the fleet's stream of turns does not push them out."""
        line = self.react("❤️")
        own = self.say("my own")
        self.ev("message.reacted", None, target=own, emoji="🙏", **OWNER)
        fleet = [f"s{i}" for i in range(20)]
        for name in fleet:
            self.add(name)
        for i in range(reducer.BADGES_KEEP // len(fleet) + 1):
            for name in fleet:
                self.turn(self.say(f"more {i}", to=name), session=name)
        self.assertEqual((self.badge(line), self.badge(own)), ("❤️", "🙏"))
        self.assertLessEqual(len(self.st["badges"]), reducer.BADGES_KEEP)

    def test_answered_messages_keep_the_thumb_past_the_open_ones_limit(self):
        """A session waits for at most EYES_KEEP taken results; already answered ones are not cleared by this limit."""
        mids = []
        for i in range(reducer.EYES_KEEP + 5):
            mids.append(self.say(f"one {i}"))
            self.turn(mids[-1])
        self.assertEqual({self.badge(m) for m in mids}, {"👍"})

    def test_the_snapshot_shows_the_owners_reactions_with_the_badges(self):
        r = views.Reader()
        for n, (type, entity, data) in enumerate((
                ("session.requested", "a", {"cwd": "/w", "session_id": "s-a", **OWNER}), ("session.started", "a", {}),
                ("session.said", "a", {"text": "Done."}),
                ("message.reacted", None, {"target": "3", "emoji": "❤️", "session": "a", "quote": "Done.", **OWNER})), 1):
            r.feed({"seq": n, "ts": 100 + n, "type": type, "entity": entity, "data": data})
        self.assertEqual(r.snapshot()["reactions"]["badges"], {"3": "❤️"})

    # --- reaction to a selected fragment (2026-10-21)  ------------------------------------------------------------
    def test_a_reaction_to_a_fragment_keeps_the_fragment_for_the_badge_and_the_session(self):
        """Selected a piece of the reply and reacted — the badge is the same, on the row, but with a fragment: the
        session gets "👍 on "fragment"", the page shows it as a hint on the badge (reactions.quotes). A reaction to the
        whole message carries no fragment."""
        self.ev("session.said", "a", text="First. Second important. Third.")
        line = str(self.seq)
        self.assertIsNone(self.ev("message.reacted", None, target=line, emoji="👍", session="a", quote="Second important",
                                  part=True, **OWNER))
        self.assertEqual(self.badge(line), "👍")
        self.assertEqual(self.st["owner_quotes"], {line: "Second important"})
        mid = self.say("next")
        self.assertIn(self.NOTE + ": 👍 to «Second important»", self.typed[mid])
        # switched to a reaction on the whole message — the fragment on the badge is cleared; cleared the reaction —
        # the same
        self.ev("message.reacted", None, target=line, emoji="❤️", session="a", quote="First. Second important. Third.",
                **OWNER)
        self.assertFalse(self.st.get("owner_quotes"))
        self.ev("message.reacted", None, target=line, emoji="👍", session="a", quote="Second important", part=True, **OWNER)
        self.ev("message.reacted", None, target=line, emoji="", session="a", **OWNER)
        self.assertFalse(self.st.get("owner_quotes"))

    def test_fragments_are_kept_for_the_last_reactions_only(self):
        for i in range(reducer.QUOTES_KEEP + 3):
            self.ev("session.said", "a", text=f"line {i}")
            self.ev("message.reacted", None, target=str(self.seq), emoji="👍", session="a", quote=f"piece {i}",
                    part=True, **OWNER)
        self.assertEqual(len(self.st["owner_quotes"]), reducer.QUOTES_KEEP)
        self.assertEqual(list(self.st["owner_quotes"].values())[-1], f"piece {reducer.QUOTES_KEEP + 2}")

    def test_the_snapshot_carries_the_fragment_only_when_there_is_one(self):
        r = views.Reader()
        evs = [("session.requested", "a", {"cwd": "/w", "session_id": "s-a", **OWNER}), ("session.started", "a", {}),
               ("session.said", "a", {"text": "Done. The rest."}),
               ("message.reacted", None, {"target": "3", "emoji": "❤️", "session": "a", "quote": "Done. The rest.",
                                          **OWNER})]
        for n, (type, entity, data) in enumerate(evs, 1):
            r.feed({"seq": n, "ts": 100 + n, "type": type, "entity": entity, "data": data})
        self.assertNotIn("quotes", r.snapshot()["reactions"])   # earlier snapshot shape while there are no fragments
        r.feed({"seq": 5, "ts": 105, "type": "message.reacted", "entity": None,
                "data": {"target": "3", "emoji": "❤️", "session": "a", "quote": "The rest", "part": True, **OWNER}})
        self.assertEqual(r.snapshot()["reactions"]["quotes"], {"3": "The rest"})
        st = r.state   # badge pushed out by the OWN_KEEP limit, the fragment is still remembered — not in the snapshot
        st["owner_badges"].pop("3")
        self.assertNotIn("quotes", r.snapshot()["reactions"])


if __name__ == "__main__":
    unittest.main()
