"""Codex (experimental): evals/prompts.py on Codex — a session's rules as a turn of a hub session on
Codex, with a fake app-server, no model.

The Codex folder of a run is its own: a copy of the evals' sign-in (<state>/codex-evals-home, the same one evals/run.py
takes, under its lock) with a session's settings (hosts/codex_set: the isolation pins, skill-installer off) and no
shell when the cases name no tools — a Claude eval turn has none either. Never the hub's sign-in, never the owner's own
~/.codex. The limits before and after go into the output. A turn may put history into the thread first (items ->
thread/inject_items), as the host gives a session the context after a compaction."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import platforms
from tests import winsafe
from tests.fakes import can, exe

from core import codexmodel

# an app-server for one eval turn: writes its CODEX_HOME, its config.toml and sign-in and every request to LOG; the
# answer is ANSWER; ROTATE — the sign-in is refreshed (auth.json rewritten), as Codex does with a stale token. Its
# settings come from <the script>.json (the test writes them): the environment of a turn is cut to a small one
SERVER = r'''
import json, os, shutil, subprocess, sys
opt = json.load(open(os.path.splitext(os.path.abspath(__file__))[0] + ".json"))
home = os.environ.get("CODEX_HOME") or ""
def read(name):
    try:
        with open(os.path.join(home, name)) as f:
            return f.read()
    except OSError:
        return None
log = open(opt["LOG"], "a")
log.write(json.dumps({"start": True, "home": home, "config": read("config.toml"), "auth": read("auth.json"),
                      "hub": sorted(k for k in os.environ if k.startswith("HUB_")),
                      "PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME"),
                      "env": sorted(os.environ)}) + "\n")
log.flush()
if opt.get("ROTATE"):
    with open(os.path.join(home, "auth.json"), "w") as f:
        f.write('{"tokens": "refreshed"}')
def out(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    m = json.loads(line); log.write(line); log.flush()
    method, rid = m.get("method"), m.get("id")
    if method == "initialize":
        out({"id": rid, "result": {"userAgent": "fake/0.162.1"}})
    elif method == "account/read":
        out({"id": rid, "result": {"account": {"type": "chatgpt", "email": "x@example.test", "planType": "pro"}}})
    elif method == "account/rateLimits/read":
        out({"id": rid, "result": {"rateLimits": {"primary": {"usedPercent": 7, "windowDurationMins": 10080}}}})
    elif method == "thread/start":
        if opt.get("RUNHUB"):   # what a `hub` command in the thread's shell would do
            got = subprocess.run([shutil.which("hub") or "hub", "send", "maps", "x"], capture_output=True, text=True)
            log.write(json.dumps({"hub_ran": [got.returncode, got.stdout]}) + "\n")
        out({"id": rid, "result": {"thread": {"id": "t1", "ephemeral": True}}})
    elif method == "thread/inject_items":
        out({"id": rid, "result": {}})
    elif method == "turn/start":
        out({"id": rid, "result": {"turn": {"id": "u1", "status": "inProgress"}}})
        out({"method": "thread/tokenUsage/updated", "params": {"threadId": "t1", "turnId": "u1", "tokenUsage": {
             "last": {"inputTokens": 900, "outputTokens": 9}, "total": {"inputTokens": 0, "outputTokens": 0}}}})
        out({"method": "item/completed", "params": {"threadId": "t1", "item": {"type": "agentMessage", "id": "a",
             "text": opt.get("ANSWER", "ok")}}})
        out({"method": "turn/completed", "params": {"threadId": "t1", "turn": {"id": "u1", "status": "completed"}}})
    elif rid is not None:
        out({"id": rid, "result": {}})
'''


class Fake:
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hubcxpe-")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        self.server = exe.script(os.path.join(self.tmp, "codex"), SERVER)   # Windows: codex.cmd, like npm's
        self.log = os.path.join(self.tmp, "log.jsonl")
        self.root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def tearDown(self):
        platforms.current.rmtree(self.tmp)

    def options(self, server, env):
        """The fake's settings from the test's environment (LOG, ANSWER, ROTATE, RUNHUB, NOLOGIN) — beside the script."""
        with open(os.path.splitext(server)[0] + ".json", "w") as f:
            json.dump({k: env[k] for k in ("LOG", "ANSWER", "ROTATE", "RUNHUB", "NOLOGIN") if k in env}, f)

    def sent(self):
        with open(self.log) as f:
            return [json.loads(x) for x in f]


class InjectItems(Fake, unittest.TestCase):
    """core/codexmodel.run(items=…): the history goes into the thread by thread/inject_items, then the turn."""

    def run_model(self, **kw):
        env = {**os.environ, "LOG": self.log, "ANSWER": "done"}
        self.options(self.server, env)
        return codexmodel.run([self.server], "hello", "RULES", None, "gpt-x", "medium", 30, env, developer=True, **kw)

    def test_items_go_into_the_thread_before_the_turn(self):
        items = [{"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "CONTEXT"}]}]
        got, spent = self.run_model(items=items)
        self.assertEqual((got, spent), ("done", (900, 9)))
        calls = [m for m in self.sent() if m.get("method")]
        methods = [m["method"] for m in calls]
        self.assertLess(methods.index("thread/inject_items"), methods.index("turn/start"))
        inject = next(m for m in calls if m["method"] == "thread/inject_items")["params"]
        self.assertEqual(inject, {"threadId": "t1", "items": items})

    def test_without_items_nothing_is_injected(self):
        self.assertEqual(self.run_model()[0], "done")
        self.assertNotIn("thread/inject_items", [m.get("method") for m in self.sent()])


class PromptEvals(Fake, unittest.TestCase):
    """evals/prompts.py --host codex — the Codex folder, the lock, the settings and the limits of a run."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "hosts"))
        with open(os.path.join(self.repo, "p.md"), "w", encoding="utf-8") as f:
            f.write("Answer briefly.")
        with open(os.path.join(self.repo, "hosts", "codex.py"), "w", encoding="utf-8") as f:
            f.write('NOTE = "NOTE-X"\n')
        g = ["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        subprocess.run(g + ["add", "-A"], check=True)
        subprocess.run(g + ["commit", "-qm", "x"], check=True)
        self.evals = os.path.join(self.state, "codex-evals-home")
        os.makedirs(self.evals)
        with open(os.path.join(self.evals, "auth.json"), "w") as f:
            f.write('{"tokens": "evals"}')
        self.hub_json = os.path.join(self.tmp, "hub.json")
        self.home = os.path.join(self.tmp, "home")   # the owner's HOME: its ~/.codex is never taken
        os.makedirs(self.home)
        self.env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CONFIG": self.hub_json, "HOME": self.home,
                    "USERPROFILE": self.home,   # the home on Windows (os.path.expanduser, Codex's ~/.codex)
                    "HUB_CODEX_BIN": self.server, "LOG": self.log, "ANSWER": "ok",
                    "EVALS_DIR": os.path.join(self.tmp, "evals")}
        self.env.pop("HUB_SESSION", None)

    def case(self, turn=None, **extra):
        path = os.path.join(self.tmp, "case.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"id": "c", "model": "opus", "effort": "medium", "prompt": {"file": "p.md"}, **extra,
                       "turns": [turn or {"id": "t", "input": "hello", "checks": [{"type": "said", "re": "^ok"}]}]}, f)
        return path

    def prompts(self, *args, env=None):
        env = env or self.env
        self.options(env["HUB_CODEX_BIN"], env)
        return subprocess.run([sys.executable, os.path.join(self.root, "evals", "prompts.py"), *args, "--repo",
                               self.repo, "--only", "after", "--trials", "1"],
                              capture_output=True, text=True, timeout=60, env=env)

    def starts(self, turns=False):
        """The app-servers of the run (each: its start line); turns=True — only those that ran a turn (not a read of
        the limits)."""
        procs = []
        for m in self.sent():
            if m.get("start"):
                procs.append({**m, "methods": []})
            elif procs:
                procs[-1]["methods"].append(m.get("method"))
        return [p for p in procs if not turns or "turn/start" in p["methods"]]

    def test_a_claude_hub_takes_the_evals_sign_in_by_default(self):
        out = self.prompts(self.case(), "--host", "codex", "--codex-model", "gpt-x")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("| 1/1 |", out.stdout)
        homes = {s["home"] for s in self.starts()}
        self.assertEqual(len(homes), 1)
        (home,) = homes
        # a copy of the evals' sign-in in a folder of the run: neither the evals' folder itself nor the hub's
        self.assertNotIn(home, (self.evals, os.path.join(self.state, "codex-home")))
        self.assertEqual({s["auth"] for s in self.starts()}, {'{"tokens": "evals"}'})
        self.assertFalse(os.path.exists(home))                                   # removed after the run
        self.assertFalse(os.path.exists(os.path.join(self.state, "codex-home")))  # the hub's sign-in: not even looked at
        self.assertFalse(os.path.exists(os.path.join(self.home, ".codex")))
        # no hub variables of the machine reach the model: only an empty hub of the run's own
        self.assertEqual({tuple(s["hub"]) for s in self.starts(turns=True)}, {("HUB_CONFIG", "HUB_CONFIG_DIR", "HUB_STATE_DIR")})
        # the limits read before and after the turn
        self.assertEqual([("turn/start" in s["methods"], "account/rateLimits/read" in s["methods"]) for s in self.starts()],
                         [(False, True), (True, False), (False, True)])

    def test_a_codex_hub_takes_the_evals_sign_in_too_not_the_hubs(self):
        with open(self.hub_json, "w") as f:
            json.dump({"host": "codex", "codex": {"model": "gpt-x"}}, f)
        hub_home = os.path.join(self.state, "codex-home")
        os.makedirs(hub_home)
        with open(os.path.join(hub_home, "auth.json"), "w") as f:
            f.write('{"tokens": "hub"}')
        bare = os.path.join(self.tmp, "bin")   # git and nothing else: no claude
        os.makedirs(bare)
        winsafe.symlink(self, shutil.which("git"), os.path.join(bare, "git"))
        out = self.prompts(self.case(), env={**self.env, "PATH": bare})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual({s["auth"] for s in self.starts()}, {'{"tokens": "evals"}'})
        self.assertIn("Codex tokens: input 900, output 9", out.stdout)
        start = next(m for m in self.sent() if m.get("method") == "thread/start")["params"]
        # the case's prompt plus the Codex addendum of the same revision — the developerInstructions of a session's thread
        self.assertEqual((start["developerInstructions"], start["model"], start["ephemeral"]),
                         ("Answer briefly.\n\nNOTE-X", "gpt-x", True))
        self.assertNotIn("baseInstructions", start)
        turn = next(m for m in self.sent() if m.get("method") == "turn/start")["params"]
        self.assertEqual((turn["input"][0]["text"], turn["effort"]), ("hello", "medium"))   # the case's effort
        self.assertNotIn("outputSchema", turn)
        with open(os.path.join(hub_home, "auth.json")) as f:
            self.assertEqual(f.read(), '{"tokens": "hub"}')

    def test_a_refreshed_sign_in_comes_back_to_the_evals_folder(self):
        out = self.prompts(self.case(), "--host", "codex", "--codex-model", "gpt-x", env={**self.env, "ROTATE": "1"})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        with open(os.path.join(self.evals, "auth.json")) as f:
            self.assertEqual(f.read(), '{"tokens": "refreshed"}')

    def test_without_the_evals_sign_in_a_refusal_before_any_codex(self):
        os.unlink(os.path.join(self.evals, "auth.json"))
        out = self.prompts(self.case(), "--host", "codex", "--codex-model", "gpt-x")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("evals/run.py --codex-login", out.stderr)
        self.assertFalse(os.path.exists(self.log))

    def test_a_running_eval_holds_the_sign_in(self):
        with open(os.path.join(self.evals, "run.lock"), "a") as held:
            platforms.current.try_lock(held)
            out = self.prompts(self.case(), "--host", "codex", "--codex-model", "gpt-x")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("another eval run", out.stderr)
        self.assertFalse(os.path.exists(self.log))

    def test_the_limits_before_and_after_are_in_the_output(self):
        out = self.prompts(self.case(), "--host", "codex", "--codex-model", "gpt-x")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("Codex quota before: week 7%", out.stdout)
        self.assertIn("Codex quota after: week 7%", out.stdout)

    def test_the_run_has_a_sessions_settings_and_its_shell(self):
        out = self.prompts(self.case(), "--host", "codex", "--codex-model", "gpt-x")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        config = self.starts()[0]["config"]
        for line in ("plugins = false", "remote_plugin = false", "memories = false", "multi_agent = false",
                     "apps = false", 'name = "skill-installer"', 'web_search = "disabled"',
                     "allow_login_shell = false", "shell_snapshot = false"):
            self.assertIn(line, config)
        # the shell stays, as a session's: without it Codex answers "no shell for hub send" instead of the commands
        self.assertNotIn("shell_tool = false", config)
        self.assertNotIn("unified_exec = false", config)

    def test_hub_in_the_turns_shell_is_a_stub(self):
        """The model may run `hub send`/`hub waiting` in the thread's shell: the first `hub` of its PATH answers "ok"
        and goes nowhere — not the live hub of the machine."""
        real = os.path.join(self.tmp, "real")   # a `hub` that would reach a hub
        os.makedirs(real)
        exe.script(os.path.join(real, "hub"), 'print("REAL")\nraise SystemExit(3)\n')
        env = {**self.env, "RUNHUB": "1", "PATH": real + os.pathsep + self.env.get("PATH", "")}
        out = self.prompts(self.case(), "--host", "codex", "--codex-model", "gpt-x", env=env)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        ran = [m["hub_ran"] for m in self.sent() if "hub_ran" in m]
        self.assertEqual(ran, [[0, "ok\n"]])
        (turn,) = self.starts(turns=True)
        first = turn["PATH"].split(os.pathsep)[0]
        # the run's own bin, then the PATH as it was; removed with the run
        self.assertEqual((os.path.basename(first), turn["PATH"].split(os.pathsep, 1)[1]), ("bin", env["PATH"]))
        self.assertFalse(os.path.exists(first))
        self.assertEqual(self.starts()[0]["PATH"], env["PATH"])   # the limits' read has the PATH as is

    @unittest.skipUnless(can.run_without_path(), "a process without PATH finds no git here (no default path)")
    def test_no_path_leaves_no_empty_entry(self):
        env = {k: v for k, v in self.env.items() if k != "PATH"}   # git from the system's default path
        self.options(env["HUB_CODEX_BIN"], env)
        out = subprocess.run([sys.executable, os.path.join(self.root, "evals", "prompts.py"), self.case(), "--repo",
                              self.repo, "--only", "after", "--trials", "1", "--host", "codex", "--codex-model",
                              "gpt-x"], capture_output=True, text=True, timeout=60, env=env, cwd=self.tmp)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        (turn,) = self.starts(turns=True)
        self.assertNotIn("", turn["PATH"].split(os.pathsep))   # an empty entry is the current folder in a shell

    def test_the_turns_environment_is_a_small_one_with_a_home_of_its_own(self):
        """A shell of the thread can read what its environment holds: only what Codex and a shell need, and a HOME
        of the run's own (the owner's dotfiles are not one `cat ~/...` away)."""
        env = {**self.env, "SOME_TOKEN": "x", "GITLAB_TOKEN": "y"}
        out = self.prompts(self.case(), "--host", "codex", "--codex-model", "gpt-x", env=env)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        (turn,) = self.starts(turns=True)
        self.assertFalse({"SOME_TOKEN", "GITLAB_TOKEN"} & set(turn["env"]))
        self.assertNotEqual(turn["HOME"], self.home)
        self.assertFalse(os.path.exists(turn["HOME"]))   # the run's, removed after it

    def test_the_skills_config_is_valid_toml_for_any_path(self):
        import importlib.util
        if not importlib.util.find_spec("tomllib"):
            self.skipTest("no tomllib")
        import tomllib
        # a name valid on every system (no quote or backslash on Windows); those two — through _toml below
        skill = os.path.join(self.home, ".agents", "skills", "a \U0001F600 ω \x7f z", "SKILL.md")
        os.makedirs(os.path.dirname(skill))
        with open(skill, "w") as f:
            f.write("---\nname: x\n---\n")
        sys.path.insert(0, os.path.join(self.root, "evals"))
        try:
            import prompts
            from unittest import mock
            with mock.patch.dict(os.environ, {"HOME": self.home, "USERPROFILE": self.home}):   # Windows: the latter
                config = tomllib.loads(prompts.run_config())
        finally:
            sys.path.remove(os.path.join(self.root, "evals"))
        paths = [e.get("path") for e in config["skills"]["config"]]
        self.assertIn(skill, paths)
        text = 'q "x" \\ y \t \x7f \U0001F600'
        self.assertEqual(tomllib.loads("v = " + prompts._toml(text))["v"], text)
        from hosts import codex_set
        for line in codex_set.CONFIG.split("[features]")[1].strip().splitlines():   # every feature CONFIG sets stays
            k, v = (x.strip() for x in line.split("="))
            self.assertEqual(config["features"][k], v == "true")

    def test_turn_items_go_into_the_thread_first(self):
        turn = {"id": "t", "input": "hello", "checks": [{"type": "said", "re": "^ok"}],
                "items": {"before": [{"role": "user", "text": "SUMMARY"}],
                          "after": [{"role": "user", "text": "SUMMARY"}, {"role": "developer", "text": "CONTEXT"}]}}
        out = self.prompts(self.case(turn), "--host", "codex", "--codex-model", "gpt-x")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        inject = next(m for m in self.sent() if m.get("method") == "thread/inject_items")["params"]["items"]
        self.assertEqual([(i["role"], i["content"][0]["text"]) for i in inject],
                         [("user", "SUMMARY"), ("developer", "CONTEXT")])
        self.assertEqual({i["type"] for i in inject}, {"message"})

    def test_turn_items_are_codex_only(self):
        turn = {"id": "t", "input": "hello", "checks": [], "items": [{"role": "developer", "text": "CONTEXT"}]}
        out = self.prompts(self.case(turn), "--host", "claude")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("Codex-only", out.stderr)

    def test_an_explicit_codex_home_is_a_sign_in_source_never_the_owners_own_nor_the_hubs(self):
        out = self.prompts(self.case(), "--host", "codex", "--codex-home", os.path.join(self.home, ".codex"),
                           "--codex-model", "gpt-x")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("own Codex folder", out.stderr)
        out = self.prompts(self.case(), "--host", "codex", "--codex-home", os.path.join(self.state, "codex-home"),
                           "--codex-model", "gpt-x")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("the hub's sign-in", out.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state, "codex-home")))
        acct = os.path.join(self.state, "moddata", "accounts", "codex", "codex-2")   # a Codex pool account's: the same
        os.makedirs(acct)
        out = self.prompts(self.case(), "--host", "codex", "--codex-home", acct, "--codex-model", "gpt-x")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("the hub's sign-in", out.stderr)
        own = os.path.join(self.tmp, "cx")   # another sign-in of evals: copied into the run, as the evals' own
        os.makedirs(own)
        with open(os.path.join(own, "auth.json"), "w") as f:
            f.write('{"tokens": "own"}')
        out = self.prompts(self.case(), "--host", "codex", "--codex-home", own, "--codex-model", "gpt-x",
                           env={**self.env, "ROTATE": "1"})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        turn = self.starts(turns=True)[0]
        self.assertNotEqual(turn["home"], own)
        self.assertEqual(self.starts()[0]["auth"], '{"tokens": "own"}')   # the first to read the copy
        self.assertIn("allow_login_shell = false", turn["config"])   # the stub holds there too
        self.assertEqual(sorted(os.listdir(own)), ["auth.json", "run.lock"])
        with open(os.path.join(own, "auth.json")) as f:
            self.assertEqual(f.read(), '{"tokens": "refreshed"}')
        with open(os.path.join(self.evals, "auth.json")) as f:
            self.assertEqual(f.read(), '{"tokens": "evals"}')   # the evals' own sign-in not taken

    def test_without_a_sign_in_a_turn_is_na(self):
        # thread/start refused as by a Codex without a sign-in
        bare = exe.script(os.path.join(self.tmp, "nologin"), SERVER.replace(
            'elif method == "thread/start":',
            'elif method == "thread/start" and opt.get("NOLOGIN"):\n'
            '        out({"id": rid, "error": {"code": -32600, "message": "Not logged in"}})\n'
            '    elif method == "thread/start":'))
        out = self.prompts(self.case(), "--host", "codex", "--codex-model", "gpt-x",
                           env={**self.env, "HUB_CODEX_BIN": bare, "NOLOGIN": "1"})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("0/0 (+1 n/a)", out.stdout)


if __name__ == "__main__":
    unittest.main()
