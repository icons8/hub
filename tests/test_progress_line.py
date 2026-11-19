"""Milestones on long work (2026-10-28): a session that starts work longer than ~30 minutes gives the owner a milestone
line on a schedule. Monitor and `until` wake only the session itself, and `hub notify` from a session is refused by the core
(the session speaks to the owner with the turn result) — so the rule makes a schedule command job, which the clock runs without a
session: `hub notify` is allowed to it, and the same `--id` updates the previous notice. The rule is a part of the system
prompt (hosts/streamjson.PROGRESS) for sessions that talk to the owner; an executor reports to whoever raised it."""
import tests  # noqa: F401 — hide the machine's settings
import re
import shlex
import unittest

from core import reducer, schedule
from hosts import streamjson
from tests.test_modapi_prompt import Base

JOB = {"channel": "cli", "owner": True, "source": "work-progress"}   # hub notify from a schedule command job


class TheMechanismTheRuleRestsOn(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "maps", cwd="/w", session_id="s-maps", kind="work", channel="web", owner=True)
        self.ev("session.started", "maps")

    def ev(self, type, entity=None, src=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq * 60, "type": type,
                                                   "entity": entity, "data": data, "src": src})
        return why

    def test_a_session_sets_its_own_command_job_and_the_job_notifies_under_one_id(self):
        spec = {"every": "30m", "command": "hub notify --id work-progress 'pack: 1 of 3'", "until": "1970-01-03",
                "channel": "session", "owner": False, "sender": "maps"}
        self.assertIsNone(self.ev("schedule.set", "work-progress", **spec))
        self.assertEqual(self.st["schedule"]["work-progress"]["state"], "active")
        self.assertIsNone(self.ev("owner.notify", None, text="pack: 1 of 3", id="work-progress", **JOB))
        self.assertIsNone(self.ev("owner.notify", None, text="pack: 2 of 3", id="work-progress", **JOB))
        (n,) = self.st["notices"].values()   # the second line updates the first, they do not pile up
        self.assertEqual(n["text"], "pack: 2 of 3")
        self.assertIsNone(self.ev("schedule.remove_requested", "work-progress", channel="session", owner=False,
                                  sender="maps"))   # the session that set the job removes it at the end of the work
        self.assertEqual(self.st["schedule"]["work-progress"]["state"], "removed")

    def test_a_removed_job_closes_its_notice_and_leaves_the_others(self):
        # 03.11: a milestone job was removed at the end of the work, and its last line stayed open for the phone and
        # the push list until the day ran out — "the summary shows stale cards". The job is gone, so is its line.
        spec = {"every": "30m", "command": "hub notify --id work-progress 'pack: 1 of 3'", "until": "1970-01-03",
                "channel": "session", "owner": False, "sender": "maps"}
        self.ev("schedule.set", "work-progress", **spec)
        self.ev("owner.notify", None, "cli", text="pack: 3 of 3", id="work-progress", **JOB)
        self.ev("owner.notify", None, "module", text="disk", id="work-progress", channel="plugin", owner=False,
                sender="mod:shop")
        self.ev("owner.notify", None, "cli", text="other", id="other", **{**JOB, "source": "other"})
        self.assertIsNone(self.ev("schedule.remove_requested", "work-progress", channel="session", owner=False,
                                  sender="maps"))
        self.assertEqual(sorted(self.st["notices"]), ["n:mod:shop:work-progress", "n:schedule:other:other"])

    def test_a_job_past_its_until_closes_its_notice(self):
        spec = {"every": "30m", "command": "hub notify --id work-progress 'pack: 1 of 3'", "until": 5000,
                "channel": "session", "owner": False, "sender": "maps"}
        self.ev("schedule.set", "work-progress", **spec)
        self.ev("owner.notify", None, "cli", text="pack: 2 of 3", id="work-progress", **JOB)
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq + 1, "ts": 5100, "type": "schedule.ended",
                                                   "entity": "work-progress", "data": {"channel": "core"}})
        self.assertIsNone(why)
        self.assertEqual(self.st["schedule"]["work-progress"]["state"], "expired")
        self.assertEqual(self.st["notices"], {})

    def test_a_job_that_is_paused_or_done_keeps_its_notice(self):
        # a one-shot job is "done" before its command runs (schedule.due -> done, then ran): the notice its command
        # sends is the result, it lives its day. Only a removed or expired job takes its line with it.
        spec = {"at": 5000, "command": "hub notify --id work-progress 'done'",
                "channel": "session", "owner": False, "sender": "maps"}
        self.assertIsNone(self.ev("schedule.set", "work-progress", **spec))
        for ts, kind, data in ((5000, "schedule.due", {"slot": 5000}), (5010, "owner.notify", {"text": "done", "id": "work-progress", **JOB})):
            self.seq += 1
            self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts, "type": kind, "src": "cli",
                                                       "entity": "work-progress" if kind != "owner.notify" else None,
                                                       "data": {"channel": "core", **data} if kind != "owner.notify" else data})
            self.assertIsNone(why, kind)
        self.assertEqual(self.st["schedule"]["work-progress"]["state"], "done")
        self.assertEqual(list(self.st["notices"]), ["n:schedule:work-progress:work-progress"])

    def test_a_session_itself_cannot_notify_that_is_why_the_job_exists(self):
        why = self.ev("owner.notify", None, text="so far 1 of 3", id="work-progress", channel="session", owner=False,
                      sender="maps")
        self.assertIsNotNone(why)


class TheRuleInTheSystemPrompt(Base):
    def test_a_session_and_a_mini_hub_get_the_part(self):
        h = self.host()
        self.assertIn(streamjson.PROGRESS, self.prompt(h.command("w", "s-1", agent_file=self.agent(h))))
        self.assertIn(streamjson.PROGRESS, self.prompt(h.command("w", "s-1", agent_file=self.agent(h), minihub=True)))

    def test_an_executor_has_no_part_it_reports_to_whoever_raised_it(self):
        h = self.host()
        for kw in ({"bus": "maps"}, {"executor_of": "maps", "agents": [("maps", "maps")]}):
            self.assertNotIn(streamjson.PROGRESS, self.prompt(h.command("w", "s-1", agent_file=self.agent(h), **kw)), kw)

    def test_the_source_check_and_the_lead_line_are_in_the_part_that_leads_get(self):
        """2026-10-30: a milestone said "0 of 10" six times (an empty answer of the source was swallowed), and 35 of 41 long
        turns had no milestone (the work was handed to executors): the lead who hands the work over sets it. The line is in
        PROGRESS itself, so it reaches whoever gets PROGRESS — a session, a mini-hub, a direction, the hub — and nobody else."""
        for needle in ("source by hand first", "\"no data\", not 0", "by the pace", "Gave an executor work",
                       "the executor sets none"):
            self.assertIn(needle, streamjson.PROGRESS)
        h = self.host()
        for kw in ({}, {"minihub": True}):   # a session (the hub, a direction: the host sees none of the difference) and a mini-hub
            self.assertIn("Gave an executor work", self.prompt(h.command("w", "s-1", agent_file=self.agent(h), **kw)), kw)
        for kw in ({"bus": "maps"}, {"executor_of": "maps", "agents": [("maps", "maps")]}):   # whoever executes
            self.assertNotIn("Gave an executor work", self.prompt(h.command("w", "s-1", agent_file=self.agent(h), **kw)),
                             kw)

    def test_the_first_milestone_is_the_jobs_and_n_is_counted(self):
        """2026-11-12, a check of a week: 16 of 62 set-ups also sent a `hub notify` from the session (refused by the core)
        — the job runs once at once and sends the first milestone itself; three milestones read "7 of 6" — n was typed
        into the command."""
        for needle in ("sends the first milestone itself at once", "no `hub notify` by hand", "on every run",
                       "n not typed in"):
            self.assertIn(needle, streamjson.PROGRESS)

    def test_the_line_is_one_short_paragraph(self):
        self.assertNotIn("\n", streamjson.PROGRESS)
        self.assertLess(len(streamjson.PROGRESS), 1600)   # was 1000; the probe rule (2026-11-09) added ~500

    def test_the_command_it_gives_is_accepted_by_the_schedule(self):
        """The example in the rule is a real `hub schedule add`: with the placeholders filled it passes `schedule.normalize`
        (a command job needs a Latin --id, and without --until it would repeat forever)."""
        (example,) = re.findall(r"`(hub schedule add [^`]+)`", streamjson.PROGRESS)
        example = example.replace("<YYYY-MM-DD + margin>", "1970-01-03").replace("<name>", "pack")
        words = shlex.split(example)
        opts = {w: words[i + 1] for i, w in enumerate(words) if w in ("--id", "--every", "--until", "--command")}
        self.assertEqual(sorted(opts), ["--command", "--every", "--id", "--until"])
        self.assertIn("hub notify --id progress-pack", opts["--command"])
        job = schedule.normalize({"id": opts["--id"], "every": opts["--every"], "until": opts["--until"],
                                  "command": opts["--command"]}, 1000.0, "Europe/Paris")
        self.assertEqual((job["kind"], job["every"], job["id"]), ("every", 1800, "progress-pack"))
        self.assertIsNotNone(job["until"])

    def test_the_part_is_named_progress_for_modules(self):
        from tests.test_modapi_prompt import Stub, parts_of
        seen = []

        def spy(point, payload):
            seen.extend(p["name"] for p in payload["parts"])
            return parts_of(payload)
        h = self.host(Stub(m=spy))
        h.command("w", "s-1", agent_file=self.agent(h))
        self.assertIn("progress", seen)


if __name__ == "__main__":
    unittest.main()
