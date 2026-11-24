"""Mini-hubs, slice 2 (the "parallelism" spec, §1 "Bus", §2 case 3): the owner talks only to the mini-hub.
A mini-hub's executor (brought up while its raiser was a mini-hub — the bus mark on the session) asks the owner no cards;
its result, abort and timeout go only to the mini-hub, and the owner's words to it go to the mini-hub with a mark. The
queue of the mini-hub's executors is visible (snapshot, hub status); rework of a finished executor at the limit goes
into the queue, not a refusal."""
import tests  # noqa: F401 — keep the machine's settings out
import json
from pathlib import Path
import unittest

from core import i18n, minihub, views
from tests.test_minihub import OWNER, Delegating, by

WEB = {"channel": "web", "owner": True}
ASK = {"tool": "AskUserQuestion", "interactive": True,
       "input": {"questions": [{"question": "Which?", "options": [{"label": "A"}, {"label": "B"}]}]}}


class Base(Delegating):
    """Helpers (no tests — subclasses do not pull them in)."""

    def raised(self, name="notes-ci", n=3):
        """The mini-hub assigned a task to executor name; it is up and took it. → (mini-hub task, executor task)."""
        self.minihub(n)
        did, mid = self.task()
        self.turn("notes", mid, "delegated", runs=[name])
        self.start(name)
        (sub,) = [d for d in self.st["delegations"].values() if d["session"] == name]
        self.ev("turn.started", name, marker=sub["message"])
        return did, sub

    def snapshot(self):
        r = views.Reader()
        r.state = self.st   # the test state as is (helpers edit it directly too: "finished and went to sleep")
        return r.snapshot(now=self.events[-1]["ts"])


class Bus(Base):
    def test_the_mark_is_a_fact_of_the_raise(self):
        self.raised()
        self.assertEqual(self.st["sessions"]["notes-ci"].get("bus"), "notes")
        self.assertEqual(minihub.boss(self.st, self.st["sessions"]["notes-ci"]), "notes")
        self.assertIsNone(self.run_task("hub-x", who="hub"))       # a hub executor is outside the bus
        self.start("hub-x")
        self.assertIsNone(self.st["sessions"]["hub-x"].get("bus"))
        self.solo("notes")                                           # the mode was turned off — an executor already up stays on the bus
        self.assertEqual(minihub.boss(self.st, self.st["sessions"]["notes-ci"]), "notes")

    def test_before_the_parallel_setting_no_bus(self):
        self.assertIsNone(self.run_task("notes-ci"))
        self.start("notes-ci")
        self.assertIsNone(self.st["sessions"]["notes-ci"].get("bus"))
        self.ev("turn.started", "notes-ci", marker=None)
        self.ev("permission.requested", "notes-ci", request_id="r1", **ASK)
        self.assertEqual(len(self.st["sessions"]["notes-ci"].get("asks") or []), 1)   # a card, as before

    def test_a_form_to_the_owner_is_refused_without_a_card(self):
        self.raised()
        self.actions.clear()
        self.ev("permission.requested", "notes-ci", request_id="r1", **ASK)
        x = self.st["sessions"]["notes-ci"]
        self.assertEqual((x["state"], x.get("asks") or [], x.get("pending")), ("busy", [], []))
        (deny,) = [a for a in self.actions if a.get("do") == "respond"]
        self.assertEqual(deny["response"]["behavior"], "deny")
        self.assertIn('hub send notes "…"', deny["response"]["message"])
        self.assertIn("into your result", deny["response"]["message"])
        self.assertFalse(minihub.asks_blocked(self.st, "notes", ASK))           # the mini-hub asks on its own

    def test_the_watchers_mark_is_refused(self):
        self.raised()
        self.ev("turn.result", "notes-ci", text="done, I need your answer: merge?")
        self.ev("turn.stopped", "notes-ci")
        why = self.ev("watch.needs_owner", "notes-ci", p=1.0, ask="merge?", channel="cli", owner=True)
        self.assertIn("notes", why)
        self.assertIsNone(self.st["sessions"]["notes-ci"].get("attention"))

    def test_the_result_goes_only_to_the_minihub(self):
        did, sub = self.raised()
        self.ev("owner.away", None, **OWNER)
        mark = self.seq
        self.ev("turn.result", "notes-ci", text="done, MR !1")
        self.ev("turn.stopped", "notes-ci")
        (report,) = [m for m in self.to("notes", mark) if sub["id"] in (m.get("origin") or "")]
        self.assertEqual([m for m in self.to("hub", mark) if sub["id"] in m["text"]], [])     # not a line to the hub
        self.assertEqual([d for d in self.st["owner"]["done"] if d["session"] == "notes-ci"], [])   # no "Done" entry
        self.assertFalse(self.st["sessions"]["notes-ci"].get("owner_out"))                       # no "new" mark

    def test_a_fallen_turn_goes_to_the_minihub_not_the_hub(self):
        did, sub = self.raised()
        mark = self.seq
        self.ev("turn.result", "notes-ci", text="", is_error=True, subtype="error_during_execution")
        self.assertEqual(len([m for m in self.to("notes", mark) if m["text"].startswith(f"STOPPED {sub['id']}")]), 1)
        self.assertEqual([m for m in self.to("hub", mark) if sub["id"] in m["text"]], [])

    def test_a_timeout_goes_to_the_minihub_not_the_hub(self):
        did, sub = self.raised()
        mark = self.seq
        self.ev("timer.fired", sub["id"], ts=self.st["delegations"][sub["id"]]["deadline"] + 1)
        (line,) = [m for m in self.to("notes", mark) if m["text"].startswith(f"TIMEOUT {sub['id']}")]
        self.assertEqual([m for m in self.to("hub", mark) if sub["id"] in m["text"]], [])

    def test_owner_words_to_an_executor_go_to_its_minihub(self):
        did, sub = self.raised()
        for extra in ({}, {"about": sub["id"]}, {"quote": "done"}, {"card": "q1"}):   # card — not its card
            mark = self.seq
            self.ev("message.received", None, to="notes-ci", text="and finish the test too", **extra, **WEB)
            (msg,) = self.to("notes", mark)
            self.assertTrue(msg["text"].startswith("The owner to executor notes-ci: and finish the test too"), msg["text"])
            self.assertTrue(msg["from_owner"])
            self.assertEqual(self.to("notes-ci", mark), [])
        mark = self.seq                                                     # the hub relaying the owner's words (--for) counts too
        self.ev("message.received", None, to="hub", text="tell notes-ci: test", **WEB)
        word = f"m{self.seq}"
        self.ev("message.received", None, to="notes-ci", text="test", **{"for": word}, **by("hub"))
        self.assertEqual(len([m for m in self.to("notes", mark + 1) if "The owner to executor notes-ci" in m["text"]]),
                         1)

    def test_an_owners_file_comment_goes_to_the_executor_its_thread_is_with(self):
        # 22.10: the owner edited an executor's spec with comments in Files (the thread is with it), but all three went to
        # the mini-hub: a reply in the thread is the turn result of whoever it reached, and the executor answered only
        # through the mini-hub's retelling
        self.raised()
        mark = self.seq
        self.ev("message.received", None, to="notes-ci", text="Owner's comment on the file `/w/spec.md`: more detail here",
                comment="abc.1", attachments=[], **WEB)
        (msg,) = self.to("notes-ci", mark)
        self.assertTrue(msg["text"].startswith("Owner's comment"), msg["text"])
        self.assertEqual(self.to("notes", mark), [])
        x = self.st["sessions"]["notes-ci"]                                 # finished and asleep — as before, to the mini-hub
        x["state"] = "retired"
        mark = self.seq
        self.ev("message.received", None, to="notes-ci", text="Owner's reply in the thread: and one more thing", comment="abc.1",
                attachments=[], **WEB)
        (msg,) = self.to("notes", mark)
        self.assertIn("and one more thing", msg["text"])
        self.assertEqual((self.to("notes-ci", mark), x["state"]), ([], "retired"))

    def test_a_file_of_an_executor_is_in_its_minihubs_feed(self):
        # 22.10: the owner talks only to the mini-hub, but an executor's file (hub send-file) landed in the executor's feed
        self.raised()
        self.ev("owner.file", None, path="/w/shots/map.png", caption="looks like this", **by("notes-ci"))
        self.run_task("hub-x", who="hub")                                   # a hub executor is outside the bus, its file stays its own
        self.start("hub-x")
        self.ev("owner.file", None, path="/w/shots/list.png", caption=None, **by("hub-x"))
        r = views.Reader()
        r.feed_all(self.events)
        files = {n: [x["path"] for x in line if x["kind"] == "file"] for n, line in r.timelines.items()}
        self.assertEqual(files.get("notes"), ["/w/shots/map.png"])
        self.assertFalse(files.get("notes-ci"))                              # not in both
        self.assertEqual([x.get("by") for x in r.timelines["notes"] if x["kind"] == "file"], ["notes-ci"])
        self.assertNotIn("by", [x for x in r.timelines["hub-x"] if x["kind"] == "file"][0])
        self.assertEqual(files.get("hub-x"), ["/w/shots/list.png"])

    def test_a_file_of_an_executors_schedule_job_is_in_both_feeds(self):
        # PR #743: a job's file is news for the hub feed as `hub notify` from a job is; the mini-hub's feed keeps its row too
        self.raised()
        self.ev("owner.file", None, path="/w/report.pdf", caption="ready", schedule_job="monthly", **by("notes-ci"))
        r = views.Reader()
        r.feed_all(self.events)
        rows = {n: [x for x in line if x["kind"] == "file"] for n, line in r.timelines.items()}
        self.assertEqual([x.get("by") for x in rows["notes"]], ["notes-ci"])
        self.assertEqual([x.get("by") for x in rows["hub"]], ["notes-ci"])
        self.assertFalse(rows.get("notes-ci"))

    def test_others_still_write_to_the_executor(self):
        self.raised()
        mark = self.seq
        self.ev("message.received", None, to="notes-ci", text="clarify", **by("notes"))
        self.assertEqual(len(self.to("notes-ci", mark)), 1)
        self.ev("message.received", None, to="notes-ci", text="/compact", **WEB)   # the owner's command goes to the session itself
        self.assertEqual(len(self.to("notes-ci", mark)), 2)

    def test_a_minihubs_word_to_its_working_executor_goes_in_between_its_steps(self):
        self.raised()
        self.ev("message.received", None, to="notes-ci", text="correction: no migration", **by("notes"))
        self.assertTrue(self.st["messages"][f"m{self.seq}"].get("injected"))
        self.ev("message.received", None, to="notes-ci", text="how are you?", **by("docs"))   # someone else's — as before
        self.assertFalse(self.st["messages"][f"m{self.seq}"].get("injected"))

    def test_the_task_text_sends_decisions_to_the_minihub(self):
        did, sub = self.raised()
        text = self.st["messages"][sub["message"]]["text"]
        self.assertNotIn("AskUserQuestion", text)
        self.assertIn('hub send notes "…"', text)
        self.assertIsNone(self.run_task("hub-x", who="hub"))
        self.start("hub-x")
        (hx,) = [d for d in self.st["delegations"].values() if d["session"] == "hub-x"]
        self.assertIn("AskUserQuestion", self.st["messages"][hx["message"]]["text"])

    def test_the_spawn_names_the_minihub(self):
        self.raised()
        from core import reducer
        action = reducer.spawn_action(self.st["sessions"]["notes-ci"], st=self.st)
        self.assertEqual(action.get("bus"), "notes")
        self.assertNotIn("bus", reducer.spawn_action(self.st["sessions"]["notes"], st=self.st))

    def test_the_owners_own_turn_on_the_executor_is_his(self):
        # review high: wake and a command are the owner's action on the session itself; the answer to them goes to the owner, as usual
        did, sub = self.raised()
        self.ev("turn.result", "notes-ci", text="done")
        self.ev("turn.stopped", "notes-ci")
        self.ev("message.received", None, to="notes-ci", text="/compact", **WEB)
        self.ev("turn.started", "notes-ci", marker=f"m{self.seq}")
        self.ev("owner.away", None, **OWNER)
        self.ev("turn.result", "notes-ci", text="compacted")
        self.assertTrue(self.st["sessions"]["notes-ci"].get("owner_out"))
        self.assertEqual([d["text"] for d in self.st["owner"]["done"] if d["session"] == "notes-ci"], ["compacted"])

    def test_a_direction_made_of_an_executor_is_out_of_the_bus(self):
        self.raised()
        self.ev("turn.result", "notes-ci", text="done")
        self.ev("turn.stopped", "notes-ci")
        self.ev("task.backlog", None, slug="ci", session="notes-ci", **OWNER)   # became a direction
        self.assertIsNone(minihub.boss(self.st, self.st["sessions"]["notes-ci"]))
        self.assertNotIn("notes", minihub.waits(self.st))
        self.st["sessions"]["notes-ci"]["minihub_since"] = self.seq             # the direction is itself a mini-hub
        self.assertIsNone(self.run_task("notes-ci-x", who="notes-ci"))
        self.start("notes-ci-x")
        self.assertEqual(self.st["sessions"]["notes-ci-x"].get("bus"), "notes-ci")   # not the former mini-hub

    def test_a_revived_executor_keeps_the_mark(self):
        self.raised()
        old = self.st["sessions"]["notes-ci"]
        self.ev("session.remove_requested", "notes-ci", **OWNER)
        self.ev("session.exited", "notes-ci")
        self.ev("session.requested", "notes-ci", session_id=old["session_id"], cwd=old["cwd"], revive=True, **OWNER)
        self.assertEqual(self.st["sessions"]["notes-ci"].get("bus"), "notes")


class Host(unittest.TestCase):
    """A mini-hub's executor at launch: the owner form is closed by a Claude Code rule, the note names the mini-hub."""

    def test_the_form_is_denied_and_the_note_names_the_minihub(self):
        import tempfile
        from tests.test_minihub import Host as MinihubHost
        spawn = MinihubHost.spawn
        cwd = tempfile.mkdtemp(prefix="minihub-bus-")
        deny, prompt = spawn(self, cwd, bus="notes")
        self.assertIn("AskUserQuestion", deny)
        self.assertIn(minihub.BUS_NOTE.format(boss="notes"), prompt)
        deny, prompt = spawn(self, cwd)
        self.assertNotIn("AskUserQuestion", deny)
        self.assertNotIn("an executor of the mini-hub", prompt)


class Queued(Base):
    """Visible queue (owner decision 2026-10-16, approved the mockup): a waiting request is a line under the mini-hub,
    the mini-hub's status is "executors: N working, K queued" (see waits_label)."""

    def test_snapshot_shows_the_queue_and_the_minihubs_wait(self):
        self.minihub(1)
        did, mid = self.task()
        self.turn("notes", mid, "delegated to two", runs=["notes-a", "notes-b"])
        self.start("notes-a")
        snap = self.snapshot()
        row = next(s for s in snap["sessions"] if s["name"] == "notes")
        self.assertEqual(row["label"], "executors: 1 working, 1 queued")
        self.assertEqual(row["status"], "idle")
        (q,) = snap["queue"]
        self.assertEqual((q["name"], q["by"], q["pos"], q["wake"]), ("notes-b", "notes", 1, False))
        self.assertEqual(i18n.t("core.minihub.queue_place"), q["why"])   # the page's language

    def test_an_owners_direct_message_waits_too(self):
        # mockup case: the owner writes to the mini-hub directly — the turn has no task, yet it waits for the same executors
        self.minihub(1)
        self.ev("message.received", None, to="notes", text="build two reports", **WEB)
        self.turn("notes", f"m{self.seq}", "delegated to two", runs=["notes-a", "notes-b"])
        self.start("notes-a")
        label = lambda: next(s for s in self.snapshot()["sessions"] if s["name"] == "notes")["label"]   # noqa: E731
        self.assertEqual(label(), "executors: 1 working, 1 queued")
        self.st["sessions"]["notes-a"].update(state="retired")              # finished and asleep — the slot is free
        self.ev("task.run_ready", "notes-b", channel="core", owner=False)
        self.assertEqual(label(), "waiting for executors · 1 working")                            # notes-b is coming up
        self.start("notes-b")
        self.st["sessions"]["notes-b"].update(state="retired")
        self.assertEqual(label(), views.STATE_LABELS["idle"])

    def test_the_label_reads_plainly_for_every_mix_of_working_and_queued(self):
        label = minihub.waits_label
        self.assertEqual(label((2, 0)), "waiting for executors · 2 working")
        self.assertEqual(label((0, 1)), "executor queued (1)")
        self.assertEqual(label((0, 3)), "executors queued (3)")
        self.assertEqual(label((2, 1)), "executors: 2 working, 1 queued")
        self.assertIsNone(label(None))
        for old in ("+", "0 +"):
            self.assertNotIn(old, "".join(label(w) for w in ((2, 0), (0, 3), (2, 1))))
        # Russian: each count takes its own plural form of the catalog (one, few, many), not one form for all
        ru = json.loads((Path(i18n.__file__).resolve().parents[1] / "i18n" / "ru.json").read_text(encoding="utf-8"))
        i18n.use("ru")
        try:
            self.assertEqual(label((1, 0)), ru["core.minihub.waits"]["one"].format(count=1))
            self.assertEqual(label((2, 0)), ru["core.minihub.waits"]["few"].format(count=2))
            self.assertEqual([label((0, n)) for n in (1, 3, 5)],
                             [ru["core.minihub.waits_only_queued"][form].format(count=n)
                              for form, n in (("one", 1), ("few", 3), ("many", 5))])
            self.assertEqual(len({label((0, n)).split(" ")[0] for n in (1, 3, 5)}), 3)   # three different words
            self.assertNotEqual(label((1, 0)).replace("1", "2"), label((2, 0)))            # "one works", "two work"
            self.assertEqual(label((2, 1)), ru["core.minihub.waits_queued"].format(n=2, queued=1))
        finally:
            i18n.use("en")

    def test_without_the_queue_only_n(self):
        self.minihub()
        did, mid = self.task()
        self.turn("notes", mid, "delegated", runs=["notes-a"])
        self.start("notes-a")
        snap = self.snapshot()
        self.assertEqual(next(s for s in snap["sessions"] if s["name"] == "notes")["label"], "waiting for executors · 1 working")
        self.assertEqual(snap["queue"], [])

    def test_a_busy_minihub_keeps_its_label(self):
        self.minihub()
        did, mid = self.task()
        self.turn("notes", mid, "delegated", runs=["notes-a"])
        self.start("notes-a")
        self.ev("message.received", None, to="notes", text="one more question", **WEB)
        self.ev("turn.started", "notes", marker=f"m{self.seq}")
        row = next(s for s in self.snapshot()["sessions"] if s["name"] == "notes")
        self.assertEqual(row["label"], views.STATE_LABELS["busy"])

    def test_only_the_queue_of_minihubs_is_shown(self):
        self.minihub()
        self.ev("owner.settings", None, timezone="UTC", parallel=3, executors={"max_live": 1}, channel="core")
        self.assertIsNone(self.run_task("notes-a"))
        self.start("notes-a")
        self.assertIsNone(self.run_task("docs-x", who="docs"))      # not a mini-hub: the queue as before, no line
        self.assertIsNone(self.run_task("notes-b"))
        self.assertEqual((self.state_of("docs-x"), self.state_of("notes-b")), ("queued", "queued"))
        self.assertEqual([(q["name"], q["pos"]) for q in self.snapshot()["queue"]], [("notes-b", 2)])


class WakeQueued(Base):
    """Case 3: rework of a finished executor when the slots are taken is not a refusal but a core queue; it comes up on
    its own and gets the text exactly once; in the queue it shows as a line "· rework"; cancel — hub task cancel."""

    def asleep(self):
        did, sub = self.raised(n=1)
        self.ev("turn.result", "notes-ci", text="done")
        self.ev("turn.stopped", "notes-ci")
        report = f"m{self.seq - 1}x1"
        self.turn("notes", report, "not all — will wake it")
        self.st["sessions"]["notes-ci"].update(state="retired")
        self.assertIsNone(self.run_task("notes-b"))
        self.start("notes-b")
        return did, sub

    def wake(self, sub, text="finish the test"):
        return self.ev("message.received", None, to="notes-ci", text=text, wake=True, about=sub["id"], **by("notes"))

    def test_a_wake_at_the_cap_waits_in_the_queue_and_comes_once(self):
        did, sub = self.asleep()
        self.assertIsNone(self.wake(sub))
        run = self.st["runs"]["notes-ci"]
        self.assertEqual((run["state"], run["wake"]["about"]), ("queued", sub["id"]))
        self.assertEqual(self.st["sessions"]["notes-ci"]["state"], "retired")            # asleep while it waits
        self.assertEqual(minihub.waiting_on(self.st, self.st["delegations"][did]), ["notes-ci"])   # notes-b is not waiting on it
        self.assertIn("in the queue", self.wake(sub, "more"))                            # the second time — a refusal with a number
        mark = self.seq
        self.assertIn("waits until someone hands in", self.ev("task.run_ready", "notes-ci", channel="core", owner=False))   # no free slots
        x = self.st["sessions"]["notes-b"]
        x.update(state="retired")                                          # a slot was freed
        from core import executors
        self.assertEqual(executors.promotable(self.st, self.seq), ["notes-ci"])
        self.assertIsNone(self.ev("task.run_ready", "notes-ci", channel="core", owner=False))
        self.assertNotIn("notes-ci", self.st.get("runs") or {})
        (woke,) = self.to("notes-ci", mark)
        self.assertEqual(woke["text"], "finish the test")
        self.assertNotEqual(self.st["sessions"]["notes-ci"]["state"], "retired")
        self.assertEqual(self.st["delegations"][sub["id"]]["state"], "working")
        self.assertEqual(executors.promotable(self.st, self.seq), [])                 # it will not come a second time

    def test_the_queue_row_says_rework(self):
        did, sub = self.asleep()
        self.wake(sub)
        (q,) = self.snapshot()["queue"]
        self.assertEqual((q["name"], q["wake"]), ("notes-ci", True))
        self.assertEqual(q["about"], sub["id"])                      # wake stays a flag (the page), the task is its own field
        # review: `hub status` took q["wake"].get("about") of the flag and fell over with AttributeError on every status
        from core import i18n
        i18n.use("en")   # `hub status` in a session's process
        self.addCleanup(i18n.use, "en")
        self.assertEqual(minihub.queue_line(q), f"notes-ci · queued #1 · rework {sub['id']} · {q['why']}")
        self.assertEqual(minihub.queue_line({**q, "about": ""}), f"notes-ci · queued #1 · wake-up · {q['why']}")
        self.assertEqual(minihub.queue_line({**q, "wake": False, "about": ""}), f"notes-ci · queued #1 · {q['why']}")
        i18n.use("ru")   # the owner's terminal: the line and the wake's name in his language
        self.assertEqual(minihub.queue_line(q), i18n.tr("ru", "core.minihub.queue_line", name="notes-ci", pos=1, why=q["why"],
                                                        kind=" · " + i18n.tr("ru", "core.minihub.rework", about=sub["id"])))

    def test_a_wake_is_named_in_one_place(self):
        # the core, `hub` and the page name a wake alike: a "rework dN" label with a task, a plain "wake-up" without
        self.assertEqual(minihub.wake_kind("d12"), "rework d12")
        self.assertEqual(minihub.wake_kind(""), "wake-up")
        self.assertEqual(minihub.wake_kind(None), "wake-up")
        self.assertEqual(minihub.wake_kind("d12", acc=True), "rework")
        self.assertEqual(minihub.wake_kind("", acc=True), "wake-up")
        from core import coretext, i18n
        self.assertEqual(coretext.human(minihub.wake_kind("d12", acc=True), "ru"),
                         i18n.tr("ru", "core.minihub.rework_acc"))

    def test_cancel_drops_it(self):
        did, sub = self.asleep()
        self.wake(sub)
        self.assertIsNone(self.ev("task.run_cancelled", "notes-ci", **by("notes")))
        self.assertNotIn("notes-ci", self.st.get("runs") or {})
        self.assertEqual(self.st["sessions"]["notes-ci"]["state"], "retired")

    def test_the_hub_raises_it_now(self):
        did, sub = self.asleep()
        self.wake(sub)
        mark = self.seq
        self.assertIsNone(self.ev("task.run_go", "notes-ci", **by("hub")))
        self.assertEqual([m["text"] for m in self.to("notes-ci", mark)], ["finish the test"])

    def test_gone_meanwhile_tells_the_minihub(self):
        did, sub = self.asleep()
        self.wake(sub)
        self.ev("session.remove_requested", "notes-ci", **OWNER)
        self.st["sessions"]["notes-b"].update(state="retired")
        mark = self.seq
        self.assertIsNone(self.ev("task.run_ready", "notes-ci", channel="core", owner=False))
        self.assertNotIn("notes-ci", self.st.get("runs") or {})
        (line,) = [m for m in self.to("notes", mark) if "notes-ci" in m["text"]]
        self.assertEqual(line.get("for_delegation"), did)

    def test_a_wake_held_by_the_budget_is_in_the_owners_card(self):
        # with mini-hubs the budget is economy mode (slice 4, tests/test_economy.py): rework not triggered by a live word
        # waits for the reset; the hub gets not a line for each but one owner card on entering the mode
        did, sub = self.asleep()
        mark = self.seq
        self.ev("budget.mode", None, mode="economy", why="window 5 h 90%", until=10 ** 10, channel="core")
        self.assertIsNone(self.wake(sub))
        self.assertIn("limit economy", self.st["runs"]["notes-ci"]["why"])
        self.assertEqual([m for m in self.to("hub", mark) if "notes-ci" in m["text"]], [])
        self.assertEqual(len([m for m in self.to("hub", mark) if "Limit economy" in m["text"]]), 1)

    def test_owner_and_hub_wake_as_before(self):
        did, sub = self.asleep()
        self.assertIsNone(self.ev("message.received", None, to="notes-ci", text="finish", wake=True, **by("hub")))
        self.assertNotIn("notes-ci", self.st.get("runs") or {})


class Visible(Base):
    """The bus is visible (owner-stop concept, slice 2): the owner's words to a mini-hub's executor still go to the
    mini-hub only (decision 15.10), but the executor's feed shows them too, marked with whom they were passed to — 27.10
    he wrote "stop" on the executor's page, the words went to the mini-hub and nothing on that page said so."""

    def rows(self, name, mid):
        r = views.Reader()
        r.feed_all(self.events)
        return [x for x in r.timelines.get(name) or [] if x.get("id") == mid]

    def test_owner_words_to_an_executor_show_in_its_feed_marked_passed_to_the_minihub(self):
        self.raised()
        self.ev("message.received", None, to="notes-ci", text="Let's stop!", **WEB)
        mid = f"m{self.seq}"
        self.assertEqual(self.st["messages"][mid]["session"], "notes")          # delivery is as before: to the mini-hub
        (mine,) = self.rows("notes-ci", mid)
        self.assertEqual((mine["kind"], mine["text"], mine.get("relayed")), ("in", "Let's stop!", "notes"))
        self.assertEqual((mine["sender"], mine["from"]["kind"], mine["channel"]), (None, "owner", "web"))
        self.assertTrue(mine["midturn"])                                          # its turn was running
        (there,) = self.rows("notes", mid)                                        # one row per feed, not two
        self.assertTrue(there["text"].startswith("The owner to executor notes-ci: Let's stop!"), there["text"])
        self.assertNotIn("relayed", there)
        r = views.Reader()                                                         # hub show: it went to the mini-hub
        r.feed_all(self.events)
        self.assertIn(i18n.t("cli.show.owner") + " → notes: Let's stop!", "\n".join(views.show(r, "notes-ci")))

    def test_the_hubs_relay_of_his_word_is_shown_too(self):
        self.raised()
        self.ev("message.received", None, to="hub", text="tell notes-ci: test", **WEB)
        word = f"m{self.seq}"
        self.ev("message.received", None, to="notes-ci", text="test", **{"for": word}, **by("hub"))
        (row,) = self.rows("notes-ci", f"m{self.seq}")
        self.assertEqual((row["text"], row["sender"], row.get("relayed")), ("test", "hub", "notes"))

    def test_what_reaches_the_executor_itself_is_not_marked(self):
        self.raised()
        for text, extra in (("clarify", by("notes")), ("/compact", WEB)):          # its mini-hub; the owner's command
            self.ev("message.received", None, to="notes-ci", text=text, **extra)
            (row,) = self.rows("notes-ci", f"m{self.seq}")
            self.assertNotIn("relayed", row)
            self.assertEqual(self.rows("notes", f"m{self.seq}"), [])
        self.st["sessions"]["notes-ci"]["state"] = "retired"                     # handed in: his message wakes it, to it
        self.ev("message.received", None, to="notes-ci", text="finish", wake=True, **WEB)
        self.assertEqual(self.st["messages"][f"m{self.seq}"]["session"], "notes-ci")
        (row,) = self.rows("notes-ci", f"m{self.seq}")
        self.assertNotIn("relayed", row)

    def after_a_restart(self, name, mid):
        """The rows of name's feed for message mid once the core has stopped (its core.snapshot ends the journal): the
        page after a restart, an archive's parse."""
        snap = {"seq": self.seq + 1, "ts": self.events[-1]["ts"] + 1, "src": "core", "type": "core.snapshot",
                "entity": None, "data": {"why": "stop", "state": {k: v for k, v in self.st.items() if not k.startswith("_")}}}
        r = views.Reader()
        r.feed_all([*self.events, snap])
        return [x for x in r.timelines.get(name) or [] if x.get("id") == mid]

    def test_the_words_passed_to_the_minihub_stay_in_the_executors_feed_after_a_restart(self):
        # 10.11: the hub's `hub send notes-ci --now --for mN` went to the mini-hub; a restart later the executor's feed
        # (and the archive's parse — hub show notes-ci --grep) no longer had the words
        self.raised()
        self.ev("message.received", None, to="hub", text="tell notes-ci: test", **WEB)
        word = f"m{self.seq}"
        self.ev("message.received", None, to="notes-ci", text="test", now=True, **{"for": word}, **by("hub"))
        mid = f"m{self.seq}"
        (row,) = self.after_a_restart("notes-ci", mid)
        self.assertEqual((row["text"], row.get("relayed")), ("test", "notes"))
        (there,) = self.after_a_restart("notes", mid)
        self.assertNotIn("relayed", there)

    def test_words_on_a_task_stay_in_the_feed_of_whoever_gave_it_after_a_restart(self):
        # the mirror of words on a task for the owner's word (views: "the owner's word is also with the hub")
        self.ev("message.received", None, to="hub", text="check the docs", **WEB)
        word = f"m{self.seq}"
        self.ev("delegation.created", None, session="api-x", **{"for": word}, context="T", **by("hub"))
        did = f"d{self.seq}"
        self.ev("message.received", None, to="api-x", text="and the readme", reply_to=did, **by("hub"))
        mid = f"m{self.seq}"
        self.assertEqual(self.st["messages"][mid]["session"], "api-x")
        (mirror,) = self.rows("hub", mid)
        self.assertEqual(mirror.get("to"), "api-x")
        self.assertEqual(self.after_a_restart("hub", mid), [mirror])
        self.assertEqual(len(self.after_a_restart("api-x", mid)), 1)

    def test_the_copies_stay_after_a_restart_when_the_session_they_name_was_renamed(self):
        # a rename rewrites the message's session, not the name the copy's row carries ("to", "relayed")
        self.raised()
        self.ev("message.received", None, to="notes-ci", text="test", **WEB)
        relayed = f"m{self.seq}"
        self.ev("message.received", None, to="hub", text="check the docs", **WEB)
        word = f"m{self.seq}"
        self.ev("delegation.created", None, session="api-x", **{"for": word}, context="T", **by("hub"))
        did = f"d{self.seq}"
        self.ev("message.received", None, to="api-x", text="and the readme", reply_to=did, **by("hub"))
        mirror = f"m{self.seq}"
        for old, new in (("notes", "notes-2"), ("api-x", "api-y")):
            # a free session is renamed at once, not when it frees up: it takes what waits for it
            while got := [m["id"] for m in self.st["messages"].values() if m["session"] == old
                          and m["state"] in ("queued", "typing")]:
                self.turn(old, got[0], "noted")
            self.assertIsNone(self.ev("session.renamed", old, to=new, **OWNER))
        self.assertEqual((self.st["messages"][relayed]["session"], self.st["messages"][mirror]["session"]),
                         ("notes-2", "api-y"))
        self.assertEqual(len(self.after_a_restart("notes-ci", relayed)), 1)
        self.assertEqual(len(self.after_a_restart("hub", mirror)), 1)

    def test_a_row_the_core_wrongly_gave_a_session_still_leaves_after_a_restart(self):
        self.raised()
        self.ev("message.received", None, to="notes-ci", text="clarify", **by("notes"))
        mid = f"m{self.seq}"
        r = views.Reader()
        r.feed_all(self.events)
        (row,) = [x for x in r.timelines["notes-ci"] if x.get("id") == mid]
        for stray in (row, {**row, "relayed": "docs"}, {**row, "to": "docs"}):   # a copy that names another owner too
            r.timelines.setdefault("api-x", []).append(dict(stray))
        r._drop_strays()
        self.assertEqual([x for x in r.timelines["api-x"] if x.get("id") == mid], [])

    def test_snapshot_names_whom_the_owners_words_go_to(self):
        self.raised()
        self.run_task("hub-x", who="hub")                                        # a hub executor is outside the bus
        self.start("hub-x")
        relay = {s["name"]: s.get("relay") for s in self.snapshot()["sessions"]}
        self.assertEqual((relay["notes-ci"], relay["notes"], relay["hub-x"]), ("notes", None, None))


class PassedToTheMinihub(Base):
    """The owner's words to a mini-hub's executor go to the mini-hub (decision 15.10), so the mini-hub must see that the
    core did not deliver them to the executor and how to pass them on: a hub relayed the owner's words with --now to a
    sleeping executor, the mini-hub got only "The owner to executor X: …", took it for a copy ("the hub woke it itself")
    and ended its turn with "—"; the executor slept four more hours."""

    asleep = WakeQueued.asleep

    def relay(self, text):
        self.ev("message.received", None, to="hub", text="the fan still runs", **WEB)
        word = f"m{self.seq}"
        self.assertIsNone(self.ev("message.received", None, to="notes-ci", text=text, now=True,
                                  **{"for": word}, **by("hub")))
        return f"m{self.seq}"

    def test_an_urgent_relay_to_a_sleeping_executor_tells_the_minihub_to_wake_it(self):
        did, sub = self.asleep()
        before = len(self.st["messages"])
        mid = self.relay("Owner: the fan still runs")
        msg = self.st["messages"][mid]
        self.assertEqual(msg["session"], "notes")                                 # the bus, as before
        self.assertEqual(len(self.st["messages"]), before + 2)                    # no copy, nothing lost
        self.assertEqual(self.st["sessions"]["notes-ci"]["state"], "retired")     # the core does not wake it itself
        text = msg.get("routing_note") or ""                                     # typed after the words (_typed)
        self.assertIn("did not deliver", text)
        self.assertIn(f'hub session wake notes-ci --about {sub["id"]} "', text)
        self.assertNotIn("hub send notes-ci", text)
        # the command of the note, run by the mini-hub, is accepted: a mini-hub wakes only with --about its task
        self.assertIsNone(self.ev("message.received", None, to="notes-ci", text="the fan still runs, check it",
                                  wake=True, about=sub["id"], **by("notes")))
        self.assertIsNotNone(self.ev("message.received", None, to="notes-ci", text="the fan still runs, check it",
                                     wake=True, **by("notes")))           # without --about — refused, as the note avoids

    def test_with_no_task_of_its_own_to_rework_the_minihub_is_sent_to_the_hub(self):
        did, sub = self.asleep()
        self.st["delegations"][sub["id"]]["state"] = "archived"   # its task went to the archive: a mini-hub's wake is refused
        mid = self.relay("Owner: the fan still runs")
        text = self.st["messages"][mid].get("routing_note") or ""
        self.assertNotIn("hub session wake notes-ci", text)
        self.assertIn('hub send hub "wake notes-ci with hub session wake', text)   # the hub wakes it, no ping-pong

    def test_words_to_a_working_executor_tell_the_minihub_to_pass_them_on(self):
        self.raised()
        self.ev("message.received", None, to="notes-ci", text="Let's stop!", **WEB)
        msg = self.st["messages"][f"m{self.seq}"]
        self.assertEqual(msg["session"], "notes")
        self.assertIn("did not deliver", msg.get("routing_note") or "")
        self.assertIn('hub send notes-ci "', msg["routing_note"])
        self.assertEqual(msg["text"].count("Let's stop!"), 1)

    hub_cli = None

    def said(self, mid):
        """What `hub send` printed after the core folded message mid (bin/hub busy_note); bin/hub is loaded once."""
        import contextlib
        import importlib.machinery
        import importlib.util
        import io
        import os
        cls = type(self)
        if cls.hub_cli is None:
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")
            loader = importlib.machinery.SourceFileLoader("hub_cli", path)
            cls.hub_cli = importlib.util.module_from_spec(importlib.util.spec_from_loader("hub_cli", loader))
            loader.exec_module(cls.hub_cli)
        hub = cls.hub_cli
        was = hub.current, hub.ME, hub.REJECTED
        hub.current, hub.ME, hub.REJECTED = (lambda: self.st), "hub", []
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                hub.busy_note(int(mid[1:]))
        finally:
            hub.current, hub.ME, hub.REJECTED = was   # the module outlives the test: it keeps none of its state
        return out.getvalue()

    def test_the_sender_learns_where_the_words_went(self):
        self.asleep()
        mid = self.relay("Owner: the fan still runs")
        said = self.said(mid)
        self.assertIn(f"{mid} went to notes, not to notes-ci", said)   # was: "accepted" and nothing else
        self.assertIn("it passes them on", said)
        # the wake is the mini-hub's (its note): a wake hint to the sender too would bring the executor the words twice
        self.assertNotIn("hub session wake", said)

    def test_off_the_bus_the_sender_line_is_as_before(self):
        """A message (or an FYI) from the hub to a sleeping executor without the owner's words goes to whoever raised it
        with its own note, not over the bus: hub send prints no new line."""
        self.asleep()
        for extra in ({}, {"fyi": True}):
            self.assertIsNone(self.ev("message.received", None, to="notes-ci", text="a question", **extra, **by("hub")))
            mid = f"m{self.seq}"
            self.assertEqual(self.st["messages"][mid]["session"], "notes")
            self.assertIsNone(self.st["messages"][mid].get("relayed_for"))
            self.assertNotIn("went to", self.said(mid))


if __name__ == "__main__":
    unittest.main()
