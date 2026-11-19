"""Bring back a deleted session with its conversation (revive a killed session):
`hub session revive <name>` — the same sessionId, folder and tools, started via --resume, not as a new session."""
import tests  # noqa: F401 — keep the machine's settings out
import os
import subprocess
import sys
import tempfile
import unittest

import platforms
from core import journal, reducer, spool

HUB_BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")

OWNER = {"channel": "web", "owner": True}
HUB = {"channel": "session", "owner": False, "sender": "hub"}


class Revive(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.acts = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.started", "hub")
        self.ev("session.requested", "db", cwd="/w/db", session_id="s-db", tools=["ms365"], **HUB)
        self.ev("session.started", "db")
        self.ev("turn.started", "db", marker=None)
        self.ev("turn.result", "db", text="done")
        self.ev("turn.stopped", "db")
        self.st["sessions"]["db"]["state"] = "removed"   # the owner deleted it
        self.assertEqual(self.st["sessions"]["db"]["state"], "removed")

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.acts += acts
        return why

    def test_revive_resumes_the_same_conversation(self):
        self.acts.clear()
        self.assertIsNone(self.ev("session.requested", "db", session_id="s-db", cwd="/w/db", revive=True, **HUB))
        s = self.st["sessions"]["db"]
        self.assertEqual((s["state"], s["tools"], s["cwd"]), ("starting", ["ms365"], "/w/db"))
        (spawn,) = [a for a in self.acts if a["do"] == "spawn"]
        self.assertEqual((spawn["session_id"], spawn["resume"]), ("s-db", True))

    def test_a_new_session_with_the_name_is_still_new(self):
        self.acts.clear()
        self.ev("session.requested", "db", session_id="s-other", cwd="/w/x", **HUB)
        (spawn,) = [a for a in self.acts if a["do"] == "spawn"]
        self.assertEqual((spawn["session_id"], spawn["resume"]), ("s-other", False))


def by(name):
    return {"channel": "session", "owner": False, "sender": name}


class ReviveByRaiser(unittest.TestCase):
    """`hub session revive` of a removed executor: the hub and the owner, and whoever raised it (as stop, remove, wake:
    guards.from_hub_owner_or_raiser) - not another session, not a direction, not an executor raised by someone else."""

    def setUp(self):
        self.st, self.seq, self.acts = reducer.empty(), 0, []
        for name, extra in (("hub", {"kind": "hub"}), ("maps", {}), ("docs", {}), ("maps-1", {"raised_by": "maps"}),
                            ("docs-1", {"raised_by": "docs"}), ("maps-2", {"raised_by": "maps", "backlog": "north"})):
            self.ev("session.requested", name, cwd=f"/home/x/{name}", session_id=f"s-{name}", tools=["web"], **OWNER)
            self.ev("session.started", name, session_id=f"s-{name}", pid=100 + self.seq)
            self.st["sessions"][name].update(extra)
        for name in ("maps-1", "docs-1", "maps-2"):
            self.ev("turn.started", name, marker=None)   # it has a conversation to resume
            self.ev("turn.result", name, text="done")
            self.ev("turn.stopped", name)
            self.st["sessions"][name]["state"] = "removed"
        self.acts.clear()

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "src": "spool", "type": type,
                                                      "entity": entity, "data": data})
        self.acts += acts
        return why

    def revive(self, name, who, **more):
        s = self.st["sessions"][name]
        return self.ev("session.requested", name, session_id=s["session_id"], cwd=s["cwd"], revive=True, **who, **more)

    def state(self, name):
        return self.st["sessions"][name]["state"]

    def test_whoever_raised_the_executor_brings_it_back(self):
        self.assertIsNone(self.revive("maps-1", by("maps")))
        self.assertEqual(self.state("maps-1"), "starting")
        (spawn,) = [a for a in self.acts if a["do"] == "spawn"]
        self.assertEqual((spawn["session_id"], spawn["resume"]), ("s-maps-1", True))
        self.assertEqual(self.st["sessions"]["maps-1"]["tools"], ["web"])   # the same set, nothing new
        self.assertEqual(self.st["sessions"]["maps-1"]["raised_by"], "maps")

    def test_a_closed_or_crashed_one_too(self):
        for state in ("closed", "failed"):
            self.setUp()
            self.st["sessions"]["maps-1"]["state"] = state
            self.assertIsNone(self.revive("maps-1", by("maps")), state)
            self.assertEqual(self.state("maps-1"), "starting")

    def test_the_hub_and_the_owner_still_do(self):
        for who in (by("hub"), OWNER, {"channel": "core"}):
            self.setUp()
            self.assertIsNone(self.revive("maps-1", who), who)
            self.assertEqual(self.state("maps-1"), "starting")

    def test_another_session_does_not_bring_back_an_executor_it_did_not_raise(self):
        # docs is a session too, and maps-1 is not its executor; and the refusal is not a request to the hub
        why = self.revive("maps-1", by("docs"))
        self.assertIsNotNone(why)
        self.assertIn("hub", why)
        self.assertEqual(self.state("maps-1"), "removed")
        self.assertFalse([m for m in self.st["messages"].values() if m["session"] == "hub"])
        self.assertFalse([a for a in self.acts if a["do"] == "spawn"])

    def test_the_raiser_does_not_bring_back_what_another_raised(self):
        self.assertIsNotNone(self.revive("docs-1", by("maps")))
        self.assertEqual(self.state("docs-1"), "removed")

    def test_a_direction_is_brought_back_by_the_hub_not_by_its_raiser(self):
        self.assertIsNotNone(self.revive("maps-2", by("maps")))
        self.assertEqual(self.state("maps-2"), "removed")
        self.assertIsNone(self.revive("maps-2", by("hub")))

    def test_a_specialist_and_a_session_of_the_hub_are_brought_back_by_the_hub_only(self):
        for name in ("docs", "maps"):
            self.st["sessions"][name]["state"] = "removed"
            self.assertIsNotNone(self.revive(name, by("maps-1")), name)
            self.assertIsNotNone(self.revive(name, by("maps")), name)   # itself, from the grave: no
            self.assertEqual(self.state(name), "removed")

    def test_a_plugin_process_and_a_module_in_its_own_name_do_not(self):
        self.assertIsNotNone(self.revive("maps-1", {"channel": "plugin", "owner": False, "sender": "ext:demo"}))
        self.assertEqual(self.state("maps-1"), "removed")

    def test_the_raiser_cannot_bring_it_back_to_another_folder_or_conversation(self):
        # the request's fields are the caller's words: a revive is the same conversation in the same folder
        s = self.st["sessions"]["maps-1"]
        self.assertIsNotNone(self.ev("session.requested", "maps-1", session_id="s-other", cwd=s["cwd"], revive=True,
                                     **by("maps")))
        self.assertIsNotNone(self.ev("session.requested", "maps-1", session_id=s["session_id"], cwd="/home/x/elsewhere",
                                     revive=True, **by("maps")))
        self.assertIsNotNone(self.revive("maps-1", by("maps"), worktree={"path": "/home/x/elsewhere", "repo": "/home/x/r"}))
        self.assertEqual(self.state("maps-1"), "removed")
        self.assertFalse([a for a in self.acts if a["do"] == "spawn"])

    def test_the_raiser_cannot_change_what_the_session_is(self):
        # only the conversation and folder come from the request: its kind, mode, profile and the rest are the caller's
        # words (a "hub" executor would skip the ceilings and be taken for the hub)
        self.st["sessions"]["maps-1"].update(profile="maps-prof", mode="plan")
        self.assertIsNone(self.revive("maps-1", by("maps"), kind="hub", mode="bypassPermissions", profile="other-prof",
                                      project="x", role="agent", tools=["ms365"], parallel=20))
        s = self.st["sessions"]["maps-1"]
        self.assertEqual((s["kind"], s["mode"], s["profile"], s["role"], s["tools"]), ("work", "plan", "maps-prof",
                                                                                       "worker", ["web"]))
        self.assertNotIn("parallel", s)

    def test_the_working_copy_comes_back_whole_or_not_at_all(self):
        wt = {"path": "/home/x/copies/maps-1", "branch": "task/maps-1", "repo": "/home/x/repo", "base_sha": "abc123"}
        for bad in ({}, {"path": wt["path"]}, {**wt, "branch": "task/other"}, {**wt, "extra": 1}, "path"):
            self.setUp()
            self.st["sessions"]["maps-1"]["worktree"] = dict(wt)
            self.assertIsNotNone(self.revive("maps-1", by("maps"), worktree=bad), bad)
            self.assertEqual(self.state("maps-1"), "removed")
        self.setUp()
        self.st["sessions"]["maps-1"]["worktree"] = dict(wt)
        self.assertIsNone(self.revive("maps-1", by("maps"), worktree=dict(wt)))
        self.assertEqual(self.st["sessions"]["maps-1"]["worktree"], wt)

    def test_without_the_revive_mark_a_removed_executor_is_not_raised_by_its_raiser(self):
        # a plain request is a new session (the hub does that), a session asks the hub as before
        s = self.st["sessions"]["maps-1"]
        self.assertIsNone(self.ev("session.requested", "maps-1", session_id="s-new", cwd=s["cwd"], **by("maps")))
        self.assertEqual(self.state("maps-1"), "removed")
        self.assertTrue([m for m in self.st["messages"].values() if m["session"] == "hub"])

    def test_the_mark_does_not_wake_a_sleeping_executor_round_the_wake_rules(self):
        # asleep, it is woken through the ceilings and the queue (hub session wake), not by a request with revive
        self.st["sessions"]["maps-1"]["state"] = "retired"
        self.assertIsNotNone(self.revive("maps-1", by("maps")))
        self.assertEqual(self.state("maps-1"), "retired")

    def test_it_meets_the_ceilings_of_a_new_request(self):
        # a revive that skipped them would raise any number of executors: the raiser has its places like for a new one
        self.st.setdefault("owner", {})["parallel"] = 1
        self.st["sessions"]["maps"]["role"] = "agent"   # a specialist: parallelism applies to it
        self.st["sessions"]["docs-1"].update(state="busy", raised_by="maps", role="executor")
        why = self.revive("maps-1", by("maps"))
        self.assertIsNotNone(why)
        self.assertEqual(self.state("maps-1"), "removed")
        self.assertIsNone(self.revive("maps-1", by("hub")))   # the hub decides for itself

    def test_a_live_one_is_the_same_refusal_as_before(self):
        self.assertIsNone(self.revive("maps-1", by("maps")))
        why = self.revive("maps-1", by("maps"))
        self.assertIn("already working", why)
        self.assertEqual(self.revive("maps-1", by("hub")), why)
        self.assertEqual(self.state("maps-1"), "starting")


class ReviveCli(unittest.TestCase):
    """The command asks the same question before it restores a working copy: a caller the core would refuse touches
    nothing."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="revive-")
        self.addCleanup(platforms.current.rmtree, self.state)
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        own = {"channel": "cli", "owner": True}
        for name, extra in (("hub", {"kind": "hub"}), ("maps", {"role": "agent"}), ("docs", {}),
                            ("maps-1", {"executor": True, "raised_by": "maps"})):
            j.append("session.requested", name, {"cwd": f"/home/x/{name}", "session_id": f"s-{name}", **extra, **own})
            j.append("session.started", name, {})
        j.append("session.stop_requested", "maps-1", own)
        j.append("session.remove_requested", "maps-1", own)

    def revive(self, me):
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CLI_WAIT": "0", "HUB_LANG": "en"}
        env.pop("HUB_SESSION", None)
        if me:
            env["HUB_SESSION"] = me
        r = subprocess.run([sys.executable, HUB_BIN, "session", "revive", "maps-1"], env=env, capture_output=True,
                           text=True)
        return r, [ev for _, ev in spool.take(os.path.join(self.state, "spool"))]

    def test_the_raiser_the_hub_and_the_owner_send_the_request(self):
        for me in ("maps", "hub", None):
            r, events = self.revive(me)
            self.assertEqual(r.returncode, 0, (me, r.stderr, r.stdout))
            ev = [e for e in events if e["type"] == "session.requested"][-1]   # the spool is read, not emptied
            self.assertEqual(ev["data"].get("sender"), me)
            self.assertEqual((ev["type"], ev["entity"], ev["data"]["revive"]), ("session.requested", "maps-1", True))

    def test_the_raiser_without_a_place_sends_nothing(self):
        # the places of a new request are asked before the working copy is restored, as the core asks them
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        j.append("owner.settings", None, {"channel": "cli", "owner": True, "parallel": 1})
        j.append("session.requested", "maps-2", {"cwd": "/home/x/maps-2", "session_id": "s-maps-2", "executor": True,
                                                 "raised_by": "maps", "channel": "cli", "owner": True})
        j.append("session.started", "maps-2", {})
        r, events = self.revive("maps")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("cannot be brought back now", r.stderr)
        self.assertFalse([e for e in events if e["type"] == "session.requested"])
        r, events = self.revive("hub")   # the hub decides for itself
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_another_session_is_told_who_may_and_nothing_is_sent(self):
        r, events = self.revive("docs")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("hub", r.stderr)
        self.assertFalse([e for e in events if e["type"] == "session.requested"])


if __name__ == "__main__":
    unittest.main()
