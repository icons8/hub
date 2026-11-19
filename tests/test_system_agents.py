"""System agents — Hub Developer and Agent Developer — are always present in every hub (decision of 2026-10-08:
system agents for everyone, always, cannot be put to sleep or deleted, checked and added on every start). In one
hub they were missing altogether: the core installed them once and did not bring back deleted ones, and `dev.enabled`
in hub.json switched them off."""
import tests  # noqa: F401 — keeps the machine's settings out
import os
import tempfile
import unittest
from unittest import mock

from core import reducer, schedule, system, views
from core.daemon import Hub
from tests.fakes import lang

OWNER = {"channel": "web", "owner": True}
HUB = {"channel": "session", "sender": "hub"}
CORE_WHO = lang.legacy("who")["core"]   # the sender id of the core's own jobs


class Rules(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        for n in ("hub-dev", "seo"):
            self.ev("session.requested", n, cwd="/w", session_id="s-" + n, role="agent", about="work", **OWNER)
        for n in ("hub", "hub-dev", "seo"):
            self.ev("session.started", n)
        self.assertIsNone(self.ev("agents.system", None, names=["hub-dev", "agent-dev"], channel="core"))

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, self.acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                           "entity": entity, "data": data})
        return why

    def test_nobody_puts_it_to_sleep_fires_removes_or_renames_it(self):
        for who in (OWNER, HUB):
            for type, data in (("session.close_requested", {}), ("session.role_set", {"role": "worker"}),
                               ("session.renamed", {"to": "dev2"})):
                why = self.ev(type, "hub-dev", **data, **who)
                self.assertIn("a system agent of the hub", why or "", (type, who))
        self.st["sessions"]["hub-dev"]["state"] = "asleep"   # closed in its own terminal — it sleeps but is not deleted
        self.assertIn("a system agent of the hub", self.ev("session.remove_requested", "hub-dev", **OWNER) or "")
        dev = self.st["sessions"]["hub-dev"]
        self.assertEqual((dev["role"], dev["state"]), ("agent", "asleep"))
        # putting itself to sleep is not allowed either
        self.st["sessions"]["hub-dev"]["state"] = "idle"
        self.assertIn("a system agent of the hub", self.ev("session.close_ready", "hub-dev", channel="session",
                                                 sender="hub-dev") or "")

    def test_other_agents_are_fired_as_before(self):
        self.assertIsNone(self.ev("session.role_set", "seo", role="worker", **OWNER))
        self.assertIsNone(self.ev("session.close_requested", "seo", **OWNER))

    def test_only_the_core_names_them(self):
        self.assertIsNotNone(self.ev("agents.system", None, names=["seo"], channel="session", sender="hub"))
        self.assertEqual(self.st["system"], ["hub-dev", "agent-dev"])

    def test_parking_still_frees_its_process(self):
        # parking is not sleep: the process comes up on the first message (core/parking)
        self.assertIsNone(self.ev("session.parked", "hub-dev", channel="core"))

    def test_page_knows_it_is_a_system_agent(self):
        r = views.Reader()
        r.state = self.st
        got = {s["name"]: s["system"] for s in r.snapshot()["sessions"]}
        self.assertEqual((got["hub-dev"], got["seo"]), (True, False))


class Returning(unittest.TestCase):
    DATA = {"cwd": "/repo", "role": "agent", "about": "hub code", "icon": "🧑‍💻", "tools": ["core:hub-developer"]}

    def test_missing_removed_and_fired(self):
        st = reducer.empty()
        self.assertEqual(system.returning(st, "hub-dev", self.DATA, "new"),
                         [("session.requested", "hub-dev", {**self.DATA, "session_id": "new"})])
        st["sessions"]["hub-dev"] = {"name": "hub-dev", "state": "removed", "cwd": "/repo", "session_id": "old",
                                     "has_history": True, "role": "agent"}
        on_disk = system.returning(st, "hub-dev", self.DATA, "new", transcript=lambda cwd, sid: True)
        self.assertEqual(on_disk, [("session.requested", "hub-dev", {"cwd": "/repo", "session_id": "old",
                                                                     "revive": True})])
        # no conversation on disk — --resume would fail: a new one, with the same profile (the agent's CLAUDE.md)
        (ev,) = system.returning(st, "hub-dev", self.DATA, "new", transcript=lambda cwd, sid: False)
        self.assertEqual((ev[2]["session_id"], ev[2]["profile"], ev[2].get("revive")), ("new", "old", None))
        st["sessions"]["hub-dev"].update(state="idle", role="worker")   # dismissed before this version
        self.assertEqual([e[0] for e in system.returning(st, "hub-dev", self.DATA, "new")], ["session.role_set"])
        st["sessions"]["hub-dev"]["role"] = "agent"
        self.assertEqual(system.returning(st, "hub-dev", self.DATA, "new"), [])


class FakeHost:
    def alive(self, name):
        return False


class Start(unittest.TestCase):
    """As in that hub: hub-dev deleted, agent-dev never there, both "disabled" in hub.json — after a start both are in place."""

    def test_every_start_brings_them_back(self):
        state = tempfile.mkdtemp()
        cfg = os.path.join(state, "hub.json")
        with open(cfg, "w") as f:
            f.write('{"update": {"enabled": false}, "dev": {"enabled": false}, "agent_dev": {"enabled": false}}')
        with mock.patch.dict(os.environ, {"HUB_CONFIG": cfg}):
            del os.environ["HUB_NO_SYSTEM_AGENTS"]   # the core's tests run without them (tests/__init__.py); patch.dict restores
            h = Hub(state, FakeHost(), registry_dir=state, clock=lambda: 1000.0)
            h.execute = lambda a: None
            h.record("session.requested", "hub-dev", {"cwd": "/w", "session_id": "s-d", "role": "agent",
                                                      "about": "code", **OWNER})
            h.record("session.ended", "hub-dev", {})
            h.record("session.remove_requested", "hub-dev", OWNER)
            self.assertEqual(h.state["sessions"]["hub-dev"]["state"], "removed")
            h.ensure_dev()
            sessions = h.state["sessions"]
            self.assertEqual(h.state["system"], ["hub-dev", "agent-dev"])
            self.assertEqual((sessions["hub-dev"]["state"], sessions["hub-dev"]["session_id"]), ("starting", "s-d"))
            self.assertEqual((sessions["agent-dev"]["state"], sessions["agent-dev"]["role"]), ("starting", "agent"))
            self.assertEqual(sessions["agent-dev"]["tools"], ["core:agent-developer"])
            # a second start brings nothing new
            seq = h.state["seq"]
            h.ensure_dev()
            self.assertEqual(h.state["seq"], seq)
            h.record("session.remove_requested", "agent-dev", OWNER)
            self.assertNotEqual(h.state["sessions"]["agent-dev"]["state"], "removed")


class Review(unittest.TestCase):
    """The daily review of the fleet (decision of 2026-10-08: once a day, from transcripts) is a system job of the core."""

    def test_the_core_keeps_it_and_nobody_else_removes_it(self):
        state = tempfile.mkdtemp()
        cfg = os.path.join(state, "hub.json")
        with open(cfg, "w") as f:
            f.write('{"update": {"enabled": false}}')
        now = [1_000_000.0]
        with mock.patch.dict(os.environ, {"HUB_CONFIG": cfg}):
            del os.environ["HUB_NO_SYSTEM_AGENTS"]
            h = Hub(state, FakeHost(), registry_dir=state, clock=lambda: now[0])
            h.execute = lambda a: None
            h.ensure_dev()
            job = h.state["schedule"]["agent-dev-review"]
            self.assertEqual((job["state"], job["session"], job["by"], job["system"], job["cron"]),
                             ("active", "agent-dev", CORE_WHO, True, "0 10 * * *"))
            self.assertEqual(h.state["trainer"], "agent-dev")
            h.record("session.requested", "hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", **OWNER})
            h.record("schedule.remove_requested", "agent-dev-review", OWNER)
            h.record("schedule.retarget", "agent-dev-review", {"session": "hub", **HUB})
            h.record("schedule.applied", None, {"jobs": [], "scope": {"session": "agent-dev"}, **HUB})
            job = h.state["schedule"]["agent-dev-review"]
            self.assertEqual((job["state"], job["session"]), ("active", "agent-dev"))
            seq = h.state["seq"]
            h.ensure_jobs()
            self.assertEqual(h.state["seq"], seq)                      # in place — no events at all
            now[0] += 24 * 86400                                       # the term (30 days) is near — the core extends it
            h.ensure_jobs()
            self.assertGreater(h.state["schedule"]["agent-dev-review"]["until"], now[0] + 29 * 86400)



class StopsAudit(unittest.TestCase):
    """The stops audit of the agents (decision of 2026-10-28: the audit runs on other hubs too and they
    do not skip it): a system job of the core with a precheck without a model; the page shows whether it runs and whether
    agent-dev marks it."""

    def test_the_core_keeps_it_with_its_check(self):
        state = tempfile.mkdtemp()
        cfg = os.path.join(state, "hub.json")
        with open(cfg, "w") as f:
            f.write('{"update": {"enabled": false}}')
        now = [1_000_000.0]
        with mock.patch.dict(os.environ, {"HUB_CONFIG": cfg}):
            del os.environ["HUB_NO_SYSTEM_AGENTS"]
            h = Hub(state, FakeHost(), registry_dir=state, clock=lambda: now[0])
            h.execute = lambda a: None
            h.ensure_dev()
            job = h.state["schedule"]["agent-dev-stops"]
            self.assertEqual((job["state"], job["session"], job["by"], job["system"], job["cron"]),
                             ("active", "agent-dev", CORE_WHO, True, "0 11 * * *"))
            self.assertTrue(job["check"].endswith(os.path.join("modules", "agent_dev", "stops-audit.py") + '"'), job["check"])
            self.assertIn("6a. Stops audit", job["prompt"])
            h.record("session.requested", "hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", **OWNER})
            h.record("schedule.remove_requested", "agent-dev-stops", OWNER)
            h.record("schedule.retarget", "agent-dev-stops", {"session": "hub", **HUB})
            h.record("schedule.set", "agent-dev-stops", {"session": "agent-dev", "cron": "0 11 * * *", "prompt": "x", **HUB})
            job = h.state["schedule"]["agent-dev-stops"]
            self.assertEqual((job["state"], job["session"]), ("active", "agent-dev"))
            self.assertIn("6a. Stops audit", job["prompt"])
            seq = h.state["seq"]
            h.ensure_jobs()
            self.assertEqual(h.state["seq"], seq)                      # in place — no events at all
            now[0] += 24 * 86400
            h.ensure_jobs()
            self.assertGreater(h.state["schedule"]["agent-dev-stops"]["until"], now[0] + 29 * 86400)

    def test_a_job_without_its_check_is_set_again(self):
        spec = {**system.STOPS, "session": "agent-dev", "check": '"py" "stops-audit.py"'}
        job = {**spec, "state": "active", "system": True, "until": 10 ** 10}
        st = {"schedule": {"agent-dev-stops": job}}
        self.assertEqual(system.due_jobs(st, [spec], 1000), [])
        job.pop("check")
        self.assertEqual([e[1] for e in system.due_jobs(st, [spec], 1000)], ["agent-dev-stops"])


class StopsAuditTicks(unittest.TestCase):
    """What the core remembers of the precheck ticks and what the page reads from it."""

    def setUp(self):
        self.st, self.seq, self.ts = reducer.empty(), 0, 1_000_000
        self.ev("session.requested", "agent-dev", cwd="/w", session_id="s-a", role="agent", about="agents", **OWNER)
        self.ev("session.started", "agent-dev")
        self.ev("agents.system", None, names=["hub-dev", "agent-dev"], trainer="agent-dev", channel="core")
        spec = {**system.STOPS, "session": "agent-dev", "check": '"py" "stops-audit.py"'}
        self.assertIsNone(self.ev("schedule.set", "agent-dev-stops", **spec, until=self.ts + 30 * 86400, system=True,
                                  channel="core"))

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, self.acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": self.ts, "type": type,
                                                           "entity": entity, "data": data})
        return why

    def tick(self, code, out="", days=1):
        self.ts += days * 86400
        self.assertIsNone(self.ev("schedule.due", "agent-dev-stops", slot=self.ts - 5, exit=code, out=out))

    def page(self):
        r = views.Reader()
        r.state = self.st
        return next(s for s in r.snapshot(now=self.ts)["sessions"] if s["name"] == "agent-dev")["stops"]

    def test_news_streak_until_done(self):
        self.assertEqual(self.page()["alarm"], None)            # not ticked yet
        self.tick(3, "stops-audit: no audit yet")
        msg = [m for m in self.st["messages"].values() if m.get("schedule") == "agent-dev-stops"]
        self.assertEqual(len(msg), 1)
        self.assertIn(schedule.CHECK_NEWS, msg[0]["text"])
        self.assertIn("no audit yet", msg[0]["text"])
        job = self.st["schedule"]["agent-dev-stops"]
        self.assertEqual((job["news"], job["news_since"]), (1, self.ts))
        asked = self.ts
        self.assertEqual((self.page()["asked"], self.page()["alarm"]), (asked, None))
        self.tick(3, "MISSED 1 d")
        self.tick(3, "MISSED 2 d")
        job = self.st["schedule"]["agent-dev-stops"]   # the fold works on a copy
        self.assertEqual((job["news"], job["news_since"]), (3, asked))
        self.assertEqual(self.page()["alarm"], "skipped")       # three ticks in a row without --done
        self.tick(0)
        job = self.st["schedule"]["agent-dev-stops"]
        self.assertEqual((job["news"], job.get("news_since"), job["news_cleared"]), (0, None, self.ts))
        self.assertEqual((self.page()["alarm"], self.page()["done"], self.page()["asked"]), (None, self.ts, None))

    def test_a_failed_check_and_a_silent_job_are_red(self):
        self.tick(1, "stops-audit: error: OSError")
        self.assertEqual((self.page()["alarm"], self.page()["check"]), ("failed", 1))
        self.tick(1, "stops-audit: error: OSError")   # the same broken check: agent-dev cannot fix the core's code
        told = [m for m in self.st["messages"].values() if m.get("schedule") == "agent-dev-stops"]
        self.assertEqual(len(told), 1)                  # once per run of errors, not every day
        self.assertIn("hub send hub-dev", told[0]["text"])   # the first system agent of this hub
        self.assertNotIn("hub schedule add", told[0]["text"])
        job = self.st["schedule"]["agent-dev-stops"]
        self.assertIsNone(self.ev("schedule.set", "agent-dev-stops", **{**system.STOPS, "session": "agent-dev",
                                  "check": '"py3" "stops-audit.py"'}, until=job["until"], system=True, channel="core"))
        self.tick(1, "stops-audit: error: another")    # a changed check that fails is told again
        told = [m for m in self.st["messages"].values() if m.get("schedule") == "agent-dev-stops"]
        self.assertEqual(len(told), 2)
        self.assertEqual(self.st["schedule"]["agent-dev-stops"].get("news", 0), 0)   # an error is not news
        self.tick(0)
        self.assertEqual(self.page()["alarm"], None)
        self.ts += 3 * 86400                                    # no schedule.due for three days
        self.assertEqual(self.page()["alarm"], "silent")
        self.st["schedule"]["agent-dev-stops"]["state"] = "removed"
        self.assertEqual(self.page()["alarm"], "off")

    def test_a_broken_check_rides_on_a_run_still_queued(self):
        self.ev("turn.started", "agent-dev", prompt_head="other work")   # busy: the job's message waits in the queue
        self.tick(3, "stops-audit: no audit yet")
        self.tick(1, "stops-audit: error: OSError")
        (m,) = [m for m in self.st["messages"].values() if m.get("schedule") == "agent-dev-stops"]
        self.assertIn("no audit yet", m["text"])
        self.assertIn(" failed (", m["text"])
        self.tick(1, "stops-audit: error: OSError")
        (m,) = [m for m in self.st["messages"].values() if m.get("schedule") == "agent-dev-stops"]
        self.assertEqual(m["text"].count(" failed ("), 1)

    def test_only_agent_dev_carries_it(self):
        self.ev("session.requested", "seo", cwd="/w", session_id="s-s", role="agent", about="work", **OWNER)
        r = views.Reader()
        r.state = self.st
        got = {s["name"]: "stops" in s for s in r.snapshot(now=self.ts)["sessions"]}
        self.assertEqual((got["agent-dev"], got["seo"]), (True, False))


if __name__ == "__main__":
    unittest.main()
