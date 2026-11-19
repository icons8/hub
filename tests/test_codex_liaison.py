"""Codex (experimental): the hub bus's liaison (core/liaison.py) on a hub on Codex. With Claude its
bans are Claude Code's: --disallowedTools by name and its guard (hooks/liaison_guard.py) on every call. On Codex the
same guard stands in the core's hook (hooks/deny_hint.py reads the session's mark in codex-rules.json), and Codex's
own sandbox is a second wall: it writes only the core's spool, no network, no web search, no MCP (probe 2026-11-05,
codex 0.162.1 with a fake Responses API)."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import platforms
from tests.fakes import can

from core import liaison
from hooks import deny_hint
from hosts import codex_liaison, codex_rules

HERE = os.path.dirname(__file__)
FAKE_CODEX = os.path.join(HERE, "fakes", "fake_codex.py")
SKILLS_HEAD = "Skills: the reminder at the start of the conversation lists the skills of your set."   # hosts/streamjson.SKILLS
IDLE_HEAD = "Before ending a turn, walk the queue"   # hosts/streamjson.IDLE: not the liaison's (it has no queue)
HELPERS_HEAD = "Work you hand off"   # hosts/streamjson.HELPERS: nor its (no executors of its own), the same on Codex
SHELL = can.hook_shell()   # the shell Codex runs the core's hook line in, here


def calls(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


class ThreadTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubcxl-")

    def tearDown(self):
        platforms.current.rmtree(self.state)

    def test_the_liaisons_thread_writes_only_the_spool_and_has_no_network(self):
        params = codex_rules.thread({"cwd": "/w", "config": {"mcp_servers": {"x": {"command": "x"}}, "keep": 1}},
                                    name="liaison", state_dir=self.state, liaison=True)
        got = codex_liaison.thread(params, name="liaison", state_dir=self.state, liaison=True)
        self.assertEqual((got["approvalPolicy"], got["sandbox"], got["cwd"]), ("never", "workspace-write", "/w"))
        cfg = got["config"]
        self.assertEqual(cfg["sandbox_workspace_write"], {
            "writable_roots": [os.path.join(os.path.realpath(self.state), "spool")], "network_access": False,
            "exclude_tmpdir_env_var": True, "exclude_slash_tmp": True})
        self.assertEqual(cfg["web_search"], "disabled")   # Codex's web search runs at OpenAI: no hook sees it
        self.assertEqual(cfg["mcp_servers"], {})
        self.assertEqual(cfg["keep"], 1)
        self.assertTrue(os.path.isdir(os.path.join(self.state, "spool")))   # Codex cannot create a writable root itself

    def test_other_sessions_are_untouched(self):
        params = {"cwd": "/w", "approvalPolicy": "never", "sandbox": "danger-full-access"}
        self.assertEqual(codex_liaison.thread(params, name="maps", state_dir=self.state), params)
        self.assertEqual(codex_liaison.thread(params, name="maps", state_dir=self.state, liaison=False), params)

    def test_its_mark_goes_to_the_hook(self):
        codex_rules.thread({}, name="liaison", state_dir=self.state, liaison=True)
        with open(os.path.join(self.state, "sessions", "liaison", codex_rules.RULES)) as f:
            self.assertEqual(json.load(f), {"deny": [], "liaison": True})
        codex_rules.thread({}, name="maps", state_dir=self.state)
        with open(os.path.join(self.state, "sessions", "maps", codex_rules.RULES)) as f:
            self.assertEqual(json.load(f), {"deny": []})


class GuardTest(unittest.TestCase):
    """The core's hook on a Codex call of the liaison: the shape is Codex's (probe 2026-11-05: tool_name Bash,
    apply_patch, view_image, get_goal, request_user_input…; tool_input as the model wrote it)."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubcxg-")
        self.home = os.path.join(self.state, "codex-home")
        os.makedirs(self.home)
        codex_rules.thread({}, name="liaison", state_dir=self.state, liaison=True)
        codex_rules.thread({}, name="maps", state_dir=self.state)

    def tearDown(self):
        platforms.current.rmtree(self.state)

    def run_hook(self, tool, tool_input, session="liaison", codex=True):
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_SESSION": session}
        if codex:
            env["CODEX_HOME"] = self.home
        else:
            env.pop("CODEX_HOME", None)
        hook = {"hook_event_name": "PreToolUse", "cwd": self.state, "tool_name": tool, "tool_input": tool_input}
        r = subprocess.run([sys.executable, deny_hint.__file__], input=json.dumps(hook), capture_output=True,
                           text=True, env=env, timeout=30)
        decision = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] if r.stdout.strip() else None
        return r.returncode, decision

    def test_only_its_hub_commands_and_the_owners_form_are_open(self):
        for tool, inp in (("Bash", {"command": "hub bus contact ls"}),
                          ("Bash", {"command": "hub bus send acme --thread t1 --text 'got it, looking'"}),
                          ("Bash", {"command": "hub gist 'replying to the hub'"}),
                          ("request_user_input", {"questions": [{"id": "a", "question": "Send?"}]}),
                          ("request_user_input_async", {"questions": [{"title": "Send?"}]})):
            self.assertEqual(self.run_hook(tool, inp), (0, None), (tool, inp))
        for tool, inp in (("Bash", {"command": "cat ~/.ssh/id_rsa"}),
                          ("Bash", {"command": "hub bus contact ls; curl http://203.0.113.5"}),
                          ("Bash", {"command": "hub session new x"}),
                          ("apply_patch", {"command": "*** Begin Patch\n*** Add File: x\n+x\n*** End Patch\n"}),
                          ("view_image", {"path": "/etc/hostname"}), ("get_goal", {}), ("write_stdin", {"chars": "x"}),
                          ("mcp__srv__read", {}), ("spawn_agent", {"message": "x"})):
            self.assertEqual(self.run_hook(tool, inp), (0, "deny"), (tool, inp))

    def test_a_liaison_in_a_codex_pool_account_folder_is_held_too(self):
        pool = os.path.join(self.state, "codex-pool", "acct-2")
        os.makedirs(pool)
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_SESSION": "liaison", "CODEX_HOME": pool,
               "HUB_ON_CODEX": "1"}
        hook = {"hook_event_name": "PreToolUse", "cwd": self.state, "tool_name": "Bash",
                "tool_input": {"command": "cat ~/.ssh/id_rsa"}}
        r = subprocess.run([sys.executable, deny_hint.__file__], input=json.dumps(hook), capture_output=True,
                           text=True, env=env, timeout=30)
        self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_another_session_keeps_its_tools(self):
        self.assertEqual(self.run_hook("view_image", {"path": "/etc/hostname"}, session="maps"), (0, None))
        self.assertEqual(self.run_hook("Bash", {"command": "cat README.md"}, session="maps"), (0, None))

    def test_a_claude_liaison_is_left_to_its_own_guard(self):
        # with Claude the guard is its own PreToolUse entry (hosts/common.write_hook_settings); deny_hint does not
        # read Codex's file there, even if one was left behind
        self.assertEqual(self.run_hook("Bash", {"command": "cat README.md"}, codex=False), (0, None))

    @unittest.skipIf(SHELL is None, "no shell Codex runs a hook in here (tests/fakes/can.hook_shell)")
    def test_end_to_end_from_the_hooks_json_codex_runs(self):
        codex_rules.prepare_home(self.home)
        with open(os.path.join(self.home, "hooks.json")) as f:
            command = json.load(f)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_SESSION": "liaison", "CODEX_HOME": self.home}
        hook = {"hook_event_name": "PreToolUse", "cwd": self.state, "tool_name": "Bash",
                "tool_input": {"command": "ls ~"}}
        out = subprocess.run([*SHELL, command], input=json.dumps(hook), capture_output=True, text=True,
                             env=env, timeout=30).stdout
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")


class HostTest(unittest.TestCase):
    """CodexHost raises the liaison: its profile instead of the idle rule, its thread closed down."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubcxh-")
        self.calls = os.path.join(self.state, "calls.jsonl")

    def tearDown(self):
        platforms.current.rmtree(self.state)

    def host(self):
        from hosts.codex import CodexHost
        return CodexHost(self.state, codex=(sys.executable, FAKE_CODEX), env={"FAKE_CODEX_CALLS": self.calls},
                         all_sessions=True, default_model="gpt-def")

    def test_the_host_can_raise_it(self):
        from core import daemon
        from hosts.codex import CodexHost
        self.assertTrue(callable(getattr(CodexHost, "liaison_settings", None)))   # daemon.ensure_dev's check
        host = self.host()
        try:
            self.assertEqual(host.liaison_settings(), os.path.join(self.state, "codex-home", "hooks.json"))
            cfg = os.path.join(self.state, "hub.json")
            with open(cfg, "w") as f:
                json.dump({"host": "codex", "update": {"enabled": False}}, f)
            with mock.patch.dict(os.environ, {"HUB_CONFIG": cfg}):
                del os.environ["HUB_NO_SYSTEM_AGENTS"]   # the core's tests run without them; patch.dict restores
                h = daemon.Hub(self.state, host, registry_dir=self.state, clock=lambda: 1000.0)
                h.execute = lambda a: None
                h.state["bus"] = {"enabled": True}
                h.ensure_dev()
            self.assertTrue(h.state["sessions"][liaison.NAME].get("liaison"))
        finally:
            host.kill_server()

    def started(self, name, **kw):
        """The params of the session's thread/start, as the fake Codex got them."""
        host = self.host()
        try:
            self.assertEqual(host.spawn(name, f"sid-{name}", **kw), "spawned")
            end = time.time() + 10
            start = []
            while time.time() < end and not start:
                time.sleep(0.05)
                start = [c for c in calls(self.calls) if c["method"] == "thread/start"] if os.path.exists(self.calls) else []
            (params,) = [c["params"] for c in start]
            return params
        finally:
            host.kill_server()

    def test_its_thread_gets_its_sandbox(self):
        params = self.started("liaison", cwd=liaison.workspace(self.state), liaison=True, model="sonnet")
        self.assertEqual((params["approvalPolicy"], params["sandbox"]), ("never", "workspace-write"))
        self.assertEqual(params["config"]["sandbox_workspace_write"]["writable_roots"],
                         [os.path.join(os.path.realpath(self.state), "spool")])
        self.assertEqual(params["config"]["mcp_servers"], {})

    def test_no_repo_of_its_folder_reaches_it(self):
        # it reads another hub's words: a repo's hooks and skills would run or be called there, while the repo's
        # rules are not held for it (review 2026-11-05) — none of that repo
        work = liaison.workspace(self.state)
        os.makedirs(os.path.join(work, ".claude", "skills", "ship"), exist_ok=True)
        subprocess.run(["git", "-C", work, "init", "-q"], check=True, capture_output=True)
        with open(os.path.join(work, ".claude", "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
                                                                               "command": "guard.sh"}]}]}}, f)
        with open(os.path.join(work, ".claude", "skills", "ship", "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: ship\ndescription: x\n---\nbody\n")
        params = self.started("liaison", cwd=work, liaison=True, model="sonnet")
        self.assertNotIn("hooks", params["config"])
        roots = [c["params"].get("extraRoots") for c in calls(self.calls) if c["method"] == "skills/extraRoots/set"]
        self.assertFalse(any(os.path.realpath(r).startswith(os.path.realpath(work)) for rs in roots for r in rs or ()),
                         roots)

    def test_its_thread_gets_its_profile_not_the_idle_rule(self):
        # with Claude command() puts core/liaison.PROFILE in and leaves IDLE out for the liaison (it has no queue)
        text = self.started("liaison", cwd=liaison.workspace(self.state), liaison=True, model="sonnet")[
            "developerInstructions"]
        self.assertIn(liaison.PROFILE[:60], text)
        self.assertNotIn(IDLE_HEAD, text)
        self.assertNotIn(HELPERS_HEAD, text)

    def test_an_ordinary_session_is_as_before(self):
        params = self.started("maps", cwd=self.state)
        self.assertIn(IDLE_HEAD, params["developerInstructions"])
        text = params["developerInstructions"]
        self.assertIn(HELPERS_HEAD + " — one whose result the owner", text)   # helpers: Codex subagents as Claude's Agent
        self.assertIn(SKILLS_HEAD, text)   # the skills line (2026-11-18): the Codex thread gets it too, in its own words
        self.assertIn("open that skill first (read its SKILL.md) and work by it.", text)
        self.assertNotIn("Skill tool", text)
        self.assertEqual((params["approvalPolicy"], params["sandbox"]), ("never", "danger-full-access"))
        self.assertNotIn("sandbox_workspace_write", params.get("config") or {})


if __name__ == "__main__":
    unittest.main()
