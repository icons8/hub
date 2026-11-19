"""A direction's board follows the facts (dir-sync spec, owner decision 2026-10-26: the core keeps the facts, marking a
card Done is the lead's call): the link between a delegation and a card (--board, TASK-N in the first line of the brief,
an executor editing the card), the layer of facts built on it (working, handed in but not sorted, no executor, no card)
and a line to the lead when an executor hands in. The core never sets Done."""
import tests  # noqa: F401 — keep the machine's settings out of the test
import unittest

from core import board, executors, i18n, reducer

OWNER = {"channel": "cli", "owner": True}


def by(name):
    return {"channel": "session", "owner": False, "sender": name}


class Case(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "maps", cwd="/m", session_id="s-maps", **OWNER)
        self.ev("session.started", "maps")
        self.assertIsNone(self.ev("task.backlog", None, slug="maps", **by("maps")))

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.st, _acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 1000 + self.seq, "type": type,
                                                       "entity": entity, "data": data})
        return why

    def raise_one(self, name, text="fix the tiles", card=None, how="flag"):
        extra = {"board": {"slug": "maps", "card": card, "how": how}} if card else {}
        self.assertIsNone(self.ev("task.run_requested", name, task=text, **by("maps"), **extra))
        run = self.st["runs"][name]
        for type, entity, data in executors.events_for(self.st, name, run, cwd="/m/w", worktree=None):
            self.assertIsNone(self.ev(type, entity, **data), (type, data))
        self.ev("session.started", name)
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == name]
        return d

    def hand_in(self, name, ts, text="Done, MR !12"):
        d = [d for d in self.st["delegations"].values() if d["session"] == name][-1]
        self.ev("turn.started", name, marker=d["message"], prompt_head="task", ts=ts - 10)
        self.ev("turn.result", name, text=text, ts=ts - 5)
        self.ev("turn.stopped", name, ts=ts - 5)
        self.ev("session.close_requested", name, retire=True, ts=ts)

    def edit(self, who, card, *fields, ts=None):
        data = OWNER if who == "owner" else by(who)
        return self.ev("direction.card", None, ts=ts, slug="maps", card=card, fields=list(fields), **data)

    def facts(self, cards, now=5000):
        return board.facts(self.st, "maps", cards, now)


class Board(Case):
    def test_a_journal_without_cards_folds_as_before(self):
        # old journals: no board field and no direction.card facts — the state has no new keys
        self.raise_one("maps-a")
        self.hand_in("maps-a", 3000)
        self.assertNotIn("boards", self.st)
        self.assertTrue(all("board" not in d for d in self.st["delegations"].values()))

    def test_explicit_link_shows_working_then_unsorted_until_the_lead_judges(self):
        d = self.raise_one("maps-a", card="TASK-9")
        self.assertEqual(d["board"], {"slug": "maps", "card": "TASK-9", "how": "flag"})
        cards = [{"id": "TASK-9", "status": "In Progress"}]
        f = self.facts(cards)["cards"]["TASK-9"]
        self.assertEqual((f["kind"], f["who"]), ("working", ["maps-a"]))
        self.hand_in("maps-a", 3000)
        f = self.facts(cards)["cards"]["TASK-9"]
        self.assertEqual((f["kind"], f["since"], f["who"]), ("unsorted", 2995, ["maps-a"]))   # "handed in since" is the time of its result
        self.assertIsNone(self.edit("maps-b", "TASK-9", "append-notes", ts=3100))   # someone else's edit does not clear it
        self.assertIsNone(self.edit("maps", "TASK-9", "title", ts=3200))            # neither does the lead's title edit
        self.assertEqual(self.facts(cards)["cards"]["TASK-9"]["kind"], "unsorted")
        self.assertIsNone(self.edit("maps", "TASK-9", "append-notes", ts=3300))     # the lead's note after the handover clears it
        self.assertEqual(self.facts(cards)["cards"]["TASK-9"]["kind"], "idle")
        self.assertEqual(self.facts(cards)["cards"]["TASK-9"]["last"], {"ts": 3300, "by": "maps"})
        self.assertNotIn("TASK-9", self.facts([{"id": "TASK-9", "status": "Done"}])["cards"])   # Done has no marks

    def test_the_owner_judges_too_and_a_judgement_before_the_handover_does_not_count(self):
        self.raise_one("maps-a", card="TASK-9")
        self.edit("maps", "TASK-9", "status", ts=2000)                                   # before the handover
        self.hand_in("maps-a", 3000)
        cards = [{"id": "TASK-9", "status": "In Progress"}]
        self.assertEqual(self.facts(cards)["cards"]["TASK-9"]["kind"], "unsorted")
        self.edit("owner", "TASK-9", "status", ts=3100)
        self.assertEqual(self.st["boards"]["maps"]["cards"]["TASK-9"], {"ts": 3100, "seq": self.seq, "by": None,
                                                                        "owner": True, "judged": 3100})
        self.assertEqual(self.facts(cards)["cards"]["TASK-9"]["kind"], "idle")

    def test_two_executors_one_handed_in_one_still_working(self):
        self.raise_one("maps-a", card="TASK-9")
        self.raise_one("maps-b", text="TASK-9: tile tests", card="TASK-9", how="brief")
        self.hand_in("maps-a", 3000)
        f = self.facts([{"id": "TASK-9", "status": "In Progress"}])["cards"]["TASK-9"]
        self.assertEqual((f["kind"], f["who"], f["handed"], f["total"]), ("working", ["maps-b"], 1, 2))

    def test_an_executor_editing_more_than_three_cards_is_a_checker_not_linked(self):
        self.raise_one("board-sync")
        for n in range(1, 6):
            self.assertIsNone(self.edit("board-sync", f"TASK-{n}", "status"))
        self.assertEqual(len(self.st["boards"]["maps"]["touched"]["board-sync"][1]), board.EDIT_LINK_MAX + 1)
        self.assertEqual(board.links(self.st, "board-sync"), [])
        cards = [{"id": f"TASK-{n}", "status": "In Progress"} for n in range(1, 6)]
        self.assertTrue(all(f["kind"] == "idle" for f in self.facts(cards)["cards"].values()))
        self.assertEqual([u["name"] for u in self.facts(cards)["unlinked"]], ["board-sync"])

    def test_a_link_by_edit_after_the_start_shows_on_the_board(self):
        self.raise_one("maps-a")
        cards = [{"id": "TASK-4", "status": "To Do"}]
        self.assertEqual([u["name"] for u in self.facts(cards)["unlinked"]], ["maps-a"])
        self.edit("maps-a", "TASK-4", "append-notes")
        self.assertEqual(board.links(self.st, "maps-a"), [("maps", "TASK-4", "edit")])
        out = self.facts(cards)
        self.assertEqual((out["cards"]["TASK-4"]["kind"], out["unlinked"]), ("working", []))
        self.hand_in("maps-a", 3000)
        self.assertEqual(self.facts(cards)["cards"]["TASK-4"]["kind"], "unsorted")   # To Do too: the work is handed in

    def test_a_file_changed_after_the_handover_without_a_fact_is_not_silent(self):
        self.raise_one("maps-a", card="TASK-9")
        self.hand_in("maps-a", 3000)
        f = self.facts([{"id": "TASK-9", "status": "In Progress", "mtime": 3500}])["cards"]["TASK-9"]
        self.assertEqual((f["kind"], f["since"]), ("unknown", 2995))
        self.edit("maps", "TASK-9", "title", ts=3502)    # an edit with a fact later than the file means it is not "unknown who"
        f = self.facts([{"id": "TASK-9", "status": "In Progress", "mtime": 3500}])["cards"]["TASK-9"]
        self.assertEqual(f["kind"], "unsorted")

    def test_an_executor_removed_by_the_term_after_the_handover_still_handed_in(self):
        # a hub with executors.remove_after_hours: a handed-in executor is removed from the list — the card does not lose "handed in"
        self.raise_one("maps-a", card="TASK-9")
        self.hand_in("maps-a", 3000)
        self.ev("session.exited", "maps-a", why="sleep", ts=3001)
        self.assertIsNone(self.ev("session.remove_requested", "maps-a", retired=True, ts=4000))
        self.assertEqual(self.st["sessions"]["maps-a"]["state"], "removed")
        f = self.facts([{"id": "TASK-9", "status": "In Progress"}])["cards"]["TASK-9"]
        self.assertEqual((f["kind"], f["since"]), ("unsorted", 2995))

    def test_an_archived_task_of_a_removed_executor_is_still_a_handover(self):
        # the delegation archive (core.archived): a removed executor's handed-in task has state archived and a clipped reply
        self.raise_one("maps-a", card="TASK-9")
        self.hand_in("maps-a", 3000)
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "maps-a"]
        self.assertTrue(d["reply"])
        d["state"] = "archived"
        f = self.facts([{"id": "TASK-9", "status": "In Progress"}])["cards"]["TASK-9"]
        self.assertEqual(f["kind"], "unsorted")
        d["reply"] = None   # cut short, not handed in
        self.assertEqual(self.facts([{"id": "TASK-9", "status": "In Progress"}])["cards"]["TASK-9"]["kind"], "idle")

    def test_a_stopped_executor_is_not_a_handover(self):
        self.raise_one("maps-a", card="TASK-9")
        self.ev("session.stop_requested", "maps-a", **by("maps"))
        f = self.facts([{"id": "TASK-9", "status": "In Progress"}])["cards"]["TASK-9"]
        self.assertEqual(f["kind"], "idle")

    def turn(self, name, start, end=None):
        """A turn of session name: started at ts start; end - its result (a running turn has none)."""
        self.ev("turn.started", name, marker=None, prompt_head="go", ts=start)
        if end:
            self.ev("turn.result", name, text="ok", ts=end)
            self.ev("turn.stopped", name, ts=end)

    def agent(self, name):
        self.ev("session.requested", name, cwd=f"/{name}", session_id=f"s-{name}", **OWNER)
        self.ev("session.started", name)

    def test_nobody_works_on_an_in_progress_card_the_lead_edited_between_turns(self):
        # the lead's last edit alone says nothing about work: its turn is over, no executor - the honest "no executor"
        cards = [{"id": "TASK-9", "status": "In Progress"}]
        self.turn("maps", 2000, 2100)
        self.edit("maps", "TASK-9", "append-notes", ts=2050)
        self.assertEqual(self.facts(cards)["cards"]["TASK-9"]["kind"], "idle")

    def test_an_edit_long_ago_in_a_long_running_turn_is_not_work_on_the_card(self):
        cards = [{"id": "TASK-9", "status": "In Progress"}]
        self.turn("maps", 2000)
        self.edit("maps", "TASK-9", "append-notes", ts=2050)
        self.assertEqual(self.facts(cards, now=2050 + board.EDITING_FRESH)["cards"]["TASK-9"]["kind"], "working")
        self.assertEqual(self.facts(cards, now=2051 + board.EDITING_FRESH)["cards"]["TASK-9"]["kind"], "idle")

    def test_the_lead_in_a_dialog_works_on_the_card_but_waits(self):
        cards = [{"id": "TASK-9", "status": "In Progress"}]
        self.turn("maps", 2000)
        self.edit("maps", "TASK-9", "append-notes", ts=2050)
        self.st["sessions"]["maps"]["state"] = "dialog"
        f = self.facts(cards, now=2100)["cards"]["TASK-9"]
        self.assertEqual((f["kind"], f["waiting"]), ("working", True))

    def test_the_lead_working_on_the_card_in_its_running_turn_is_the_one_working(self):
        cards = [{"id": "TASK-9", "status": "In Progress"}]
        self.turn("maps", 2000)
        self.assertIsNone(self.edit("maps", "TASK-9", "append-notes", ts=2050))
        f = self.facts(cards, now=2100)["cards"]["TASK-9"]
        self.assertEqual((f["kind"], f["who"], f["waiting"], f["stuck"]), ("working", ["maps"], False, False))
        self.assertEqual(board.verdict(f), i18n.t("core.board.working", who="maps"))   # the process's language
        self.assertEqual(self.facts(cards, now=2100)["counts"]["working"], 1)
        self.ev("turn.result", "maps", text="ok", ts=2200)   # the turn is over: the card is nobody's again
        self.ev("turn.stopped", "maps", ts=2200)
        self.assertEqual(self.facts(cards, now=2100)["cards"]["TASK-9"]["kind"], "idle")

    def test_an_edit_before_the_running_turn_does_not_count(self):
        cards = [{"id": "TASK-9", "status": "In Progress"}]
        self.edit("maps", "TASK-9", "append-notes", ts=1500)
        self.turn("maps", 2000)
        self.assertEqual(self.facts(cards)["cards"]["TASK-9"]["kind"], "idle")

    def test_another_agent_editing_the_card_in_its_running_turn_is_the_one_working(self):
        cards = [{"id": "TASK-9", "status": "In Progress"}]
        self.agent("backend")
        self.turn("backend", 2000)
        self.edit("backend", "TASK-9", "append-notes", ts=2050)
        self.assertEqual(self.facts(cards, now=2100)["cards"]["TASK-9"]["who"], ["backend"])
        self.edit("maps", "TASK-9", "append-notes", ts=2060)   # the last editor's turn decides: the lead is not busy
        self.assertEqual(self.facts(cards, now=2100)["cards"]["TASK-9"]["kind"], "idle")

    def test_the_owner_editing_is_not_a_session_working_and_only_in_progress_counts(self):
        self.turn("maps", 2000)
        self.edit("owner", "TASK-9", "status", ts=2050)
        self.edit("maps", "TASK-4", "append-notes", ts=2060)
        out = self.facts([{"id": "TASK-9", "status": "In Progress"}, {"id": "TASK-4", "status": "To Do"}])["cards"]
        self.assertEqual(out["TASK-9"]["kind"], "idle")
        self.assertNotIn("TASK-4", out)

    def test_a_running_turn_does_not_hide_a_handover_waiting_for_the_lead(self):
        self.raise_one("maps-a", card="TASK-9")
        self.hand_in("maps-a", 3000)
        self.turn("maps", 3100)
        self.edit("maps", "TASK-9", "title", ts=3150)   # a title is not a judgement: still "handed in, not sorted out"
        self.assertEqual(self.facts([{"id": "TASK-9", "status": "In Progress"}], now=3200)["cards"]["TASK-9"]["kind"],
                         "unsorted")

    def test_unlinked_executors_of_the_last_day_and_counts(self):
        self.raise_one("maps-a", card="TASK-9")
        self.raise_one("maps-b")
        out = self.facts([{"id": "TASK-9", "status": "In Progress"}], now=2000)
        self.assertEqual([u["name"] for u in out["unlinked"]], ["maps-b"])
        self.assertEqual(out["counts"], {"working": 1, "unsorted": 0, "unlinked": 1})
        self.assertEqual(self.facts([], now=10 ** 6)["unlinked"], [])               # older than a day — not in the list

    def test_card_facts_need_a_slug_and_a_card(self):
        self.assertIsNotNone(self.ev("direction.card", None, slug="maps", card="x", **by("maps")))
        self.assertIsNotNone(self.ev("direction.card", None, card="TASK-1", **by("maps")))
        self.assertIsNone(self.ev("direction.card", None, slug="maps", card="task-3", fields=["status"], **by("maps")))
        self.assertIn("TASK-3", self.st["boards"]["maps"]["cards"])

    def test_brief_card_is_one_token_of_the_first_line(self):
        self.assertEqual(board.brief_card("Task TASK-41 of the direction maps: tiles\nTASK-2 below"), "TASK-41")
        self.assertIsNone(board.brief_card("TASK-1 and TASK-2 at once"))
        self.assertIsNone(board.brief_card("fix the tiles\nTASK-3"))
        self.assertEqual(board.brief_card("TASK-7, TASK-7 again"), "TASK-7")
        self.assertEqual(board.card_id("task-07"), "TASK-7")


class Lag(Case):
    """The board lags (boards-sync A): after the last card edit the lead got the owner's word or raised an executor without
    a card, and LAG_GRACE has passed - the facts carry "lag"; a card edit clears it. Read-only: nothing is written."""

    def owner_says(self, ts, to="maps", text="next: code first, then the switch", deliver=True):
        self.assertIsNone(self.ev("message.received", None, ts=ts, to=to, text=text, **OWNER))
        mid = f"m{self.seq}"
        if deliver:   # the lead's turn on it began: delivered
            self.ev("turn.started", to, marker=mid, prompt_head=text, ts=ts + 1)
            self.ev("turn.stopped", to, ts=ts + 2)
            self.assertEqual(self.st["messages"][mid]["state"], "delivered")
        return mid

    def test_not_his_words_and_not_yet_delivered_do_not_count(self):
        # review: × on a card is the core's note "from the owner", a slash command is not a word about the work, and a
        # message still in the queue the lead has not seen
        self.edit("maps", "TASK-1", "status", ts=2000)
        cards = [{"id": "TASK-1", "status": "To Do", "mtime": 1999}]
        self.owner_says(3000, text="/compact")
        mid = self.owner_says(3100)
        self.st["messages"][mid]["card_dismissed"] = True
        self.assertIsNone(self.ev("message.received", None, ts=3150, to="maps", text="Fix the plugin", action=True,
                                  **OWNER))   # a page button, not a word about the work
        self.ev("turn.started", "maps", marker=f"m{self.seq}", ts=3151)
        self.ev("turn.stopped", "maps", ts=3152)
        self.owner_says(3200, deliver=False)
        self.assertNotIn("lag", self.facts(cards, now=9000))

    def test_a_task_to_a_session_that_is_not_its_executor_does_not_count(self):
        # review: `hub task new <specialist>` from the lead is not an executor of the direction: it never gets a card
        self.edit("maps", "TASK-1", "status", ts=1005)
        self.ev("session.requested", "reviewer", cwd="/r", session_id="s-r", **OWNER)
        self.assertIsNone(self.ev("delegation.created", None, session="reviewer", for_session="maps", context="check",
                                  **by("maps")))
        self.assertNotIn("lag", self.facts([{"id": "TASK-1", "status": "To Do", "mtime": 1005}], now=9000))
        # nor a task the owner gives the direction's executor (without a card) himself: the lead did not give it
        self.raise_one("maps-a")
        self.edit("maps", "TASK-1", "status", ts=2000)
        self.assertIsNone(self.ev("delegation.created", None, ts=2100, session="maps-a", context="look at the logs", **OWNER))
        self.assertNotIn("lag", self.facts([{"id": "TASK-1", "status": "To Do", "mtime": 2000}], now=9000))

    def test_the_owners_word_lights_the_mark_after_the_grace_and_an_edit_clears_it(self):
        self.edit("maps", "TASK-1", "status", ts=2000)
        cards = [{"id": "TASK-1", "status": "To Do", "mtime": 1999}]
        self.owner_says(3000)
        self.assertNotIn("lag", self.facts(cards, now=3000 + board.LAG_GRACE - 1))   # the lead edits in the same turn
        self.assertEqual(self.facts(cards, now=3000 + board.LAG_GRACE)["lag"],
                         {"since": 3000, "age": board.LAG_GRACE, "delegations": 0, "owner": 1})
        self.edit("maps", "TASK-1", "append-notes", ts=3400)
        self.assertNotIn("lag", self.facts(cards, now=4000))

    def test_an_executor_without_a_card_counts_one_with_a_card_does_not(self):
        self.edit("maps", "TASK-1", "status", ts=1005)
        self.raise_one("maps-a", card="TASK-1")        # the card shows "working": not a lag
        self.raise_one("maps-b")
        self.ev("message.received", None, to="maps", text="done", **by("maps-b"))   # a session's word is not the owner's
        self.owner_says(1500, to="hub")                                                  # nor the owner's word to another
        lag = self.facts([{"id": "TASK-1", "status": "In Progress", "mtime": 1005}], now=5000)["lag"]
        self.assertEqual((lag["delegations"], lag["owner"]), (1, 0))

    def test_a_newer_card_file_is_an_edit_too(self):
        # a card written while hubd refused direction.card (old code): the file's time still counts
        self.owner_says(3000)
        self.assertNotIn("lag", self.facts([{"id": "TASK-1", "status": "To Do", "mtime": 3100}], now=5000))
        self.assertIn("lag", self.facts([{"id": "TASK-1", "status": "To Do", "mtime": 2900}], now=5000))

    def test_no_cards_or_no_known_edit_time_no_mark(self):
        self.owner_says(3000)
        self.assertNotIn("lag", self.facts([], now=5000))
        self.assertNotIn("lag", self.facts([{"id": "TASK-1", "status": "To Do"}], now=5000))

    def test_the_line(self):
        # the direction commands' output: in the language of the process (a session's — English)
        i18n.use("en")
        self.addCleanup(i18n.use, "en")
        self.assertEqual(board.lag_line({"since": 1, "age": 8 * 60 + 5, "delegations": 2, "owner": 1}),
                         "the board lags: after the last card edit 2 tasks without a card, 1 word of the owner, 8 min")
        self.assertEqual(board.lag_line({"since": 1, "age": 2 * 3600 + 36 * 60, "delegations": 0, "owner": 3}),
                         "the board lags: after the last card edit 3 words of the owner, 2 h 36 min")
        self.assertEqual(board.lag_line({"since": 1, "age": 3 * 86400 + 60, "delegations": 5, "owner": 0}),
                         "the board lags: after the last card edit 5 tasks without a card, 3 d")
        self.assertEqual(board.lag_line(None), "")

    def test_the_line_in_russian_keeps_its_plural_forms(self):
        i18n.use("ru")   # a process whose language is Russian
        self.addCleanup(i18n.use, "en")
        self.assertEqual(board.lag_line({"since": 1, "age": 8 * 60 + 5, "delegations": 2, "owner": 1}),
                         i18n.tr("ru", "core.board.lag", parts=i18n.tr("ru", "core.board.lag_delegations", count=2) + ", "
                                 + i18n.tr("ru", "core.board.lag_owner", count=1),
                                 age=i18n.tr("ru", "core.board.age_min", min=8)))
        self.assertIn(i18n.tr("ru", "core.board.lag_delegations", count=5), board.lag_line(
            {"since": 1, "age": 60, "delegations": 5, "owner": 0}))
        self.assertNotEqual(i18n.tr("ru", "core.board.lag_delegations", count=5),
                            i18n.tr("ru", "core.board.lag_delegations", count=2))


class HandedLine(Case):
    """K4: when an executor hands in, the lead gets a line about its card with a ready command; Done gets no line; it does not wake the lead."""

    def lead_notes(self):
        return [m for m in self.st["messages"].values() if m["session"] == "maps" and "handed in" in m["text"]]

    def test_the_line_rides_on_the_lessons_note(self):
        self.raise_one("maps-a", card="TASK-9")
        d = [d for d in self.st["delegations"].values() if d["session"] == "maps-a"][-1]
        self.ev("turn.started", "maps-a", marker=d["message"], prompt_head="task", ts=2990)
        self.ev("turn.result", "maps-a", text="Done.\n\n**Lessons**\n- tiles are versioned", ts=2995)
        self.ev("turn.stopped", "maps-a", ts=2995)
        self.ev("session.close_requested", "maps-a", retire=True, ts=3000,
                cards=[{"slug": "maps", "card": "TASK-9", "status": "In Progress"}])
        (note,) = self.lead_notes()
        self.assertIn("tiles are versioned", note["text"])
        self.assertIn("Card TASK-9 (In Progress): maps-a handed in; nobody else works on it", note["text"])
        self.assertIn("`hub direction maps task edit TASK-9 -s Done`", note["text"])
        self.assertNotIn("fyi", note)

    def test_without_lessons_the_line_comes_aside_without_waking(self):
        self.raise_one("maps-a", card="TASK-9")
        self.raise_one("maps-b", card="TASK-9")
        d = [d for d in self.st["delegations"].values() if d["session"] == "maps-a"][-1]
        self.ev("turn.started", "maps-a", marker=d["message"], prompt_head="task", ts=2990)
        self.ev("turn.result", "maps-a", text="Done.\n\nLessons: none", ts=2995)
        self.ev("turn.stopped", "maps-a", ts=2995)
        self.ev("session.close_requested", "maps-a", retire=True, ts=3000,
                cards=[{"slug": "maps", "card": "TASK-9", "status": "In Progress"}])
        (note,) = self.lead_notes()
        self.assertTrue(note.get("fyi"))
        self.assertEqual(note["state"], "aside")
        self.assertIn("still working on it: maps-b", note["text"])

    def test_no_line_for_a_done_card_or_without_cards(self):
        self.raise_one("maps-a", card="TASK-9")
        d = [d for d in self.st["delegations"].values() if d["session"] == "maps-a"][-1]
        self.ev("turn.started", "maps-a", marker=d["message"], prompt_head="task", ts=2990)
        self.ev("turn.result", "maps-a", text="Done.\n\nLessons: none", ts=2995)
        self.ev("turn.stopped", "maps-a", ts=2995)
        self.ev("session.close_requested", "maps-a", retire=True, ts=3000,
                cards=[{"slug": "maps", "card": "TASK-9", "status": "Done"}])
        self.assertEqual(self.lead_notes(), [])

    def test_a_card_line_of_one_removed_with_its_task_open_does_not_say_handed_in(self):
        self.raise_one("maps-a", card="TASK-9")
        d = [d for d in self.st["delegations"].values() if d["session"] == "maps-a"][-1]
        self.ev("turn.started", "maps-a", marker=d["message"], prompt_head="task", ts=2990)
        self.ev("turn.result", "maps-a", text="", is_error=True, subtype="error_during_execution", ts=2995)
        self.ev("turn.stopped", "maps-a", ts=2995)
        self.st["delegations"][d["id"]]["state"] = "stopped"
        self.st["sessions"]["maps-a"]["state"] = "asleep"
        self.assertIsNone(self.ev("session.remove_requested", "maps-a", ts=9000,
                                  cards=[{"slug": "maps", "card": "TASK-9", "status": "In Progress"}], **OWNER))
        (note,) = [m for m in self.st["messages"].values() if m["session"] == "maps" and "TASK-9" in m["text"]]
        self.assertNotIn("handed in its task", note["text"])
        self.assertIn("not done", note["text"])


if __name__ == "__main__":
    unittest.main()


class Cli(unittest.TestCase):
    """bin/hub: a card edit records a direction.card fact quietly; --board and the brief's TASK-N make the link in task.run_requested."""

    def setUp(self):
        import os
        import subprocess
        import sys
        import tempfile
        from core import journal
        self.tmp = tempfile.mkdtemp(prefix="board-cli-")
        self.repo = os.path.join(self.tmp, "repo")
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        subprocess.run(["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-q",
                        "--allow-empty", "-m", "init"], check=True)   # a direction's repo has a commit (copies)
        self.env = {**{k: v for k, v in os.environ.items() if not k.startswith("HUB_")},
                    "HUB_STATE_DIR": self.tmp, "HUB_DIRECTIONS": os.path.join(self.tmp, "directions"),
                    "HUB_CLI_WAIT": "0", "HOME": self.tmp}
        j = journal.Journal(os.path.join(self.tmp, "journal.jsonl"))
        for name in ("maps", "notes"):
            j.append("session.requested", name, {"cwd": "/w", "session_id": f"s-{name}", "channel": "cli", "owner": True})
        j.append("task.backlog", None, {"slug": "maps", **by("maps")})
        self.hub("direction", "new", "maps", "--repo", self.repo, me=None)
        self.hub("direction", "maps", "task", "create", "Tiles", me=None)
        self.events()
        self.sys = sys

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def hub(self, *args, me="maps"):
        import os
        import subprocess
        import sys
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = {**self.env, **({"HUB_SESSION": me} if me else {})}
        return subprocess.run([sys.executable, os.path.join(repo, "bin", "hub"), *args], env=env, capture_output=True,
                              text=True)

    def events(self, kind=None):
        import os
        from core import spool
        taken = spool.take(os.path.join(self.tmp, "spool"))
        for path, _ in taken:
            os.remove(path)
        return [e for _, e in taken if kind is None or e["type"] == kind]

    def test_a_new_direction_without_repo_takes_the_leads_git_folder(self):
        import json
        import os
        from core import journal
        j = journal.Journal(os.path.join(self.tmp, "journal.jsonl"))
        j.append("session.requested", "fixer", {"cwd": self.repo, "session_id": "s-fixer", "channel": "cli", "owner": True})
        meta = lambda slug: json.load(open(os.path.join(self.tmp, "directions", slug, "direction.json")))["repo"]
        real = os.path.realpath
        # a session in a project repo starts its own direction: that repo, not an empty one of its own
        self.assertEqual(self.hub("direction", "new", "tiles", me="fixer").returncode, 0)
        self.assertEqual(real(meta("tiles")), real(self.repo))
        # the hub for a session (--session): the session's repo
        self.assertEqual(self.hub("direction", "new", "fixes", "--session", "fixer", me=None).returncode, 0)
        self.assertEqual(real(meta("fixes")), real(self.repo))
        # a session whose folder is not git (/w): a git folder of its own for the direction
        self.assertEqual(self.hub("direction", "new", "notes2", me="notes").returncode, 0)
        self.assertEqual(real(meta("notes2")), real(os.path.join(self.tmp, "direction-repos", "notes2")))
        # an executor in a core copy: the repo of its copy, not the copy the core removes when it hands in
        import subprocess
        copy = os.path.join(self.repo, ".claude", "worktrees", "fixer-a")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", "task/fixer-a", copy], check=True)
        j.append("session.requested", "fixer-a", {"cwd": copy, "session_id": "s-fa", "executor": True, "raised_by": "fixer",
                                                  "worktree": {"path": copy, "branch": "task/fixer-a", "repo": self.repo},
                                                  "channel": "cli", "owner": True})
        self.assertEqual(self.hub("direction", "new", "layers", me="fixer-a").returncode, 0)
        self.assertEqual(real(meta("layers")), real(self.repo))
        # the hub (or the owner) without --session: the lead is the session named like the direction
        j.append("session.requested", "sso", {"cwd": self.repo, "session_id": "s-sso", "channel": "cli", "owner": True})
        self.assertEqual(self.hub("direction", "new", "sso", me=None).returncode, 0)
        self.assertEqual(real(meta("sso")), real(self.repo))

    def test_a_session_that_leads_a_direction_cannot_start_a_second_one(self):
        import os
        r = self.hub("direction", "new", "tiles", me="maps")   # maps already leads the direction maps
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("hub direction new tiles --session <name>", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "directions", "tiles")))   # no folder nobody leads
        self.assertEqual(self.events("task.backlog"), [])
        # the same direction again is not this refusal (the folder exists: its own message)
        r = self.hub("direction", "new", "maps", "--repo", self.repo, me="maps")
        self.assertNotIn("--session", r.stderr)
        # a session without a direction takes it; the hub starts one for a session
        self.assertEqual(self.hub("direction", "new", "notes2", me="notes").returncode, 0)
        self.assertEqual(self.hub("direction", "new", "fixes", "--session", "notes", "--repo", self.repo, me=None).returncode, 0)
        self.assertEqual([e["data"]["slug"] for e in self.events("task.backlog")], ["notes2", "fixes"])

    def test_archive_by_the_new_name_carries_the_old_names_too(self):
        self.assertEqual(self.hub("direction", "rename", "maps", "atlas", me=None).returncode, 0)
        self.events()
        self.assertEqual(self.hub("direction", "archive", "atlas", me=None).returncode, 0)
        (e,) = self.events("task.backlog_closed")
        self.assertEqual(e["data"]["slug"], "atlas")
        self.assertEqual(sorted(e["data"]["slugs"]), ["atlas", "maps"])

    def test_card_edits_leave_a_quiet_fact(self):
        r = self.hub("direction", "maps", "task", "create", "Layers")
        self.assertEqual(r.stdout.strip().splitlines()[-1], i18n.tr("en", "directions.cli.created_id", id="TASK-2"))
        (e,) = self.events()
        self.assertEqual(e["type"], "direction.card")
        self.assertEqual((e["data"]["slug"], e["data"]["card"], e["data"]["fields"], e["data"]["sender"]),
                         ("maps", "TASK-2", ["create"], "maps"))
        r = self.hub("direction", "maps", "task", "edit", "1", "-s", "In Progress", "--append-notes", "took it")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("accepted", r.stdout)
        (e,) = self.events()
        self.assertEqual((e["data"]["card"], e["data"]["fields"]), ("TASK-1", ["append-notes", "status"]))
        self.hub("direction", "maps", "task", "archive", "2")
        self.assertEqual([e["data"]["fields"] for e in self.events()], [["archive"]])
        self.hub("direction", "maps", "task", "list")
        self.hub("direction", "maps", "task", "edit", "9", "-s", "Done")             # no such card — no fact either
        self.hub("direction", "maps", "task", "view", "1")
        self.assertEqual(self.events(), [])

    def test_board_flag_and_the_brief_token_link_the_run(self):
        self.hub("task", "run", "maps-a", "fix the tiles", "--work", "normal", "--board", "task-1")
        (e,) = self.events("task.run_requested")
        self.assertEqual(e["data"]["board"], {"slug": "maps", "card": "TASK-1", "how": "flag"})
        self.assertEqual(e["data"]["task"], "fix the tiles")
        r = self.hub("task", "run", "maps-b", "Task TASK-1 of the direction maps: tests\nmore details", "--work", "normal")
        (e,) = self.events("task.run_requested")
        self.assertEqual(e["data"]["board"], {"slug": "maps", "card": "TASK-1", "how": "brief"})
        self.assertNotIn("This task has no board card", r.stdout)

    def test_without_a_card_one_line_not_a_refusal(self):
        r = self.hub("task", "run", "maps-c", "Task TASK-5: no such card", "--work", "normal")
        (e,) = self.events("task.run_requested")
        self.assertNotIn("board", e["data"])
        self.assertIn("This task has no board card — the owner will not see it on the maps board", r.stdout)
        r = self.hub("task", "run", "notes-a", "fix the notes", "--work", "normal", me="notes")   # not a direction
        self.assertNotIn("no board card", r.stdout)

    def test_task_list_resume_and_list_show_the_facts(self):
        import os
        from core import journal
        j = journal.Journal(os.path.join(self.tmp, "journal.jsonl"))
        j.append("session.requested", "maps-a", {"cwd": "/w", "session_id": "s-a", "role": "executor", "executor": True,
                                                 "raised_by": "maps", "about": "tiles", **OWNER})
        j.append("session.started", "maps-a", {})
        j.append("delegation.created", None, {"session": "maps-a", "for_session": "maps", "context": "tiles",
                                              "board": {"slug": "maps", "card": "TASK-1", "how": "flag"}, **by("maps")})
        j.append("session.requested", "maps-b", {"cwd": "/w", "session_id": "s-b", "role": "executor", "executor": True,
                                                 "raised_by": "maps", "about": "one-off", **OWNER})
        j.append("delegation.created", None, {"session": "maps-b", "for_session": "maps", "context": "one-off", **by("maps")})
        out = self.hub("direction", "maps", "task", "list").stdout
        self.assertIn("TASK-1 - Tiles · ⚙ working: maps-a\n", out)
        self.assertIn("no card in a day: maps-b", out)
        self.assertIn("TASK-1 - Tiles · ⚙ working: maps-a", self.hub("direction", "resume", "maps").stdout)
        self.assertIn(i18n.tr("en", "directions.cli.working") + ": 1", self.hub("direction", "list").stdout)
        self.assertNotIn("the board lags", out)   # the delegation without a card is fresh: within the grace

    def test_task_list_resume_and_list_show_the_lag(self):
        import os
        import time
        from core import journal
        j = journal.Journal(os.path.join(self.tmp, "journal.jsonl"))
        card = os.path.join(self.tmp, "directions", "maps", "backlog", "tasks")
        (path,) = [os.path.join(card, f) for f in os.listdir(card)]
        os.utime(path, (time.time() - 3600, time.time() - 3600))   # the card was last edited an hour ago
        e = j.append("message.received", None, {"to": "maps", "text": "code first, then the switch", **OWNER},
                     ts=time.time() - 20 * 60)
        j.append("session.started", "maps", {})
        j.append("turn.started", "maps", {"marker": f"m{e['seq']}"}, ts=time.time() - 20 * 60 + 1)   # delivered
        line = "the board lags: after the last card edit 1 word of the owner, 20 min"
        self.assertIn(line, self.hub("direction", "maps", "task", "list").stdout)
        self.assertIn(line, self.hub("direction", "resume", "maps").stdout)
        self.assertIn(" · " + line, self.hub("direction", "list").stdout)

    def test_bad_board_flags_are_call_errors(self):
        r = self.hub("task", "run", "maps-d", "x", "--work", "normal", "--board", "TASK-9")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(i18n.tr("en", "directions.cli.board_no_card", card="TASK-9", slug="maps"), r.stderr)
        r = self.hub("task", "run", "notes-b", "x", "--work", "normal", "--board", "TASK-1", me="notes")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(i18n.tr("en", "directions.cli.board_only_direction"), r.stderr)
        self.assertEqual(self.events("task.run_requested"), [])


class DaemonReadsTheCard(unittest.TestCase):
    """On a handover the daemon records the linked card's status from disk: the reducer writes the lead a K4 line from it."""

    def test_retire_carries_the_card_status(self):
        import os
        import tempfile
        import types
        from unittest import mock
        from core import daemon
        from modules.directions import direction
        tmp = tempfile.mkdtemp(prefix="board-retire-")
        root = os.path.join(tmp, "directions")
        with mock.patch.dict(os.environ, {"HUB_DIRECTIONS": root}):
            os.makedirs(root)
            direction.backlog(os.path.join(root, "maps"), ["init", "maps"], capture=True)
            direction.backlog(os.path.join(root, "maps"), ["task", "create", "Tiles", "-s", "In Progress"], capture=True)
            h = daemon.Hub(tmp, types.SimpleNamespace())
            h.execute = lambda a: None
            h.record("session.requested", "maps", {"cwd": "/m", "session_id": "s-m", **OWNER})
            h.record("session.started", "maps", {})
            h.record("task.backlog", None, {"slug": "maps", **by("maps")})
            h.record("task.run_requested", "maps-a", {"task": "tiles", "board": {"slug": "maps", "card": "TASK-1"},
                                                     **by("maps")})
            for type, entity, data in executors.events_for(h.state, "maps-a", h.state["runs"]["maps-a"], cwd="/w",
                                                           worktree=None):
                h.record(type, entity, data)
            self.assertEqual(h.handed_cards("maps-a"),
                             {"cards": [{"slug": "maps", "card": "TASK-1", "status": "In Progress"}]})
            self.assertEqual(h.handed_cards("maps"), {})          # not an executor — nothing
            recorded = []
            h.record = lambda type, name=None, data=None, **_: recorded.append((type, name, data))
            with mock.patch.object(daemon.executors, "retiring", return_value=[("sleep", "maps-a")]):
                h.retire_executors()
            self.assertEqual(recorded[0][2]["cards"][0]["card"], "TASK-1")

class DecisionsOfTheDirection(Case):
    """Slice 2: `hub decided` from a direction's lead or its executor also goes into the backlog's decisions/ (the command is as before)."""

    def decided(self, who, text="We push straight to the customer's main", ts=None):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 5000 + self.seq,
                                                      "type": "owner.decided", "entity": None,
                                                      "data": {"text": text, **by(who)}})
        self.assertIsNone(why)
        return [a for a in acts if a["do"] == "board_decision"]

    def test_the_lead_and_its_executor_but_not_others(self):
        self.raise_one("maps-a")
        self.ev("session.requested", "notes", cwd="/n", session_id="s-n", **OWNER)
        (a,) = self.decided("maps")
        self.assertEqual((a["slug"], a["by"], a["text"], a["seq"]), ("maps", "maps", "We push straight to the customer's main",
                                                                     self.seq))
        self.assertEqual(self.decided("maps-a")[0]["by"], "maps-a")
        self.assertEqual(self.decided("notes"), [])
        (note,) = [m for m in self.st["messages"].values() if m["session"] == "maps" and "maps-a" in m["text"]
                   and "An owner's decision" in m["text"]]
        self.assertIn("The core puts it into the decisions of the direction's backlog itself", note["text"])
        self.assertEqual(self.decided("maps", text="  "), [])

    def test_the_daemon_writes_it_once_and_resume_shows_it(self):
        import os
        import subprocess
        import sys
        import tempfile
        from unittest import mock
        from modules.directions import direction
        tmp = tempfile.mkdtemp(prefix="board-decided-")
        root = os.path.join(tmp, "directions")
        with mock.patch.dict(os.environ, {"HUB_DIRECTIONS": root, "HUB_STATE_DIR": tmp}):
            os.makedirs(root)
            direction.backlog(os.path.join(root, "maps"), ["init", "maps"], capture=True)
            path = os.path.join(root, "maps")
            first = direction.record_decision(path, 812, "We push straight to the customer's main: rebase, no MR merges", "maps", 5000)
            self.assertIsNone(direction.record_decision(path, 812, "We push straight to the customer's main", "maps", 5000))
            second = direction.record_decision(path, 813, "The second", "maps-a", 5001)
            self.assertEqual([os.path.basename(p)[:10] for p in (first, second)], ["decision-1", "decision-2"])
            with open(first, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("## Decision\n\nWe push straight to the customer's main: rebase, no MR merges", text)
            self.assertIn("[decided:812]", text)
            repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            out = subprocess.run([sys.executable, os.path.join(repo, "modules", "directions", "direction.py"), "resume",
                                  "maps"], capture_output=True, text=True, env=os.environ.copy()).stdout
            self.assertIn("We push straight to the customer's main", out)
            self.assertNotIn("-- cleanup --", out)                # an empty "Consequences" section does not make an empty decision

    def test_the_daemon_executes_the_action(self):
        import os
        import tempfile
        import types
        from unittest import mock
        from core import daemon
        from modules.directions import direction
        tmp = tempfile.mkdtemp(prefix="board-decided-d-")
        root = os.path.join(tmp, "directions")
        with mock.patch.dict(os.environ, {"HUB_DIRECTIONS": root}):
            os.makedirs(root)
            direction.backlog(os.path.join(root, "maps"), ["init", "maps"], capture=True)
            h = daemon.Hub(tmp, types.SimpleNamespace())
            h.execute({"do": "board_decision", "session": None, "slug": "maps", "seq": 9, "ts": 5000, "by": "maps",
                       "text": "Yes"})
            h.execute({"do": "board_decision", "session": None, "slug": "nope", "seq": 10, "ts": 5000, "by": "maps",
                       "text": "No backlog, we do not crash"})
            for t in h.writers:   # the daemon's threads write
                t.join(10)
            self.assertEqual(len(os.listdir(os.path.join(root, "maps", "backlog", "decisions"))), 1)

    def test_withdrawing_the_decision_removes_its_backlog_file(self):
        # `hub decided list` once made the decision "list": withdrawing it must not leave the file in the backlog
        import os
        import tempfile
        import types
        from unittest import mock
        from core import daemon
        from modules.directions import direction
        (a,) = self.decided("maps", text="list")
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 6000, "type": "owner.decided_revoked",
                                                      "entity": None, "data": {"seq": a["seq"], **OWNER}})
        self.assertIsNone(why)
        (drop,) = [x for x in acts if x["do"] == "board_decision_revoke"]
        self.assertEqual(drop, {"do": "board_decision_revoke", "session": None, "slug": "maps", "seq": a["seq"]})
        tmp = tempfile.mkdtemp(prefix="board-decided-r-")
        root = os.path.join(tmp, "directions")
        with mock.patch.dict(os.environ, {"HUB_DIRECTIONS": root}):
            os.makedirs(root)
            direction.backlog(os.path.join(root, "maps"), ["init", "maps"], capture=True)
            path = os.path.join(root, "maps")
            direction.record_decision(path, 1, "a decision written by hand has no mark", "maps", 5000)
            h = daemon.Hub(tmp, types.SimpleNamespace())
            for action in (a, drop, drop):   # a repeat of the drop finds nothing and does not fail
                h.execute(action)
                for t in h.writers:
                    t.join(10)
            left = os.listdir(os.path.join(path, "backlog", "decisions"))
            self.assertEqual(len(left), 1)
            self.assertIsNone(direction.drop_decision(path, a["seq"]))

    def test_a_decision_found_in_the_journal_is_withdrawn_from_the_backlog_too(self):
        # a record rebuilt at an update has no slug: the author's direction is taken
        (a,) = self.decided("maps", text="list")
        del self.st["decisions"][-1]["slug"]
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 6000, "type": "owner.decided_revoked",
                                                      "entity": None, "data": {"seq": a["seq"], **OWNER}})
        self.assertIsNone(why)
        self.assertEqual([x["slug"] for x in acts if x["do"] == "board_decision_revoke"], ["maps"])

    def test_the_withdrawal_waits_for_the_file_that_is_still_being_written(self):
        # the writer of the file waits for the backlog lock; the drop must not run before it and find nothing
        import os
        import tempfile
        import threading
        import time
        import types
        from unittest import mock
        from core import daemon
        from modules.directions import direction
        (a,) = self.decided("maps", text="list")
        tmp = tempfile.mkdtemp(prefix="board-decided-w-")
        root = os.path.join(tmp, "directions")
        gate, real = threading.Event(), direction.record_decision

        def slow(*args):
            gate.wait(10)
            return real(*args)
        with mock.patch.dict(os.environ, {"HUB_DIRECTIONS": root}), mock.patch.object(direction, "record_decision", slow):
            os.makedirs(root)
            direction.backlog(os.path.join(root, "maps"), ["init", "maps"], capture=True)
            path = os.path.join(root, "maps")
            h = daemon.Hub(tmp, types.SimpleNamespace())
            h.execute(a)
            h.execute({"do": "board_decision_revoke", "session": None, "slug": "maps", "seq": a["seq"]})
            time.sleep(0.3)   # the drop has its chance to run first  # portable: nothing to wait for, the held writer shows no sign
            gate.set()
            for t in list(h.writers):
                t.join(10)
            self.assertEqual(os.listdir(os.path.join(path, "backlog", "decisions")), [])

    def test_a_note_that_quotes_the_mark_is_not_removed(self):
        import os
        import tempfile
        from unittest import mock
        from modules.directions import direction
        tmp = tempfile.mkdtemp(prefix="board-decided-q-")
        root = os.path.join(tmp, "directions")
        with mock.patch.dict(os.environ, {"HUB_DIRECTIONS": root}):
            os.makedirs(root)
            direction.backlog(os.path.join(root, "maps"), ["init", "maps"], capture=True)
            path = os.path.join(root, "maps")
            folder = os.path.join(path, "backlog", "decisions")
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, "decision-1 - note.md"), "w", encoding="utf-8") as f:
                f.write("---\nid: decision-1\n---\nsee [decided:7] for the reason\n")
            self.assertIsNone(direction.drop_decision(path, 7))
            self.assertEqual(len(os.listdir(folder)), 1)


class ByReview(Case):
    """Branch review fixes: a renamed direction, the same executor name reused, cards whose id is not TASK-N."""

    def test_a_reused_executor_name_does_not_inherit_old_cards(self):
        self.raise_one("maps-a", card="TASK-1")
        self.hand_in("maps-a", 3000)
        self.ev("session.exited", "maps-a", why="sleep", ts=3001)
        self.ev("session.remove_requested", "maps-a", retired=True, ts=3002)
        self.assertIsNone(self.ev("task.run_requested", "maps-a", task="more", board={"slug": "maps", "card": "TASK-5"},
                                  **by("maps")))
        run = self.st["runs"]["maps-a"]
        for type, entity, data in executors.events_for(self.st, "maps-a", run, cwd="/m/w", worktree=None):
            self.assertIsNone(self.ev(type, entity, ts=4000, **data), (type, data))
        self.assertEqual(board.links(self.st, "maps-a"), [("maps", "TASK-5", "flag")])

    def test_a_follow_up_task_without_a_card_keeps_the_link(self):
        self.raise_one("maps-a", card="TASK-9")
        self.assertEqual(self.st["sessions"]["maps-a"]["board"], {"slug": "maps", "card": "TASK-9", "how": "flag"})
        self.assertIsNone(self.ev("delegation.created", None, session="maps-a", for_session="maps", context="one more",
                                  **by("maps")))
        self.assertEqual(board.links(self.st, "maps-a"), [("maps", "TASK-9", "flag")])

    def test_card_ids_other_than_task_n_still_match(self):
        self.raise_one("maps-a", card="TASK-12")
        out = self.facts([{"id": "task-12", "status": "In Progress"}])
        self.assertEqual(out["cards"]["task-12"]["kind"], "working")


class ByReviewFiles(unittest.TestCase):
    """Review fixes: a direction's files — a session's decision is not passed off as the owner's word, resume does not lose
    its own decisions behind the ones the core wrote, the daemon does not wait for the backlog lock, a renamed direction is found."""

    def setUp(self):
        import os
        import tempfile
        from unittest import mock
        from modules.directions import direction
        self.tmp = tempfile.mkdtemp(prefix="board-review-")
        self.root = os.path.join(self.tmp, "directions")
        os.makedirs(self.root)
        self.env = mock.patch.dict(os.environ, {"HUB_DIRECTIONS": self.root, "HUB_STATE_DIR": self.tmp})
        self.env.start()
        self.addCleanup(self.env.stop)
        direction.backlog(os.path.join(self.root, "maps"), ["init", "maps"], capture=True)
        self.path = os.path.join(self.root, "maps")

    def test_a_recorded_decision_is_the_sessions_not_the_owners(self):
        from modules.directions import direction
        with open(direction.record_decision(self.path, 7, "We push to main directly", "maps-a", 5000), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("status: proposed\nsource: hub-decided\n", text)
        self.assertIn("Recorded by the session maps-a with hub decided", text)
        self.assertNotIn("owner's", text.split("## Context")[1].split("## Decision")[0].split("summary")[0])

    def test_resume_keeps_own_decisions_next_to_recorded_ones(self):
        import io
        from contextlib import redirect_stdout
        from modules.directions import direction
        direction.decide("maps", ["Invariant: do not touch prod", "--decision", "stage only"])
        for n in range(6):
            direction.record_decision(self.path, 100 + n, f"reversible {n}", "maps-a", 5000 + n)
        out = io.StringIO()
        with redirect_stdout(out):
            direction.resume("maps")
        self.assertIn("Invariant: do not touch prod", out.getvalue())
        self.assertIn("reversible 5", out.getvalue())

    def test_a_renamed_direction_is_found_by_its_old_name(self):
        import os
        from modules.directions import direction
        direction.rename("maps", "geo")
        self.assertEqual(direction.path_of("maps"), os.path.join(self.root, "geo"))
        self.assertEqual(direction.path_of("geo"), os.path.join(self.root, "geo"))
        self.assertTrue(direction.record_decision(direction.path_of("maps"), 8, "Yes", "maps", 5000))

    def test_the_daemon_does_not_wait_for_the_backlog_lock(self):
        import os
        import threading
        import types
        from core import daemon
        from modules.directions import minibacklog
        h = daemon.Hub(self.tmp, types.SimpleNamespace())
        held, release = threading.Event(), threading.Event()

        def hold():
            with minibacklog.writing(self.path):
                held.set()
                release.wait(10)
        threading.Thread(target=hold, daemon=True).start()
        held.wait(5)
        h.execute({"do": "board_decision", "session": None, "slug": "maps", "seq": 9, "ts": 5000, "by": "maps",
                   "text": "Yes"})   # it returned while the lock is held: the write waits for it in its own thread, not the core
        (writer,) = h.writers
        self.assertTrue(writer.is_alive())
        folder = os.path.join(self.path, "backlog", "decisions")
        self.assertEqual(os.listdir(folder), [])
        release.set()
        writer.join(10)
        self.assertEqual(len(os.listdir(folder)), 1)
