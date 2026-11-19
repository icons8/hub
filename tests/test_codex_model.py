"""Codex (experimental): the core's model on a Codex hub — the watcher
("waiting for you", urgency, alerts), push and the voice summary call the hub's `codex app-server` (an ephemeral,
read-only thread, outputSchema), not `claude -p`; the "working on" line comes from the Codex plan, without a model; the
critic is Codex on the hub's Codex folder, without a claude fallback."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

import platforms
from unittest import mock

from core import codexmodel

# an app-server for one question: writes the requests and the environment to $LOG; the answer is $ANSWER, or a turn failure by $MODE
SERVER = r'''
import json, os, sys
log = open(os.environ["LOG"], "a")
log.write(json.dumps({"env": {"CODEX_HOME": os.environ.get("CODEX_HOME"),
                              "claude": sorted(k for k in os.environ if k.startswith(("ANTHROPIC", "CLAUDE", "HUB_"))),
                              "core_model": os.environ.get("HUB_CORE_MODEL"), "on_codex": os.environ.get("HUB_ON_CODEX")},
                      "argv": sys.argv[1:]}) + "\n")
def out(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    m = json.loads(line); log.write(line); log.flush()
    if m.get("method") == "initialize":
        out({"id": m["id"], "result": {"userAgent": "fake/0.162.1"}})
    elif m.get("method") == "hooks/list":   # the core's hook in CODEX_HOME: $HOOKS — trusted (by default) | modified | none | error
        home, mode = os.environ["CODEX_HOME"], os.environ.get("HOOKS", "trusted")
        path = os.path.join(os.path.realpath(home), "hooks.json")
        if mode == "error":
            out({"id": m["id"], "error": {"code": -1, "message": "hooks are not available"}}); continue
        hooks = [] if mode == "none" else [{"key": path + ":pre_tool_use:0:0", "sourcePath": path, "currentHash": "sha256:1",
                                            "trustStatus": "trusted" if mode == "trusted" else "modified"}]
        out({"id": m["id"], "result": {"data": [{"cwd": c, "hooks": hooks, "warnings": [], "errors": []}
                                                for c in m["params"].get("cwds") or [None]]}})
    elif m.get("method") == "thread/start":
        if os.environ.get("MODE") == "nologin":
            out({"id": m["id"], "error": {"code": -32600, "message": "Not logged in: requiresOpenaiAuth"}}); continue
        out({"id": m["id"], "result": {"thread": {"id": "t1", "ephemeral": True}}})
    elif m.get("method") == "turn/start":
        out({"id": m["id"], "result": {"turn": {"id": "u1", "status": "inProgress"}}})
        out({"method": "turn/started", "params": {"threadId": "t1", "turn": {"id": "u1"}}})
        if os.environ.get("MODE") == "limit":
            out({"method": "turn/completed", "params": {"threadId": "t1", "turn": {"id": "u1", "status": "failed",
                 "error": {"message": "usage limit", "codexErrorInfo": "usageLimitExceeded"}}}}); continue
        if os.environ.get("MODE") == "unauthorized":
            out({"method": "turn/completed", "params": {"threadId": "t1", "turn": {"id": "u1", "status": "failed",
                 "error": {"message": "401", "codexErrorInfo": "unauthorized"}}}}); continue
        for tin, tout in ((9000, 30), (600, 10)):   # an update per model call: last holds its tokens
            out({"method": "thread/tokenUsage/updated", "params": {"threadId": "t1", "turnId": "u1", "tokenUsage": {
                 "last": {"inputTokens": tin, "outputTokens": tout}, "total": {"inputTokens": 0, "outputTokens": 0}}}})
        out({"method": "item/completed", "params": {"threadId": "t1", "item": {"type": "agentMessage",
             "id": "a", "text": os.environ.get("ANSWER", "{}")}}})
        out({"method": "turn/completed", "params": {"threadId": "t1", "turn": {"id": "u1", "status": "completed"}}})
'''


def write_core_hooks(home):
    """The core's hook file in a Codex folder (what hosts/codex_rules.prepare_home leaves there): the core's model call
    runs only where Codex trusts it, and SERVER above says it does (HOOKS)."""
    from hosts import codex_rules
    os.makedirs(home, exist_ok=True)
    with open(os.path.join(home, "hooks.json"), "w") as f:
        json.dump(codex_rules.hooks_json(), f)


class CodexModelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hubcxm-")
        self.state = os.path.join(self.tmp, "state")
        self.conf = os.path.join(self.tmp, "conf")
        os.makedirs(self.state)
        os.makedirs(self.conf)
        self.server = os.path.join(self.tmp, "server.py")
        with open(self.server, "w") as f:
            f.write(SERVER)
        self.log = os.path.join(self.tmp, "log.jsonl")
        self.write_hooks(os.path.join(self.state, "codex-home"))
        self.env = mock.patch.dict(os.environ, {"HUB_CONFIG": os.path.join(self.conf, "hub.json"), "HUB_STATE_DIR": self.state,
                                                "LOG": self.log, "ANTHROPIC_API_KEY": "x", "CLAUDE_CODE_OAUTH_TOKEN": "y"})
        self.env.start()
        codexmodel._DOWN["until"] = 0.0

    def tearDown(self):
        self.env.stop()
        codexmodel._DOWN["until"] = 0.0
        platforms.current.rmtree(self.tmp)

    write_hooks = staticmethod(lambda home: write_core_hooks(home))

    def hub_json(self, **cfg):
        with open(os.path.join(self.conf, "hub.json"), "w") as f:
            json.dump(cfg, f)

    def sent(self):
        with open(self.log) as f:
            return [json.loads(x) for x in f]

    def ask(self, **kw):
        return codexmodel.ask("turn result", "PROMPT", {"type": "object", "required": ["needs"], "properties": {
            "needs": {"type": "boolean"}, "ask": {"type": "string"}}}, state_dir=self.state,
            codex=[sys.executable, self.server], **kw)

    def test_ephemeral_read_only_thread_with_schema_and_hub_codex_home(self):
        self.hub_json(host="codex")
        with mock.patch.dict(os.environ, {"ANSWER": '{"needs": true, "ask": "Say yes"}'}):
            got = self.ask()
        self.assertEqual(got, {"needs": True, "ask": "Say yes"})
        log = self.sent()
        from hosts import codex_set   # the isolation pins: no subagents from a trusted folder's .codex/config.toml
        # the core's model has no shell tool (a second layer beside the hook), the isolation pins as for every call
        self.assertEqual(log[0]["argv"], ["app-server", *codex_set.core_model_args(), *codex_set.args()])
        self.assertIn("features.shell_tool=false", log[0]["argv"])
        self.assertEqual(log[0]["env"]["CODEX_HOME"], os.path.join(self.state, "codex-home"))
        # neither the Claude sign-in nor the hub's variables — but the two marks of the core's own model: the hook
        # (hooks/deny_hint.py) refuses every tool of such a call
        self.assertEqual(log[0]["env"]["claude"], ["HUB_CORE_MODEL", "HUB_ON_CODEX"])
        self.assertEqual((log[0]["env"]["core_model"], log[0]["env"]["on_codex"]), ("1", "1"))
        start = next(m for m in log if m.get("method") == "thread/start")["params"]
        self.assertEqual((start["ephemeral"], start["sandbox"], start["approvalPolicy"], start["baseInstructions"],
                          start["model"]), (True, "read-only", "never", "PROMPT", "gpt-6-luna"))
        turn = next(m for m in log if m.get("method") == "turn/start")["params"]
        self.assertEqual(turn["effort"], "low")
        self.assertEqual(turn["outputSchema"]["required"], ["needs", "ask"])   # a strict schema: all fields
        self.assertIs(turn["outputSchema"]["additionalProperties"], False)

    def asked(self):
        """The thread the call started and the turn it ran (None where the call did not get that far)."""
        log = self.sent()
        return (next((m for m in log if m.get("method") == "thread/start"), None),
                next((m for m in log if m.get("method") == "turn/start"), None))

    def test_no_call_while_the_core_hook_is_not_trusted(self):
        """The core's model has no tools only while Codex runs the core's hook for it (hooks/deny_hint.py refuses every
        tool of HUB_CORE_MODEL=1): the hook file gone, Codex not trusting it (changed, new Codex), or hooks/list failing
        — no thread, no turn, an Error (the watcher moves the question, as on any failure). Not a pause: the next call
        asks again, the hook may be trusted by then."""
        home = os.path.join(self.state, "codex-home")
        for label, mode, files in (("modified", "modified", True), ("not listed", "none", True), ("error", "error", True),
                                   ("no file", "trusted", False)):
            if not files:
                os.remove(os.path.join(home, "hooks.json"))
            with mock.patch.dict(os.environ, {"HOOKS": mode, "ANSWER": '{"needs": true, "ask": "x"}'}):
                with self.assertRaises(codexmodel.Error, msg=label) as e:
                    self.ask(now=1000.0)
            self.assertNotIsInstance(e.exception, codexmodel.Off, label)
            self.assertEqual(self.asked(), (None, None), label)   # not a word reached the model
            self.assertEqual(codexmodel._DOWN["until"], 0.0, label)
            self.assertNotIn("login", str(e.exception).lower(), label)
            platforms.current.rmtree(self.state)
            os.makedirs(self.state)
            os.remove(self.log)
            self.write_hooks(home)
        with mock.patch.dict(os.environ, {"ANSWER": '{"needs": true, "ask": "x"}'}):   # trusted: the call goes
            self.assertEqual(self.ask(now=1000.0), {"needs": True, "ask": "x"})
        self.assertTrue(all(self.asked()))

    def test_a_call_not_made_says_so_in_the_log_once_per_pause(self):
        """The owner sees why the watcher is silent (not a silent skip), and the log is not flooded: every result asks again."""
        import io
        codexmodel._UNTRUSTED.clear()
        os.remove(os.path.join(self.state, "codex-home", "hooks.json"))
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            for now in (1000.0, 1001.0, 1000.0 + codexmodel.PAUSE + 1):
                with self.assertRaises(codexmodel.Untrusted):
                    self.ask(now=now)
        lines = [x for x in err.getvalue().splitlines() if "the core's model was not called" in x]
        self.assertEqual(len(lines), 2)   # the first, and again after PAUSE

    def test_the_hook_is_looked_up_in_the_folder_the_call_runs_in(self):
        """A Codex account pool: the call runs in the active account's folder, whose own hooks.json holds the trust —
        the hub folder's being fine is not enough."""
        account = os.path.join(self.tmp, "acct-2")
        self.write_hooks(account)
        with mock.patch.object(codexmodel, "home", return_value=account), \
                mock.patch.dict(os.environ, {"ANSWER": '{"needs": true, "ask": "x"}'}):
            self.assertEqual(self.ask(now=1000.0)["needs"], True)
            os.remove(os.path.join(account, "hooks.json"))
            with self.assertRaises(codexmodel.Error):
                self.ask(now=1000.0)

    def test_model_from_hub_json(self):
        self.hub_json(host="codex", codex={"watch_model": "gpt-x", "watch_effort": "medium"})
        with mock.patch.dict(os.environ, {"ANSWER": '{"needs": false, "ask": ""}'}):
            self.ask()
        log = self.sent()
        self.assertEqual(next(m for m in log if m.get("method") == "thread/start")["params"]["model"], "gpt-x")
        self.assertEqual(next(m for m in log if m.get("method") == "turn/start")["params"]["effort"], "medium")

    def test_no_login_pauses_calls_for_ten_minutes(self):
        for mode in ("unauthorized", "nologin"):
            codexmodel._DOWN["until"] = 0.0
            with mock.patch.dict(os.environ, {"MODE": mode}):
                with self.assertRaises(codexmodel.Error):
                    self.ask(now=1000.0)
                spawned = len([x for x in self.sent() if "argv" in x])
                with self.assertRaises(codexmodel.Error):
                    self.ask(now=1000.0 + codexmodel.PAUSE - 1)   # without a new process
                self.assertEqual(len([x for x in self.sent() if "argv" in x]), spawned)
        with mock.patch.dict(os.environ, {"ANSWER": '{"needs": false, "ask": ""}'}):
            self.assertEqual(self.ask(now=1000.0 + codexmodel.PAUSE + 1)["needs"], False)

    def test_login_lifts_the_pause(self):
        """Pilot 2026-10-19: sign-in (`hub codex login` is another process) did not lift the watcher's pause — ~9 min
        without "waiting for you". A new sign-in file in the hub's Codex folder lifts the pause."""
        home = os.path.join(self.state, "codex-home")
        os.makedirs(home, exist_ok=True)
        with mock.patch.dict(os.environ, {"MODE": "unauthorized"}):
            with self.assertRaises(codexmodel.Error):
                self.ask(now=1000.0)
            with self.assertRaises(codexmodel.Error):
                self.ask(now=1001.0)   # paused: no process
        spawned = len([x for x in self.sent() if "argv" in x])
        with open(os.path.join(home, "auth.json"), "w") as f:
            f.write("{}")
        with mock.patch.dict(os.environ, {"ANSWER": '{"needs": true, "ask": "?"}'}):
            self.assertEqual(self.ask(now=1002.0)["needs"], True)
        self.assertEqual(len([x for x in self.sent() if "argv" in x]), spawned + 1)

    def test_limit_pause_holds_until_the_login_changes(self):
        # limit: the pause holds; a new sign-in (another account with headroom) lifts it
        home = os.path.join(self.state, "codex-home")
        os.makedirs(home, exist_ok=True)
        with mock.patch.dict(os.environ, {"MODE": "limit"}):
            with self.assertRaises(codexmodel.Error):
                self.ask(now=1000.0)
        spawned = len([x for x in self.sent() if "argv" in x])
        with self.assertRaises(codexmodel.Error):
            self.ask(now=1001.0)
        self.assertEqual(len([x for x in self.sent() if "argv" in x]), spawned)
        with open(os.path.join(home, "auth.json"), "w") as f:
            f.write("{}")
        with mock.patch.dict(os.environ, {"ANSWER": '{"needs": false, "ask": ""}'}):
            self.assertEqual(self.ask(now=1002.0)["needs"], False)

    def test_answer_mentioning_login_does_not_pause(self):
        # an answer not by the schema with the word "login" is not "no sign-in"
        with mock.patch.dict(os.environ, {"ANSWER": "open the login page and the rate limit"}):
            with self.assertRaises(codexmodel.Error):
                self.ask(now=1000.0)
        self.assertEqual(codexmodel._DOWN["until"], 0.0)

    def test_spend_goes_to_the_journal(self):
        """The call's tokens (the sum of last over the updates) are a codex.spent fact in the core's mailbox (spool); the caller is the entity."""
        import glob
        with mock.patch.dict(os.environ, {"ANSWER": '{"needs": false, "ask": ""}'}):
            self.ask(by="watch")
        facts = []
        for p in glob.glob(os.path.join(self.state, "spool", "*.json")):
            with open(p) as f:
                facts.append(json.load(f))
        (fact,) = [x for x in facts if x["type"] == "codex.spent"]
        self.assertEqual(fact["entity"], "codex:watch")
        self.assertEqual((fact["data"]["tokens_in"], fact["data"]["tokens_out"], fact["data"]["model"]),
                         (9600, 40, "gpt-6-luna"))
        self.assertNotIn("channel", fact["data"])

    def test_other_failure_does_not_pause(self):
        with mock.patch.dict(os.environ, {"ANSWER": "not json"}):
            with self.assertRaises(codexmodel.Error):
                self.ask(now=1000.0)
        self.assertEqual(codexmodel._DOWN["until"], 0.0)

    def test_tests_never_call_the_live_model(self):
        with mock.patch.dict(os.environ, {"HUB_NO_MODEL": "1"}):
            with self.assertRaises(codexmodel.Error):
                codexmodel.ask("x", "p", {"type": "object", "properties": {}})


class WatchOnCodexTest(unittest.TestCase):
    """The watcher and everything that calls its model (push, the voice summary) go through codexmodel on a Codex hub."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hubcxw-")
        self.env = mock.patch.dict(os.environ, {"HUB_CONFIG": os.path.join(self.tmp, "hub.json"),
                                                "HUB_STATE_DIR": self.tmp})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        platforms.current.rmtree(self.tmp)

    def hub_json(self, **cfg):
        with open(os.path.join(self.tmp, "hub.json"), "w") as f:
            json.dump(cfg, f)

    def test_watch_model_goes_to_codex_on_codex_hub(self):
        from modules.watch import watch
        self.hub_json(host="codex")
        with mock.patch.object(codexmodel, "ask", return_value={"needs": True, "ask": "x"}) as cx, \
                mock.patch("subprocess.run") as run:
            self.assertEqual(watch.needs_owner("result"), {"needs": True, "ask": "x"})
        run.assert_not_called()
        self.assertEqual(cx.call_args[0][1], watch.NEEDS_PROMPT)

    def test_claude_hub_keeps_claude(self):
        from modules.watch import watch
        self.hub_json()
        with mock.patch.object(codexmodel, "ask") as cx, mock.patch.dict(os.environ, {"HUB_NO_MODEL": "1"}):
            with self.assertRaises(ValueError):
                watch.needs_owner("result")
        cx.assert_not_called()

    def test_recap_on_codex_hub_is_not_a_model_call(self):
        from modules.watch import watch
        self.hub_json(host="codex")
        called = []
        w = watch.Watcher(self.tmp, recap=lambda text: called.append(text) or "line")
        w.state["sessions"]["maps"] = {"name": "maps", "state": "busy", "turn_seq": 5, "since": 1.0}
        w.steps["maps"] = [{"seq": 5, "type": "turn.started", "entity": "maps", "ts": 1.0, "data": {}},
                           {"seq": 6, "type": "turn.tool", "entity": "maps", "ts": 2.0,
                            "data": {"name": "Bash", "input": {"command": "ls"}}}]
        w.counted["maps"] = 50
        w.recaps(1000.0)
        self.assertEqual(called, [])


class CriticOnCodexTest(unittest.TestCase):
    def test_codex_hub_critic_uses_hub_codex_home_and_no_claude_fallback(self):
        from modules.critic import critic
        tmp = tempfile.mkdtemp(prefix="hubcxc-")
        self.addCleanup(platforms.current.rmtree, tmp)
        draft = os.path.join(tmp, "d.md")
        with open(draft, "w") as f:
            f.write("draft")
        cfg = {"host": "codex", "critic": {"enabled": True}}
        runs = []

        def run(cmd, cwd, stdin=None, env=None):
            runs.append((cmd, env))
            raise critic.Error("usage limit reached")
        with mock.patch.dict(os.environ, {"HUB_STATE_DIR": tmp}), mock.patch("shutil.which", return_value="/x/codex"):
            with self.assertRaises(critic.Error) as e:
                critic.review(cfg, draft, run=run)
        self.assertEqual(len(runs), 1)   # no claude fallback
        self.assertEqual(runs[0][0][0], "/x/codex")
        self.assertEqual(runs[0][1]["CODEX_HOME"], os.path.join(tmp, "codex-home"))
        self.assertIn("Codex refused", str(e.exception))

    def critic_model(self, listed, critic_cfg=None):
        """The `-m` and the effort of the critic's codex command on a Codex hub whose sign-in lists `listed` models
        ((id, default, efforts) — the file core/codexcli.fetch_models writes); None — the list was not read."""
        from modules.critic import critic
        tmp = tempfile.mkdtemp(prefix="hubcxc-")
        self.addCleanup(platforms.current.rmtree, tmp)
        draft = os.path.join(tmp, "d.md")
        with open(draft, "w") as f:
            f.write("draft")
        if listed is not None:
            with open(os.path.join(tmp, "codex-models.json"), "w") as f:
                json.dump({"ts": time.time(), "models": [{"id": i, "title": i, "efforts": e, "default": d, "effort": None}
                                                         for i, d, e in listed]}, f)
        runs = []

        def run(cmd, cwd, stdin=None, env=None):
            runs.append(cmd)
            return "ok"
        cfg = {"host": "codex", "critic": {"enabled": True, **(critic_cfg or {})}}
        with mock.patch.dict(os.environ, {"HUB_STATE_DIR": tmp}), mock.patch("shutil.which", return_value="/x/codex"):
            who, _ = critic.review(cfg, draft, run=run)
        cmd = runs[0]
        return cmd[cmd.index("-m") + 1], cmd[cmd.index("-c") + 1], who

    def test_codex_hub_critic_asks_a_model_the_sign_in_has(self):
        # a plan without the default model: the critic must not refuse on every call, it takes the sign-in's own default
        both = ["low", "medium", "high"]
        self.assertEqual(self.critic_model([("gpt-5-mini", True, both), ("gpt-5", False, both)])[0], "gpt-5-mini")
        self.assertEqual(self.critic_model([("gpt-5-mini", False, both), ("gpt-5", False, both)])[0], "gpt-5-mini")   # no default marked: the first
        # the default critic model is on the plan: it is the one asked
        self.assertEqual(self.critic_model([("gpt-5-mini", True, both), ("gpt-6-astra", False, both)])[0], "gpt-6-astra")
        # the list not read yet: as before
        self.assertEqual(self.critic_model(None)[0], "gpt-6-astra")
        # the owner's own choice stays, listed or not
        self.assertEqual(self.critic_model([("gpt-5-mini", True, both)], {"model": "gpt-7"})[0], "gpt-7")

    def test_codex_hub_critic_effort_is_one_the_model_knows(self):
        low = ["low", "medium"]
        got = self.critic_model([("gpt-5-mini", True, low)])
        self.assertEqual((got[0], got[1]), ("gpt-5-mini", "model_reasoning_effort=medium"))   # high asked, medium is its top
        self.assertIn("gpt-5-mini · medium", got[2])
        self.assertEqual(self.critic_model([("gpt-5-mini", True, low)], {"effort": "low"})[1], "model_reasoning_effort=low")
        self.assertEqual(self.critic_model([("gpt-5-mini", True, ["high"])], {"effort": "low"})[1], "model_reasoning_effort=high")   # none that low
        self.assertEqual(self.critic_model([("gpt-5-mini", True, [])])[1], "model_reasoning_effort=high")   # none listed: as asked

    def test_claude_hub_critic_unchanged(self):
        from modules.critic import critic
        tmp = tempfile.mkdtemp(prefix="hubcxc-")
        self.addCleanup(platforms.current.rmtree, tmp)
        draft = os.path.join(tmp, "d.md")
        with open(draft, "w") as f:
            f.write("draft")
        runs = []

        def run(cmd, cwd, stdin=None, env=None):
            runs.append(cmd)
            if len(runs) == 1:
                raise critic.Error("usage limit reached")
            return "ok"
        with mock.patch("shutil.which", return_value="/x/codex"):
            who, out = critic.review({"critic": {"enabled": True}}, draft, run=run)
        self.assertTrue(who.startswith("Claude"))


class CodexOnlyRefusalsTest(unittest.TestCase):
    """bin/hub on a Codex hub: evals are claude-only, other session modes are refused, hub take is codex resume."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hubcxr-")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        with open(os.path.join(self.tmp, "hub.json"), "w") as f:
            json.dump({"host": "codex"}, f)
        self.env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CONFIG": os.path.join(self.tmp, "hub.json")}
        self.env.pop("HUB_SESSION", None)
        self.root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def tearDown(self):
        platforms.current.rmtree(self.tmp)

    def hub(self, *args):
        return subprocess.run([sys.executable, os.path.join(self.root, "bin", "hub"), *args], capture_output=True,
                              text=True, timeout=60, env=self.env)

    def test_mcp_login_is_not_refused_as_claude_only(self):
        # slice B: the core's way, as the page's "Sign in" (tests/test_codex_mcp.py HubMcpLoginOnCodex)
        out = self.hub("mcp", "login", "maps", "srv")
        self.assertNotEqual(out.returncode, 0)
        self.assertNotIn("only with claude", out.stderr)
        self.assertIn("no session maps", out.stderr)

    def test_session_mode_codex_cannot_hold_refused(self):
        # slice H: the modes Codex holds (hosts/codex_rules.MODES) start; another one is refused before the spool
        out = self.hub("session", "new", "maps", "--mode", "auto")
        self.assertNotEqual(out.returncode, 0)
        # the owner's terminal reads the core's phrase in the hub's language (the tests' one is English)
        from core import i18n
        self.assertIn(i18n.t("core.codex_rules.mode_unknown", mode="auto"), out.stderr)
        ru = dict(self.env, HUB_LANG="ru")   # a Russian hub's terminal reads it in Russian
        got = subprocess.run([sys.executable, os.path.join(self.root, "bin", "hub"), "session", "new", "maps", "--mode", "auto"],
                             capture_output=True, text=True, timeout=60, env=ru)
        self.assertIn(i18n.tr("ru", "core.codex_rules.mode_unknown", mode="auto"), got.stderr)
        self.assertNotEqual(i18n.tr("ru", "core.codex_rules.mode_unknown", mode="auto"),
                            i18n.t("core.codex_rules.mode_unknown", mode="auto"))
        self.assertFalse(os.path.exists(os.path.join(self.state, "spool")) and os.listdir(os.path.join(self.state, "spool")))
        self.env["HUB_CLI_WAIT"] = "0"   # no core here: the fact stays in the spool
        out = self.hub("session", "new", "maps", "--mode", "plan")
        self.assertEqual(out.returncode, 0, out.stderr)
        (name,) = os.listdir(os.path.join(self.state, "spool"))
        with open(os.path.join(self.state, "spool", name)) as f:
            self.assertEqual(json.load(f)["data"]["mode"], "plan")

    def test_session_evals_on_the_journal_need_their_own_sign_in(self):
        # evals/run.py on Codex runs on the evals' own sign-in (tests/test_evals_codex.py), never on the hub's: without
        # it — a refusal with what to do, before any run
        case = os.path.join(self.tmp, "x.json")
        with open(case, "w") as f:
            json.dump({"id": "x", "real": ["w"], "events": [], "inject": [], "checks": []}, f)
        out = subprocess.run([sys.executable, os.path.join(self.root, "evals", "run.py"), case, "--codex-model", "m"],
                             capture_output=True, text=True, timeout=60, env=self.env)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("evals/run.py --codex-login", out.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state, "codex-home")))   # the hub's sign-in: not even looked at

    # evals/prompts.py on Codex (its sign-in, folder and settings) — tests/test_codex_prompt_evals.py

    def test_take_command_is_codex_resume_in_hub_codex_home(self):
        sys.path.insert(0, os.path.join(self.root, "bin"))
        from core import codexmodel as cm
        os.makedirs(os.path.join(self.state, "sessions", "maps"))
        with open(os.path.join(self.state, "sessions", "maps", "codex-threads.json"), "w") as f:
            json.dump({"sid-1": "thread-9"}, f)
        cmd, env = cm.take_command(self.state, "maps", "sid-1", base={"PATH": "/bin", "ANTHROPIC_API_KEY": "x"},
                                   codex="/x/codex")
        self.assertEqual(cmd, ["/x/codex", "resume", "--dangerously-bypass-approvals-and-sandbox", "thread-9"])
        self.assertEqual(env["CODEX_HOME"], os.path.join(self.state, "codex-home"))
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertIsNone(cm.take_command(self.state, "maps", "sid-2", base={}, codex="/x/codex"))


class PlanRecapTest(unittest.TestCase):
    """turn/plan/updated of a session's turn -> the "working on" line (session.recap by plan), like TodoWrite on Claude."""

    def test_in_progress_step_becomes_recap_once(self):
        from hosts import codex_rules

        class P:
            pass
        proc = P()
        plan = {"threadId": "t", "turnId": "u", "plan": [{"step": "Reading the spec", "status": "completed"},
                                                          {"step": "Writing a test", "status": "inProgress"}]}
        self.assertEqual(codex_rules.notice(proc, "maps", "turn/plan/updated", plan),
                         [("session.recap", "maps", {"text": "Writing a test", "by": "plan"})])
        self.assertEqual(codex_rules.notice(proc, "maps", "turn/plan/updated", plan), [])   # the same step — stay silent
        done = {**plan, "plan": [{"step": "Writing a test", "status": "completed"}]}
        self.assertEqual(codex_rules.notice(proc, "maps", "turn/plan/updated", done), [])
        self.assertEqual(codex_rules.notice(proc, "maps", "turn/plan/updated", {**plan, "turnId": "u2"}),
                         [("session.recap", "maps", {"text": "Writing a test", "by": "plan"})])   # a new turn — again
        self.assertEqual(codex_rules.notice(proc, "maps", "something/else", {}), [])


if __name__ == "__main__":
    unittest.main()
