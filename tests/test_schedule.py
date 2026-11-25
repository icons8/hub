"""Schedule: time (cron, every, one-off, owner time zone), the clock (waits for hubd, one fact per slot), end to end:
clock -> spool -> hubd -> message to a session with a label, CLI `hub schedule`."""
import tests  # noqa: F401 — hide the machine's settings (~/.config/hub-core), see tests/__init__.py
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from zoneinfo import ZoneInfo

from core import i18n, journal, langdata, reducer, schedule, spool
from modules.scheduler.clock import Clock, read_status
from tests.fakes import tz
from tests.test_replays import run
from tests.test_tools import HUB_CLI, HubCase

TZ = "Europe/Berlin"


def ts(text):
    return datetime.datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(TZ)).timestamp()


@tz.need
class TimeMath(unittest.TestCase):
    def test_cron_fields_and_dom_dow_or(self):
        dt = datetime.datetime(2026, 10, 5, 9, 0, tzinfo=ZoneInfo(TZ))  # Monday
        self.assertTrue(schedule.cron_match("0 9 * * 1-5", dt))
        self.assertTrue(schedule.cron_match("*/15 8-10 * * *", dt))
        self.assertFalse(schedule.cron_match("0 9 * * 0,6", dt))
        self.assertTrue(schedule.cron_match("0 9 1 * 1", dt))   # both set: the weekday alone is enough
        self.assertTrue(schedule.cron_match("0 9 * * 7", dt.replace(day=4)))  # 7 = Sunday
        for bad in ("61 * * * *", "* * *", "a * * * *", "5-1 * * * *"):
            with self.assertRaises(schedule.Invalid, msg=bad):
                schedule.parse_cron(bad)

    def test_cron_next_in_owner_timezone_across_dst(self):
        # 25.10.2026 Europe changes the clocks: 09:00 local is still 09:00
        nxt = schedule.cron_next("0 9 * * *", ts("2026-10-24 10:00"), TZ)
        self.assertEqual(nxt, ts("2026-10-25 09:00"))
        self.assertEqual(nxt - ts("2026-10-24 09:00"), 25 * 3600)

    def test_normalize_limits(self):
        now = ts("2026-09-29 10:00")
        job = schedule.normalize({"id": "x", "session": "a", "prompt": "p", "every": "2h"}, now, TZ)
        self.assertEqual((job["every"], job["start"], job["until"]), (7200, now + 7200, now + 14 * 86400))
        for spec, why in (({"every": "4m"}, "5 minutes"), ({"every": "1h", "until": "2026-11-30"}, "30 days"),
                          ({"at": "2026-09-29 09:00"}, "passed — it is 2026-09-29 10:00 \\(Europe/Berlin\\)"), ({"at": "2026-09-30 10:00", "cron": "* * * * *"}, "exactly one"),
                          ({"id": "bad id", "every": "1h"}, "id"),
                          # a number (seconds): the same limit as for text
                          ({"every": 1}, "5 minutes"), ({"every": 60}, "5 minutes"), ({"every": -300}, "5 minutes"),
                          ({"every": [1]}, "every")):
            with self.assertRaisesRegex(schedule.Invalid, why):
                schedule.normalize({"id": "x", "session": "a", "prompt": "p", **spec}, now, TZ)

    def test_slot_due_latest_only_and_cron_window(self):
        now = ts("2026-09-29 10:00")
        job = {**schedule.normalize({"id": "x", "session": "a", "prompt": "p", "every": "30m"}, now, TZ), "created": now}
        self.assertIsNone(schedule.slot_due(job, now + 60, TZ))
        self.assertEqual(schedule.slot_due(job, now + 5 * 1800 + 10, TZ), now + 5 * 1800)  # the last one, not all five
        self.assertIsNone(schedule.slot_due({**job, "last_slot": now + 5 * 1800}, now + 5 * 1800 + 10, TZ))
        cron = {**schedule.normalize({"id": "c", "session": "a", "prompt": "p", "cron": "0 * * * *"}, now, TZ),
                "created": now}
        self.assertEqual(schedule.slot_due(cron, now + 3600 + 30, TZ), now + 3600)
        self.assertIsNone(schedule.slot_due(cron, now + 20 * 3600 + 1800 - 3600 * 7, TZ) and None)
        # idle longer than the 6 h window: slots before the window are not searched
        self.assertEqual(schedule.slot_due(cron, now + 10 * 3600 + 30, TZ), now + 10 * 3600)

    def test_next_due_and_when_text(self):
        now = ts("2026-09-29 10:00")
        job = {**schedule.normalize({"id": "x", "session": "a", "prompt": "p", "every": "30m"}, now, TZ), "state": "active"}
        self.assertEqual(schedule.next_due(job, now + 4000, TZ), now + 5400)
        self.assertEqual(schedule.when_text(job, TZ), "every 30 min until 13.10")
        at = {**schedule.normalize({"id": "y", "session": "a", "prompt": "p", "at": "2026-09-30 08:15"}, now, TZ), "state": "active"}
        self.assertEqual(schedule.when_text(at, TZ), "once 30.09 08:15")
        self.assertIsNone(schedule.next_due({**at, "last_slot": at["at"]}, now, TZ))


@tz.need
class ClockChange(unittest.TestCase):
    """Clock-change nights: a wall-clock minute fires once, at its first moment; a minute the clock
    skips fires once, shifted over the gap (where cron_next puts it); a slot is never in the future. An hourly job
    runs on real minutes, in the repeated hour too."""
    NY = "America/New_York"

    def utc(self, text):
        return datetime.datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=datetime.timezone.utc).timestamp()

    def run_night(self, expr, start, stop):
        """A tick every minute, like the clock; the core records each accepted slot. -> (fired, rejected)."""
        job = {"kind": "cron", "cron": expr, "until": self.utc(stop) + 86400, "created": self.utc(start) - 86400}
        fired, rejected = [], 0
        t = self.utc(start)
        while t < self.utc(stop):
            slot = schedule.slot_due(job, t, self.NY)
            if slot is not None:
                if slot > t + 60:   # the core rejects a slot that has not come yet (schedule.apply)
                    rejected += 1
                else:
                    fired.append(slot)
                    job["last_slot"] = slot
            t += 60
        return fired, rejected

    def test_fall_back_fires_once(self):
        # 01.11.2026: in New York 01:00-01:59 happens twice (EDT, then EST)
        fired, rejected = self.run_night("30 1 * * *", "2026-11-01 04:00", "2026-11-01 09:00")
        self.assertEqual((fired, rejected), ([self.utc("2026-11-01 05:30")], 0))

    def test_spring_forward_fires_once_without_rejections(self):
        # 08.03.2026: there is no 02:00-02:59 in New York; 02:30 fires at 03:30 EDT, where cron_next puts it,
        # when asked before the change
        fired, rejected = self.run_night("30 2 * * *", "2026-03-08 05:00", "2026-03-08 09:00")
        self.assertEqual((fired, rejected), ([self.utc("2026-03-08 07:30")], 0))
        self.assertEqual(schedule.cron_next("30 2 * * *", self.utc("2026-03-08 06:00"), self.NY), fired[0])

    def test_cron_next_in_the_repeated_hour_is_ahead(self):
        # at 01:30 EST (second pass) the job's next slot by fixed hours is after the repeated hour, not in the past
        after = self.utc("2026-11-01 06:30")
        self.assertEqual(schedule.cron_next("*/10 1,2 * * *", after, self.NY), self.utc("2026-11-01 07:00"))
        job = {"kind": "cron", "cron": "*/10 1,2 * * *", "until": after + 86400, "state": "active"}
        self.assertGreater(schedule.next_due(job, after, self.NY), after)

    def test_a_job_of_every_hour_runs_in_the_repeated_hour_too(self):
        # every hour means every real hour: the second 01:00-01:59 pass is an ordinary hour, not 70 minutes of silence
        fired, rejected = self.run_night("*/10 * * * *", "2026-11-01 05:00", "2026-11-01 07:30")
        self.assertEqual(rejected, 0)
        self.assertEqual(fired, [self.utc("2026-11-01 05:00") + i * 600 for i in range(15)])
        for t in fired[:-1]:
            self.assertEqual(schedule.cron_next("*/10 * * * *", t, self.NY), t + 600)
        self.assertEqual(schedule.cron_next("*/10 * * * *", self.utc("2026-11-01 05:55"), self.NY),
                         self.utc("2026-11-01 06:00"))

    def test_a_job_of_every_hour_on_the_night_the_clocks_go_forward(self):
        fired, rejected = self.run_night("*/30 * * * *", "2026-03-08 06:00", "2026-03-08 08:00")
        self.assertEqual((fired, rejected), ([self.utc("2026-03-08 06:00") + i * 1800 for i in range(4)], 0))
        self.assertEqual(schedule.cron_next("*/30 * * * *", self.utc("2026-03-08 06:45"), self.NY),
                         self.utc("2026-03-08 07:00"))


class HumanWhen(unittest.TestCase):
    """'When' in words, not cron (owner decision 2026-10-03: the schedule should give a good overview of what runs
    and how); anything not understood is shown as is."""

    def test_cron_and_every_in_words(self):
        h = schedule.human_cron
        self.assertEqual([h(c) for c in ("0 */3 * * *", "*/15 * * * *", "0 * * * *", "15 4 * * *", "0 11 * * 1-5",
                                          "17 7 * * 1", "0 10,17 * * 1-5", "0 9-20 * * 1-5", "30 10 * * 0,6",
                                          "0 10 * * 3", "0 10 1 * *", "bad")],
                         ["every 3 h", "every 15 min", "every hour", "every day at 04:15", "on weekdays at 11:00",
                          "on Mondays at 07:17", "on weekdays at 10:00 and 17:00", "on weekdays every hour from 9 to 20",
                          "on weekends at 10:30", "on Wednesdays at 10:00", 'cron "0 10 1 * *"', 'cron "bad"'])
        every = lambda v: schedule.when_text({"kind": "every", "every": v, "until": None}, TZ)  # noqa: E731
        self.assertEqual([every(v) for v in (1800, 3600, 7200, 86400, 3 * 86400)],
                         ["every 30 min indefinitely", "every hour indefinitely", "every 2 h indefinitely",
                          "every day indefinitely", "every 3 days indefinitely"])


class Wakes(unittest.TestCase):
    def test_a_command_wakes_only_the_session_it_names_in_hub_session_wake(self):
        cmd = lambda c, session=None: schedule.wakes({"command": c, "session": session}, "fe-ci")  # noqa: E731
        self.assertEqual([cmd(c) for c in ("hub session wake fe-ci", "X=1 hub session wake 'fe-ci' --about d1 x",
                                           'hub  session wake "fe-ci"; echo ok', "hub session wake fe-ci-2",
                                           "hub session wake xfe-ci", "hub send fe-ci x", "echo session wake")],
                         [True, True, True, False, False, False, False])
        self.assertFalse(cmd("echo ok", session="fe-ci"))      # handed to it, but runs a command — does not wake it
        self.assertTrue(schedule.wakes({"session": "fe-ci", "command": ""}, "fe-ci"))
        self.assertFalse(schedule.wakes({"session": "fe", "command": ""}, "fe-ci"))


@tz.need
class ScheduleView(unittest.TestCase):
    """Owner decision 2026-10-01: the same tasks that must run later are not separate items; show how the schedule is
    set up and when the other runs will be."""

    def view(self, jobs, now):
        from core import reducer, views
        st = reducer.empty()
        st["owner"]["tz"] = TZ
        for j in jobs:
            st["schedule"][j["id"]] = {"state": "active", "runs": 0, "by": "a", "created": now, "since": now,
                                       **schedule.normalize(j, now, TZ)}
        return {j["id"]: j for j in views.schedule_view(st, now)}

    def test_upcoming_runs_and_one_group_for_the_same_task(self):
        now = ts("2026-10-01 09:00")
        v = self.view([{"id": "first", "session": "s", "at": "2026-10-03 10:00", "prompt": "Paywall check. Step 1"},
                       {"id": "weekly", "session": "s", "cron": "0 10 * * 3", "until": "2026-10-29",
                        "prompt": "Paywall check. Step 1 and a tail"},
                       {"id": "c1", "session": "hub", "at": "2026-10-08 10:17", "prompt": "Check X"},
                       {"id": "c2", "session": "hub", "at": "2026-10-28 10:17", "prompt": "Check X"},
                       {"id": "other", "session": "s", "every": "2h", "prompt": "something else entirely"}], now)
        self.assertEqual(v["weekly"]["upcoming"][:2], [ts("2026-10-07 10:00"), ts("2026-10-14 10:00")])
        self.assertEqual(v["first"]["upcoming"], [ts("2026-10-03 10:00")])
        self.assertEqual(len(v["other"]["upcoming"]), schedule.UPCOMING)
        self.assertEqual(v["first"]["group"], v["weekly"]["group"])       # one task, different schedules
        self.assertEqual(v["c1"]["group"], v["c2"]["group"])
        self.assertNotEqual(v["c1"]["group"], v["first"]["group"])       # a different addressee
        self.assertNotEqual(v["other"]["group"], v["first"]["group"])

    def test_groups_read_each_job_text_once(self):
        # the snapshot groups the active and paused jobs on every build: each job's text used to be cut and joined
        # again for every group head it was compared with
        reads = []
        class Job(dict):
            def get(self, k, d=None):
                if k in ("prompt", "command"):
                    reads.append(self["id"])
                return dict.get(self, k, d)
        jobs = [Job(id=f"j{i}", session=f"s{i % 3}", prompt=f"task number {i} " * 3) for i in range(300)]
        jobs += [Job(id="again", session="s0", prompt="task number 0 task number 0")]
        got = schedule.groups(jobs)
        self.assertEqual(got["again"], "j0")             # the same task under another job is still one group
        self.assertEqual(len(set(got.values())), 300)
        self.assertLessEqual(len(reads), 3 * len(jobs))   # a few reads per job, not per pair (89 thousand before)


@tz.need
class ClockTest(unittest.TestCase):
    """The clock without hubd: we write the journal ourselves and look at what landed in the spool."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubsched-")
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        self.now = ts("2026-09-29 10:00")
        self.clock = Clock(self.state, clock=lambda: self.now, awake=lambda: self.now)
        self.j.append("owner.settings", data={"timezone": TZ}, ts=self.now)
        self.j.append("session.requested", "a", {"cwd": "/w", "session_id": "sid-a"}, ts=self.now)
        self.j.append("schedule.set", "x", {"session": "a", "every": "30m", "prompt": "p", "channel": "cli", "owner": True},
                      ts=self.now)

    def tearDown(self):
        shutil.rmtree(self.state, ignore_errors=True)

    def spooled(self):
        d = os.path.join(self.state, "spool")
        items = [e for _, e in spool.take(d)]
        for n in os.listdir(d) if os.path.isdir(d) else []:
            os.unlink(os.path.join(d, n))
        return items

    def beat(self, age=0):
        path = os.path.join(self.state, "hubd.alive")
        open(path, "w").close()
        os.utime(path, (self.now - age, self.now - age))

    @unittest.skipUnless(os.name == "nt", "a file open by a reader cannot be replaced; Windows only")
    def test_status_survives_a_reader_holding_the_file(self):
        # 2026-10-16 Windows: the page reads scheduler.json, os.replace raised PermissionError, the clock crashed and
        # restarted
        self.beat()
        self.clock.step()
        held = open(os.path.join(self.state, "scheduler.json"), encoding="utf-8")
        threading.Timer(0.3, held.close).start()
        self.clock.step()   # waited for the reader, did not crash
        held.close()
        self.assertEqual(read_status(self.state, self.now)["state"], "running")

    def test_waits_for_hubd_then_one_fact_per_slot(self):
        self.now += 1800 + 5
        self.clock.step()
        self.assertEqual(self.spooled(), [])  # no heartbeat: wait, send nothing
        self.assertEqual(read_status(self.state, self.now)["state"], "waiting")
        self.beat(age=60)
        self.clock.step()
        self.assertEqual(self.spooled(), [])  # stale heartbeat: hubd is down
        self.beat()
        wait = self.clock.step()
        items = self.spooled()
        self.assertEqual([(e["type"], e["entity"], e["data"]) for e in items],
                         [("schedule.due", "x", {"slot": ts("2026-09-29 10:30")})])
        self.assertEqual(items[0]["src"], "scheduler")
        self.clock.step()
        self.assertEqual(self.spooled(), [])  # the core has not recorded it yet: do not send a second time
        self.assertEqual(read_status(self.state, self.now)["state"], "running")
        self.assertLessEqual(wait, 30)

    def test_ended_after_until(self):
        self.now += 15 * 86400
        self.beat()
        self.j.append("schedule.due", "x", {"slot": self.now - 60}, ts=self.now - 60)  # the last slot has already happened
        self.clock.step()
        self.assertEqual([e["type"] for e in self.spooled()], ["schedule.ended"])

    def test_command_job_runs_once_per_slot_and_reports(self):
        """A command job (monitors, owner decision 2026-09-30): the core marked the launch, the clock runs it
        and reports schedule.ran."""
        out = os.path.join(self.state, "ran.txt")
        # the python path is quoted: on Windows it sits in "Program Files" (2026-10-11: unquoted, the command did not
        # start)
        cmd = f"\"{sys.executable}\" -c \"import os; open('{out.replace(chr(92), '/')}', 'a').write(os.environ['HUB_SCHEDULE_JOB'] + chr(10))\""
        self.j.append("schedule.set", "gate", {"every": "5m", "command": cmd, "channel": "cli", "owner": True},
                      ts=self.now)
        self.now += 300
        self.beat()
        self.clock.step()
        due = [e for e in self.spooled() if e["entity"] == "gate"]
        self.assertEqual([e["type"] for e in due], ["schedule.due"])
        self.j.append("schedule.due", "gate", due[0]["data"], ts=self.now)  # as the core would record it
        self.clock.step()
        self.assertEqual(self.clock.running, {})   # first "starting" as a fact in the core
        started = [(e["type"], e["data"]) for e in self.spooled()]
        self.assertEqual(started, [("schedule.started", due[0]["data"])])
        self.j.append(*started[0][:1], "gate", started[0][1], ts=self.now)
        self.clock.step()
        for _ in range(100):
            if self.clock.running:
                time.sleep(0.05)
                self.clock.step()
        self.clock.step()
        self.assertEqual(open(out).read().split(), ["gate"])
        ran = [e for e in self.spooled() if e["type"] == "schedule.ran"]
        self.assertEqual([(e["entity"], e["data"]["slot"], e["data"]["exit"]) for e in ran],
                         [("gate", due[0]["data"]["slot"], 0)])
        self.clock.step()  # the core has not recorded ran yet: do not run a second time
        self.assertEqual(open(out).read().split(), ["gate"])

    def test_a_progress_job_is_started_by_the_clock_at_once_when_it_is_set(self):
        """A milestone (id progress-*) gets its first run at the start, not after an interval; another command job does not."""
        out = os.path.join(self.state, "first.txt")
        cmd = f"\"{sys.executable}\" -c \"import os; open('{out.replace(chr(92), '/')}', 'a').write(os.environ['HUB_SCHEDULE_JOB'] + chr(10))\""
        for jid in ("progress-gen", "backup"):
            self.j.append("schedule.set", jid, {"every": "30m", "command": cmd, "channel": "cli", "owner": True}, ts=self.now)
        self.beat()
        self.clock.step()   # no slot has come: only the run the core marked when it was set
        started = [(e["type"], e["entity"]) for e in self.spooled()]
        self.assertEqual(started, [("schedule.started", "progress-gen")])

    def ingest(self):
        """Like hubd: facts from the spool go into the journal (the clock's fold drops rejections itself)."""
        for e in self.spooled():
            self.j.append(e["type"], e.get("entity"), e.get("data"), src=e.get("src"), ts=self.now)

    def test_a_command_is_not_rerun_after_a_clock_restart(self):
        """The clock restarted while the job command was running (or it finished but schedule.ran was not recorded
        yet): the launch existed only in the clock's memory, so the new one ran it a second time (two backup dumps, a
        lock fight). The launch is a fact in the core before Popen; after a restart it is not repeated, and the hub is
        told "outcome unknown"."""
        out = os.path.join(self.state, "ran.txt")
        cmd = f"\"{sys.executable}\" -c \"open('{out.replace(chr(92), '/')}', 'a').write('run' + chr(10))\""

        def runs():
            with open(out) as f:
                return f.read().split()
        self.j.append("session.requested", "hub", {"cwd": "/h", "session_id": "sid-h", "kind": "hub"}, ts=self.now)
        self.j.append("schedule.set", "gate", {"every": "5m", "command": cmd, "channel": "cli", "owner": True},
                      ts=self.now)
        self.now += 300
        self.beat()
        for _ in range(5):   # slot -> launch (the core records every fact)
            self.clock.step()
            self.ingest()
            if self.clock.running:
                break
        proc = self.clock.running["gate"][0]
        proc.wait(timeout=20)   # the command ended, the outcome was not recorded, and the clock restarted
        self.clock = Clock(self.state, clock=lambda: self.now, awake=lambda: self.now)
        for _ in range(3):
            self.clock.step()
            self.ingest()
        for r in list(self.clock.running.values()):
            r[0].wait(timeout=20)
        self.assertEqual(runs(), ["run"])
        job = self.clock.tail.reader.state["schedule"]["gate"]
        self.assertNotIn("last_exit", job)   # no code: not an "error" on the page
        (note,) = [m for m in self.clock.tail.reader.state["messages"].values() if m["session"] == "hub"]
        self.assertIn("the outcome is unknown", note["text"])
        self.now += 300   # the next slot is as usual
        self.beat()
        for _ in range(5):
            self.clock.step()
            self.ingest()
            if self.clock.running:
                break
        self.clock.running["gate"][0].wait(timeout=20)
        self.assertEqual(runs(), ["run", "run"])

    def test_an_old_core_that_does_not_record_the_start_still_gets_the_command_run(self):
        # a hubd of an earlier version does not know schedule.started (rejection): after START_WAIT the clock runs the
        # job as before
        from modules.scheduler import clock as clock_mod
        out = os.path.join(self.state, "ran.txt")
        cmd = f"\"{sys.executable}\" -c \"open('{out.replace(chr(92), '/')}', 'a').write('run' + chr(10))\""
        self.j.append("schedule.set", "gate", {"every": "5m", "command": cmd, "channel": "cli", "owner": True},
                      ts=self.now)
        self.now += 300
        self.beat()
        self.clock.step()
        self.ingest()
        self.clock.step()
        self.assertEqual([e["type"] for e in self.spooled()], ["schedule.started"])   # not recorded
        self.clock.step()
        self.assertEqual(self.clock.running, {})
        self.now += clock_mod.START_WAIT
        self.beat()
        self.clock.step()
        self.clock.running["gate"][0].wait(timeout=20)
        with open(out) as f:
            self.assertEqual(f.read().split(), ["run"])

    def test_a_slot_held_in_a_dark_wake_is_not_started_when_a_newer_slot_is_due_at_the_full_wake(self):
        """An every-2h command job: its slot came in a dark wake (held), the Mac slept through the next one and woke fully.
        Both slots were started two seconds apart: the held slot's start went out first, the core recorded it before the
        newer slot's due, the clock started the command for it, and the newer slot then took the run over (the held run's
        report was refused as unexpected). A catch-up is one run, for the last slot."""
        out = os.path.join(self.state, "ran.txt")
        cmd = f"\"{sys.executable}\" -c \"open('{out.replace(chr(92), '/')}', 'a').write('run' + chr(10))\""
        self.j.append("schedule.set", "gate", {"every": "2h", "command": cmd, "channel": "cli", "owner": True},
                      ts=self.now)
        first = self.now + 2 * 3600
        self.now = first + 5
        self.j.append("core.dark", data={"on": True}, ts=self.now)
        self.beat()
        self.clock.step()
        self.ingest()   # the first slot's due is recorded in the dark wake, the launch waits
        self.clock.step()
        self.assertEqual(self.clock.running, {})
        self.assertEqual(self.spooled(), [])
        second = first + 2 * 3600
        self.now = second + 6 * 60 + 57   # the full wake
        self.j.append("core.dark", data={"on": False}, ts=self.now)
        self.beat()
        self.clock.step()
        facts = self.spooled()   # the core records them one by one, the clock looks in between
        self.assertEqual([(e["type"], e["data"]["slot"]) for e in facts if e["entity"] == "gate"],
                         [("schedule.due", second)])
        for e in facts:
            self.j.append(e["type"], e.get("entity"), e.get("data"), src=e.get("src"), ts=self.now)
            self.clock.step()
            self.ingest()
        for _ in range(3):
            self.now += 1
            self.beat()
            self.clock.step()
            self.ingest()
            for r in list(self.clock.running.values()):
                r[0].wait(timeout=20)
        with open(out) as f:
            self.assertEqual(f.read().split(), ["run"])
        job = self.clock.tail.reader.state["schedule"]["gate"]
        self.assertEqual((job["ran_slot"], job["run_slot"], job["started_slot"]), (second, second, second))

    def test_a_core_lagging_past_the_start_wait_does_not_get_the_command_run_twice(self):
        """The core took longer than START_WAIT to record schedule.started (a backed-up spool), so the clock ran the
        command on its own — and then restarted before the core caught up. The new clock saw no start in the core,
        asked again, and once the start was recorded ran the command a second time. The launch is marked on disk before
        Popen: the slot is not run twice, the hub is told "outcome unknown"."""
        from modules.scheduler import clock as clock_mod
        out = os.path.join(self.state, "ran.txt")
        cmd = f"\"{sys.executable}\" -c \"open('{out.replace(chr(92), '/')}', 'a').write('run' + chr(10))\""
        self.j.append("session.requested", "hub", {"cwd": "/h", "session_id": "sid-h", "kind": "hub"}, ts=self.now)
        self.j.append("schedule.set", "gate", {"every": "5m", "command": cmd, "channel": "cli", "owner": True},
                      ts=self.now)
        self.now += 300
        self.beat()
        self.clock.step()
        self.ingest()   # the slot came: run_slot
        self.clock.step()
        held = self.spooled()   # schedule.started waits in the spool: the core lags
        self.assertEqual([e["type"] for e in held], ["schedule.started"])
        self.now += 90   # a 90 s lag, longer than START_WAIT
        self.beat()
        self.clock.step()
        self.clock.running["gate"][0].wait(timeout=20)
        held += self.spooled()
        self.clock = Clock(self.state, clock=lambda: self.now, awake=lambda: self.now)   # restarted before the outcome was reported
        self.clock.step()
        for e in held:   # the core catches up
            self.j.append(e["type"], e.get("entity"), e.get("data"), src=e.get("src"), ts=self.now)
        for _ in range(4):
            self.ingest()
            self.now += clock_mod.START_WAIT
            self.beat()
            self.clock.step()
            for r in list(self.clock.running.values()):
                r[0].wait(timeout=20)
        with open(out) as f:
            self.assertEqual(f.read().split(), ["run"])
        self.ingest()
        job = self.clock.tail.reader.state["schedule"]["gate"]
        self.assertEqual(job.get("ran_slot"), job["run_slot"])
        self.assertNotIn("last_exit", job)

    def test_a_launch_the_core_does_not_record_is_reported_unknown_once_while_it_stands(self):
        """The mark says the slot was run and the core does not record the report (it stands or lags for longer than the
        10 minutes `sent` remembers): the report "outcome unknown" is not put again every 10 minutes — the repeats are
        rejected by the core one by one, a stream of refusals in the journal when it catches up."""
        out = os.path.join(self.state, "ran.txt")
        cmd = f"\"{sys.executable}\" -c \"open('{out.replace(chr(92), '/')}', 'a').write('run' + chr(10))\""
        self.j.append("session.requested", "hub", {"cwd": "/h", "session_id": "sid-h", "kind": "hub"}, ts=self.now)
        self.j.append("schedule.set", "gate", {"every": "5m", "command": cmd, "channel": "cli", "owner": True},
                      ts=self.now)
        self.now += 300
        self.beat()
        self.clock.step()
        self.ingest()   # the slot came: run_slot
        self.clock.step()
        self.now += 90
        self.beat()
        self.clock.step()   # START_WAIT passed with no record of the start: the command runs, the mark is written
        self.clock.running["gate"][0].wait(timeout=20)
        self.spooled()   # the core records nothing from here on
        self.clock = Clock(self.state, clock=lambda: self.now, awake=lambda: self.now)
        told = []
        for _ in range(4):
            self.now += 700
            self.beat()
            self.clock.step()
            told += [e for e in self.spooled() if e["type"] == "schedule.ran"]
        self.assertEqual([e["data"]["exit"] for e in told], ["unknown"])

    def test_launch_marks_of_old_runs_are_swept(self):
        """`logs/schedule/<id>.started` (and the `.tmp` of an interrupted write) were left for good, one per job ever run,
        including removed ones: a mark older than two days is removed on start and then once an hour."""
        d = os.path.join(self.state, "logs", "schedule")
        os.makedirs(d, exist_ok=True)

        def mark(name, age):
            path = os.path.join(d, name)
            with open(path, "w") as f:
                f.write("{}")
            os.utime(path, (self.now - age, self.now - age))

        mark("old.started", 3 * 86400)
        mark("old2.started.tmp", 3 * 86400)
        mark("fresh.started", 3600)
        self.clock = Clock(self.state, clock=lambda: self.now, awake=lambda: self.now)   # a restart
        self.assertEqual(sorted(os.listdir(d)), ["fresh.started"])
        mark("later.started", 3 * 86400)   # grew old while the clock works: the hourly sweep
        self.now += 3601
        self.beat()
        self.clock.step()
        self.assertEqual(sorted(os.listdir(d)), ["fresh.started"])
    def test_a_hung_command_times_out_at_the_clock_s_own_pace(self):
        """While a command runs and the journal is quiet, main() turns once per MAX_SLEEP plus its 2 s wait: a hung
        command is stopped once MAX_RUN of awake time has passed, not before."""
        from modules.scheduler import clock as clock_mod
        from tests.fakes import procs
        command = f"\"{sys.executable}\" -c \"import time; time.sleep(600)\""
        began = self.now
        self.clock.start({"id": "hung", "command": command, "run_slot": self.now}, self.now)
        self.addCleanup(procs.kill, self.clock.running["hung"][0].pid)
        while self.now - began <= clock_mod.MAX_RUN:
            self.assertIn("hung", self.clock.running)
            self.assertEqual(self.spooled(), [])
            self.now += clock_mod.MAX_SLEEP + 2
            self.clock.finish(self.now, {})
        self.assertNotIn("hung", self.clock.running)
        self.assertEqual([(e["type"], e["data"]["exit"]) for e in self.spooled()], [("schedule.ran", "timeout")])

    def test_timeout_stops_the_whole_tree(self):
        """A command running past MAX_RUN: the shell's descendants are stopped too, not only the shell (they held
        the log open and kept running without a limit)."""
        from modules.scheduler import clock as clock_mod
        from tests.fakes import procs
        pidfile, script = os.path.join(self.state, "grandchild.pid"), os.path.join(self.state, "tree.py")
        with open(script, "w") as f:
            f.write("import subprocess, sys, time\n"
                    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                    f"open({pidfile!r}, 'w').write(str(p.pid))\n"
                    "time.sleep(60)\n")
        command = f"\"{sys.executable}\" \"{script}\""
        self.clock.start({"id": "slow", "command": command, "run_slot": self.now}, self.now)
        proc = self.clock.running["slow"][0]
        self.addCleanup(procs.kill, proc.pid)
        for _ in range(200):
            if os.path.exists(pidfile) and open(pidfile).read():
                break
            time.sleep(0.05)
        grandchild = int(open(pidfile).read())
        self.addCleanup(procs.kill, grandchild, time.time())
        self.assertTrue(procs.alive(grandchild))
        self.now += clock_mod.MAX_RUN + 1
        self.clock.finish(self.now, {})
        self.assertEqual([(e["type"], e["data"]["exit"]) for e in self.spooled()], [("schedule.ran", "timeout")])
        for _ in range(100):
            if not procs.alive(grandchild):
                break
            time.sleep(0.05)
        self.assertFalse(procs.alive(grandchild))

    def test_status_down_when_stale(self):
        self.assertEqual(read_status(self.state)["state"], "down")


def _as(sender):
    return {"channel": "session", "owner": False, "sender": sender}


OWNER = {"channel": "cli", "owner": True}


class SessionCommands(unittest.TestCase):
    """A command put by a session belongs to it (schedule.owner_of, like jobOf on the page) while the session exists:
    removal by session and its list include commands too; once the session is gone, the command keeps running and
    belongs to no one."""

    def events(self, *extra):
        base = [{"t": 0, "type": "owner.settings", "data": {"timezone": "UTC"}}]
        for name, kind in (("hub", "hub"), ("w", None), ("x", None)):
            base += [{"t": 0, "type": "session.requested", "entity": name,
                      "data": {"cwd": "/w", "session_id": f"sid-{name}", **({"kind": kind} if kind else OWNER)}},
                     {"t": 1, "type": "session.started", "entity": name, "data": {"session_id": f"sid-{name}"}}]
        base += [{"t": 2, "type": "schedule.set", "entity": "w-mon", "data": {"every": "1h", "command": "true", **_as("w")}},
                 {"t": 2, "type": "schedule.set", "entity": "x-mon", "data": {"every": "1h", "command": "true", **_as("x")}},
                 {"t": 2, "type": "schedule.set", "entity": "own", "data": {"every": "1h", "command": "true", **OWNER}},
                 {"t": 2, "type": "schedule.set", "entity": "w-job",
                  "data": {"session": "w", "every": "1h", "prompt": "p", **_as("hub")}}]
        return base + list(extra)

    def test_a_start_is_recorded_once_per_slot(self):
        # two clock lives sent "starting" for one slot: the first is recorded, the second life knows the launch is not
        # its own
        slot = 3600.0
        st, _, rejected = run(self.events(
            {"t": 3600, "type": "schedule.started", "entity": "own", "data": {"slot": slot}},   # the slot has not come yet
            {"t": 3601, "type": "schedule.due", "entity": "own", "data": {"slot": slot}},
            {"t": 3602, "type": "schedule.started", "entity": "own", "data": {"slot": slot}},
            {"t": 3603, "type": "schedule.started", "entity": "own", "data": {"slot": slot}},
            {"t": 3604, "type": "schedule.ran", "entity": "own", "data": {"slot": slot, "exit": "unknown"}}))
        self.assertEqual(rejected, 2)
        job = st["schedule"]["own"]
        self.assertEqual((job["started_slot"], job["ran_slot"], job.get("last_exit")), (slot, slot, None))
        (note,) = [m for m in st["messages"].values() if m.get("schedule") == "own"]
        self.assertEqual(note["session"], "hub")
        self.assertIn("the outcome is unknown", note["text"])

    def test_an_unknown_outcome_of_a_removed_job_is_not_sent_to_the_hub(self):
        slot = 3600.0
        st, _, _ = run(self.events())
        _, _, why = reducer.reduce(st, {"seq": 99, "ts": 3600, "src": "test", "type": "schedule.started", "entity": "own",
                                        "data": {}})   # not started yet, no slot: a rejection, not a core failure
        self.assertEqual(why, schedule.RUN_UNEXPECTED)
        st, _, rejected = run(self.events(
            {"t": 3601, "type": "schedule.due", "entity": "own", "data": {"slot": slot}},
            {"t": 3602, "type": "schedule.started", "entity": "own", "data": {"slot": slot}},
            {"t": 3603, "type": "schedule.remove_requested", "entity": "own", "data": OWNER},
            {"t": 3604, "type": "schedule.ran", "entity": "own", "data": {"slot": slot, "exit": "unknown"}}))
        self.assertEqual(rejected, 0)
        self.assertEqual([m for m in st["messages"].values() if m.get("schedule") == "own"], [])

    def test_apply_for_a_session_removes_its_commands_too(self):
        st, _, rejected = run(self.events({"t": 3, "type": "schedule.applied",
                                           "data": {"jobs": [], "scope": {"session": "w"}, **_as("hub")}}))
        self.assertEqual(rejected, 0)
        self.assertEqual({j: x["state"] for j, x in st["schedule"].items()},
                         {"w-mon": "removed", "w-job": "removed", "x-mon": "active", "own": "active"})

    def test_owner_of_a_command_is_its_session_while_it_is_there(self):
        st = run(self.events())[0]
        self.assertEqual({j: schedule.owner_of(st, x) for j, x in st["schedule"].items()},
                         {"w-mon": "w", "x-mon": "x", "own": "", "w-job": "w"})
        st = run(self.events({"t": 3, "type": "session.renamed", "entity": "w", "data": {"to": "w2", **OWNER}}))[0]
        self.assertEqual(schedule.owner_of(st, st["schedule"]["w-mon"]), "w2")

    def test_a_command_of_a_removed_session_runs_on_and_is_nobody_s(self):
        st, _, rejected = run(self.events(
            {"t": 3, "type": "session.close_requested", "entity": "w", "data": OWNER},   # put to sleep, then delete
            {"t": 4, "type": "turn.result", "entity": "w", "data": {"text": "HANDOFF-DONE\nDone: everything"}},
            {"t": 5, "type": "session.remove_requested", "entity": "w", "data": OWNER},
            {"t": 6, "type": "schedule.applied", "data": {"jobs": [], "scope": {"session": "w"}, **_as("hub")}},
            {"t": 3601, "type": "schedule.due", "entity": "w-mon", "data": {"slot": 3602, "channel": "core"}}))
        self.assertEqual(st["sessions"]["w"]["state"], "removed")
        mon = st["schedule"]["w-mon"]
        self.assertEqual((mon["state"], mon.get("run_slot"), schedule.owner_of(st, mon)), ("active", 3602, ""))
        self.assertEqual(st["schedule"]["w-job"]["state"], "removed")   # a session's job goes by session, as before

    def test_a_handed_over_command_is_of_the_session_it_went_to(self):
        # the handoff before sleep shows the session its commands and asks "pass on what is needed" (hub schedule
        # retarget): the monitor
        # now belongs to the one it was handed to: her list and her cleanup
        st, _, rejected = run(self.events(
            {"t": 3, "type": "schedule.retarget", "entity": "w-mon", "data": {"session": "x", **_as("w")}},
            {"t": 4, "type": "schedule.applied", "data": {"jobs": [], "scope": {"session": "w"}, **_as("hub")}}))
        self.assertEqual(rejected, 0)
        mon = st["schedule"]["w-mon"]
        self.assertEqual((mon["state"], schedule.owner_of(st, mon)), ("active", "x"))
        st = run(self.events({"t": 3, "type": "schedule.retarget", "entity": "w-mon", "data": {"session": "x", **_as("w")}},
                             {"t": 4, "type": "schedule.applied", "data": {"jobs": [], "scope": {"session": "x"}, **_as("hub")}}))[0]
        self.assertEqual(st["schedule"]["w-mon"]["state"], "removed")
        # in the set of the one it was handed to, it stays hers (the same set does not reset the handoff)
        st, _, rejected = run(self.events(
            {"t": 3, "type": "schedule.retarget", "entity": "w-mon", "data": {"session": "x", **_as("w")}},
            {"t": 4, "type": "schedule.applied", "data": {"jobs": [{"id": "w-mon", "every": "2h", "command": "true"}],
                                                          "scope": {"session": "x"}, **_as("hub")}}))
        self.assertEqual(rejected, 0)
        mon = st["schedule"]["w-mon"]
        self.assertEqual((mon["state"], mon["every"], schedule.owner_of(st, mon)), ("active", 7200, "x"))

    def test_set_with_a_session_hands_the_command_over(self):
        # 2026-10-24: `hub schedule add --id w-mon --session x --command …` from the hub answered "accepted" but the
        # command stayed
        # with w: the command's session was dropped silently. Now it hands over (like retarget); without session, the
        # previous owner
        st, _, rejected = run(self.events(
            {"t": 3, "type": "schedule.set", "entity": "w-mon", "data": {"every": "1h", "command": "true", "session": "x",
                                                                         **_as("hub")}}))
        self.assertEqual(rejected, 0)
        self.assertEqual(schedule.owner_of(st, st["schedule"]["w-mon"]), "x")
        st, _, rejected = run(self.events(
            {"t": 3, "type": "schedule.set", "entity": "w-mon", "data": {"every": "1h", "command": "true", "session": "x",
                                                                         **_as("hub")}},
            {"t": 4, "type": "schedule.set", "entity": "w-mon", "data": {"every": "2h", "command": "true", **_as("hub")}}))
        self.assertEqual(rejected, 0)
        self.assertEqual(schedule.owner_of(st, st["schedule"]["w-mon"]), "x")   # a set without session does not reset the handoff
        # a new command comes with session at once; an empty string means "not set"
        st, _, rejected = run(self.events(
            {"t": 3, "type": "schedule.set", "entity": "new-mon", "data": {"every": "1h", "command": "true", "session": "x",
                                                                           **_as("hub")}}))
        self.assertEqual((rejected, schedule.owner_of(st, st["schedule"]["new-mon"])), (0, "x"))

    def test_set_with_a_session_by_a_stranger_is_rejected_not_ignored(self):
        # a command may be changed by the hub, the owner, the one who put it and the one it was handed to; others get a
        # rejection with a reason
        _, _, rejected = run(self.events(
            {"t": 3, "type": "schedule.set", "entity": "w-mon", "data": {"every": "1h", "command": "true", "session": "x",
                                                                         **_as("x")}}))
        self.assertEqual(rejected, 1)   # x does not own w-mon (session not set, by = w)
        # no such session: a rejection, not a silent "accepted"
        st, _, rejected = run(self.events(
            {"t": 3, "type": "schedule.set", "entity": "w-mon", "data": {"every": "1h", "command": "true", "session": "nope",
                                                                         **_as("hub")}}))
        self.assertEqual(rejected, 1)
        self.assertEqual(schedule.owner_of(st, st["schedule"]["w-mon"]), "w")
        # a command cannot be planted on another session (the hub, the owner and the session itself can)
        for who, bad in ((_as("w"), 1), (_as("x"), 0), (_as("hub"), 0), (OWNER, 0)):
            _, _, rejected = run(self.events(
                {"t": 3, "type": "schedule.set", "entity": "fresh", "data": {"every": "1h", "command": "true",
                                                                           "session": "x", **who}}))
            self.assertEqual(rejected, bad, who)
        # session together with prompt on a command is still a rejection
        _, _, rejected = run(self.events(
            {"t": 3, "type": "schedule.set", "entity": "w-mon", "data": {"every": "1h", "command": "true", "prompt": "p",
                                                                         "session": "x", **_as("hub")}}))
        self.assertEqual(rejected, 1)

    def test_list_for_a_session_shows_its_commands(self):
        state = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, state, True)
        with open(os.path.join(state, "journal.jsonl"), "w", encoding="utf-8") as f:
            for seq, e in enumerate(self.events(), 1):
                f.write(json.dumps({"seq": seq, "ts": e["t"], "src": "test", "type": e["type"],
                                    "entity": e.get("entity"), "data": e.get("data") or {}}, ensure_ascii=False) + "\n")
        env = {k: v for k, v in os.environ.items() if k != "HUB_SESSION"}
        out = subprocess.run([sys.executable, HUB_CLI, "schedule", "list", "--session", "w"], capture_output=True,
                             text=True, timeout=20, env={**env, "HUB_STATE_DIR": state}).stdout
        self.assertEqual(sorted(line.split()[0] for line in out.splitlines() if not line.startswith(" ")),
                         ["w-job", "w-mon"])
        self.assertIn("w-mon  → command · w", out)


class ScheduleE2E(HubCase):
    """Clock and hubd together: a job from a session CLI -> slot -> message to the hub with a schedule label."""

    def cli(self, *args, session=None):
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CLI_WAIT": "0"}
        env.pop("HUB_SESSION", None)
        if session:
            env["HUB_SESSION"] = session
        return subprocess.run([sys.executable, HUB_CLI, *args], env=env, capture_output=True, text=True, timeout=20)

    def test_clock_fires_into_hub_session(self):
        self.until(lambda: self.hub.state["sessions"]["hub"]["state"] == "idle")
        r = self.cli("schedule", "add", "--session", "hub", "--every", "5m", "--prompt", "check the queue", "--id", "q")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.until(lambda: "q" in self.hub.state.get("schedule", {}))
        self.assertEqual(self.hub.state["schedule"]["q"]["by"], langdata.legacy("who")["owner"])   # a data value the page reads
        later = time.time() + 301
        clock = Clock(self.state, clock=lambda: later)
        self.hub.clock = lambda: time.time() + 301  # the core stamps events with the same "in 5 minutes"
        self.hub.beat()
        os.utime(os.path.join(self.state, "hubd.alive"), (later, later))  # the clock is "in 5 minutes", the heartbeat is fresh
        clock.step()
        self.until(lambda: self.hub.state["schedule"]["q"].get("runs") == 1)
        msg = next(m for m in self.hub.state["messages"].values() if m.get("schedule") == "q")
        self.assertEqual(msg["session"], "hub")
        self.assertTrue(msg["text"].startswith("[⏰ schedule q] check the queue"))
        self.until(lambda: self.hub.state["messages"][msg["id"]]["state"] == "delivered")
        out = self.cli("schedule", "list").stdout
        self.assertIn("q  → hub  every 5 min", out)
        # a session does not remove someone else's job (set by the owner, delivered to the hub)
        subprocess.run([sys.executable, HUB_CLI, "schedule", "rm", "q"], capture_output=True, text=True, timeout=20,
                       env={**os.environ, "HUB_STATE_DIR": self.state, "HUB_CLI_WAIT": "0.2", "HUB_SESSION": "hub-dev"})
        self.until(lambda: any(e["type"] == "core.rejected" for e in journal.read(os.path.join(self.state, "journal.jsonl"))
                               if e["data"].get("type") == "schedule.remove_requested"))
        self.assertEqual(self.hub.state["schedule"]["q"]["state"], "active")
        # the CLI did not wait for the answer (HUB_CLI_WAIT=0): the rejection reaches the session as a message
        # (2026-10-06: the core silently ignored it)
        self.until(lambda: any(m["session"] == "hub-dev" and "The core rejected" in m["text"] and "schedule.remove_requested q"
                               in m["text"] for m in self.hub.state["messages"].values()))
        # the hub cleans up declaratively: an empty set for the hub session's jobs removes q
        apply = subprocess.run([sys.executable, HUB_CLI, "schedule", "apply", "--session", "hub", "-"], input="[]",
                               env={**os.environ, "HUB_STATE_DIR": self.state, "HUB_CLI_WAIT": "0"},
                               capture_output=True, text=True, timeout=20)
        self.assertEqual(apply.returncode, 0, apply.stderr)
        self.until(lambda: self.hub.state["schedule"]["q"]["state"] == "removed")
        # a removal is said in words ("removed"), not only "accepted" (2026-10-06: the hub took an accepted removal for
        # not done)
        self.cli("schedule", "add", "--session", "hub", "--every", "5m", "--prompt", "more", "--id", "q2")
        self.until(lambda: "q2" in self.hub.state.get("schedule", {}))
        proc = subprocess.Popen([sys.executable, HUB_CLI, "schedule", "rm", "q2"], stdout=subprocess.PIPE, text=True,
                                env={k: v for k, v in {**os.environ, "HUB_STATE_DIR": self.state,
                                                       "HUB_CLI_WAIT": "10"}.items() if k != "HUB_SESSION"})
        self.until(lambda: proc.poll() is not None)   # the core runs while the CLI waits for the answer
        self.assertIn("removed: q2", proc.stdout.read())

    def test_add_command_with_a_session_hands_it_over(self):
        # 2026-10-24: the CLI dropped --session on --command, the core answered "accepted", and the job did not change
        self.until(lambda: self.hub.state["sessions"]["hub"]["state"] == "idle")
        self.cli("schedule", "add", "--id", "mon", "--every", "1h", "--command", "true")
        self.until(lambda: "mon" in self.hub.state.get("schedule", {}))
        self.assertEqual(schedule.owner_of(self.hub.state, self.hub.state["schedule"]["mon"]), "")
        self.cli("schedule", "add", "--id", "mon", "--session", "hub", "--every", "1h", "--command", "true")
        self.until(lambda: schedule.owner_of(self.hub.state, self.hub.state["schedule"]["mon"]) == "hub")
        self.assertIn("mon  → command · hub", self.cli("schedule", "list").stdout)

    def test_an_expired_job_is_listed_with_its_end_and_extend_brings_it_back(self):
        # 30.10: an expired watcher vanished from `hub schedule list` (only --all showed "expired"); renewing it meant
        # retyping the whole job
        self.until(lambda: self.hub.state["sessions"]["hub"]["state"] == "idle")
        tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        self.cli("schedule", "add", "--session", "hub", "--every", "1h", "--prompt", "look", "--id", "w",
                 "--check", "true", "--until", tomorrow)
        self.until(lambda: "w" in self.hub.state.get("schedule", {}))
        until = self.hub.state["schedule"]["w"]["until"]
        clock = self.hub.clock
        self.hub.clock = lambda: until + 60
        self.hub.record("schedule.ended", "w", {})
        self.hub.clock = clock
        self.until(lambda: self.hub.state["schedule"]["w"]["state"] == "expired")
        self.assertIn("ended", self.cli("schedule", "list").stdout)
        self.assertIn("hub schedule extend w", self.cli("schedule", "list").stdout)
        r = self.cli("schedule", "extend", "w")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.until(lambda: self.hub.state["schedule"]["w"]["state"] == "active")
        job = self.hub.state["schedule"]["w"]
        self.assertEqual((job["prompt"], job["check"], job["every"], job["session"]), ("look", "true", 3600, "hub"))
        self.assertGreater(job["until"], time.time() + 13 * 86400)
        r = self.cli("schedule", "extend", "nope")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(i18n.t("core.schedule.extend_no_job"), r.stderr)   # the owner's terminal: his language

    def test_list_says_when_it_cuts_a_long_text(self):
        # 2026-10-24: a command's tail was cut to 200 characters silently, so an edit at the end looked "not applied"
        long = "true " + "x" * 300 + " # the end"
        self.cli("schedule", "add", "--id", "long", "--every", "1h", "--command", long)
        self.until(lambda: "long" in self.hub.state.get("schedule", {}))
        short = self.cli("schedule", "list").stdout
        self.assertIn("(+", short)
        self.assertNotIn("# the end", short)
        self.assertIn("# the end", self.cli("schedule", "list", "--full").stdout)


@tz.need
class ClockReadsWithoutPage(unittest.TestCase):
    """The clock needs only the core state: it does not build the page snapshot (views.Reader.snapshot, 0.1 s on a live
    journal) on every new event (26.10: the idle clock held ~13-22% CPU, more than half of it a snapshot nobody read)."""

    def test_the_clock_never_builds_the_page_snapshot(self):
        from unittest import mock
        from core import views
        state = tempfile.mkdtemp(prefix="hubsched-snap-")
        self.addCleanup(shutil.rmtree, state, True)
        j = journal.Journal(os.path.join(state, "journal.jsonl"))
        now = ts("2026-09-29 10:00")
        j.append("owner.settings", data={"timezone": TZ}, ts=now)
        j.append("session.requested", "a", {"cwd": "/w", "session_id": "sid-a"}, ts=now)
        with mock.patch.object(views.Reader, "snapshot", side_effect=AssertionError("page snapshot in the clock")):
            clock = Clock(state, clock=lambda: now)
            open(os.path.join(state, "hubd.alive"), "w").close()
            os.utime(os.path.join(state, "hubd.alive"), (now, now))
            clock.step()
            j.append("schedule.set", "x", {"session": "a", "every": "30m", "prompt": "p", "channel": "cli",
                                           "owner": True}, ts=now)
            self.assertTrue(clock.tail.poll())   # the clock still sees what is new in the journal (wakes its own round)
            clock.step()
        self.assertIn("x", clock.tail.reader.state["schedule"])


class PausedWhy(unittest.TestCase):
    """Why a job is paused is the core's phrase: the state holds the English with its marks (paused_why_l10n), the page
    and `hub schedule list` show it in the reader's language."""
    EVENTS = [
        {"t": 100.0, "type": "owner.settings", "data": {"timezone": "UTC"}},
        {"t": 100.0, "type": "session.requested", "entity": "hub", "data": {"cwd": "/w", "session_id": "s-h", "kind": "hub"}},
        {"t": 100.0, "type": "session.started", "entity": "hub", "data": {"session_id": "s-h"}},
        {"t": 100.0, "type": "session.requested", "entity": "maps", "data": {"cwd": "/w", "session_id": "s-m"}},
        {"t": 100.0, "type": "session.started", "entity": "maps", "data": {"session_id": "s-m"}},
        {"t": 101.0, "type": "schedule.set", "entity": "maps-daily",
         "data": {"session": "maps", "cron": "0 9 * * *", "prompt": "report", "channel": "cli", "owner": True}},
        {"t": 101.0, "type": "session.ended", "entity": "maps", "data": {"reason": "prompt_input_exit"}},
        {"t": 102.0, "type": "session.remove_requested", "entity": "maps", "data": {"channel": "cli", "owner": True}},
        {"t": 86400.0 + 9 * 3600 + 30, "type": "schedule.due", "entity": "maps-daily", "data": {"slot": 86400.0 + 9 * 3600}},
    ]

    def test_the_owner_reads_why_a_job_is_paused_in_his_language(self):
        from core import coretext, views
        st, _, _ = run(json.loads(json.dumps(self.EVENTS)))
        job = st["schedule"]["maps-daily"]
        self.assertEqual((job["state"], job["paused_why"]), ("paused", "session maps is gone (removed)"))
        st = json.loads(json.dumps(st))   # as state.json and a reader's fold keep it
        job = st["schedule"]["maps-daily"]
        gone = i18n.tr("ru", "core.schedule.paused_gone", session="maps", gone=i18n.tr("ru", "core.schedule.gone_removed"))
        self.assertEqual(schedule.state_text(job, 0, "UTC", "ru"), i18n.tr("ru", "core.schedule.state_paused", why=gone))
        self.assertEqual(schedule.state_text(job, 0, "UTC", "en"), "paused: session maps is gone (removed)")
        (row,) = views.schedule_view(st, 0)
        self.assertEqual(row["paused_why"], "session maps is gone (removed)")   # the page, in the page server's language
        self.assertNotIn("paused_why_l10n", row)
        st, _, _ = run(json.loads(json.dumps(self.EVENTS)) + [{"t": 86400.0 + 9 * 3600 + 60, "type": "schedule.retarget",
                                                              "entity": "maps-daily",
                                                              "data": {"session": "hub", "channel": "cli", "owner": True}}])
        job = st["schedule"]["maps-daily"]
        self.assertIsNone(job["paused_why"])
        self.assertNotIn("paused_why_l10n", job)
        self.assertIsNone(coretext.got(job, "paused_why"))


if __name__ == "__main__":
    unittest.main()
