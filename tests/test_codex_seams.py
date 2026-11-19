"""Codex (experimental): the seams of slices A/B/C are in place and without
behaviour — the Codex host works as without them, the fake answers with the test's data ($FAKE_CODEX_REPLIES),
`executor.unmerged` is not accepted yet."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE = os.path.join(ROOT, "tests", "fakes", "fake_codex.py")


class FakeReplies(unittest.TestCase):
    def test_method_and_notes_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            replies = os.path.join(tmp, "r.json")
            with open(replies, "w") as f:
                json.dump({"account/read": {"account": None, "requiresOpenaiAuth": True},
                           "account/logout": {"error": {"code": -1, "message": "no"}},
                           "account/login/start": [{"result": {"type": "chatgptDeviceCode", "loginId": "l1"},
                                                    "notes": [{"method": "account/login/completed",
                                                               "params": {"success": False, "loginId": "l1"}}]},
                                                   {"result": {"loginId": "l2"}}],
                           "_notes": [{"method": "account/rateLimits/updated", "params": {"rateLimits": {}}}]}, f)
            calls = os.path.join(tmp, "calls.jsonl")
            env = {**os.environ, "CODEX_HOME": tmp, "FAKE_CODEX_REPLIES": replies, "FAKE_CODEX_CALLS": calls}
            inp = "\n".join(json.dumps(m) for m in (
                {"id": 0, "method": "initialize", "params": {}}, {"id": 1, "method": "account/read", "params": {}},
                {"id": 2, "method": "account/logout", "params": {}}, {"id": 3, "method": "nope", "params": {}},
                {"id": 4, "method": "account/login/start", "params": {}},
                {"id": 5, "method": "account/login/start", "params": {}},
                {"id": 6, "method": "account/login/start", "params": {}})) + "\n"
            out = subprocess.run([sys.executable, FAKE], input=inp, capture_output=True, text=True, env=env,
                                 timeout=30).stdout
            with open(calls, encoding="utf-8") as f:
                logged = [json.loads(x) for x in f]
        got = [json.loads(x) for x in out.splitlines()]
        self.assertEqual(got[1]["method"], "account/rateLimits/updated")
        by = {g.get("id"): g for g in got if "id" in g}
        self.assertEqual(by[1]["result"]["requiresOpenaiAuth"], True)
        self.assertEqual(by[2]["error"]["message"], "no")
        self.assertEqual(by[3], {"id": 3, "result": {}})   # a method nobody knows: an empty result, not an error
        self.assertEqual([(c["method"], c["known"]) for c in logged],
                         [("initialize", True), ("account/read", True), ("account/logout", True), ("nope", False),
                          ("account/login/start", True), ("account/login/start", True), ("account/login/start", True)])
        self.assertEqual([by[i]["result"]["loginId"] for i in (4, 5, 6)], ["l1", "l2", "l2"])   # per call
        after = [g for g in got if g.get("method") == "account/login/completed"]
        self.assertEqual(len(after), 1)
        self.assertGreater(got.index(after[0]), got.index(by[4]))


class FakeHooksList(unittest.TestCase):
    def test_hooks_json_and_trust(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "hooks.json"), "w") as f:
                json.dump({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}]}}, f)
            env = {**os.environ, "CODEX_HOME": tmp}
            ask = json.dumps({"id": 1, "method": "hooks/list", "params": {"cwds": ["/w"]}}) + "\n"
            run = lambda: [json.loads(x) for x in subprocess.run(  # noqa: E731
                [sys.executable, FAKE], input=ask, capture_output=True, text=True, env=env, timeout=30).stdout.splitlines()]
            (hook,) = run()[0]["result"]["data"][0]["hooks"]
            self.assertEqual((hook["key"], hook["eventName"], hook["trustStatus"]),
                             (os.path.join(os.path.realpath(tmp), "hooks.json") + ":pre_tool_use:0:0", "preToolUse",
                              "untrusted"))   # by the real path, as Codex names it (macOS's TMPDIR is a link)
            with open(os.path.join(tmp, "config.toml"), "w") as f:
                # the key as a TOML string, as the core writes it ("\\" of a Windows path escaped)
                f.write(f'[hooks.state.{json.dumps(hook["key"])}]\ntrusted_hash = "{hook["currentHash"]}"\n')
            self.assertEqual(run()[0]["result"]["data"][0]["hooks"][0]["trustStatus"], "trusted")


class Stubs(unittest.TestCase):
    def test_stubs_change_nothing(self):
        from hosts import codex_limits, codex_rules
        # codex_rules.thread/features — with slice B's behaviour: tests/test_codex_rules.py
        self.assertEqual(codex_rules.notice(None, "maps", "turn/plan/updated", {}), [])
        # slice A's seams — its tests (tests/test_codex_limits.py); other replies it hands to the host
        self.assertIsNone(codex_limits.response(None, "maps", "thread/start", {}, {"id": 1, "error": {}}))

    def test_unmerged_not_accepted_yet(self):
        from core import executors
        from core.tools import Rejected
        with self.assertRaises(Rejected):
            executors.on_unmerged({}, {}, {}, {})


class Seams(unittest.TestCase):
    """The host's parts are in files of their own and the host calls
    them at the seams (hosts/codex.py, "Seams") — the slices of wave 1 edit only their files."""

    def host(self):
        import io
        import types
        from hosts import codex
        host = codex.CodexHost.__new__(codex.CodexHost)
        popen = types.SimpleNamespace(stdin=io.StringIO(), poll=lambda: None, pid=1)
        proc = codex.CodexProcess(popen, "sid", False)
        proc.thread = "t"
        host.procs = {"a": proc}
        return host, proc, popen

    def sent(self, popen, method):
        return [json.loads(x) for x in popen.stdin.getvalue().splitlines() if json.loads(x).get("method") == method]

    def test_turn_extra_goes_with_the_next_turn_start_together_with_the_effort(self):
        """A compaction's context (slice D) and an effort set before the turn (slice A): one turn/start carries both;
        a refused turn/start keeps the context for the retry, a good answer clears it, turn/steer does not carry it."""
        host, proc, popen = self.host()
        proc.effort = "high"                                                       # /effort high (slice A)
        proc.turn_extra["additionalContext"]["direction"] = "the board's rules"   # after a compaction (slice D)
        proc.backlog.append([{"type": "text", "text": "x"}])
        host._flush(proc)
        first = self.sent(popen, "turn/start")[-1]["params"]
        want = {"direction": {"kind": "application", "value": "the board's rules"}}
        self.assertEqual((first["effort"], first["additionalContext"]), ("high", want))
        host._response("a", proc, {"id": self.sent(popen, "turn/start")[-1]["id"],
                                   "error": {"code": -32600, "message": "turn already active"}})
        again = self.sent(popen, "turn/start")[-1]
        self.assertEqual((again["params"]["effort"], again["params"]["additionalContext"]), ("high", want))
        proc.turn_extra["additionalContext"]["other"] = "added while the answer was on its way"
        host._response("a", proc, {"id": again["id"], "result": {"turn": {"id": "u1"}}})
        self.assertEqual(proc.turn_extra["additionalContext"], {"other": "added while the answer was on its way"})
        proc.backlog.append([{"type": "text", "text": "mid-turn"}])
        host._flush(proc)                                                          # the turn runs: an insertion
        (steer,) = self.sent(popen, "turn/steer")
        self.assertNotIn("additionalContext", steer["params"])
        self.assertIn("other", proc.turn_extra["additionalContext"])
        host._line("a", {"method": "turn/completed", "params": {"threadId": "t", "turn": {"id": "u1", "status": "completed"}}})
        proc.backlog.append([{"type": "text", "text": "y"}])
        host._flush(proc)
        last = self.sent(popen, "turn/start")[-1]
        self.assertEqual(list(last["params"]["additionalContext"]), ["other"])
        host._response("a", proc, {"id": last["id"], "result": {"turn": {"id": "u2"}}})
        host._line("a", {"method": "turn/completed", "params": {"threadId": "t", "turn": {"id": "u2", "status": "completed"}}})
        proc.backlog.append([{"type": "text", "text": "z"}])
        host._flush(proc)
        self.assertNotIn("additionalContext", self.sent(popen, "turn/start")[-1]["params"])

    def test_a_rewritten_context_stays_for_the_next_turn(self):
        host, proc, popen = self.host()
        proc.turn_extra["additionalContext"]["direction"] = "old"
        proc.backlog.append([{"type": "text", "text": "x"}])
        host._flush(proc)
        proc.turn_extra["additionalContext"]["direction"] = "new"
        host._response("a", proc, {"id": self.sent(popen, "turn/start")[-1]["id"], "result": {"turn": {"id": "u1"}}})
        self.assertEqual(proc.turn_extra["additionalContext"], {"direction": "new"})

    def test_the_host_calls_every_seam(self):
        from unittest import mock
        from hosts import codex, codex_cmds, codex_items, codex_liaison, codex_life, codex_limits, codex_mcp, codex_rules
        host, proc, popen = self.host()
        calls = []

        def spy(mod, fn, ret):
            def f(*a, **kw):
                calls.append(f"{mod.__name__.split('.')[-1]}.{fn}")
                return ret(*a, **kw) if callable(ret) else ret
            return mock.patch.object(mod, fn, f, create=True)
        with spy(codex_cmds, "notice", [("x.cmds", "a", {})]), spy(codex_mcp, "notice", [("x.mcp", "a", {})]), \
                spy(codex_life, "notice", []), spy(codex_items, "notice", []), spy(codex_limits, "notice", []), \
                spy(codex_rules, "notice", []):
            facts = host._line("a", {"method": "some/new", "params": {"threadId": "t"}})
        self.assertEqual([f[0] for f in facts], ["x.cmds", "x.mcp"])
        self.assertEqual(sorted(calls), sorted(["codex_limits.notice", "codex_cmds.notice", "codex_mcp.notice",
                                                "codex_items.notice", "codex_life.notice", "codex_rules.notice"]))
        calls.clear()
        with spy(codex_items, "child", [("x.child", "a", {})]):   # another thread — a subagent's notification
            self.assertEqual(host._line("a", {"method": "item/started", "params": {"threadId": "t2", "item": {}}}),
                             [("x.child", "a", {})])
        # a response: the first module that takes it; the host does not touch it further
        rid = proc.call("x/own", {})
        with spy(codex_mcp, "response", [("x.mcp", "a", {})]), spy(codex_life, "response", [("x.life", "a", {})]):
            self.assertEqual(host._response("a", proc, {"id": rid, "result": {}}), [("x.mcp", "a", {})])
        self.assertNotIn("codex_life.response", calls)
        # a request the host does not know: the slices'; none — -32601
        with spy(codex_mcp, "request", lambda proc, name, rid, method, p: [("x.elicit", name, {})]
                 if method == "mcpServer/elicitation/request" else None):
            self.assertEqual(host._request("a", proc, 7, "mcpServer/elicitation/request", {}), [("x.elicit", "a", {})])
            self.assertEqual(host._request("a", proc, 8, "x/other", {}), [])
        self.assertEqual(json.loads(popen.stdin.getvalue().splitlines()[-1])["error"]["code"], -32601)
        # the owner's answer to a card a slice put in proc.asks
        proc.asks["cx-9"] = (9, "mcpServer/elicitation/request", {"q": 1})
        with spy(codex_mcp, "answer_request", lambda proc, method, data, response: {"action": "accept"}):
            host.alive = lambda name: True
            self.assertEqual(host.respond("a", "cx-9", {"behavior": "allow"}), "answered")
        self.assertEqual(json.loads(popen.stdin.getvalue().splitlines()[-1]), {"id": 9, "result": {"action": "accept"}})
        # the start: info into session.info, start of each
        rid = proc.call("thread/start", {})
        with spy(codex_mcp, "info", {"mcp": ["browser"]}), spy(codex_cmds, "info", {"builtins": ["model"]}), \
                spy(codex_life, "start", None), mock.patch.object(host, "_remember", lambda *a: None, create=True):
            facts = host._response("a", proc, {"id": rid, "result": {"thread": {"id": "t"}}})
        (info,) = [f[2] for f in facts if f[0] == "session.info"]
        self.assertEqual((info["mcp"], info["builtins"], info["host"]), (["browser"], ["model"], "codex"))
        self.assertIn("codex_life.start", calls)
        # the end of a turn and of a compaction
        proc.turn = "u1"
        with spy(codex_life, "turn_done", lambda proc, name, turn: [("x.done", name, {"turn": turn.get("id")})]):
            facts = host._line("a", {"method": "turn/completed", "params": {"threadId": "t",
                                                                            "turn": {"id": "u1", "status": "completed"}}})
        self.assertEqual([f[0] for f in facts], ["turn.stopped", "turn.result", "x.done"])
        proc.compacts = 1
        with spy(codex_life, "compacted", [("x.compacted", "a", {})]):
            facts = host._line("a", {"method": "thread/compacted", "params": {"threadId": "t"}})
        self.assertEqual([f[0] for f in facts], ["session.compacted", "x.compacted"])

    def test_actions_go_through_their_slice(self):
        from unittest import mock
        from hosts import codex_cmds, codex_life, codex_mcp
        host, proc, popen = self.host()
        host.alive = lambda name: True
        host.mod_input = lambda name, text, message=None: text
        with mock.patch.object(codex_cmds, "intercept", lambda host, name, proc, text, **kw: "set" if text == "/x" else None):
            self.assertEqual(host.type("a", "/x", message="m1"), "set")
            self.assertEqual(self.sent(popen, "turn/start"), [])
            with mock.patch.object(codex_cmds, "input", lambda proc, inp, text: inp + [{"type": "skill", "name": "s"}]):
                self.assertEqual(host.type("a", "hello", message="m2"), "sent")
        self.assertEqual(self.sent(popen, "turn/start")[-1]["params"]["input"][-1], {"type": "skill", "name": "s"})
        with mock.patch.object(codex_cmds, "compact", lambda host, name, proc, text: "refused" if text else None):
            self.assertEqual(host.compact("a", text="keep the plan"), "refused")
            self.assertEqual(self.sent(popen, "thread/compact/start"), [])
            self.assertEqual(host.compact("a"), "sent")
        with mock.patch.object(codex_mcp, "status", lambda host, name, proc: {"servers": []}), \
                mock.patch.object(codex_life, "recap", lambda host, name, proc: "sent"), \
                mock.patch.object(codex_life, "reap", lambda host, pid, sid: pid == 5):
            self.assertEqual((host.mcp_status("a"), host.recap("a"), host.reap(5, "sid"), host.reap(6, "sid")),
                             ({"servers": []}, "sent", True, False))

    def test_the_core_model_still_finds_the_answer_in_the_host(self):
        """core/codexmodel takes the turn's text from hosts.codex._answer (the core's model): it moved to codex_items."""
        from types import SimpleNamespace
        from hosts import codex, codex_items
        self.assertIs(codex._answer, codex_items.answer)
        proc = SimpleNamespace(texts=[("commentary", "c"), ("final_answer", "a"), ("final_answer", "b")], last_text="b")
        self.assertEqual(codex._answer(proc, limit=10 ** 9), "a\n\nb")

    def test_a_session_start_goes_through_its_slices(self):
        """spawn: codex_items.features and codex_set.args on the command line, codex_set.thread_config and
        codex_liaison.thread in thread/start — a real CodexHost with the fake app-server."""
        import platforms
        from unittest import mock
        from core import spool
        from core.daemon import Hub
        from hosts import codex_items, codex_liaison, codex_set
        from hosts.codex import CodexHost
        state = tempfile.mkdtemp(prefix="hubcx0-")
        self.addCleanup(platforms.current.rmtree, state)
        calls, argv = os.path.join(state, "calls.jsonl"), os.path.join(state, "argv")
        host = CodexHost(state, codex=(sys.executable, FAKE), all_sessions=True, default_model="gpt-test",
                         env={"FAKE_TURN_S": "0.2", "FAKE_CODEX_CALLS": calls, "FAKE_ARGV_DIR": argv})
        hub = Hub(state, host, registry_dir=os.path.join(state, "registry"))
        self.addCleanup(host.kill_server)
        with mock.patch.object(codex_items, "features", lambda **s: ("x_feature",)), \
                mock.patch.object(codex_set, "args", lambda enabled: ["-c", f"x.enabled={','.join(enabled)}"]), \
                mock.patch.object(codex_set, "thread_config", lambda mcp, env: {"x": bool(env.get("CODEX_HOME"))}), \
                mock.patch.object(codex_liaison, "thread", lambda params, **s: {**params, "x_liaison": s["liaison"]}):
            hub.start()
            spool.put(os.path.join(state, "spool"), "session.requested", "a", {"cwd": state, "session_id": "sid-a"})
            end = time.time() + 10
            while hub.state["sessions"].get("a", {}).get("state") != "idle" and time.time() < end:
                hub.tick()
                time.sleep(0.03)
        self.assertEqual(hub.state["sessions"]["a"]["state"], "idle")
        (got,) = [g for g in (json.load(open(os.path.join(argv, f))) for f in os.listdir(argv))
                  if g["env"].get("HUB_SESSION") == "a"]   # not the hook trust runs of the host's start
        self.assertIn("x.enabled=default_mode_request_user_input,x_feature", got["argv"])
        self.assertEqual(got["argv"][-4:], ["--enable", "default_mode_request_user_input", "--enable", "x_feature"])
        with open(calls, encoding="utf-8") as f:
            (start,) = [c["params"] for c in map(json.loads, f) if c["method"] == "thread/start"]
        self.assertEqual((start["config"], start["x_liaison"]), ({"x": True}, False))

    def test_a_feature_a_slice_turns_on_is_not_pinned_off(self):
        from hosts import codex_set
        self.assertIn("features.multi_agent=false", codex_set.args(()))
        pins = codex_set.args(("multi_agent", "default_mode_request_user_input"))
        self.assertNotIn("features.multi_agent=false", pins)
        self.assertIn("features.multi_agent_v2=false", pins)
        self.assertIn("features.hooks=true", codex_set.args(("hooks",)))   # an "on" pin stays

    def test_the_stubs_keep_todays_behavior(self):
        from hosts import codex, codex_liaison, codex_life
        # codex_cmds (slice A), codex_mcp (slice B), codex_items (slice C) have their behaviour now: tests/test_codex_cmds.py,
        # test_codex_mcp.py, test_codex_items.py
        self.assertEqual(codex_life.turn_done(None, "a", {}), [])   # recap, reap, compacted — slice D: tests/test_codex_life.py
        # slice H raises the liaison on Codex (tests/test_codex_liaison.py); other sessions' threads stay as they are
        self.assertTrue(callable(codex.CodexHost.liaison_settings))
        self.assertIsNone(codex_liaison.check(True))
        self.assertEqual(codex_liaison.thread({"a": 1}), {"a": 1})


if __name__ == "__main__":
    unittest.main()
