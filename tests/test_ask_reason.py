"""Why Claude Code asks permission from a session in bypassPermissions (the owner was tired of pressing confirm,
and the core did not keep the reason): the project's ask rule, a sensitive path — in the fact and on the card."""
import tests  # noqa: F401 — ignore this machine's settings
import unittest

from core import reducer
from hosts.streamjson import ask_reason


class AskReason(unittest.TestCase):
    def test_rule_reason_and_path(self):
        self.assertEqual(ask_reason({"matched_ask_rule": {"source": "projectSettings", "tool_name": "Bash",
                                                          "rule_content": "git push --force*"}}),
                         "ask rule Bash(git push --force*) (projectSettings)")
        self.assertEqual(ask_reason({"decision_reason": "Claude requested permissions to edit /w/.git/config which "
                                                        "is a sensitive file.", "blocked_path": "/w/.git/config"}),
                         "Claude requested permissions to edit /w/.git/config which is a sensitive file.")
        self.assertEqual(ask_reason({"blocked_path": "/w/x"}), "path /w/x")
        self.assertIsNone(ask_reason({"tool_name": "Bash"}))

    def test_reason_reaches_the_pending_request(self):
        st = reducer.empty()
        for seq, (t, data) in enumerate([("session.requested", {"cwd": "/w", "session_id": "s"}),
                                         ("session.started", {}),
                                         ("permission.requested", {"request_id": "r1", "tool": "Bash",
                                                                   "input": {"command": "git push --force-with-lease"},
                                                                   "why": "ask rule Bash(git push --force*)"})], 1):
            st, _, _ = reducer.reduce(st, {"seq": seq, "ts": 100 + seq, "type": t, "entity": "w", "data": data})
        self.assertEqual(st["sessions"]["w"]["pending"][0]["why"], "ask rule Bash(git push --force*)")


class AutoAllow(unittest.TestCase):
    """14.10, owner decision: make it a setting, off by default; then make it global — answer yes to all confirmation
    requests (not to form questions). When on, the core answers by itself."""
    CD = "Compound command contains cd with output redirection - manual approval required to prevent path resolution bypass"

    def test_every_confirmation_but_a_question_is_automatic(self):
        from hosts.streamjson import auto_allow
        self.assertTrue(auto_allow({"tool_name": "Bash", "decision_reason": self.CD}))
        self.assertTrue(auto_allow({"tool_name": "Bash", "decision_reason": self.CD, "matched_ask_rule": {
            "tool_name": "Bash", "rule_content": "git push --force*"}}))              # the project's ask rule — too
        self.assertTrue(auto_allow({"tool_name": "Edit", "decision_reason": "Claude requested permissions to edit "
                                                                           "/w/.git/config which is a sensitive file."}))
        self.assertTrue(auto_allow({"tool_name": "mcp__x__send", "input": {}}))
        self.assertFalse(auto_allow({"tool_name": "AskUserQuestion", "requires_user_interaction": True}))  # a form
        self.assertFalse(auto_allow({"tool_name": "AskUserQuestion"}))
        self.assertFalse(auto_allow({"tool_name": "ExitPlanMode"}))               # a plan is also a question for the owner
        self.assertFalse(auto_allow({"tool_name": "Bash", "requires_user_interaction": True}))

    def run_events(self, src):
        st, acts = reducer.empty(), []
        evs = [("session.requested", {"cwd": "/w", "session_id": "s"}, "web"), ("session.started", {}, "host"),
               ("turn.started", {"marker": None}, "hook"),
               ("permission.requested", {"request_id": "r1", "tool": "Bash", "input": {"command": "cd /w/a; make > l"},
                                         "why": self.CD, "auto": "allow"}, src)]
        for seq, (t, data, s) in enumerate(evs, 1):
            st, a, _ = reducer.reduce(st, {"seq": seq, "ts": 100 + seq, "src": s, "type": t, "entity": "w", "data": data})
            acts = a
        return st, acts

    def test_the_core_answers_at_once_and_nothing_waits_for_the_owner(self):
        st, acts = self.run_events("host")
        self.assertEqual(st["sessions"]["w"]["pending"], [])
        self.assertNotEqual(st["sessions"]["w"]["state"], "dialog")
        (r,) = [a for a in acts if a["do"] == "respond"]
        self.assertEqual((r["request_id"], r["response"]["behavior"], r["response"]["updatedInput"]["command"]),
                         ("r1", "allow", "cd /w/a; make > l"))

    def test_the_watch_mark_goes_as_with_an_ordinary_request(self):
        st, acts = reducer.empty(), []
        evs = [("session.requested", {"cwd": "/w", "session_id": "s"}, "web"), ("session.started", {}, "host"),
               ("turn.started", {"marker": None}, "hook")]
        for seq, (t, data, s) in enumerate(evs, 1):
            st, _, _ = reducer.reduce(st, {"seq": seq, "ts": 100 + seq, "src": s, "type": t, "entity": "w", "data": data})
        st["sessions"]["w"]["attention"] = {"why": "x"}
        st, _, _ = reducer.reduce(st, {"seq": 9, "ts": 200, "src": "host", "type": "permission.requested", "entity": "w",
                                       "data": {"request_id": "r1", "tool": "Bash", "input": {}, "auto": "allow"}})
        self.assertNotIn("attention", st["sessions"]["w"])

    def test_a_mark_not_from_the_host_is_ignored(self):
        st, acts = self.run_events("session")       # a session cannot allow itself
        self.assertEqual([p["request_id"] for p in st["sessions"]["w"]["pending"]], ["r1"])
        self.assertFalse([a for a in acts if a["do"] == "respond"])


class AutoAllowHost(unittest.TestCase):
    def test_host_marks_only_with_the_setting_on(self):
        import tempfile
        from hosts.streamjson import StreamHost
        h = StreamHost(tempfile.mkdtemp(prefix="autoallow-"), claude=("claude",))
        msg = {"type": "control_request", "request_id": "r1", "request": {
            "subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "cd /w/a; git commit -m x"},
            "decision_reason": "Compound commands with cd and git require permission checks for enhanced security"}}
        h.auto_allow_on = lambda: False
        ((_, _, off),) = h._line("w", msg)
        self.assertNotIn("auto", off)
        h.auto_allow_on = lambda: True
        ((_, _, on),) = h._line("w", msg)
        self.assertEqual(on["auto"], "allow")
        ask = {"type": "control_request", "request_id": "r2", "request": {
            "subtype": "can_use_tool", "tool_name": "AskUserQuestion", "input": {"questions": []},
            "requires_user_interaction": True}}
        ((_, _, q),) = h._line("w", ask)
        self.assertNotIn("auto", q)                 # a form question always goes to the owner

    def test_setting_key(self):
        import json, os, tempfile
        from hosts.streamjson import StreamHost
        d = tempfile.mkdtemp(prefix="autoallow-")
        h = StreamHost(d, claude=("claude",))
        old, os.environ["HUB_CONFIG"] = os.environ.get("HUB_CONFIG"), os.path.join(d, "hub.json")
        try:
            self.assertFalse(h.auto_allow_on())     # off by default
            with open(os.path.join(d, "hub.json"), "w") as f:
                json.dump({"permissions": {"auto_allow": True}}, f)
            self.assertTrue(h.auto_allow_on())
        finally:
            os.environ.pop("HUB_CONFIG")
            if old is not None:
                os.environ["HUB_CONFIG"] = old


if __name__ == "__main__":
    unittest.main()
