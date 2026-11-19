"""A session withdraws its own question to the owner (07.10, hub: two sessions asked the same rule, and there was
no way to remove the duplicate). `hub question withdraw qN ["why"]` — a session (its own question) or the hub (any):
the card leaves the page, Telegram and reminders. The core hints to the hub when a new question repeats an open
question of another session."""
import tests  # noqa: F401 — keep the machine's settings out
import unittest

from core import reducer

OWNER = {"channel": "cli", "owner": True}
Q = {"questions": [{"question": "Delete the site on a complaint without checking?", "options": [{"label": "Yes"}, {"label": "No"}]}]}


def by(name):
    return {"channel": "session", "owner": False, "sender": name}


class Withdraw(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.started", "hub")
        for n in ("api", "front"):
            self.ev("session.requested", n, cwd=f"/w/{n}", session_id=f"s-{n}", **OWNER)
            self.ev("session.started", n)
            self.ev("turn.started", n, marker=None)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                   "entity": entity, "data": data})
        return why

    def ask(self, name, rid):
        self.ev("permission.requested", name, request_id=rid, tool="AskUserQuestion", interactive=True, input=Q)
        return self.st["sessions"][name]["asks"][-1]["id"]

    def hub_texts(self):
        return [m["text"] for m in self.st["messages"].values() if m["session"] == "hub"]

    def test_a_session_withdraws_its_own_question(self):
        qid = self.ask("api", "r1")
        self.assertIsNotNone(self.ev("question.withdrawn", "front", id=qid, **by("front")))   # someone else's — not allowed
        self.assertIsNone(self.ev("question.withdrawn", "api", id=qid, why="duplicate", **by("api")))
        self.assertEqual(self.st["sessions"]["api"].get("asks"), [])
        self.assertFalse([k for k in self.st["owner"]["reminders"] if qid in k or ":r1" in k])

    def test_the_hub_withdraws_any_and_the_session_is_told(self):
        qid = self.ask("api", "r1")
        self.assertIsNone(self.ev("question.withdrawn", "api", id=qid, why="same question at front", **by("hub")))
        told = [m["text"] for m in self.st["messages"].values() if m["session"] == "api"]
        self.assertTrue(any(qid in t and "same question at front" in t for t in told))

    def test_the_hub_hears_about_a_repeated_question(self):
        q1 = self.ask("front", "r1")
        q2 = self.ask("api", "r2")
        hint = [t for t in self.hub_texts() if q1 in t and q2 in t]
        self.assertEqual(len(hint), 1)
        self.assertIn("hub question withdraw", hint[0])


class Command(unittest.TestCase):
    def test_hub_question_withdraw_finds_the_session(self):
        import json
        import os
        import subprocess
        import sys
        import tempfile
        from core import journal
        state = tempfile.mkdtemp(prefix="qw-")
        j = journal.Journal(os.path.join(state, "journal.jsonl"))
        j.append("session.requested", "api", {"cwd": "/w", "session_id": "s", **OWNER})
        j.append("session.started", "api", {})
        j.append("turn.started", "api", {"marker": None})
        e = j.append("permission.requested", "api", {"request_id": "r1", "tool": "AskUserQuestion", "interactive": True,
                                                     "input": Q})
        hub = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")
        env = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0"}
        env.pop("HUB_SESSION", None)
        r = subprocess.run([sys.executable, hub, "question", "withdraw", f"q{e['seq']}", "duplicate"], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (item,) = [json.load(open(os.path.join(state, "spool", n))) for n in os.listdir(os.path.join(state, "spool"))]
        self.assertEqual((item["type"], item["entity"], item["data"]["id"], item["data"]["why"]),
                         ("question.withdrawn", "api", f"q{e['seq']}", "duplicate"))

    def test_withdraw_and_show_find_the_session_by_one_helper(self):
        # hub question withdraw and show look up the question's session with one helper — so both refuse the same way
        import importlib.machinery
        import importlib.util
        import os
        import subprocess
        import sys
        import tempfile
        from core import journal
        state = tempfile.mkdtemp(prefix="qs-")
        j = journal.Journal(os.path.join(state, "journal.jsonl"))
        j.append("session.requested", "api", {"cwd": "/w", "session_id": "s", **OWNER})
        j.append("session.started", "api", {})
        j.append("turn.started", "api", {"marker": None})
        e = j.append("permission.requested", "api", {"request_id": "r1", "tool": "AskUserQuestion", "interactive": True,
                                                     "input": Q})
        hub = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")
        loader = importlib.machinery.SourceFileLoader("hub_cli", hub)
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("hub_cli", loader))
        loader.exec_module(mod)
        mod.STATE = state
        self.assertEqual(mod.asker_or_exit(f"q{e['seq']}"), "api")
        with self.assertRaises(SystemExit) as gone:
            mod.asker_or_exit("q999")
        self.assertEqual(str(gone.exception), "no open question q999 (already answered or withdrawn)")
        env = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0"}
        env.pop("HUB_SESSION", None)
        for cmd in (["withdraw", "q999"], ["show", "q999"]):
            r = subprocess.run([sys.executable, hub, "question", *cmd], env=env, capture_output=True, text=True,
                               timeout=60)
            self.assertEqual((r.returncode, r.stderr.strip()), (1, str(gone.exception)), cmd)


if __name__ == "__main__":
    unittest.main()
