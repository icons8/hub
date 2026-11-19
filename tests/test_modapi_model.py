"""A module calls a model through the core (Module API 2.7, the model capability; owner decision: let hub modules call
a model through the core, without access to account tokens): hubmod.model asks the page, which identifies the module
by its launch secret, checks the capability, the owner's permission (modules.model) and the limits — then runs
claude -p itself, on a pool account; the module receives only the answer. Spend is the module.model event in the
journal, and a mod:<name> line in "Usage"."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from core import hubmod, journal, modelcall, modrun, modules, usage, version
from modules.web import modmodel
from tests.fakes import exe
from tests.fakes import cut

OK = {"type": "result", "is_error": False, "result": "one line", "total_cost_usd": 0.0021,
      "modelUsage": {"claude-haiku-x": {}}}


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


class Root(unittest.TestCase):
    """A state directory, hub.json and a digest module on disk (modules.paths) with the model capability."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="modmodel-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.state = os.path.join(self.dir, "state")
        self.mod = os.path.join(self.dir, "mods", "digest")
        self.config = os.path.join(self.dir, "hub.json")
        os.makedirs(self.state)
        self.manifest(["model"])
        self.settings(allowed=["digest"])
        patch = mock.patch.dict(os.environ, {"HUB_CONFIG": self.config})
        patch.start()
        self.addCleanup(patch.stop)

    def manifest(self, caps, api="^2.7", name="digest"):
        write(os.path.join(os.path.dirname(self.mod), name, "module.json"),
              {"name": name, "version": "1.0.0", "api": api, "run": [sys.executable, "main.py"], "capabilities": caps})

    def settings(self, allowed):
        write(self.config, {"modules": {"paths": [os.path.dirname(self.mod)], "enabled": ["digest"], "model": allowed}})

    def cfg(self):
        from core import catalog
        return catalog.load_config(self.state)

    def running(self, name="digest", secret="s" * 64):
        write(os.path.join(modrun.run_dir(self.state), f"{name}.run.json"), {"pid": 1, "port": 1, "secret": secret})
        return secret


class Manifest(Root):
    def test_the_right_is_of_api_2_7(self):
        self.assertIn("model", modules.CAPABILITIES)
        self.assertEqual(version.manifest_since("model"), "2.7")
        self.assertEqual(modules.read(self.mod, "path")["errors"], [])
        self.manifest(["model"], api="^2.6")
        self.assertTrue(modules.read(self.mod, "path")["errors"])

    def test_the_consent_list_has_its_shape(self):
        self.assertEqual(modules.settings({"modules": {"model": ["a"]}})["model"], ["a"])
        self.assertEqual(modules.settings({"modules": {"model": "a"}})["model"], [])   # a typo gives empty, not a crash


class Checked(unittest.TestCase):
    def bad(self, **body):
        with self.assertRaises(modelcall.Error) as e:
            modelcall.checked({"prompt": "p", **body})
        self.assertEqual(e.exception.code, "bad")

    def test_defaults(self):
        self.assertEqual(modelcall.checked({"prompt": "p"}), ("p", "", "haiku", None, 1024, 60))

    def test_what_is_refused(self):
        self.bad(prompt=" ")
        self.bad(model="opus")
        self.bad(model="claude-opus-5-5")
        self.bad(text=3)
        self.bad(schema=[1])
        self.bad(max_tokens=0)
        self.bad(max_tokens=4097)
        self.bad(max_tokens=True)
        self.bad(timeout=121)
        self.bad(text="x" * modelcall.MAX_INPUT)                                  # together with the prompt, over the limit
        self.bad(schema={"description": "x" * modelcall.MAX_INPUT})              # the schema counts toward input
        with self.assertRaises(modelcall.Error):
            modelcall.checked([])

    def test_the_command_line_stays_within_what_the_os_gives(self):
        # the prompt and schema go to claude as arguments: Windows allows 32,767 characters for the whole line, Linux 128 KB per argument
        self.bad(schema={"description": "é" * (modelcall.MAX_SCHEMA // 6 + 1)})   # json.dumps gives \uXXXX: six times longer
        self.bad(schema={"description": '"' * (modelcall.MAX_SCHEMA // 3)})      # Windows doubles the quotes
        got = modelcall.checked({"prompt": "é\n" * 40_000, "text": "é" * 10_000})   # the prompt goes by file, the input by stdin
        self.assertEqual(len(got[0]), 80_000)
        self.bad(prompt="a\0b")
        self.bad(prompt="a\ud800b")


class Limits(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="modmodel-lim-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.now = [1000.0]

    def limits(self):
        return modelcall.Limits(self.dir, clock=lambda: self.now[0])

    def code(self, lim, name):
        with self.assertRaises(modelcall.Error) as e:
            lim.take(name)
        return e.exception.code

    def test_two_at_once_per_module_four_in_all(self):
        lim = self.limits()
        lim.take("a")
        lim.take("a")
        self.assertEqual(self.code(lim, "a"), "busy")
        lim.take("b")
        lim.take("b")
        self.assertEqual(self.code(lim, "c"), "busy")
        lim.give("a")
        lim.take("c")

    def test_sixty_an_hour_and_a_restart_does_not_reset_it(self):
        lim = self.limits()
        for _ in range(modelcall.PER_HOUR):
            lim.take("a")
            lim.give("a")
        self.assertEqual(self.code(lim, "a"), "limit")
        self.assertEqual(self.code(self.limits(), "a"), "limit")   # a new page process — the same count
        lim.take("b")                                              # another module has its own
        self.now[0] += modelcall.HOUR + 1
        self.limits().take("a")


class Call(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ, {"HUB_NO_MODEL": ""})
        patch.start()
        self.addCleanup(patch.stop)
        self.calls, self.prompts = [], []

    def run_fake(self, out=OK, raise_=None):
        def run(cmd, **kw):
            self.calls.append((cmd, kw))
            with open(cmd[cmd.index("--system-prompt-file") + 1], encoding="utf-8") as f:
                self.prompts.append(f.read())
            if raise_:
                raise raise_
            return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")
        return run

    def test_the_command_and_its_environment(self):
        with mock.patch.dict(os.environ, {"HUB_SESSION": "maps", "HUB_STATE_DIR": "/x"}):
            got = modelcall.call("p", "input", "haiku", None, 512, 30, run=self.run_fake())
        self.assertEqual(got, {"text": "one line", "usd": 0.0021, "model": "claude-haiku-x"})
        cmd, kw = self.calls[0]
        self.assertEqual(cmd[cmd.index("--model") + 1], "haiku")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")                     # no tools
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "")
        for flag in ("--strict-mcp-config", "--no-session-persistence"):
            self.assertIn(flag, cmd)
        self.assertEqual(self.prompts, ["p"])                                      # the prompt goes by file, not as an argument
        self.assertFalse(os.path.exists(cmd[cmd.index("--system-prompt-file") + 1]))   # the file is removed after the call
        self.assertNotIn("--json-schema", cmd)
        self.assertEqual((kw["input"], kw["timeout"]), ("input", 30))
        self.assertEqual(kw["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], "512")
        self.assertFalse([k for k in kw["env"] if k.startswith("HUB_")])       # the call needs no hub variables

    @cut.needs("profile")
    def test_the_account_of_the_pool(self):
        from modules.accounts import accounts
        pool = {"on": True, "accounts": [{"name": "work", "token": "tok-w"}]}
        with mock.patch.object(accounts, "load", return_value=pool), \
                mock.patch.object(accounts, "enabled", return_value=True):
            modelcall.call("p", "", "haiku", None, 10, 10, account="work", run=self.run_fake())
        self.assertEqual(self.calls[0][1]["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "tok-w")

    def test_a_schema_gives_json(self):
        out = {**OK, "structured_output": {"line": "x"}}
        got = modelcall.call("p", "", "sonnet", {"type": "object"}, 10, 10, run=self.run_fake(out))
        self.assertEqual(got["json"], {"line": "x"})
        self.assertIn("--json-schema", self.calls[0][0])
        self.assertNotIn("--effort", self.calls[0][0])                          # low is only for haiku

    def test_failures(self):
        def code(**kw):
            with self.assertRaises(modelcall.Error) as e:
                modelcall.call("p", "", "haiku", kw.pop("schema", None), 10, 10, run=self.run_fake(**kw))
            return e.exception.code, e.exception.usd
        self.assertEqual(code(out={"is_error": True, "result": "limit", "total_cost_usd": 0.01}), ("failed", 0.01))
        self.assertEqual(code(out={"is_error": True, "result": "x"}), ("failed", None))
        self.assertEqual(code(out={**OK}, schema={"type": "object"}), ("failed", 0.0021))   # the answer does not match the schema
        self.assertEqual(code(raise_=subprocess.TimeoutExpired("claude", 10)), ("timeout", None))
        self.assertEqual(code(raise_=OSError("no such thing")), ("failed", None))
        self.assertEqual(code(raise_=ValueError("embedded null byte")), ("failed", None))

    def test_which_model_answered(self):
        out = {**OK, "modelUsage": {"claude-haiku-x": {"costUSD": 0.0001}, "claude-sonnet-y": {"costUSD": 0.01}}}
        self.assertEqual(modelcall.call("p", "", "sonnet", None, 10, 10, run=self.run_fake(out))["model"],
                         "claude-sonnet-y")   # the auxiliary haiku alongside is not the one that was called
        self.assertEqual(modelcall.call("p", "", "haiku", None, 10, 10, run=self.run_fake({**OK, "modelUsage": {}}))
                         ["model"], "haiku")

    def test_no_live_model_in_tests(self):
        with mock.patch.dict(os.environ, {"HUB_NO_MODEL": "1"}):
            with self.assertRaises(modelcall.Error) as e:
                modelcall.call("p", "", "haiku", None, 10, 10, run=self.run_fake())
        self.assertEqual(e.exception.code, "unavailable")
        self.assertEqual(self.calls, [])


class Answer(Root):
    """The page handler without HTTP: who calls, the capability, the permission, the limits, the spend event."""

    def setUp(self):
        super().setUp()
        self.secret, self.facts, self.got = self.running(), [], []
        self.limits = modelcall.Limits(self.state)

    def call(self, *args, account=None):
        self.got.append((args, account))
        return {"text": "ok", "usd": 0.003, "model": "claude-haiku-x"}

    def ask(self, body=None, headers=None, peer="127.0.0.1", call=None):
        h = {"X-Hub-Module": "digest", "X-Hub-Module-Secret": self.secret, **(headers or {})}
        raw = json.dumps(body if body is not None else {"prompt": "p", "text": "t"}).encode()
        return modmodel.answer(self.state, self.cfg(), "work", h, peer, raw, self.limits,
                               lambda t, e, d: self.facts.append((t, e, d)), call=call or self.call)

    def test_the_call_and_its_spend(self):
        self.assertEqual(self.ask(), (200, {"text": "ok", "usd": 0.003, "model": "claude-haiku-x"}))
        self.assertEqual(self.got, [(("p", "t", "haiku", None, 1024, 60), "work")])   # the account comes from the core snapshot
        (t, e, d), = self.facts
        self.assertEqual((t, e), ("module.model", "digest"))
        self.assertEqual({k: d[k] for k in ("module", "account", "ok", "cost_usd", "model")},
                         {"module": "digest", "account": "work", "ok": True, "cost_usd": 0.003,
                          "model": "claude-haiku-x"})
        self.assertNotIn("p", json.dumps(d))                                    # the prompt text is not in the journal

    def code(self, **kw):
        status, out = self.ask(**kw)
        self.assertEqual(status, modelcall.STATUS[out["code"]])
        return out["code"]

    def test_who_may_call(self):
        self.assertEqual(self.code(headers={"X-Hub-Module-Secret": "x" * 64}), "denied")   # the wrong secret
        self.assertEqual(self.code(headers={"X-Hub-Module-Secret": ""}), "denied")
        self.assertEqual(self.code(peer="192.0.2.7"), "denied")                         # not from this machine
        self.assertEqual(self.code(headers={"X-Hub-Module": "other"}), "denied")        # not started by the hub
        self.manifest(["net"])
        self.assertEqual(self.code(), "denied")                                         # no capability
        self.assertEqual((self.got, self.facts), ([], []))

    def test_off_until_the_owner_allows(self):
        self.settings(allowed=[])
        self.assertEqual(self.code(), "off")
        self.settings(allowed=["digest"])
        self.assertEqual(self.ask()[0], 200)

    def test_a_body_nested_too_deep_is_bad(self):
        status, out = modmodel.answer(self.state, self.cfg(), None, {"X-Hub-Module": "digest",
                                      "X-Hub-Module-Secret": self.secret}, "127.0.0.1", b"[" * 200_000 + b"]" * 200_000,
                                      self.limits, lambda *a: self.facts.append(a), call=self.call)
        self.assertEqual((status, out["code"]), (400, "bad"))

    def test_bad_requests_cost_nothing(self):
        self.assertEqual(self.code(body={"prompt": "p", "model": "opus"}), "bad")
        status, out = modmodel.answer(self.state, self.cfg(), None, {"X-Hub-Module": "digest",
                                      "X-Hub-Module-Secret": self.secret}, "127.0.0.1", b"{", self.limits,
                                      lambda *a: self.facts.append(a), call=self.call)
        self.assertEqual(out["code"], "bad")
        self.assertEqual((self.got, self.facts), ([], []))

    def test_a_failed_call_is_written_without_a_price(self):
        def fail(*args, account=None):
            raise modelcall.Error("timeout", "no answer")
        self.assertEqual(self.code(call=fail), "timeout")
        (_, _, d), = self.facts
        self.assertEqual((d["ok"], d["code"], d["cost_usd"]), (False, "timeout", None))
        self.assertEqual(self.limits.live, {})                                  # the slot is given back

    def test_claude_that_did_not_start_spent_nothing(self):
        def missing(*args, account=None):
            raise modelcall.Error("failed", "claude did not start", ran=False)
        self.assertEqual(self.code(call=missing), "failed")
        self.assertEqual(self.facts, [])                                        # not "unpriced": there was no spend

    def test_no_model_in_tests_writes_no_spend(self):
        self.assertEqual(self.code(call=modelcall.call), "unavailable")         # HUB_NO_MODEL comes from tests/__init__
        self.assertEqual(self.facts, [])


class OnCodexHub(Root):
    """Codex (experimental): on a hub with "host": "codex" a module's call goes to the hub's Codex
    (core/codexmodel: its sign-in, its CODEX_HOME), never to claude; on a Claude hub the path is claude, as before."""

    def setUp(self):
        super().setUp()
        from tests.test_codex_model import SERVER, write_core_hooks
        write_core_hooks(os.path.join(self.state, "codex-home"))
        self.secret, self.facts = self.running(), []
        self.limits = modelcall.Limits(self.state)
        codex, self.log = os.path.join(self.dir, "codex"), os.path.join(self.dir, "codex.log")
        codex = exe.script(codex, SERVER)   # Windows: codex.cmd beside codex.py — a shebang file does not start there
        env = mock.patch.dict(os.environ, {"HUB_CODEX_BIN": codex, "LOG": self.log, "HUB_NO_MODEL": "",
                                           "HUB_STATE_DIR": self.state, "ANSWER": '{"line": "x"}'})
        env.start()
        self.addCleanup(env.stop)
        from core import codexmodel
        codexmodel._DOWN["until"] = 0.0
        self.addCleanup(codexmodel._DOWN.update, until=0.0)

    def hub(self, **extra):
        write(self.config, {"modules": {"paths": [os.path.dirname(self.mod)], "enabled": ["digest"], "model": ["digest"]},
                            **extra})

    def ask(self, **body):
        h = {"X-Hub-Module": "digest", "X-Hub-Module-Secret": self.secret}
        return modmodel.answer(self.state, self.cfg(), None, h, "127.0.0.1", json.dumps({"prompt": "p", **body}).encode(),
                               self.limits, lambda t, e, d: self.facts.append((t, e, d)))

    def sent(self):
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(x) for x in f]

    def spent(self):
        import glob
        out = []
        for p in glob.glob(os.path.join(self.state, "spool", "*.json")):
            with open(p, encoding="utf-8") as f:
                out.append(json.load(f))
        return [x for x in out if x["type"] == "codex.spent"]

    def test_a_codex_hub_answers_through_codex(self):
        self.hub(host="codex")
        schema = {"type": "object", "properties": {"line": {"type": "string"}}}
        with mock.patch.object(subprocess, "run") as run:
            status, out = self.ask(text="input", schema=schema)
        run.assert_not_called()                                                 # no claude
        self.assertEqual((status, out), (200, {"json": {"line": "x"}, "usd": None, "model": "gpt-6-luna"}))
        log = self.sent()
        self.assertEqual(log[0]["env"]["CODEX_HOME"], os.path.join(self.state, "codex-home"))
        start = next(m for m in log if m.get("method") == "thread/start")["params"]
        self.assertEqual((start["baseInstructions"], start["model"], start["ephemeral"]), ("p", "gpt-6-luna", True))
        turn = next(m for m in log if m.get("method") == "turn/start")["params"]
        self.assertEqual((turn["input"][0]["text"], turn["effort"]), ("input", "low"))
        (t, e, d), = self.facts
        self.assertEqual((t, e, d["ok"], d["model"], d["cost_usd"]), ("module.model", "digest", True, "gpt-6-luna", None))
        (fact,) = self.spent()                                                  # the tokens — for "Usage"
        self.assertEqual((fact["entity"], fact["data"]["tokens_in"]), ("codex:mod:digest", 9600))

    def test_text_without_a_schema_and_sonnet_is_the_sessions_model(self):
        self.hub(host="codex", codex={"model": "gpt-x"})
        with mock.patch.dict(os.environ, {"ANSWER": "one line"}):
            status, out = self.ask(model="sonnet")
        self.assertEqual((status, out), (200, {"text": "one line", "usd": None, "model": "gpt-x"}))
        log = self.sent()
        self.assertEqual(next(m for m in log if m.get("method") == "thread/start")["params"]["model"], "gpt-x")
        turn = next(m for m in log if m.get("method") == "turn/start")["params"]
        self.assertNotIn("outputSchema", turn)
        self.assertEqual(turn["effort"], "medium")

    def test_no_codex_sign_in_is_unavailable_and_spends_nothing(self):
        self.hub(host="codex")
        with mock.patch.dict(os.environ, {"MODE": "nologin"}):
            self.assertEqual(self.ask()[1]["code"], "failed")
            self.assertEqual(self.ask()[1]["code"], "unavailable")              # the pause: no process, no spend
        self.assertEqual([d["code"] for _, _, d in self.facts], ["failed"])
        with mock.patch.dict(os.environ, {"HUB_NO_MODEL": "1"}):
            from core import codexmodel
            codexmodel._DOWN["until"] = 0.0
            self.assertEqual(self.ask()[1]["code"], "unavailable")
        self.assertEqual(len(self.facts), 1)

    def test_a_codex_that_exits_at_once_is_a_failed_call(self):
        # an old codex without app-server, a broken folder: its stdin is closed before the first request (a broken pipe)
        # — a refusal with a code, not an exception out of the page's handler
        self.hub(host="codex")

        class Gone:
            def __init__(self, *a, **kw):
                self.stdout, self.stdin = iter(()), self

            def write(self, _):
                raise BrokenPipeError(32, "Broken pipe")

            def close(self):
                pass

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0
        with mock.patch.object(subprocess, "Popen", Gone):
            self.assertEqual(self.ask()[1]["code"], "failed")

    def test_a_claude_hub_keeps_claude(self):
        self.hub()
        runs = []

        def run(cmd, **kw):
            runs.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, json.dumps(OK), "")
        with mock.patch.object(subprocess, "run", run):
            status, out = self.ask()
        self.assertEqual((status, out["model"]), (200, "claude-haiku-x"))
        self.assertEqual(runs[0][1:3], ["-p", "--model"])
        self.assertFalse(os.path.exists(self.log))                              # codex was not started


class Page(Root):
    """hubmod.model → a real page (/api/module-model) → a faked claude: the whole path except the model."""

    def setUp(self):
        super().setUp()
        from modules.web.server import Web
        self.web = Web(self.state)
        self.server = self.web.serve(port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.web.stop.set)
        secret = self.running()
        env = mock.patch.dict(os.environ, {"HUB_STATE_DIR": self.state, "HUB_MODULE": "digest",
                                           "HUB_MODULE_SECRET": secret, "HUB_NO_MODEL": "",
                                           "HUB_WEB_PORT": str(self.server.server_address[1])})
        env.start()
        self.addCleanup(env.stop)
        here = os.getcwd()
        os.chdir(self.mod)   # hubmod reads its capability from module.json in the process directory
        self.addCleanup(os.chdir, here)
        self.runs = []

        def run(cmd, **kw):
            self.runs.append(kw)
            return subprocess.CompletedProcess(cmd, 0, json.dumps({**OK, "structured_output": {"n": 2}}), "")
        patch = mock.patch.object(subprocess, "run", run)
        patch.start()
        self.addCleanup(patch.stop)

    def spooled(self):
        from core import spool
        return [e for _, e in spool.take(os.path.join(self.state, "spool"))]

    def test_the_module_gets_only_the_answer(self):
        got = hubmod.model("count", text="a b", schema={"type": "object"}, max_tokens=64, timeout=20)
        self.assertEqual(got, {"json": {"n": 2}, "usd": 0.0021, "model": "claude-haiku-x"})
        self.assertEqual(self.runs[0]["input"], "a b")
        self.assertEqual(self.runs[0]["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], "64")
        (fact,) = [e for e in self.spooled() if e["type"] == "module.model"]
        self.assertEqual((fact["entity"], fact["src"], fact["data"]["cost_usd"]), ("digest", "web", 0.0021))
        # the core accepts the fact (it does not count a rejected usage): this is the only way spend reaches "Usage"
        from core import reducer
        st, _, why = reducer.reduce(reducer.empty(), {**fact, "seq": 1, "ts": 1.0})
        self.assertIsNone(why)
        self.assertNotIn("modules", st)   # the fact is for "Usage" and leaves module state alone
        # the same type from a module (hubmod.fact, hubmod.owner) or a session is not spend: the core rejects it
        for src, ch in (("module", "plugin"), ("module", "mod:digest"), ("session", "session"), ("web", "plugin")):
            _, _, why = reducer.reduce(reducer.empty(), {**fact, "seq": 2, "ts": 2.0, "src": src,
                                                         "data": {**fact["data"], "channel": ch}})
            self.assertIn("page", str(why))

    def test_refusals_reach_the_module_as_codes(self):
        self.settings(allowed=[])
        with self.assertRaises(hubmod.ModelError) as e:
            hubmod.model("p")
        self.assertEqual(e.exception.code, "off")
        self.settings(allowed=["digest"])
        with self.assertRaises(hubmod.ModelError) as e:
            hubmod.model("p", model="opus")
        self.assertEqual(e.exception.code, "bad")
        self.assertEqual(self.runs, [])

    def test_without_the_right_the_page_is_not_asked(self):
        self.manifest(["net"])
        with mock.patch.dict(os.environ, {"HUB_WEB_PORT": "1"}):   # if it asked, the result would be unavailable
            with self.assertRaises(hubmod.ModelError) as e:
                hubmod.model("p")
        self.assertEqual(e.exception.code, "denied")

    def test_no_live_model_in_the_module_either(self):
        with mock.patch.dict(os.environ, {"HUB_NO_MODEL": "1", "HUB_WEB_PORT": "1"}):
            with self.assertRaises(hubmod.ModelError) as e:
                hubmod.model("p")
        self.assertEqual(e.exception.code, "unavailable")

    def test_no_page_is_unavailable(self):
        with mock.patch.dict(os.environ, {"HUB_WEB_PORT": "1"}):
            with self.assertRaises(hubmod.ModelError) as e:
                hubmod.model("p")
        self.assertEqual(e.exception.code, "unavailable")

    def test_the_owner_allows_on_the_modules_tab(self):
        from tests.test_web import WebTest
        port = self.server.server_address[1]
        req = WebTest.req.__get__(type("H", (), {"port": port})())
        self.settings(allowed=[])
        body = json.loads(req("GET", "/api/modules")[1])
        digest = next(m for m in body["installed"] if m["name"] == "digest")
        self.assertEqual((digest["model"], digest["model_allowed"]), (True, False))
        self.assertEqual(req("POST", "/api/modules/model", {"name": "digest", "allowed": True})[0], 200)
        with open(self.config) as f:
            self.assertEqual(json.load(f)["modules"]["model"], ["digest"])
        self.assertEqual(req("POST", "/api/modules/model", {"name": "nope", "allowed": True})[0], 404)
        self.assertEqual(req("POST", "/api/modules/model", {"name": "digest", "allowed": False})[0], 200)
        with open(self.config) as f:
            self.assertEqual(json.load(f)["modules"]["model"], [])
        mtime = os.stat(self.config).st_mtime_ns
        self.assertEqual(req("POST", "/api/modules/model", {"name": "nope", "allowed": False})[0], 200)
        self.assertEqual(os.stat(self.config).st_mtime_ns, mtime)              # nothing to forbid — the file is untouched
        self.assertEqual(req("POST", "/api/modules/model", {"name": "", "allowed": False})[0], 400)

    def test_a_module_named_model_keeps_its_routes(self):
        from core import modclaims
        self.assertTrue(modclaims.core_path("/api/module-model"))
        self.assertTrue(modclaims.core_path("/api/modules/model"))


class Spend(unittest.TestCase):
    """"Usage": a module's model call is its own line mod:<name>; without a price it is counted, not zeroed; a module's forgery is not counted."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="modmodel-usage-")
        self.addCleanup(shutil.rmtree, self.state, True)
        usage.reset()
        self.addCleanup(usage.reset)
        self.seq = 0

    def ev(self, type, entity, data, ts):
        self.seq += 1
        with open(os.path.join(self.state, "journal.jsonl"), "a") as f:
            f.write(journal._line({"seq": self.seq, "ts": ts, "src": "web", "type": type, "entity": entity,
                                   "data": data}))

    def test_module_calls_are_their_own_line(self):
        import time
        now = time.time()
        web = {"channel": "web", "owner": True}
        self.ev("turn.result", "maps", {"cost_usd": 1.0, "text": "turn"}, now - 30)
        self.ev("module.model", "digest", {**web, "module": "digest", "cost_usd": 0.25, "ok": True, "ms": 900,
                                                "model": "claude-haiku-x"}, now - 20)
        self.ev("module.model", "digest", {**web, "module": "digest", "cost_usd": 0.25, "ok": True}, now - 15)
        self.ev("module.model", "digest", {**web, "module": "digest", "cost_usd": None, "ok": False,
                                                "code": "timeout"}, now - 10)
        # the same type from the module itself (hubmod.fact) — the mailbox sets channel plugin: not spend
        self.ev("module.model", "digest", {"channel": "plugin", "owner": False, "sender": "mod:digest",
                                                "cost_usd": 99.0}, now - 5)
        self.ev("module.model", "maps", {"channel": "plugin", "owner": False, "sender": "mod:digest",
                                              "cost_usd": 50.0}, now - 4)
        r = usage.report(self.state, now=now)
        self.assertEqual(r["days"][-1]["by"], {"maps": 1.0, "mod:digest": 0.5})
        self.assertEqual(r["unpriced"], {"mod:digest": 1})
        self.assertEqual([t["session"] for t in r["top"]], ["maps"])   # "the most expensive turns" are session turns

    @cut.needs("stats")
    def test_the_hubs_report_keeps_to_sessions(self):
        import time
        from modules.stats import stats
        now = time.time()
        self.ev("turn.result", "maps", {"cost_usd": 1.0, "text": "turn", "turns": 1}, now - 30)
        self.ev("module.model", "digest", {"channel": "web", "cost_usd": 0.5, "ok": True}, now - 20)
        self.ev("module.model", "digest", {"channel": "web", "cost_usd": None, "ok": False}, now - 10)
        rows = stats.sessions(self.state, now - 3600, now + 1)
        self.assertEqual([r["name"] for r in rows], ["maps"])   # module calls do not go out and do not break the total


if __name__ == "__main__":
    unittest.main()
