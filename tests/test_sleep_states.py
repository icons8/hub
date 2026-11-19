""""Asleep" is a state of the SESSION state machine, not a set of flags (owner decision 2026-10-25; state machine
audit, findings 5, 11, 17, 20): a session without a process has three states: asleep, parked (free without a process,
core/parking), and retired (an executor handed in its work or was stopped: messages do not wake it). The page, the
module slice, Telegram, the watcher and the CLI read the same state; the snapshot and the module slice keep their old
shape (API contract). Old state.json files and journal snapshots are upgraded on read; rollback is
tools/sleep_states.py down."""
import tests  # noqa: F401 — ignore the machine's settings
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest

from core import executors, journal, machines, modstate, reducer, views

OWNER = {"channel": "cli", "owner": True}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def by(name):
    return {"channel": "session", "owner": False, "sender": name}


class Base(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "maps", cwd="/m", session_id="s-maps", **OWNER)
        self.ev("session.requested", "frontend", cwd="/fe", session_id="s-fe", role="agent", about="Frontend", **OWNER)
        for n in ("hub", "maps", "frontend"):
            self.ev("session.started", n)

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.st, self.acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 1000 + self.seq, "type": type,
                                                           "entity": entity, "data": data})
        self.actions += self.acts
        return why

    def sess(self, name):
        return self.st["sessions"][name]

    def done(self, name="frontend-ci"):
        """The frontend executor did the task and reported: it is free (not yet put away by the core)."""
        self.assertIsNone(self.ev("task.run_requested", name, task="fix CI", **by("frontend")))
        run = self.st["runs"][name]
        for type, entity, data in executors.events_for(self.st, name, run, cwd="/fe/.claude/worktrees/x", worktree=None):
            self.assertIsNone(self.ev(type, entity, **data), (type, data))
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == name]
        self.ev("session.started", name)
        self.ev("turn.started", name, marker=d["message"], prompt_head="task")
        self.ev("turn.result", name, text="Done, MR !12")
        self.ev("turn.stopped", name)


class Parked(Base):
    def test_parking_is_a_state_without_a_flag(self):
        self.assertIsNone(self.ev("session.parked", "maps", channel="core", why="idle"))
        s = self.sess("maps")
        self.assertEqual(s["state"], "parked")
        self.assertNotIn("parked", s)
        self.assertIn("release", [a["do"] for a in self.acts])
        self.assertNotIn("end_tree", [a["do"] for a in self.acts])   # a free session's test stand stays until it is woken

    def test_a_message_wakes_a_parked_session(self):
        self.ev("session.parked", "maps", channel="core", why="idle")
        self.ev("message.received", None, to="maps", text="hello", **OWNER)
        self.assertEqual(self.sess("maps")["state"], "starting")
        self.assertEqual([a["session"] for a in self.acts if a["do"] == "spawn"], ["maps"])

    def test_wake_and_owner_sleep(self):
        self.ev("session.parked", "maps", channel="core", why="idle")
        self.assertIsNone(self.ev("session.wake_requested", "maps", **OWNER))
        self.assertEqual(self.sess("maps")["state"], "starting")
        self.ev("session.started", "maps")
        self.ev("session.parked", "maps", channel="core", why="idle")
        since = self.sess("maps")["since"]
        # the owner's "Put to sleep": no process, so it sleeps at once, without handing over its work (like clearing the
        # flag in main: same since)
        self.assertIsNone(self.ev("session.close_requested", "maps", channel="web", owner=True))
        self.assertEqual((self.sess("maps")["state"], self.sess("maps")["since"]), ("asleep", since))
        self.assertIn("already asleep", self.ev("session.close_requested", "maps", **OWNER))

    def test_parked_may_be_removed_and_compacted(self):
        self.ev("session.parked", "maps", channel="core", why="idle")
        self.assertIsNone(self.ev("session.compact_requested", "maps", **OWNER))
        self.assertTrue(self.sess("maps").get("compact_wake"))
        self.assertIsNone(self.ev("session.remove_requested", "maps", **OWNER))
        self.assertEqual(self.sess("maps")["state"], "removed")

    def test_late_facts_keep_it_parked(self):
        self.ev("session.parked", "maps", channel="core", why="idle")
        for type in ("session.exited", "session.ended", "turn.stopped"):
            self.assertIsNone(self.ev(type, "maps"), type)
            self.assertEqual(self.sess("maps")["state"], "parked", type)
        self.assertIsNotNone(self.ev("session.stop_requested", "maps", **OWNER))   # not an executor: nothing to stop


class Retired(Base):
    def test_retire_and_stop_give_the_retired_state(self):
        self.done()
        self.assertIsNone(self.ev("session.close_requested", "frontend-ci", retire=True))
        s = self.sess("frontend-ci")
        self.assertEqual(s["state"], "retired")
        self.assertNotIn("retired", s)
        self.assertIn("end_tree", [a["do"] for a in self.acts])   # nobody is left for its background to serve
        self.done("frontend-ui")
        self.assertIsNone(self.ev("session.stop_requested", "frontend-ui", **OWNER))
        self.assertEqual(self.sess("frontend-ui")["state"], "retired")

    def test_a_message_goes_to_the_raiser_and_wake_is_the_owners_or_the_hubs(self):
        self.done()
        self.ev("session.close_requested", "frontend-ci", retire=True)
        self.ev("message.received", None, to="frontend-ci", text="and also README", **by("maps"))
        self.assertEqual(self.st["messages"][f"m{self.seq}"]["session"], "frontend")
        self.assertEqual(self.sess("frontend-ci")["state"], "retired")
        self.assertIn("woken by the owner, the hub or whoever raised it", self.ev("session.wake_requested", "frontend-ci", **by("maps")))
        self.assertIsNone(self.ev("session.wake_requested", "frontend-ci", **OWNER))
        self.assertEqual(self.sess("frontend-ci")["state"], "starting")

    def test_wake_with_a_message_from_the_owner(self):
        self.done()
        self.ev("session.close_requested", "frontend-ci", retire=True)
        self.assertIsNone(self.ev("message.received", None, to="frontend-ci", text="finish it", wake=True, **OWNER))
        self.assertEqual(self.st["messages"][f"m{self.seq}"]["session"], "frontend-ci")
        self.assertEqual(self.sess("frontend-ci")["state"], "starting")

    def test_woken_by_the_hub_then_put_to_sleep_is_no_longer_retired(self):
        # spec §1: any exit from retired ends "handed in" (in main session.requested kept the flag, so after an
        # ordinary sleep messages no longer woke it)
        self.done()
        self.ev("session.close_requested", "frontend-ci", retire=True)
        self.assertIsNone(self.ev("session.requested", "frontend-ci", **by("hub")))
        self.ev("session.started", "frontend-ci")
        self.ev("session.ended", "frontend-ci", reason="prompt_input_exit")
        self.assertEqual(self.sess("frontend-ci")["state"], "asleep")
        self.ev("message.received", None, to="frontend-ci", text="more", **by("maps"))
        self.assertEqual(self.st["messages"][f"m{self.seq}"]["session"], "frontend-ci")

    def test_no_longer_an_executor_is_plain_asleep(self):
        self.done()
        self.ev("session.close_requested", "frontend-ci", retire=True)
        self.assertIsNone(self.ev("task.backlog", None, session="frontend-ci", slug="ci", **by("hub")) or None)
        self.assertEqual(self.sess("frontend-ci")["state"], "asleep")

    def test_removed_by_the_raiser(self):
        self.done()
        self.ev("session.close_requested", "frontend-ci", retire=True)
        self.assertIsNone(self.ev("session.remove_requested", "frontend-ci", **by("frontend")))
        self.assertEqual(self.sess("frontend-ci")["state"], "removed")


class Readers(Base):
    def row(self, name):
        r = views.Reader()
        r.state = self.st
        return next(s for s in r.snapshot()["sessions"] if s["name"] == name)

    def test_snapshot_and_module_slice_keep_their_form(self):
        self.ev("session.parked", "maps", channel="core", why="idle")
        self.done()
        self.ev("session.close_requested", "frontend-ci", retire=True)
        p, r = self.row("maps"), self.row("frontend-ci")
        self.assertEqual((p["state"], p["label"], p["parked"], p["retired"], p["status"]),
                         ("idle", "idle", True, False, "idle"))
        self.assertEqual((r["state"], r.get("parked", False), r["retired"], r["status"]), ("asleep", False, True, "asleep"))
        rows = modstate.session_rows(self.st)["sessions"]
        self.assertEqual({k: rows["maps"][k] for k in ("state", "parked", "asleep", "busy", "retired")},
                         {"state": "asleep", "parked": True, "asleep": True, "busy": False, "retired": False})
        self.assertEqual({k: rows["frontend-ci"][k] for k in ("state", "parked", "asleep", "retired")},
                         {"state": "asleep", "parked": False, "asleep": True, "retired": True})

    def test_telegram_counts_a_parked_session_as_free(self):
        from modules.telegram import bot
        self.ev("session.parked", "maps", channel="core", why="idle")
        b = bot.Bot.__new__(bot.Bot)
        b.reader, b.clock, b.names = type("R", (), {"state": self.st})(), lambda: 2000.0, {}
        line = bot.Bot.status_text(b).splitlines()[0]
        self.assertIn("free 3", line)   # hub, frontend and the parked maps
        self.assertNotIn("asleep", line)


class Upgrade(unittest.TestCase):
    OLD = {"sessions": {
        "a": {"name": "a", "state": "asleep", "parked": True, "since": 5},
        "b": {"name": "b", "state": "asleep", "retired": 7, "role": "executor", "since": 7},
        "c": {"name": "c", "state": "removed", "retired": 7, "role": "executor"},
        "d": {"name": "d", "state": "asleep", "since": 3},
        "e": {"name": "e", "state": "idle", "retired": 2, "role": "executor"},
        "f": {"name": "f", "state": "asleep", "retired": 7, "role": "executor", "backlog": "x"}}}

    def test_old_state_is_upgraded_once(self):
        st = reducer.upgrade(copy.deepcopy(self.OLD))
        self.assertEqual({n: s["state"] for n, s in st["sessions"].items()},
                         {"a": "parked", "b": "retired", "c": "removed", "d": "asleep", "e": "idle", "f": "asleep"})
        self.assertFalse(any("parked" in s or "retired" in s for s in st["sessions"].values()))
        self.assertEqual(reducer.upgrade(copy.deepcopy(st)), st)

    def test_snapshot_and_state_file_upgrade(self):
        st = reducer.restore({"seq": 9, "data": {"state": copy.deepcopy(self.OLD)}})
        self.assertEqual(st["sessions"]["a"]["state"], "parked")
        with tempfile.TemporaryDirectory() as d:
            j = journal.Journal(os.path.join(d, "journal.jsonl"))
            j.append("core.started")
            old = {**reducer.empty(), **copy.deepcopy(self.OLD), "seq": 1}
            journal.save_state(d, old)
            self.assertEqual(journal.load_state(d)["sessions"]["b"]["state"], "retired")

    def test_every_read_path_runs_every_migration(self):
        # an earlier hub: sleep flags and the watcher's service keys next to session names; one read path
        # (reducer.loaded) converts both, and if a step dropped out in a branch merge another set of tests would stay
        # green (review 26.10)
        old = {**copy.deepcopy(self.OLD), "watch": {"surrender": ["m3"], "core": 5000}}
        with tempfile.TemporaryDirectory() as d:
            j = journal.Journal(os.path.join(d, "journal.jsonl"))
            j.append("core.started")
            journal.save_state(d, {**reducer.empty(), **copy.deepcopy(old), "seq": 1})
            for st in (reducer.restore({"seq": 9, "data": {"state": copy.deepcopy(old)}}), journal.load_state(d)):
                self.assertEqual((st["sessions"]["a"]["state"], st["sessions"]["b"]["state"]), ("parked", "retired"))
                self.assertEqual((st["watch"].get(reducer.SURRENDER_KEY), st["watch"].get(reducer.CORE_KEY)), (["m3"], 5000))

    def test_down_writes_the_old_form_for_old_code(self):
        st = reducer.upgrade(copy.deepcopy(self.OLD))
        old = reducer.legacy(copy.deepcopy(st))
        self.assertEqual((old["sessions"]["a"]["state"], old["sessions"]["a"]["parked"]), ("asleep", True))
        self.assertEqual((old["sessions"]["b"]["state"], old["sessions"]["b"]["retired"]), ("asleep", 7))
        with tempfile.TemporaryDirectory() as d:
            j = journal.Journal(os.path.join(d, "journal.jsonl"))
            j.append("core.started")
            journal.save_state(d, {**reducer.empty(), **st, "seq": 1})
            subprocess.run([sys.executable, os.path.join(ROOT, "tools", "sleep_states.py"), "down", d], check=True,
                           capture_output=True, cwd=ROOT)
            with open(os.path.join(d, "state.json"), encoding="utf-8") as f:
                saved = json.load(f)["state"]["sessions"]
            self.assertEqual((saved["a"]["state"], saved["a"]["parked"]), ("asleep", True))
            last = journal.read(os.path.join(d, "journal.jsonl"))[-1]
            self.assertEqual((last["type"], last["data"]["why"]), ("core.snapshot", "downgrade"))
            self.assertEqual(last["data"]["state"]["sessions"]["b"]["state"], "asleep")
            # the old code takes this snapshot even without state.json (the whole journal, from the last snapshot)
            os.unlink(os.path.join(d, "state.json"))
            self.assertEqual(reducer.replay(journal.read(os.path.join(d, "journal.jsonl")))["sessions"]["a"]["state"],
                             "parked")   # the new code reads it the new way again (upgrade)
            from core import lock
            held = lock.acquire(os.path.join(d, "hubd.lock"))   # hubd is running: do not touch its files
            try:
                r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "sleep_states.py"), "down", d],
                                   capture_output=True, text=True, cwd=ROOT)
            finally:
                held.close()
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("hubd is running", r.stderr)


if __name__ == "__main__":
    unittest.main()
