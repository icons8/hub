"""Pre-check of a schedule job (`check`): a "is there anything new" watcher ticked as a session turn every 15 minutes,
and almost all turns ended with "nothing new". Now a session's job has an optional command: the clock
(modules/scheduler) runs it without a session; exit 0 — nothing new, do not wake the session (one schedule.ran in the
journal); exit 3 — wake it, the command's output goes into the text; any other code or a timeout — wake it with an
error, so that the check is not silently lost."""
import tests  # noqa: F401 — keep the machine's settings out (~/.config/hub-core), see tests/__init__.py
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import platforms
from core import flow, journal, reducer, schedule, spool, views
from modules.scheduler import clock as clockmod
from modules.scheduler.clock import Clock
from tests.fakes import tz
from tests.test_schedule import TZ, ts
from tests.test_tools import HUB_CLI

CORE = {"channel": "core"}


class Reducer(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("owner.settings", timezone="UTC")
        self.ev("session.requested", "gp", cwd="/w", session_id="s", channel="cli", owner=True)
        self.ev("session.started", "gp")
        self.ev("schedule.set", "watch", session="gp", every="1m", prompt="watch tick", check="watch.py --new",
                channel="session", sender="gp")

    def ev(self, t, e=None, ts=None, **d):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": self.seq if ts is None else ts, "type": t,
                                                   "entity": e, "data": d})
        return why

    def msgs(self):
        return [m for m in self.st["messages"].values() if m.get("schedule") == "watch"]

    def due(self, n=0, **res):
        slot = self.st["schedule"]["watch"]["start"] + 60 * n
        return self.ev("schedule.due", "watch", ts=slot + 2, slot=slot, took=1.2, **res, **CORE)

    def test_one_minute_only_with_a_check(self):
        self.assertEqual(self.st["schedule"]["watch"]["every"], 60)
        why = self.ev("schedule.set", "plain1", session="gp", every="1m", prompt="x", channel="session", sender="gp")
        self.assertIn("5 minutes", why)

    def test_a_check_timeout_is_a_bounded_field_of_the_job(self):
        self.assertNotIn("check_timeout", self.st["schedule"]["watch"])
        for given, kept in ((300, 300), ("45", 45), (90.0, 90), (900, 900)):
            why = self.ev("schedule.set", "watch", session="gp", every="1m", prompt="watch tick", check="watch.py --new",
                          check_timeout=given, channel="session", sender="gp")
            self.assertIsNone(why, given)
            self.assertEqual(self.st["schedule"]["watch"]["check_timeout"], kept)
        self.ev("schedule.set", "watch", session="gp", every="1m", prompt="watch tick", check="watch.py --new",
                channel="session", sender="gp")
        self.assertNotIn("check_timeout", self.st["schedule"]["watch"])   # set again without it: the default

    def test_a_check_timeout_that_is_not_a_sane_number_is_refused_not_a_crash(self):
        for bad in ("abc", -1, 0, 0.5, 901, 10 ** 400, float("inf"), float("nan"), True, [], {}, "10 ** 400", "1" * 5000):
            why = self.ev("schedule.set", "w2", session="gp", every="1m", prompt="x", check="c", check_timeout=bad,
                          channel="session", sender="gp")
            self.assertIn("check_timeout", why or "", repr(bad)[:40])
            self.assertNotIn("w2", self.st["schedule"])
        for none in (None, ""):   # not given (the command line drops an empty one too)
            self.assertIsNone(self.ev("schedule.set", "w2", session="gp", every="1m", prompt="x", check="c",
                                      check_timeout=none, channel="session", sender="gp"))
            self.assertNotIn("check_timeout", self.st["schedule"]["w2"])
        why = self.ev("schedule.set", "w3", session="gp", every="1h", prompt="x", check_timeout=60, channel="session", sender="gp")
        self.assertIn("check_timeout", why)   # without a check there is nothing to time

    def test_extending_a_job_keeps_its_check_timeout(self):
        self.ev("schedule.set", "watch", session="gp", every="1m", prompt="watch tick", check="watch.py --new",
                check_timeout=300, channel="session", sender="gp")
        spec = schedule.extend_spec(self.st["schedule"]["watch"], None, 100, "UTC")
        self.assertEqual(spec["check_timeout"], 300)

    def test_exit_0_is_quiet(self):
        self.assertIsNone(self.due(exit=0))
        job = self.st["schedule"]["watch"]
        self.assertEqual(self.msgs(), [])
        self.assertEqual((job["last_check"], job["quiet"], job["runs"]), (0, 1, 0))
        self.assertIsNone(self.due(1, exit=0))
        self.assertEqual(self.st["schedule"]["watch"]["quiet"], 2)

    def test_exit_3_wakes_with_the_whole_output(self):
        out = "\n".join(f"IT-{k}: new comment" for k in range(500))   # ~12 thousand characters — in full
        self.due(exit=3, out=out)
        [m] = self.msgs()
        self.assertEqual(m["session"], "gp")
        self.assertIn("watch tick", m["text"])
        self.assertIn(out, m["text"])
        self.assertNotIn("failed", m["text"])
        self.assertEqual(self.st["schedule"]["watch"]["runs"], 1)

    def test_other_exit_or_timeout_wakes_with_the_error(self):
        for code in (1, "timeout"):
            with self.subTest(code=code):
                self.setUp()
                self.due(exit=code, out="Traceback: boom")
                [m] = self.msgs()
                self.assertIn("watch tick", m["text"])
                self.assertIn("watch.py --new", m["text"])
                self.assertIn("Traceback: boom", m["text"])
                self.assertIn("timeout" if code == "timeout" else "code 1", m["text"])

    def test_slot_without_a_code_wakes_as_before(self):
        # a clock of an old version does not run the pre-check — the job is not lost
        self.due()
        self.assertEqual([m["text"] for m in self.msgs()], ["[⏰ schedule watch] watch tick"])

    def test_news_while_the_last_wake_is_queued_joins_it(self):
        # the session is busy: news from the next check is appended to the queued one, not lost
        self.ev("turn.started", "gp", marker=None, prompt_head="other business")
        self.due(exit=3, out="task 1")
        self.due(1, exit=3, out="task 2")
        self.due(2, exit=1, out="Traceback")
        [m] = self.msgs()
        self.assertEqual(m["state"], "queued")
        self.assertIn("task 1", m["text"])
        self.assertIn("task 2", m["text"])
        self.assertNotIn("Traceback", m["text"])

    def test_check_belongs_to_a_session_job(self):
        why = self.ev("schedule.set", "mon", every="15m", command="true", check="x", channel="cli", owner=True)
        self.assertIsNotNone(why)
        self.assertNotIn("mon", self.st["schedule"])

    def test_changing_only_the_check_is_an_edit(self):
        self.ev("schedule.set", "watch", session="gp", every="1m", prompt="watch tick", check="watch.py --all",
                channel="session", sender="gp")
        self.assertEqual(self.st["schedule"]["watch"]["check"], "watch.py --all")

    def test_job_without_check_wakes_as_before(self):
        self.ev("schedule.set", "plain", session="gp", every="15m", prompt="simply", channel="session", sender="gp")
        slot = self.st["schedule"]["plain"]["start"]
        self.ev("schedule.due", "plain", ts=slot + 1, slot=slot, **CORE)
        self.assertEqual([m["text"] for m in self.st["messages"].values() if m.get("schedule") == "plain"],
                         ["[⏰ schedule plain] simply"])


class Shown(unittest.TestCase):
    """The session feed and the Flow: a quiet check shows no line and no arrow; "something new" looks like an ordinary firing."""

    def events(self):
        out = []

        def ev(t, e=None, **d):
            out.append({"seq": len(out) + 1, "ts": 1000 + len(out), "type": t, "entity": e, "data": d})
        ev("session.requested", "gp", cwd="/w", session_id="s", channel="cli", owner=True)
        ev("session.started", "gp")
        ev("schedule.set", "watch", session="gp", every="1m", prompt="tick", check="w.py", channel="cli", owner=True)
        ev("schedule.due", "watch", slot=1003, exit=0, took=1, channel="core")
        ev("schedule.due", "watch", slot=1004, exit=3, took=1, out="new", channel="core")
        return out

    def test_feed_shows_only_the_wake(self):
        rd = views.Reader()
        for e in self.events():
            rd.feed(e)
        items = [x for x in rd.timelines["gp"] if x.get("channel") == "schedule"]
        self.assertEqual([(x["job"], x["seq"]) for x in items], [("watch", 5)])
        self.assertIn("new", items[0]["text"])

    def test_flow_history_draws_only_the_wake(self):
        lines = [json.dumps(e, ensure_ascii=False, separators=(",", ":")).encode() for e in self.events()]
        sig = [(x["seq"], x["from"], x["to"]) for x in flow.signals(lines) if x["from"] == "schedule"]
        self.assertEqual(sig, [(5, "schedule", "gp")])


@tz.need
class ClockRuns(unittest.TestCase):
    """The clock runs the pre-check before the slot: without a session (the environment is on its behalf), with a
    timeout, one per job; the outcome goes into the same schedule.due."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubcheck-")
        self.cwd = tempfile.mkdtemp(prefix="hubcheck-cwd-")
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        self.now = ts("2026-09-29 10:00")
        self.slept = 0   # seconds the machine slept: the wall clock ran on, the awake clock did not
        self.clock = Clock(self.state, clock=lambda: self.now, awake=lambda: self.now - self.slept)
        self.j.append("owner.settings", data={"timezone": TZ}, ts=self.now)
        self.j.append("session.requested", "a", {"cwd": self.cwd, "session_id": "sid-a"}, ts=self.now)

    def tearDown(self):
        shutil.rmtree(self.state, ignore_errors=True)
        shutil.rmtree(self.cwd, ignore_errors=True)

    def spooled(self):
        d = os.path.join(self.state, "spool")
        items = [e for _, e in spool.take(d)]
        for n in os.listdir(d) if os.path.isdir(d) else []:
            os.unlink(os.path.join(d, n))
        return items

    def beat(self):
        path = os.path.join(self.state, "hubd.alive")
        open(path, "w").close()
        os.utime(path, (self.now, self.now))

    def set_job(self, code):
        self.j.append("schedule.set", "w", {"session": "a", "every": "1m", "prompt": "p", "check": code,
                                            "channel": "cli", "owner": True}, ts=self.now)

    def drain(self):
        out = []
        for _ in range(200):
            out += [e for e in self.spooled() if e["entity"] == "w"]
            if not self.clock.running:
                break
            time.sleep(0.05)
            self.now += 0.05
            self.beat()
            self.clock.step()
        return out + [e for e in self.spooled() if e["entity"] == "w"]

    def run_check(self, code):
        """The slot has come — the clock first runs the check and sends one schedule.due with its outcome."""
        self.set_job(code)
        self.now += 60
        self.beat()
        self.clock.step()
        self.assertEqual(list(self.clock.running), ["w"])
        evs = self.drain()
        self.assertEqual([e["type"] for e in evs], ["schedule.due"])
        return evs[0]["data"]

    def test_reports_code_and_output_runs_as_the_session_in_its_folder(self):
        py = f"\"{sys.executable}\""
        code = (f"{py} -c \"import os, sys; print(os.environ.get('HUB_SESSION'), os.path.realpath(os.getcwd())); "
                f"sys.exit(3)\"")
        data = self.run_check(code)
        self.assertEqual(data["exit"], 3)
        self.assertEqual(data["out"].split(), ["a", os.path.realpath(self.cwd)])

    def owner_settings(self, env, names):
        """The owner's Claude Code settings `env` and hub.json `user_env` (the names of it that jobs and sessions get)."""
        claude = os.path.join(self.state, "claude")
        os.makedirs(claude)
        with open(os.path.join(claude, "settings.json"), "w") as f:
            json.dump({"env": env}, f)
        cfg = os.path.join(self.state, "hub.json")
        with open(cfg, "w") as f:
            json.dump({"user_env": names}, f)
        patch = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": claude, "HUB_CONFIG": cfg})
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_check_and_a_command_get_the_user_env_like_the_sessions(self):
        # the credentials of work tools the owner listed in hub.json `user_env` reach sessions; a check or a command of
        # the schedule is the same work and got them not (a check calling a tracker's CLI failed with no login)
        self.owner_settings({"TRACKER_TOKEN": "t-1", "HUB_SESSION": "x", "OTHER": "no", "HTTPS_PROXY": "http://proxy.test:3128"},
                            ["TRACKER_TOKEN", "HUB_SESSION"])
        code = f"\"{sys.executable}\" -c \"import os, sys; print(os.environ.get('TRACKER_TOKEN'), os.environ.get('OTHER'), " \
               f"os.environ.get('HUB_SESSION')); sys.exit(3)\""
        data = self.run_check(code)
        self.assertEqual(data["out"].split(), ["t-1", "None", "a"])   # a name not listed stays out; the hub's own win
        env = self.clock.env({"id": "w"})   # the command job launches with this env
        self.assertEqual((env["TRACKER_TOKEN"], env["HTTPS_PROXY"]), ("t-1", "http://proxy.test:3128"))   # + the always-passed login/network names
        self.assertNotIn("HUB_SESSION", self.clock.env({"id": "w"}))

    def test_quiet_run_carries_no_output(self):
        data = self.run_check(f"\"{sys.executable}\" -c \"print('nothing new, 40 list lines')\"")
        self.assertEqual(data["exit"], 0)
        self.assertNotIn("out", data)
        self.assertEqual([n for n in os.listdir(os.path.join(self.state, "logs", "schedule")) if n.endswith(".check")], [])

    def test_long_output_keeps_the_start_and_the_file(self):
        data = self.run_check(f"\"{sys.executable}\" -c \"import sys; print('é' * 30000); sys.exit(3)\"")
        self.assertTrue(data["out"].startswith("é" * 20000))
        full = data["out"].rsplit("in full: ", 1)[1].rstrip("]")
        self.assertIn(full, data["out"])
        self.assertEqual(open(full, encoding="utf-8").read().strip(), "é" * 30000)

    def test_error_keeps_only_the_tail(self):
        data = self.run_check(f"\"{sys.executable}\" -c \"import sys; print('x' * 9000 + 'THE END'); sys.exit(1)\"")
        self.assertEqual(data["exit"], 1)
        self.assertLessEqual(len(data["out"]), 4000)
        self.assertIn("THE END", data["out"])

    def test_result_of_a_changed_check_is_dropped(self):
        self.set_job(f"\"{sys.executable}\" -c \"import sys, time; time.sleep(0.5); sys.exit(3)\"")
        self.now += 60
        self.beat()
        self.clock.step()
        self.j.append("schedule.set", "w", {"session": "a", "every": "1m", "prompt": "p", "check": "another",
                                            "channel": "cli", "owner": True}, ts=self.now)   # changed while the check was running
        evs = self.drain()
        # the outcome of the old one (3) is not sent; the slot is decided on the next round — by the new check already
        # (the new check text is not an existing command)
        self.assertEqual([(e["type"], e["data"]["exit"] not in (0, 3)) for e in evs], [("schedule.due", True)])

    def test_one_run_per_job_at_a_time(self):
        # the check outlasts the interval: the next slot waits for it to end, a second copy is not started
        self.set_job(f"\"{sys.executable}\" -c \"import time; time.sleep(1.5)\"")
        self.now += 60
        self.beat()
        self.clock.step()
        first = self.clock.running["w"][0]
        self.now += 120
        self.beat()
        self.clock.step()
        self.assertIs(self.clock.running["w"][0], first)
        evs = self.drain()   # the first one ended — the next starts at once, for the last slot that came (the missed ones count as one)
        self.assertEqual([(e["type"], e["data"]["slot"]) for e in evs],
                         [("schedule.due", ts("2026-09-29 10:01")), ("schedule.due", ts("2026-09-29 10:03"))])

    def test_timeout_is_reported(self):
        old = clockmod.CHECK_TIMEOUT
        clockmod.CHECK_TIMEOUT = 0.5
        try:
            data = self.run_check(f"\"{sys.executable}\" -c \"import time; time.sleep(30)\"")
        finally:
            clockmod.CHECK_TIMEOUT = old
        self.assertEqual(data["exit"], "timeout")

    def test_a_jobs_own_check_timeout_replaces_the_default(self):
        old = clockmod.CHECK_TIMEOUT
        clockmod.CHECK_TIMEOUT = 100
        try:
            self.j.append("schedule.set", "w", {"session": "a", "every": "1m", "prompt": "p", "check_timeout": 1,
                                                "check": f"\"{sys.executable}\" -c \"import time; time.sleep(30)\"",
                                                "channel": "cli", "owner": True}, ts=self.now)
            self.now += 60
            self.beat()
            self.clock.step()
            evs = self.drain()
        finally:
            clockmod.CHECK_TIMEOUT = old
        self.assertEqual([e["data"]["exit"] for e in evs], ["timeout"])   # not after the 100 s of the default

    def test_the_limit_is_the_one_the_check_began_with(self):
        old = clockmod.CHECK_TIMEOUT
        clockmod.CHECK_TIMEOUT = 100
        try:
            self.j.append("schedule.set", "w", {"session": "a", "every": "1m", "prompt": "p", "check_timeout": 600,
                                                "check": f"\"{sys.executable}\" -c \"import time; time.sleep(30)\"",
                                                "channel": "cli", "owner": True}, ts=self.now)
            self.now += 60
            self.beat()
            self.clock.step()
            self.assertEqual(self.clock.limits["w"], 600)
            self.j.append("schedule.set", "w", {"session": "a", "every": "1m", "prompt": "p", "check_timeout": 1,
                                                "check": f"\"{sys.executable}\" -c \"import time; time.sleep(30)\"",
                                                "channel": "cli", "owner": True}, ts=self.now)   # lowered while it runs
            self.now += 5
            self.beat()
            self.clock.step()
            self.assertEqual(list(self.clock.running), ["w"])           # not killed by the new limit
        finally:
            clockmod.CHECK_TIMEOUT = old
            for proc, *_ in self.clock.running.values():
                proc.kill()

    def test_a_longer_check_timeout_lets_a_slow_check_finish(self):
        old = clockmod.CHECK_TIMEOUT
        clockmod.CHECK_TIMEOUT = 0.5
        try:
            self.j.append("schedule.set", "w", {"session": "a", "every": "1m", "prompt": "p", "check_timeout": 60,
                                                "check": f"\"{sys.executable}\" -c \"import sys, time; time.sleep(1.2); sys.exit(3)\"",
                                                "channel": "cli", "owner": True}, ts=self.now)
            self.now += 60
            self.beat()
            self.clock.step()
            evs = self.drain()
        finally:
            clockmod.CHECK_TIMEOUT = old
        self.assertEqual([e["data"]["exit"] for e in evs], [3])

    def test_machine_sleep_is_not_work_time(self):
        # The machine slept while the check ran (the process was frozen with it): the clock woke 3 hours later by the
        # wall clock, which is not 3 hours of the check's work
        self.set_job(f"\"{sys.executable}\" -c \"import time; time.sleep(1)\"")
        self.now += 60
        self.beat()
        self.clock.step()
        self.now += 3 * 3600
        self.slept += 3 * 3600
        self.beat()
        self.clock.step()
        self.assertEqual(list(self.clock.running), ["w"])
        evs = [e for e in self.drain() if e["type"] == "schedule.due"]
        self.assertEqual([e["data"]["exit"] for e in evs[:1]], [0])
        self.assertLess(evs[0]["data"]["took"], 60)   # portable: took is counted on the test's own fake clock, not wall time


class Cli(unittest.TestCase):
    def hub(self, *args):
        state = tempfile.mkdtemp(prefix="hubcheck-cli-")
        env = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0", "HUB_SESSION": "gp"}
        r = subprocess.run([sys.executable, HUB_CLI, *args], env=env, capture_output=True, text=True, timeout=20)
        evs = [ev for _, ev in spool.take(os.path.join(state, "spool"))]
        shutil.rmtree(state, ignore_errors=True)
        return r, evs

    def test_add_with_check(self):
        r, evs = self.hub("schedule", "add", "--every", "15m", "--prompt", "tick", "--check", "watch.py --new")
        self.assertEqual(r.returncode, 0, r.stderr)
        [e] = [e for e in evs if e["type"] == "schedule.set"]
        self.assertEqual((e["data"]["check"], e["data"]["prompt"]), ("watch.py --new", "tick"))

    def test_add_with_a_check_timeout(self):
        r, evs = self.hub("schedule", "add", "--every", "15m", "--prompt", "tick", "--check", "watch.py --new",
                          "--check-timeout", "300")
        self.assertEqual(r.returncode, 0, r.stderr)
        [e] = [e for e in evs if e["type"] == "schedule.set"]
        self.assertEqual(e["data"]["check_timeout"], "300")   # the core validates it (schedule.normalize)

    def test_a_check_timeout_needs_a_check(self):
        r, evs = self.hub("schedule", "add", "--every", "15m", "--prompt", "tick", "--check-timeout", "300")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual([e for e in evs if e["type"] == "schedule.set"], [])

    def test_list_shows_the_limit_of_a_check(self):
        state = tempfile.mkdtemp(prefix="hubcheck-list-")
        env = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0", "HUB_SESSION": "gp", "HUB_LANG": "en"}
        j = journal.Journal(os.path.join(state, "journal.jsonl"))
        j.append("owner.settings", data={"timezone": "UTC"})
        j.append("session.requested", "gp", {"cwd": "/w", "session_id": "s", "channel": "cli", "owner": True})
        j.append("session.started", "gp", {"session_id": "s"})
        j.append("schedule.set", "w", {"session": "gp", "every": "5m", "prompt": "p", "check": "true", "check_timeout": 600,
                                       "channel": "session", "sender": "gp"})
        r = subprocess.run([sys.executable, HUB_CLI, "schedule", "list"], env=env, capture_output=True, text=True, timeout=20)
        platforms.current.rmtree(state)
        self.assertIn("pre-check: $ true", r.stdout)
        self.assertIn("(limit 600 s)", r.stdout)

    def test_check_needs_a_prompt(self):
        r, evs = self.hub("schedule", "add", "--id", "m", "--every", "15m", "--command", "true", "--check", "x")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual([e for e in evs if e["type"] == "schedule.set"], [])

    def test_help_describes_check(self):
        r, _ = self.hub("schedule", "add", "--help")   # the index names commands; flags are in the command's help
        self.assertIn("--check", r.stdout + r.stderr)
        self.assertIn("code 0 — nothing new", r.stdout)
        self.assertIn("--check-timeout", r.stdout)

    def test_help_says_which_shell_runs_command_and_check(self):
        # the clock starts both with shell=True: cmd.exe on Windows, /bin/sh elsewhere; the help says so (an agent wrote bash there)
        r, _ = self.hub("schedule", "add", "--help")
        self.assertIn("cmd.exe", r.stdout + r.stderr)
        self.assertIn("/bin/sh", r.stdout + r.stderr)
        self.assertIn("full path", r.stdout + r.stderr)   # --command starts in ~, --check in the session's folder

    def test_polling_job_without_check_gets_a_hint(self):
        # 11.11: sessions set polling jobs ("look at the new comments") without --check, and every tick woke the model
        for when in (("--every", "10m"), ("--cron", "*/10 * * * *")):
            with self.subTest(when=when):
                r, evs = self.hub("schedule", "add", *when, "--prompt", "look at the new comments on MR !12")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(len([e for e in evs if e["type"] == "schedule.set"]), 1)   # a hint, not a refusal
                self.assertIn("add --check: the model wakes only when there is something new", r.stdout)
                self.assertTrue(r.stdout.rstrip().splitlines()[-1].startswith("A polling job?"), r.stdout)   # one line, the last

    def test_no_hint_where_a_check_does_not_apply(self):
        for args in (("--every", "10m", "--prompt", "tick", "--check", "true"),
                     ("--at", "2099-01-01 09:00", "--prompt", "call back"),
                     ("--every", "10m", "--id", "m", "--command", "true")):
            with self.subTest(args=args):
                r, _ = self.hub("schedule", "add", *args)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertNotIn("model wakes only", r.stdout)


if __name__ == "__main__":
    unittest.main()
