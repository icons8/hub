"""The executor's model and effort are set per task (decision of 2026-10-06: model and effort belong in the core):
`hub task run … --model sonnet --effort medium`, the same for `hub task new` (applies when the hub hands the task to an
executor) and for `hub route … executor`. Without flags — from settings (`model`, `effort` in hub.json), as for all
sessions; an unknown value is refused at once, rather than an executor starting without the flag."""
import tests  # noqa: F401 — keep the machine's settings out
import os
import subprocess
import sys
import tempfile
import unittest

from core import executors, journal, reducer, spool

OWNER = {"channel": "web", "owner": True}
HUB = {"channel": "session", "owner": False, "sender": "hub"}
MAPS = {"channel": "session", "owner": False, "sender": "maps"}
BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")


class TaskModel(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "devops", cwd="/k", session_id="s-d", role="agent", about="DevOps", **OWNER)
        self.ev("session.requested", "maps", cwd="/m", session_id="s-m", role="agent", about="Maps", **OWNER)
        for n in ("hub", "devops", "maps"):
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        return why

    def raise_ready(self):
        (name, run), = [(n, r) for n, r in self.st["runs"].items() if r.get("state") == "ready"]
        for type, entity, data in executors.events_for(self.st, name, run, cwd="/k/.claude/worktrees/x", worktree=None):
            self.assertIsNone(self.ev(type, entity, **data), (type, data))
        (spawn,) = [a for a in self.actions if a["do"] == "spawn" and a.get("session") == name]
        return self.st["sessions"][name], spawn

    def test_run_with_model_and_effort(self):
        self.assertIsNone(self.ev("task.run_requested", "hub-sweep", task="summary of the logs", model="sonnet",
                                  effort="medium", **HUB))
        sess, spawn = self.raise_ready()
        self.assertEqual((sess["model"], sess["effort"]), ("sonnet", "medium"))
        self.assertEqual((spawn["model"], spawn["effort"]), ("sonnet", "medium"))

    def test_without_flags_nothing_of_its_own(self):
        self.ev("task.run_requested", "hub-sweep", task="summary", **HUB)
        sess, spawn = self.raise_ready()
        self.assertNotIn("model", sess)                # from settings — at launch (catalog.model_flags)
        self.assertNotIn("effort", spawn)

    def test_unknown_model_or_effort_is_refused(self):
        self.assertIn("sonnet", self.ev("task.run_requested", "a", task="x", model="gpt-9", **HUB) or "")
        self.assertIn("high", self.ev("task.run_requested", "b", task="x", effort="huge", **HUB) or "")
        self.assertFalse(self.st.get("runs"))

    def test_a_task_routed_to_an_executor_keeps_its_model(self):
        self.ev("turn.started", "devops", marker=None, prompt_head="long work")
        self.assertIsNone(self.ev("delegation.created", None, session="devops", context="check the certificate",
                                  for_session="maps", model="opus", effort="high", **MAPS))
        d = f"d{self.seq}"
        self.assertIsNone(self.ev("message.routed", d, route="executor", name="certs", **HUB))
        sess, spawn = self.raise_ready()
        self.assertEqual((spawn["model"], spawn["effort"]), ("opus", "high"))

    def test_the_hub_route_overrides_it(self):
        self.ev("turn.started", "devops", marker=None, prompt_head="long work")
        self.ev("delegation.created", None, session="devops", context="check", for_session="maps", effort="high",
                **MAPS)
        self.assertIsNone(self.ev("message.routed", f"d{self.seq}", route="executor", name="certs", model="sonnet",
                                  **HUB))
        sess, spawn = self.raise_ready()
        self.assertEqual((spawn["model"], spawn["effort"]), ("sonnet", "high"))

    def test_bad_model_on_a_task_is_refused(self):
        self.assertIsNotNone(self.ev("delegation.created", None, session="devops", context="x", for_session="maps",
                                     model="gpt-9", **MAPS))


class TaskModelCli(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="task-model-")
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        for n in ("maps", "devops"):
            j.append("session.requested", n, {"cwd": "/w", "session_id": f"s-{n}", "channel": "cli", "owner": True})

    def hub(self, *args, me="hub"):
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CLI_WAIT": "0", "HUB_SESSION": me}
        r = subprocess.run([sys.executable, BIN, *args], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        return [ev for _, ev in spool.take(os.path.join(self.state, "spool"))]

    def test_flags(self):
        (ev,) = self.hub("task", "run", "maps-sweep", "summary", "--work", "normal", "--model", "sonnet", "--effort",
                         "medium")
        self.assertEqual((ev["data"]["task"], ev["data"]["model"], ev["data"]["effort"]), ("summary", "sonnet", "medium"))
        ev = self.hub("task", "new", "devops", "--effort", "high", "check", me="maps")[-1]
        self.assertEqual((ev["data"]["context"], ev["data"]["effort"]), ("check", "high"))
        self.assertNotIn("model", ev["data"])
        ev = self.hub("route", "d7", "executor", "--name", "certs", "--work", "risky", "--model", "opus")[-1]
        self.assertEqual((ev["data"]["name"], ev["data"]["model"]), ("certs", "opus"))

    def refused(self, *args, me="devops", owner=False):
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CLI_WAIT": "0"}
        env.pop("HUB_SESSION", None)
        if not owner:
            env["HUB_SESSION"] = me
        r = subprocess.run([sys.executable, BIN, *args], env=env, capture_output=True, text=True)
        return r, [ev for _, ev in spool.take(os.path.join(self.state, "spool"))]

    def test_routing_a_task_that_already_names_its_model(self):
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        j.append("session.requested", "hub", {"cwd": "/h", "session_id": "s-hub", "kind": "hub", "channel": "cli",
                                              "owner": True})
        j.append("delegation.created", None, {"session": "devops", "context": "x", "for_session": "maps",
                                              "model": "opus", "effort": "high", **MAPS})
        (did,) = reducer_ids(self.state)
        (ev,) = self.hub("route", did, "executor", "--name", "certs")             # the task already carries model and effort
        self.assertEqual(ev["data"]["route"], "executor")


def reducer_ids(state):
    import json
    out = subprocess.run([sys.executable, BIN, "status", "--json"], capture_output=True, text=True,
                         env={**os.environ, "HUB_STATE_DIR": state}).stdout
    return list(json.loads(out)["delegations"])


if __name__ == "__main__":
    unittest.main()
