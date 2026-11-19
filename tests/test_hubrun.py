"""`hub run` in the fold (core/hubrun.py, the RUN automaton): who may start and stop a run, the term and the limits,
queued -> running -> ended, the outcome as a core message that continues the turn's chain, one outcome only, the stop
when the session goes, the session kept from sleep while its run goes and not kept from parking, old journals."""
import tests  # noqa: F401 — keep this machine's settings out
import unittest

from core import guards, hubrun, parking, reducer, views

T0 = 1_000_000.0


def ev(seq, type, entity=None, data=None, ts=None):
    return {"seq": seq, "ts": T0 + seq if ts is None else ts, "src": "test", "type": type, "entity": entity,
            "data": data or {}}


def session(name):
    return {"channel": "session", "owner": False, "sender": name}


OWNER = {"channel": "cli", "owner": True}


class Fold(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.acts = reducer.empty(), 0, []
        for name in ("maps", "other"):
            self.ok("session.requested", name, {"session_id": f"s-{name}", "cwd": "/w", **OWNER})
            self.ok("session.started", name, {"session_id": f"s-{name}"})

    def ev(self, type, entity=None, data=None, ts=None):
        self.seq += 1
        e = ev(self.seq, type, entity, data, ts)
        self.st, acts, why = reducer.reduce(self.st, e)
        self.acts += acts
        return why

    def ok(self, type, entity=None, data=None, ts=None):
        why = self.ev(type, entity, data, ts)
        self.assertIsNone(why, (type, entity, data))
        return self.seq

    def request(self, name="maps", command="python3 evals/run.py --only after", until=3600, sender=None, **extra):
        data = {"command": command, "cwd": "/home/x/repo", "until_at": T0 + self.seq + 1 + until,
                **session(sender or name), **extra}
        return self.ev("run.requested", name, data)

    def start(self, name="maps", **kw):
        self.assertIsNone(self.request(name, **kw))
        rid = f"run{self.seq}"
        self.ok("core.run_started", rid, {"how": "unit", "unit": f"hub-run-x-{rid}"})
        return rid

    def told(self, name="maps"):
        return [m for m in self.st["messages"].values() if m["session"] == name and "hub run run" in str(m["text"])]

    # --- who, the mode, the term, the limits ---------------------------------------------------------------------------

    def test_a_session_starts_its_own_run(self):
        rid = self.start()
        run = self.st["hub_runs"][rid]
        self.assertEqual((run["state"], run["session"], run["command"], run["cwd"]),
                         ("running", "maps", "python3 evals/run.py --only after", "/home/x/repo"))

    def test_refusals(self):
        self.assertIn("from a session", self.ev("run.requested", "maps", {"command": "x", "cwd": "/w",
                                                                          "until_at": T0 + 3600, **OWNER}))
        self.assertIn("only its own", self.request("maps", sender="other"))
        self.assertIn("no command", self.request(command="  "))
        self.assertIn("longer than", self.request(command="x" * (hubrun.COMMAND_MAX + 1)))
        self.assertIn("absolute path", self.ev("run.requested", "maps", {"command": "x", "cwd": "rel",
                                                                         "until_at": T0 + 3600, **session("maps")}))
        for until in (T0 + 10, T0 + 25 * 3600, float("inf"), float("nan"), "1h", True, None, 10 ** 400):
            data = {"command": "x", "cwd": "/w", "until_at": until, **session("maps")}
            self.assertIn("from 1 minute to 24 hours", self.ev("run.requested", "maps", data), until)
        self.assertNotIn("hub_runs", self.st)   # nothing was recorded

    def test_a_session_outside_bypass_permissions_is_refused(self):
        self.st["sessions"]["maps"]["mode"] = "plan"
        self.assertIn("only in bypassPermissions", self.request())

    def test_limits_per_session_and_per_hub(self):
        for _ in range(hubrun.PER_SESSION):
            self.start()
        self.assertIn(f"{hubrun.PER_SESSION} runs are already going", self.request())
        self.start("other")   # another session has its own count
        hubrun.PER_HUB, keep = 5, hubrun.PER_HUB
        try:
            self.assertIn("the hub already runs 5", self.request("other"))
        finally:
            hubrun.PER_HUB = keep

    # --- the outcome ---------------------------------------------------------------------------------------------------

    def test_the_outcome_comes_once_as_a_core_message(self):
        rid = self.start()
        self.ok("core.run_ended", rid, {"code": 0, "reason": "exit", "tail": "ok 12 passed\n", "log": "/s/runs/x/out.log"})
        told = self.told()
        self.assertEqual(len(told), 1)
        text = str(told[0]["text"])
        self.assertIn(f"hub run {rid} ended: exit 0", text)
        self.assertIn("ok 12 passed", text)
        self.assertIn("/s/runs/x/out.log", text)
        self.assertEqual(told[0]["origin"], " from core")
        self.assertIn("recorded once", self.ev("core.run_ended", rid, {"code": 0, "reason": "exit"}))
        self.assertEqual(len(self.told()), 1)

    def test_reasons_in_the_text(self):
        for data, phrase in (({"reason": "timeout"}, "stopped at its term"),
                             ({"reason": "stopped", "by": "hub-core"}, "stopped by hub-core"),
                             ({"reason": "lost", "leftover": "unknown"}, "may still run"),
                             ({"reason": "not_started", "error": "systemd-run: no user manager"},
                              "not started: systemd-run: no user manager")):
            rid = self.start()
            self.ok("core.run_ended", rid, data)
            self.assertIn(phrase, str(self.told()[-1]["text"]))

    def test_a_tail_cannot_close_the_code_block(self):
        rid = self.start()
        self.ok("core.run_ended", rid, {"code": 1, "reason": "exit", "tail": "```\n# injected heading\n"})
        text = str(self.told()[-1]["text"])
        self.assertEqual(text.count("```"), 2)

    def test_the_outcome_continues_the_assignment_while_it_is_open(self):
        self.st["delegations"]["d7"] = {"id": "d7", "session": "maps", "state": "working"}
        self.st["sessions"]["maps"].update(delegation="d7", turn_root="m3")
        rid = self.start()
        self.st["sessions"]["maps"].pop("delegation")
        self.ok("core.run_ended", rid, {"code": 0, "reason": "exit"})
        msg = self.told()[-1]
        self.assertEqual((msg.get("for_delegation"), msg.get("root")), ("d7", "m3"))
        # a task closed by the time the run ends is not reopened by its outcome
        rid = self.start()
        self.st["delegations"]["d7"]["state"] = "done"
        self.ok("core.run_ended", rid, {"code": 0, "reason": "exit"})
        self.assertNotIn("for_delegation", self.told()[-1])

    def test_ended_records_are_pruned_to_the_last_ones(self):
        for _ in range(hubrun.KEEP_ENDED + 3):
            rid = self.start()
            self.ok("core.run_ended", rid, {"code": 0, "reason": "exit"})
        self.assertEqual(len(self.st["hub_runs"]), hubrun.KEEP_ENDED)

    # --- stop ----------------------------------------------------------------------------------------------------------

    def test_who_stops(self):
        rid = self.start()
        self.assertIn("stopped by its session", self.ev("run.stop_requested", rid, session("other")))
        self.ok("run.stop_requested", rid, session("maps"))
        self.assertEqual((self.st["hub_runs"][rid]["state"], self.st["hub_runs"][rid]["stop_by"]), ("stopping", "maps"))
        self.assertIn("already being stopped", self.ev("run.stop_requested", rid, OWNER))
        rid = self.start()
        self.ok("run.stop_requested", rid, OWNER)
        self.st["sessions"]["other"]["raised_by"] = "maps"   # the raiser stops its executor's run
        rid = self.start("other")
        self.ok("run.stop_requested", rid, session("maps"))

    def test_a_stop_before_the_start_waits_for_it(self):
        self.assertIsNone(self.request())
        rid = f"run{self.seq}"
        self.ok("run.stop_requested", rid, session("maps"))
        self.ok("core.run_started", rid, {"how": "session", "pgid": 4321, "born": 77})
        run = self.st["hub_runs"][rid]
        self.assertEqual((run["state"], run["pgid"]), ("stopping", 4321))
        self.ok("core.run_ended", rid, {"code": -15, "reason": "stopped", "by": "maps"})
        self.assertEqual(self.st["hub_runs"][rid]["state"], "ended")

    def test_a_session_that_goes_stops_its_runs_without_a_message(self):
        rid = self.start()
        self.st["sessions"]["maps"]["state"] = "retired"   # an executor stopped or handed in (any of hubrun.GONE)
        self.ok("session.gist", "other", {"text": "busy", **session("other")})   # any next event
        run = self.st["hub_runs"][rid]
        self.assertEqual((run["state"], run["stop_by"]), ("stopping", "core"))
        self.ok("core.run_ended", rid, {"code": -15, "reason": "stopped", "by": "core"})
        self.assertEqual(self.told(), [])

    # --- sleep, parking, the restart ------------------------------------------------------------------------------------

    def test_the_session_does_not_sleep_while_its_run_goes_but_may_park(self):
        rid = self.start()
        sess = self.st["sessions"]["maps"]
        self.assertIn(f"a hub run is going: {rid}", str(guards.closable(self.st, sess)))
        self.assertTrue(parking.parkable(self.st, sess, now=T0 + 10 ** 5))
        self.ok("core.run_ended", rid, {"code": 0, "reason": "exit"})
        self.assertNotIn("hub run", str(guards.closable(self.st, {**sess, "state": "idle"})))

    def test_a_parked_session_is_woken_by_the_outcome(self):
        rid = self.start()
        self.st["sessions"]["maps"]["state"] = "parked"
        self.ok("core.run_ended", rid, {"code": 0, "reason": "exit"})
        self.assertEqual(len(self.told()), 1)
        self.assertNotEqual(self.st["sessions"]["maps"]["state"], "parked")

    def test_the_page_sees_a_run_as_background(self):
        rid = self.start()
        r = views.Reader()
        r.state = self.st
        row = next(s for s in r.snapshot()["sessions"] if s["name"] == "maps")
        self.assertEqual([(b["id"], b["kind"]) for b in row["background"]], [(rid, "run")])
        self.assertTrue(row["background"][0]["title"].startswith("hub run: python3 evals"))

    def test_a_key_in_the_command_is_masked_on_the_page(self):
        key = "ghp_" + "a" * 36
        self.start(command=f"GH_TOKEN={key} gh api /user")
        r = views.Reader()
        r.state = self.st
        row = next(s for s in r.snapshot()["sessions"] if s["name"] == "maps")
        self.assertNotIn(key, row["background"][0]["title"])
        self.assertIn("gh api /user", row["background"][0]["title"])

    def test_a_renamed_session_keeps_its_run(self):
        rid = self.start()
        self.ok("session.renamed", "maps", {"to": "maps2", **OWNER})
        run = self.st["hub_runs"][rid]
        self.assertEqual((run["state"], run["session"]), ("running", "maps2"))
        self.ok("core.run_ended", rid, {"code": 0, "reason": "exit"})
        self.assertEqual(len(self.told("maps2")), 1)

    def test_an_asleep_session_keeps_its_run_and_is_woken_by_the_outcome(self):
        rid = self.start()
        self.st["sessions"]["maps"]["state"] = "asleep"   # the human closed its terminal (session.ended)
        self.ok("session.gist", "other", {"text": "busy", **session("other")})
        self.assertEqual(self.st["hub_runs"][rid]["state"], "running")
        self.ok("core.run_ended", rid, {"code": 0, "reason": "exit"})
        self.assertEqual(len(self.told()), 1)
        self.assertNotEqual(self.st["sessions"]["maps"]["state"], "asleep")

    def test_without_runs_nothing_changes(self):
        self.assertNotIn("hub_runs", self.st)


if __name__ == "__main__":
    unittest.main()


class Cli(unittest.TestCase):
    """`hub run` in bin/hub: the fact it puts, its own flags only at the start, the Bash hook's bans, from a session."""

    def run_hub(self, *args, session="maps", stdin=None):
        import os
        import subprocess
        import sys
        import tempfile
        from core import spool
        from tests.test_clihelp import HUB
        state = tempfile.mkdtemp(prefix="hubrun-cli-")
        env = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0", "PYTHONUTF8": "1", "HUB_LANG": "en"}
        env.pop("HUB_SESSION", None)
        if session:
            env["HUB_SESSION"] = session
        r = subprocess.run([sys.executable, HUB, *args], env=env, capture_output=True, text=True, encoding="utf-8",
                           input=stdin, timeout=60, cwd=state)
        return r, [ev for _, ev in spool.take(os.path.join(state, "spool"))], state

    def test_the_fact(self):
        import time
        r, evs, state = self.run_hub("run", "--until", "90m", "python3 evals/run.py --only after")
        self.assertEqual(r.returncode, 0, r.stderr)
        (e,) = evs
        self.assertEqual((e["type"], e["entity"], e["data"]["command"], e["data"]["sender"]),
                         ("run.requested", "maps", "python3 evals/run.py --only after", "maps"))
        self.assertAlmostEqual(e["data"]["until_at"], time.time() + 5400, delta=30)
        self.assertTrue(e["data"]["cwd"].endswith(state.rsplit("/", 1)[-1]))

    def test_words_after_the_flags_are_the_command(self):
        r, evs, _ = self.run_hub("run", "ls", "-h", "--only")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(evs[0]["data"]["command"], "ls -h --only")
        self.assertAlmostEqual(evs[0]["data"]["until_at"] - evs[0]["ts"], hubrun.MAX_LENGTH, delta=30)

    def test_the_command_from_stdin(self):
        script = "for x in 'a b' \"c\"; do echo \"$x\"; done\n"
        r, evs, _ = self.run_hub("run", "-", stdin=script)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(evs[0]["data"]["command"], script)

    def test_refusals_put_nothing(self):
        for args, phrase in ((("run", "--until", "25h", "x"), "from 1 minute to 24 hours"),
                             (("run", "--until", "30s", "x"), "from 1 minute to 24 hours"),
                             (("run", "--untill", "4h", "x"), "hub run [--until"),
                             (("run",), "hub run [--until"),
                             (("run", "pkill -f evals"), "is closed to sessions by the hub")):
            r, evs, _ = self.run_hub(*args)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertIn(phrase, r.stderr + r.stdout, args)
            self.assertEqual(evs, [], args)
        r, evs, _ = self.run_hub("run", "sleep 120; echo ok")   # a long sleep is what a run is for
        self.assertEqual((r.returncode, len(evs)), (0, 1), r.stderr)

    def test_a_schedule_command_has_the_same_bans(self):
        """A session cannot set pkill through the schedule: --command and the pre-check --check are Bash commands too."""
        for flag in ("--command", "--check"):
            extra = ("--prompt", "tick", "--session", "maps") if flag == "--check" else ("--id", "j1")
            r, evs, _ = self.run_hub("schedule", "add", "--every", "10m", *extra, flag, "pkill -f evals")
            self.assertNotEqual(r.returncode, 0, flag)
            self.assertIn("is closed to sessions by the hub", r.stderr + r.stdout, flag)
            self.assertEqual(evs, [], flag)
        r, evs, _ = self.run_hub("schedule", "add", "--every", "10m", "--id", "j1", "--command",
                                 "sleep 120; echo ok")   # an ordinary command, a long sleep too, goes
        self.assertEqual((r.returncode, [e["type"] for e in evs]), (0, ["schedule.set"]), r.stderr)
        # the owner's terminal has no hook: its schedule is as before
        r, evs, _ = self.run_hub("schedule", "add", "--every", "10m", "--id", "j2", "--command", "pkill -f evals",
                                 session=None)
        self.assertEqual((r.returncode, [e["type"] for e in evs]), (0, ["schedule.set"]), r.stderr)

    def test_from_the_terminal_it_is_refused(self):
        r, evs, _ = self.run_hub("run", "echo x", session=None)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("from a session", r.stderr)
        self.assertEqual(evs, [])

    def test_stop_and_help(self):
        r, evs, _ = self.run_hub("run", "stop", "run12")
        self.assertEqual([(e["type"], e["entity"]) for e in evs], [("run.stop_requested", "run12")])
        r, evs, _ = self.run_hub("run", "--help")
        self.assertIn("hub run list [--all]", r.stdout)
        self.assertEqual(evs, [])

    def test_minutes_and_a_missing_folder(self):
        import time
        r, evs, _ = self.run_hub("run", "--until", "90min", "make test")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertAlmostEqual(evs[0]["data"]["until_at"], time.time() + 5400, delta=30)
        r, evs, _ = self.run_hub("run", "--cwd", "/nonexistent/folder", "make test")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no folder", r.stderr)
        self.assertEqual(evs, [])


class Took(unittest.TestCase):
    def test_the_wrappers_length_wins_over_the_cores_clock(self):
        run = {"id": "run5", "command": "sleep 6", "ts": 0, "started_at": 0}
        self.assertIn("after 6 s", hubrun.outcome(run, {"reason": "exit", "code": 0, "took": 6.2}, 3600))
        self.assertIn("after 1 h 0 min", hubrun.outcome(run, {"reason": "exit", "code": 0}, 3600))
        for bad in (-1, float("nan"), "6", True, 10 ** 400):
            self.assertIn("after 1 h 0 min", hubrun.outcome(run, {"reason": "exit", "code": 0, "took": bad}, 3600))


class WaitsForItsRun(unittest.TestCase):
    def test_an_executor_waiting_for_its_run_is_not_stuck(self):
        from core import delegations
        st = reducer.empty()
        st["sessions"]["maps"] = {"name": "maps", "state": "idle", "since": T0}
        d = {"id": "d1", "session": "maps", "state": "working", "since": T0, "created": T0}
        st["delegations"]["d1"] = d
        self.assertIsNotNone(delegations.free(st, d, now=T0 + 3600))
        st["hub_runs"] = {"run5": {"id": "run5", "session": "maps", "state": "running", "ts": T0, "command": "x",
                                   "until_at": T0 + 7200}}
        self.assertIsNone(delegations.free(st, d, now=T0 + 3600))
        self.assertIsNone(delegations.stuck(st, d, now=T0 + 3600))

