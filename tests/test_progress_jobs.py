"""Milestone jobs (`hub schedule add --id progress-<name> --command … hub notify …`): the core makes them reliable, the
text of the rule to sessions does not. A check of a week of milestones (3 of 38 stood after the work was done, 43% of all
notices were repeats, one job wrote "no data" 27 times in a row, the first notice came only after one interval):
  * a new progress job runs once at once — the first notice is at the start, not after an interval;
  * a progress job whose notices repeat, or say "no data", several times in a row is removed by the core and its setter
    hears one line (not the owner);
  * a progress job of a removed session is removed.
Other command jobs (backups, watchers) are not touched by any of this."""
import tests  # noqa: F401 — keep the machine's settings out
import unittest

from core import i18n, langdata, schedule
from tests.test_replays import run

OWNER = {"channel": "cli", "owner": True}
NO_DATA = langdata.get("ru", "words", "no_data")   # the words a source-less milestone says (the rule to sessions is Russian)


def _as(sender):
    return {"channel": "session", "owner": False, "sender": sender}


def job_notice(job, text, **extra):
    """What `hub notify` files from a command job (bin/hub speaker): the owner's terminal, the job named."""
    return {"type": "owner.notify", "data": {"id": job, "title": "Progress", "text": text, "channel": "cli",
                                              "owner": True, "schedule_job": job, **extra}}


class Case(unittest.TestCase):
    def events(self, *extra, job="progress-gen", command="echo ok"):
        base = [{"t": 0, "type": "owner.settings", "data": {"timezone": "UTC"}}]
        for name, kind in (("hub", "hub"), ("lead", None)):
            base += [{"t": 0, "type": "session.requested", "entity": name,
                      "data": {"cwd": "/w", "session_id": f"sid-{name}", **({"kind": kind} if kind else OWNER)}},
                     {"t": 1, "type": "session.started", "entity": name, "data": {"session_id": f"sid-{name}"}}]
        base += [{"t": 2, "type": "schedule.set", "entity": job,
                  "data": {"every": "30m", "command": command, **_as("lead")}}]
        out = []
        for i, e in enumerate(extra):
            out.append({"t": 100 + i, **e})   # an item may bring its own "t"
        return base + out

    def notes(self, st, to):
        return [m for m in st["messages"].values() if m["session"] == to and "progress-gen" in m["text"]]


class FirstNoticeAtOnce(Case):
    def test_a_new_progress_job_gets_a_run_at_once(self):
        st, _, rejected = run(self.events())
        job = st["schedule"]["progress-gen"]
        self.assertEqual(rejected, 0)
        self.assertEqual(job.get("run_slot"), 2)   # the clock launches what has a run_slot without a result
        self.assertNotIn("ran_slot", job)

    def test_the_launch_and_its_result_are_recorded_as_for_any_run(self):
        st, _, rejected = run(self.events(
            {"type": "schedule.started", "entity": "progress-gen", "data": {"slot": 2}},
            {"type": "schedule.ran", "entity": "progress-gen", "data": {"slot": 2, "exit": 0, "took": 0.1}}))
        job = st["schedule"]["progress-gen"]
        self.assertEqual((rejected, job["ran_slot"], job["last_exit"]), (0, 2, 0))

    def test_the_regular_slots_go_on_after_it(self):
        st, _, rejected = run(self.events(
            {"type": "schedule.ran", "entity": "progress-gen", "data": {"slot": 2, "exit": 0}},
            {"t": 1900, "type": "schedule.due", "entity": "progress-gen", "data": {"slot": 1802}}))
        self.assertEqual((rejected, st["schedule"]["progress-gen"]["run_slot"]), (0, 1802))

    def test_other_command_jobs_are_not_run_when_set(self):
        st, _, _ = run(self.events(job="nightly-backup"))
        self.assertNotIn("run_slot", st["schedule"]["nightly-backup"])

    def test_a_session_job_is_not_run_when_set(self):
        evs = self.events()
        evs.append({"t": 3, "type": "schedule.set", "entity": "progress-x",
                    "data": {"every": "30m", "session": "lead", "prompt": "p", **_as("hub")}})
        st, _, _ = run(evs)
        self.assertNotIn("run_slot", st["schedule"]["progress-x"])

    def test_the_same_set_again_and_an_extension_do_not_run_it_again(self):
        again = {"type": "schedule.set", "entity": "progress-gen",
                 "data": {"every": "30m", "command": "echo ok", "until": "1970-01-20", **_as("lead")}}
        st, _, rejected = run(self.events(
            {"type": "schedule.ran", "entity": "progress-gen", "data": {"slot": 2, "exit": 0}}, again))
        self.assertEqual((rejected, st["schedule"]["progress-gen"]["run_slot"]), (0, 2))   # still the first run, done

    def test_a_changed_command_is_run_at_once(self):
        st, _, _ = run(self.events(
            {"type": "schedule.ran", "entity": "progress-gen", "data": {"slot": 2, "exit": 0}},
            {"type": "schedule.set", "entity": "progress-gen",
             "data": {"every": "30m", "command": "echo changed", **_as("lead")}}))
        job = st["schedule"]["progress-gen"]
        self.assertEqual((job["run_slot"], job.get("ran_slot")), (100 + 1, 2))

    def test_a_set_again_after_the_removal_is_run(self):
        st, _, _ = run(self.events(
            {"type": "schedule.ran", "entity": "progress-gen", "data": {"slot": 2, "exit": 0}},
            {"type": "schedule.remove_requested", "entity": "progress-gen", "data": _as("lead")},
            {"type": "schedule.set", "entity": "progress-gen",
             "data": {"every": "30m", "command": "echo ok", **_as("lead")}}))
        self.assertEqual(st["schedule"]["progress-gen"]["run_slot"], 102)


class RepeatsAreRemoved(Case):
    LIMIT = getattr(schedule, "MILESTONE_LIMIT", 0)

    def test_the_limit_is_four(self):
        self.assertEqual(self.LIMIT, 4)

    def feed(self, texts, job="progress-gen"):
        return run(self.events(*[job_notice(job, t) for t in texts], job=job))

    def test_the_same_text_in_a_row_removes_the_job_and_tells_the_setter(self):
        st, _, rejected = self.feed(["3 of 10"] * self.LIMIT)
        self.assertEqual(rejected, 0)
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "removed")
        (note,) = self.notes(st, "lead")
        self.assertIn("progress-gen", note["text"])
        self.assertIn(str(self.LIMIT), note["text"])
        self.assertEqual(self.notes(st, "hub"), [])   # the owner's own sessions: nobody else is told

    def test_one_less_leaves_it(self):
        st, _, _ = self.feed(["3 of 10"] * (self.LIMIT - 1))
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "active")
        self.assertEqual(self.notes(st, "lead"), [])

    def test_a_change_starts_the_count_again(self):
        st, _, _ = self.feed(["3 of 10"] * 3 + ["4 of 10"] + ["4 of 10"] * 2)
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "active")

    def test_spaces_do_not_make_a_text_different(self):
        st, _, _ = self.feed(["3 of 10", "3  of 10 ", " 3 of\n10", "3 of 10"])
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "removed")

    def test_no_data_in_a_row_removes_it_even_when_the_text_differs(self):
        st, _, _ = self.feed([f"gen: {NO_DATA} ({i})" for i in range(self.LIMIT)])
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "removed")
        (note,) = self.notes(st, "lead")
        self.assertIn("progress-gen", note["text"])

    def test_the_english_words_count_too_and_the_note_says_which_reading_it_was(self):
        st, _, _ = self.feed([f"batch {i}: No data." for i in range(self.LIMIT)])
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "removed")
        (note,) = self.notes(st, "lead")
        self.assertIn("no data", note["text"])   # the English text of the hub (core.schedule.milestone_empty)
        self.assertNotIn("without change", note["text"])

    def test_the_same_no_data_text_is_reported_as_no_data(self):
        st, _, _ = self.feed([f"gen: {NO_DATA}"] * self.LIMIT)
        (note,) = self.notes(st, "lead")
        self.assertIn("no data", note["text"])

    def test_a_change_at_the_end_of_a_long_text_is_a_change(self):
        st, _, _ = self.feed([("x" * 600) + str(i) for i in range(self.LIMIT + 1)])
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "active")

    def test_words_that_only_contain_the_phrase_are_not_an_empty_reading(self):
        st, _, _ = self.feed([f"piano data {i}" for i in range(self.LIMIT + 2)])
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "active")

    def test_no_data_for_one_source_inside_a_healthy_line_is_not_an_empty_reading(self):
        # a milestone with real progress that also mentions one source without data: the work goes on, the job stays
        lines = [f"batch {i}: {i * 40} of 500 files done, one of three sources had no data, about 20 min left"
                 for i in range(self.LIMIT + 2)]
        st, _, _ = self.feed(lines)
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "active")
        self.assertEqual(self.notes(st, "lead"), [])
        st, _, _ = self.feed([f"{i * 40} of 500 done; {NO_DATA} for the source B; the rest is on schedule, 20 min left"
                              for i in range(self.LIMIT + 2)])
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "active")

    def test_a_line_of_changing_numbers_that_mentions_no_data_stays(self):
        st, _, _ = self.feed([f"{i * 120} of 500 done, {NO_DATA} for {i + 1} sources" for i in range(self.LIMIT + 2)])
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "active")

    def test_one_notice_with_data_ends_the_no_data_row(self):
        texts = [NO_DATA] * 3 + ["2 of 9"] + [NO_DATA] * 3
        st, _, _ = self.feed(texts)
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "active")

    def test_a_notice_of_another_job_is_not_counted(self):
        evs = self.events(job_notice("progress-gen", "same"), job_notice("other", "same"), job_notice("progress-gen", "same"),
                          job_notice("other", "same"), job_notice("progress-gen", "same"))
        st, _, _ = run(evs)
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "active")

    def test_other_command_jobs_are_never_removed_by_their_notices(self):
        st, _, _ = self.feed(["same"] * (self.LIMIT + 3), job="nightly-backup")
        self.assertEqual(st["schedule"]["nightly-backup"]["state"], "active")

    def test_a_manual_notice_without_a_job_changes_nothing(self):
        evs = self.events(*[{"type": "owner.notify", "data": {"id": "progress-gen", "text": "same", **OWNER}}] * 6)
        st, _, rejected = run(evs)
        self.assertEqual((rejected, st["schedule"]["progress-gen"]["state"]), (0, "active"))

    def test_a_removed_job_stays_quiet_on_later_notices(self):
        st, _, rejected = self.feed(["same"] * (self.LIMIT + 2))
        self.assertEqual(rejected, 0)
        self.assertEqual(len(self.notes(st, "lead")), 1)

    def test_a_new_command_starts_the_count_again(self):
        evs = self.events(*[job_notice("progress-gen", "same")] * 3,
                          {"type": "schedule.set", "entity": "progress-gen",
                           "data": {"every": "30m", "command": "echo changed", **_as("lead")}},
                          job_notice("progress-gen", "same"))
        st, _, _ = run(evs)
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "active")

    def test_a_setter_who_is_asleep_is_not_woken_the_hub_hears(self):
        evs = self.events(*[{"type": "session.close_requested", "entity": "lead", "data": OWNER},
                            {"type": "turn.result", "entity": "lead", "data": {"text": "HANDOFF-DONE\nok"}},
                            *[job_notice("progress-gen", "same")] * self.LIMIT])
        st, _, _ = run(evs)
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "removed")
        self.assertEqual(self.notes(st, "lead"), [])
        (note,) = self.notes(st, "hub")
        self.assertIn("lead", note["text"])

    def test_the_text_follows_the_language_of_the_hub(self):
        en = i18n.tr("en", "core.schedule.milestone_same", id="progress-x", n=4)
        ru = i18n.tr("ru", "core.schedule.milestone_same", id="progress-x", n=4)
        self.assertIn("progress-x", en)
        self.assertNotEqual(en, ru)


class GoneSetter(Case):
    def test_the_progress_jobs_of_a_removed_session_are_removed(self):
        st, _, rejected = run(self.events(
            {"type": "session.close_requested", "entity": "lead", "data": OWNER},
            {"type": "turn.result", "entity": "lead", "data": {"text": "HANDOFF-DONE\nok"}},
            {"type": "session.remove_requested", "entity": "lead", "data": OWNER}))
        self.assertEqual((rejected, st["sessions"]["lead"]["state"]), (0, "removed"))
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "removed")

    def test_another_command_of_that_session_runs_on(self):
        evs = self.events(
            {"type": "schedule.set", "entity": "lead-mon", "data": {"every": "1h", "command": "true", **_as("lead")}},
            {"type": "session.close_requested", "entity": "lead", "data": OWNER},
            {"type": "turn.result", "entity": "lead", "data": {"text": "HANDOFF-DONE\nok"}},
            {"type": "session.remove_requested", "entity": "lead", "data": OWNER})
        st, _, _ = run(evs)
        self.assertEqual(st["schedule"]["lead-mon"]["state"], "active")

    def test_the_progress_job_of_another_session_stays(self):
        evs = self.events(
            {"type": "schedule.set", "entity": "progress-other", "data": {"every": "30m", "command": "true", **_as("hub")}},
            {"type": "session.close_requested", "entity": "lead", "data": OWNER},
            {"type": "turn.result", "entity": "lead", "data": {"text": "HANDOFF-DONE\nok"}},
            {"type": "session.remove_requested", "entity": "lead", "data": OWNER})
        st, _, _ = run(evs)
        self.assertEqual(st["schedule"]["progress-other"]["state"], "active")

    def test_a_job_handed_to_another_session_follows_that_session(self):
        evs = self.events(
            {"type": "schedule.retarget", "entity": "progress-gen", "data": {"session": "hub", **OWNER}},
            {"type": "session.close_requested", "entity": "lead", "data": OWNER},
            {"type": "turn.result", "entity": "lead", "data": {"text": "HANDOFF-DONE\nok"}},
            {"type": "session.remove_requested", "entity": "lead", "data": OWNER})
        st, _, _ = run(evs)
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "active")


def ran(slot, exit=0, job="progress-gen"):
    return {"type": "schedule.ran", "entity": job, "data": {"slot": slot, "exit": exit, "took": 0.1}}


def due(slot, job="progress-gen"):
    return {"t": slot + 1, "type": "schedule.due", "entity": job, "data": {"slot": slot}}


SLOTS = [2 + 1800 * i for i in range(8)]   # the first run at the set (t=2), then every 30 min


class SilentRuns(Case):
    """A run of a milestone that sent the owner nothing (the source printed nothing, `hub notify` refused an empty
    text): the owner sees silence. A check of a week (12.11): one job ran six times in three hours without a single
    notice, and nobody heard of it."""
    LIMIT = getattr(schedule, "MILESTONE_LIMIT", 4)

    def runs(self, *notices_at, n=1, exit=0, job="progress-gen", command="echo ok"):
        """n runs from the first slot; a notice in the runs whose number is in notices_at."""
        evs = []
        for i, slot in enumerate(SLOTS[:n]):
            if i:
                evs.append(due(slot, job))
            if i in notices_at:
                evs.append({"t": slot + 2, **job_notice(job, f"{i} of 10")})
            evs.append({"t": slot + 3, **ran(slot, exit, job)})
        return run(self.events(*evs, job=job, command=command))

    def quiet(self, st, to="lead"):
        return [m for m in self.notes(st, to) if "removed" not in m["text"]]

    def test_a_run_without_a_notice_tells_the_setter(self):
        st, _, rejected = self.runs()
        self.assertEqual(rejected, 0)
        (note,) = self.quiet(st)
        self.assertIn("progress-gen", note["text"])
        self.assertIn("hub notify", note["text"])
        self.assertIn("source by hand", note["text"])   # not the whole command: its hub notify from a session is refused
        self.assertNotIn("command by hand", note["text"])
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "active")

    def test_a_run_with_its_notice_says_nothing(self):
        st, _, _ = self.runs(0, 1, 2, n=3)
        self.assertEqual(self.notes(st, "lead"), [])

    def test_the_setter_hears_it_once_a_row(self):
        st, _, _ = self.runs(n=self.LIMIT - 1)
        self.assertEqual(len(self.quiet(st)), 1)

    def test_a_failed_run_without_a_notice_says_its_code(self):
        st, _, _ = self.runs(exit=2)
        (note,) = self.quiet(st)
        self.assertIn("2", note["text"])

    def test_silent_runs_in_a_row_remove_the_job(self):
        st, _, _ = self.runs(n=self.LIMIT)
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "removed")
        self.assertEqual(len([m for m in self.notes(st, "lead") if "removed" in m["text"]]), 1)

    def test_a_notice_ends_the_row(self):
        st, _, _ = self.runs(2, n=self.LIMIT + 2)
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "active")
        self.assertEqual(len(self.quiet(st)), 2)   # the row before the notice and the one after it

    def test_an_unknown_outcome_is_not_counted(self):
        st, _, _ = run(self.events({"type": "schedule.ran", "entity": "progress-gen", "data": {"slot": 2, "exit": "unknown"}}))
        self.assertEqual(self.notes(st, "lead"), [])

    def test_other_command_jobs_are_not_watched(self):
        st, _, _ = self.runs(n=self.LIMIT + 1, job="nightly-backup")
        self.assertEqual(st["schedule"]["nightly-backup"]["state"], "active")
        self.assertEqual([m for m in st["messages"].values() if "nightly-backup" in m["text"]], [])


class WorkGoesOn(Case):
    """The same text four times is a stuck milestone only when nothing works for it: a check of a week (12.11) — six of
    18 removals hit a long step with a flat number, and the setter set each one again the same minute. "Works" is a fact
    of the core tied to the milestone: a task or a `hub run` its setter started in a turn that set it is still going.
    The setter being in a turn, some other executor of it working, or a background command is not: a lead with many
    tasks always has some (that kept seven of nine milestones of finished work), a Monitor outlives the work."""
    LIMIT = getattr(schedule, "MILESTONE_LIMIT", 4)

    def events(self, *extra, job="progress-gen", command="echo ok", task=True):
        """The lead, in a turn on the owner's word, hands a task to ex and sets the milestone, then ends the turn."""
        base = [{"t": 0, "type": "owner.settings", "data": {"timezone": "UTC"}}]
        for name, kind in (("hub", "hub"), ("lead", None), ("ex", None), ("other", None)):
            base += [{"t": 0, "type": "session.requested", "entity": name,
                      "data": {"cwd": "/w", "session_id": f"sid-{name}", **({"kind": kind} if kind else OWNER)}},
                     {"t": 1, "type": "session.started", "entity": name, "data": {"session_id": f"sid-{name}"}}]
        n = len(base) + 1   # the owner's message is the next event: m<n>
        base += [{"t": 2, "type": "message.received", "data": {"to": "lead", "text": "run the long job", **OWNER}},
                 {"t": 2, "type": "turn.started", "entity": "lead", "data": {"marker": f"m{n}"}}]
        self.task_message = f"m{len(base) + 1}"   # the task's message to ex is named by its event (core/delegations.py)
        if task:
            base.append({"t": 2, "type": "delegation.created", "data": {"session": "ex", "context": "the long part",
                                                                        **_as("lead")}})
        base += [{"t": 2, "type": "schedule.set", "entity": job, "data": {"every": "30m", "command": command, **_as("lead")}},
                 {"t": 3, "type": "turn.result", "entity": "lead", "data": {"text": "started"}},
                 {"t": 3, "type": "turn.stopped", "entity": "lead", "data": {}}]
        return base + [{"t": 100 + i, **e} for i, e in enumerate(extra)]

    def same(self, n):
        return [job_notice("progress-gen", "3 of 10")] * n

    def handed_in(self):
        return [{"type": "turn.started", "entity": "ex", "data": {"marker": self.task_message}},
                {"type": "turn.result", "entity": "ex", "data": {"text": "done"}},
                {"type": "turn.stopped", "entity": "ex", "data": {}}]

    def test_the_task_of_its_turn_keeps_it(self):
        st, _, rejected = run(self.events(*self.same(self.LIMIT + 2)))
        self.assertEqual(rejected, 0)
        job = st["schedule"]["progress-gen"]
        (d,) = st["delegations"].values()
        self.assertEqual((d["root"], d["state"]), (job.get("root"), "working"))   # the link the rule reads
        self.assertEqual(job["state"], "active")
        self.assertEqual(self.notes(st, "lead"), [])

    def test_without_the_task_it_is_removed_as_before(self):
        st, _, _ = run(self.events(*self.same(self.LIMIT), task=False))
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "removed")

    def test_a_task_of_another_turn_does_not_keep_it(self):
        later = [{"type": "message.received", "data": {"to": "lead", "text": "another thing", **OWNER}}]
        st, _, _ = run(self.events(*later, task=False))
        n = len(self.events(task=False)) + 1
        evs = self.events(*later, {"type": "turn.started", "entity": "lead", "data": {"marker": f"m{n}"}},
                          {"type": "delegation.created", "data": {"session": "other", "context": "x", **_as("lead")}},
                          *self.same(self.LIMIT), task=False)
        st, _, rejected = run(evs)
        self.assertEqual(rejected, 0)
        self.assertEqual(len([d for d in st["delegations"].values() if d["state"] == "working"]), 1)
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "removed")

    def test_the_setter_in_a_turn_does_not_keep_it(self):
        busy = {"type": "turn.started", "entity": "lead", "data": {"marker": None}}
        st, _, _ = run(self.events(busy, *self.same(self.LIMIT), task=False))
        self.assertEqual(st["sessions"]["lead"]["state"], "busy")
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "removed")

    def test_a_background_command_of_the_setter_does_not_keep_it(self):
        bg = {"type": "session.background", "entity": "lead",
              "data": {"tasks": [{"id": "b1", "kind": "bash", "title": "monitor"}]}}
        st, _, _ = run(self.events(bg, *self.same(self.LIMIT), task=False))
        self.assertTrue(st["sessions"]["lead"]["background"])
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "removed")

    def hub_run(self):
        """A `hub run` the lead starts in the turn of the milestone, right after setting it."""
        return {"t": 2, "type": "run.requested", "entity": "lead",
                "data": {"command": "make all", "cwd": "/w", "until_at": 2 + 3600, **_as("lead")}}

    def test_failing_silent_runs_are_removed_with_the_task_held(self):
        evs = []
        for i, slot in enumerate(SLOTS[:self.LIMIT]):
            evs += ([due(slot)] if i else []) + [{"t": slot + 3, **ran(slot, exit=1)}]
        st, _, _ = run(self.events(*evs))
        self.assertEqual(next(iter(st["delegations"].values()))["state"], "working")
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "removed")

    def test_a_job_of_an_older_core_keeps_its_root_when_set_again(self):
        n = len(self.events()) + 1
        evs = self.events({"type": "message.received", "data": {"to": "lead", "text": "extend it", **OWNER}},
                          {"type": "turn.started", "entity": "lead", "data": {"marker": f"m{n}"}},
                          {"type": "schedule.set", "entity": "progress-gen",
                           "data": {"every": "30m", "command": "echo ok", "until": "1970-01-20", **_as("lead")}},
                          *self.same(self.LIMIT + 1))
        at = next(i for i, e in enumerate(evs) if e["type"] == "message.received" and e["data"]["text"] == "extend it")
        st, _, _ = run(evs[:at])
        first = st["schedule"]["progress-gen"]["root"]
        # the state an older core left: a root, no roots
        from core import reducer
        st["schedule"]["progress-gen"].pop("roots")
        for seq, raw in enumerate(evs[at:], at + 1):
            st, _, why = reducer.reduce(st, {"seq": seq, "ts": raw["t"], "src": "test", "type": raw["type"],
                                             "entity": raw.get("entity"), "data": raw.get("data") or {}})
            self.assertIsNone(why)
        job = st["schedule"]["progress-gen"]
        self.assertEqual(job["roots"], [first, f"m{n}"])
        self.assertEqual(job["state"], "active")

    def test_a_hub_run_of_its_turn_keeps_it(self):
        evs = self.events(*self.same(self.LIMIT + 2), task=False)
        evs.insert(next(i for i, e in enumerate(evs) if e["type"] == "turn.result"), self.hub_run())
        st, _, rejected = run(evs)
        self.assertEqual(rejected, 0)
        (r,) = st["hub_runs"].values()
        self.assertEqual(r.get("root"), st["schedule"]["progress-gen"]["root"])
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "active")

    def test_a_set_again_in_a_later_turn_keeps_the_tasks_of_the_first(self):
        n = len(self.events()) + 1
        later = [{"type": "message.received", "data": {"to": "lead", "text": "extend it", **OWNER}},
                 {"type": "turn.started", "entity": "lead", "data": {"marker": f"m{n}"}},
                 {"type": "schedule.set", "entity": "progress-gen",
                  "data": {"every": "30m", "command": "echo ok", "until": "1970-01-20", **_as("lead")}},
                 {"type": "turn.result", "entity": "lead", "data": {"text": "extended"}},
                 {"type": "turn.stopped", "entity": "lead", "data": {}}]
        st, _, rejected = run(self.events(*later, *self.same(self.LIMIT + 1)))
        job = st["schedule"]["progress-gen"]
        self.assertEqual((rejected, job["root"]), (0, f"m{n}"))   # the chain of the later turn
        self.assertEqual(len(job["roots"]), 2)
        self.assertEqual(job["state"], "active")

    def test_once_the_task_is_handed_in_the_next_repeat_removes_it(self):
        self.events()
        st, _, _ = run(self.events(*self.same(self.LIMIT + 2), *self.handed_in(), *self.same(1)))
        self.assertEqual(next(iter(st["delegations"].values()))["state"], "replied")
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "removed")
        (note,) = self.notes(st, "lead")
        self.assertIn(str(self.LIMIT + 3), note["text"])

    def test_no_data_with_the_task_held_is_still_removed(self):
        # "no data" is a broken source, not a flat number: the work going on does not make it a milestone
        st, _, _ = run(self.events(*[job_notice("progress-gen", NO_DATA)] * self.LIMIT))
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "removed")

    def test_silent_runs_with_the_task_held_keep_it(self):
        # a command that notifies only when its number changes: the removal would clear the owner's last milestone while
        # the work goes on; the setter has heard of the first silent run already
        evs = []
        for i, slot in enumerate(SLOTS[:self.LIMIT + 1]):
            evs += ([due(slot)] if i else []) + [{"t": slot + 3, **ran(slot)}]
        st, _, _ = run(self.events(*evs))
        self.assertEqual(next(iter(st["delegations"].values()))["state"], "working")
        self.assertEqual(st["schedule"]["progress-gen"]["state"], "active")
        self.assertEqual(len(self.notes(st, "lead")), 1)



class RemovalIsAFact(Case):
    """The core removing a milestone leaves a fact in the journal with the reason (a check of a week could find the
    removals only by the core's note to the setter)."""
    LIMIT = getattr(schedule, "MILESTONE_LIMIT", 4)

    def facts(self, actions):
        return [a for a in actions if a["do"] == "milestone_removed"]

    def test_the_fold_asks_for_the_fact_with_the_reason(self):
        _, actions, _ = run(self.events(*[job_notice("progress-gen", "3 of 10")] * self.LIMIT))
        (fact,) = self.facts(actions)
        self.assertEqual((fact["job"], fact["why"], fact["n"], fact["by"]), ("progress-gen", "same", self.LIMIT, "lead"))
        self.assertEqual(fact["of"], len(self.events()) + self.LIMIT)   # the notice that removed it

    def test_no_data_and_silence_have_their_reasons(self):
        _, actions, _ = run(self.events(*[job_notice("progress-gen", NO_DATA)] * self.LIMIT))
        self.assertEqual([f["why"] for f in self.facts(actions)], ["empty"])
        evs = []
        for i, slot in enumerate(SLOTS[:self.LIMIT]):
            evs += ([due(slot)] if i else []) + [{"t": slot + 3, **ran(slot)}]
        _, actions, _ = run(self.events(*evs))
        self.assertEqual([f["why"] for f in self.facts(actions)], ["silent"])

    def test_the_setter_removed_is_a_reason_too(self):
        _, actions, _ = run(self.events(
            {"type": "session.close_requested", "entity": "lead", "data": OWNER},
            {"type": "turn.result", "entity": "lead", "data": {"text": "HANDOFF-DONE\nok"}},
            {"type": "session.remove_requested", "entity": "lead", "data": OWNER}))
        self.assertEqual([(f["job"], f["why"]) for f in self.facts(actions)], [("progress-gen", "gone")])

    def test_a_removal_by_the_setter_asks_for_nothing(self):
        _, actions, _ = run(self.events({"type": "schedule.remove_requested", "entity": "progress-gen", "data": _as("lead")}))
        self.assertEqual(self.facts(actions), [])

    def test_the_fact_is_known_to_the_core(self):
        from core import machines, reducer
        self.assertIn("schedule.milestone_removed", machines.FACTS)
        st, _, _ = run(self.events())
        _, _, why = reducer.reduce(st, {"seq": 99, "ts": 200, "src": "core", "type": "schedule.milestone_removed",
                                        "entity": "progress-gen", "data": {"why": "same", "n": 4, "of": 98}})
        self.assertIsNone(why)

    def test_the_daemon_writes_it_into_the_journal(self):
        import json
        import os
        import tempfile
        import types
        from core import daemon
        tmp = tempfile.mkdtemp(prefix="milestone-fact-")
        h = daemon.Hub(tmp, types.SimpleNamespace())
        h.record("owner.settings", None, {"timezone": "UTC", **OWNER}, src="cli")
        h.record("schedule.set", "progress-gen", {"every": "30m", "command": "echo ok", **OWNER}, src="cli")
        for _ in range(self.LIMIT):
            h.record("owner.notify", None, job_notice("progress-gen", "3 of 10")["data"], src="cli")
        with open(h.journal.path, encoding="utf-8") as f:
            events = [json.loads(line) for line in f]
        self.assertEqual(h.state["schedule"]["progress-gen"]["state"], "removed")
        (fact,) = [e for e in events if e["type"] == "schedule.milestone_removed"]
        self.assertEqual((fact["entity"], fact["data"]["why"], fact["data"]["n"]), ("progress-gen", "same", self.LIMIT))
        self.assertFalse([e for e in events if e["type"] == "core.rejected"])
        self.assertTrue(os.path.exists(h.journal.path))


if __name__ == "__main__":
    unittest.main()
