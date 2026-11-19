"""Codex (experimental): bans for hub sessions on Codex — the core's
PreToolUse hook (hooks/deny_hint.py) in hooks.json of the hub's Codex folder, trusted by the hash from hooks/list, no
Codex subagents of its own; a mini-hub gets no apply_patch edits in its own folder, no git commit and push; a mini-hub
executor gets no tool to ask the owner. If the hook did not become trusted, the session is not started."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import platforms
from tests.fakes import can

from hooks import deny_hint
from hosts import codex_rules
from hosts.codex import ON_CODEX_ENV

HERE = os.path.dirname(__file__)
FAKE_CODEX = os.path.join(HERE, "fakes", "fake_codex.py")
FAKE_H = os.path.join(HERE, "fakes", "codex_h.py")
CONFIG = '# Codex of the hub sessions\ncli_auth_credentials_store = "file"\n\n[features]\napps = false\nmemories = false\n'
SHELL = can.hook_shell()   # the shell Codex runs the core's hook line in, here


class Proc:
    """The part of hosts.codex.CodexProcess the permission seams use."""

    def __init__(self, host=None, items=None):
        self.asks, self.backlog, self.items, self.start_params = {}, [], dict(items or {}), {}
        if host is not None:
            self.host = host


class Host:
    def __init__(self, auto=False):
        self.auto, self.flushed = auto, 0

    def auto_allow_on(self):
        return self.auto

    def _flush(self, proc):
        self.flushed += 1


class HomeTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubcxr-")
        self.home = os.path.join(self.state, "codex-home")
        os.makedirs(self.home)
        with open(os.path.join(self.home, "config.toml"), "w") as f:
            f.write(CONFIG)

    def tearDown(self):
        platforms.current.rmtree(self.state)

    def prepare(self, replies=None):
        env = {**os.environ, "CODEX_HOME": self.home}
        if replies is not None:
            path = os.path.join(self.state, "replies.json")
            with open(path, "w") as f:
                json.dump(replies, f)
            env["FAKE_CODEX_REPLIES"] = path
        codex_rules.prepare_home(self.home, codex=(sys.executable, FAKE_CODEX), env=env)

    def config(self):
        with open(os.path.join(self.home, "config.toml")) as f:
            return f.read()

    def test_hook_written_trusted_by_codex_hash_and_no_own_subagents(self):
        self.prepare()
        with open(os.path.join(self.home, "hooks.json")) as f:
            hooks = json.load(f)["hooks"]
        (entry,) = hooks["PreToolUse"]
        self.assertIn("deny_hint.py", entry["hooks"][0]["command"])
        cfg = self.config()
        self.assertIn("[features]\nmulti_agent = false\n", cfg)
        # the key as a TOML string: "\\" of a Windows path escaped; Codex's own, by the real path (macOS's TMPDIR is a link)
        key = os.path.join(os.path.realpath(self.home), "hooks.json") + ":pre_tool_use:0:0"
        self.assertIn(f'[hooks.state.{json.dumps(key)}]', cfg)
        self.assertTrue(codex_rules.trusted(self.state))
        self.assertEqual(codex_rules.features(("default_mode_request_user_input",), name="maps", state_dir=self.state),
                         ("default_mode_request_user_input",))
        self.prepare()   # the hub starts again: the host rewrites config.toml — the same trust, no duplicates
        self.assertEqual(self.config().count("multi_agent"), 1)
        self.assertTrue(codex_rules.trusted(self.state))

    def test_windows_hook_is_one_command_line_codex_runs(self):
        # Claude Code's exec form (platforms/windows.hook_command: command + args) — Codex ignores args and ran the bare
        # python: the hook "completed", the denied command ran (probe 2026-11-05). Codex runs the line in PowerShell
        # -Command or cmd /C: plain words only
        from unittest import mock
        import platforms
        exe = {"type": "command", "command": "C:\\Py314\\python.exe", "args": ["-X", "utf8", "C:\\hub\\deny_hint.py"]}
        with mock.patch.object(platforms.current, "hook_command", return_value=exe):
            self.prepare()
        with open(os.path.join(self.home, "hooks.json")) as f:
            (hook,) = json.load(f)["hooks"]["PreToolUse"][0]["hooks"]
        self.assertEqual(hook, {"type": "command", "command": "C:\\Py314\\python.exe -X utf8 C:\\hub\\deny_hint.py"})
        self.assertTrue(codex_rules.trusted(self.state))

    def test_a_hook_path_with_a_space_goes_by_its_short_name(self):
        # an 8.3 name always has "~" (PROGRA~1): it is a plain word for both shells, unless it starts the word
        from unittest import mock
        import platforms
        exe = {"type": "command", "command": "C:\\Program Files\\Py\\python.exe", "args": ["C:\\hub\\deny_hint.py"]}
        with mock.patch.object(platforms.current, "hook_command", return_value=exe), \
                mock.patch.object(codex_rules, "_short", lambda path: "C:\\PROGRA~1\\Py\\python.exe"):
            self.prepare()
        with open(os.path.join(self.home, "hooks.json")) as f:
            (hook,) = json.load(f)["hooks"]["PreToolUse"][0]["hooks"]
        self.assertEqual(hook["command"], "C:\\PROGRA~1\\Py\\python.exe C:\\hub\\deny_hint.py")
        self.assertIsNone(codex_rules._hook_line(["~\\py\\python.exe", "x"]))   # PowerShell: "~" first is the home

    def test_a_hook_path_that_cannot_be_a_plain_word_means_no_session(self):
        # a space in the path and no 8.3 name (here, off Windows, there is none): no line that both shells read —
        # better no session than no bans
        from unittest import mock
        import platforms
        exe = {"type": "command", "command": "C:\\Program Files\\Py\\python.exe", "args": ["C:\\hub\\deny_hint.py"]}
        with mock.patch.object(platforms.current, "hook_command", return_value=exe):
            self.prepare()
        with open(os.path.join(self.home, "hooks.json")) as f:
            self.assertEqual(json.load(f)["hooks"]["PreToolUse"], [])
        self.assertFalse(codex_rules.trusted(self.state))

    def test_a_folder_reached_through_a_link_is_trusted_by_codexs_own_path(self):
        # Codex names its hooks by the real path (live 0.162.1, Linux and darwin: a CODEX_HOME through a link — macOS's
        # TMPDIR /var → /private/var — is listed as the link's target): the core's hook is found in its answer by that
        # path too, else it was «not trusted» and no session started (Mac bridge, 2026-11-05)
        from tests.winsafe import try_symlink
        link = os.path.join(self.state, "via")
        if not try_symlink(self.state, link):
            self.skipTest("no links here")
        self.home = os.path.join(link, "codex-home")
        self.prepare()
        self.assertTrue(codex_rules.trusted(link))
        self.assertIn(json.dumps(os.path.join(os.path.realpath(self.state), "codex-home", "hooks.json") +
                                 ":pre_tool_use:0:0"), self.config())   # Codex's own key, not the link's

    def test_untrusted_hook_means_no_session(self):
        untrusted = {"data": [{"cwd": self.home, "warnings": [], "errors": [], "hooks": [{
            "key": os.path.join(self.home, "hooks.json") + ":pre_tool_use:0:0", "sourcePath":
            os.path.join(self.home, "hooks.json"), "currentHash": "sha256:1", "trustStatus": "modified"}]}]}
        self.prepare({"hooks/list": untrusted})
        self.assertFalse(codex_rules.trusted(self.state))
        with self.assertRaises(RuntimeError) as e:
            codex_rules.features(("x",), name="maps", state_dir=self.state)
        self.assertIn("the bans do not hold", str(e.exception))

    def test_hooks_list_failure_means_no_session(self):
        self.prepare({"hooks/list": {"error": {"code": -1, "message": "none"}}})
        self.assertFalse(codex_rules.trusted(self.state))

    def test_untrusted_retried_on_spawn_after_a_minute(self):
        self.prepare({"hooks/list": {"error": {"code": -1, "message": "slow"}}})
        t = codex_rules._TRUSTED[os.path.abspath(self.home)]
        t["env"] = {**os.environ, "CODEX_HOME": self.home}   # Codex is back: hooks/list answers again
        self.assertFalse(codex_rules.trusted(self.state, now=t["at"] + 1))   # before RETRY — no new attempt
        self.assertTrue(codex_rules.trusted(self.state, now=t["at"] + codex_rules.RETRY))

    def test_codex_found_after_the_start_is_trusted_at_once(self):
        # a hubd that came up before Codex CLI was installed: the bare name, then the installer's path (2026-11-19)
        now = [["codex-not-installed-here"]]
        env = {**os.environ, "CODEX_HOME": self.home}
        codex_rules.prepare_home(self.home, codex=now[0], env=env, find=lambda: now[0])
        t = codex_rules._TRUSTED[os.path.abspath(self.home)]
        with mock.patch.object(codex_rules, "_trust", wraps=codex_rules._trust) as tried:
            self.assertFalse(codex_rules.trusted(self.state, now=t["at"] + 1))
            self.assertEqual(tried.call_count, 0)   # still not there: no app-server at every spawn, RETRY holds
            now[0] = [sys.executable, FAKE_CODEX]
            self.assertTrue(codex_rules.trusted(self.state, now=t["at"] + 1))   # not after RETRY
        self.assertEqual(codex_rules._TRUSTED[os.path.abspath(self.home)]["codex"], [sys.executable, FAKE_CODEX])

    def test_another_codex_after_the_trust_is_checked_again_before_a_session_runs_it(self):
        # the trust is that Codex's (its hash of the hook): a codex found anew later (the hub's own copy put in place)
        # does not inherit it — a session would run with no bans while the core thinks they hold
        now = [[sys.executable, FAKE_CODEX]]
        codex_rules.prepare_home(self.home, codex=now[0], env={**os.environ, "CODEX_HOME": self.home}, find=lambda: now[0])
        self.assertTrue(codex_rules.trusted(self.state))
        now[0] = [os.path.join(self.state, "no-such-codex")]   # one that does not answer: not trusted
        self.assertFalse(codex_rules.trusted(self.state))
        now[0] = [sys.executable, FAKE_CODEX]
        self.assertTrue(codex_rules.trusted(self.state))

    def test_retry_does_not_duplicate_the_trust_table(self):
        key = os.path.join(self.home, "hooks.json") + ":pre_tool_use:0:0"
        stale = {"data": [{"cwd": self.home, "warnings": [], "errors": [], "hooks": [{
            "key": key, "sourcePath": os.path.join(self.home, "hooks.json"), "currentHash": "sha256:1",
            "trustStatus": "modified"}]}]}
        self.prepare({"hooks/list": stale})
        self.prepare({"hooks/list": stale})   # another attempt over the earlier entry
        self.assertEqual(self.config().count(f"[hooks.state.{json.dumps(key)}]"), 1)
        self.prepare()   # and the real hash afterwards — still one table
        self.assertEqual(self.config().count(f"[hooks.state.{json.dumps(key)}]"), 1)
        self.assertTrue(codex_rules.trusted(self.state))

    def test_known_modes_start_an_unknown_one_is_refused_not_promoted(self):
        # slice H: every mode Claude Code knows that Codex can hold starts; one it cannot ("auto" — a model reviewer of
        # approvals, not checked) or a typo is refused, never silently promoted to full access
        self.prepare()
        for mode in (None, "bypassPermissions", "acceptEdits", "default", "manual", "plan", "dontAsk"):
            self.assertEqual(codex_rules.features(("x",), name="maps", state_dir=self.state, mode=mode), ("x",), mode)
        for mode in ("auto", "bypass", "yolo"):
            with self.assertRaises(RuntimeError) as e:
                codex_rules.features(("x",), name="maps", state_dir=self.state, mode=mode)
            self.assertIn(mode, str(e.exception))

    def test_multi_agent_on_is_turned_off(self):
        with open(os.path.join(self.home, "config.toml"), "w") as f:
            f.write(CONFIG.replace("apps = false", "multi_agent = true\napps = false"))
        self.prepare()
        self.assertIn("multi_agent = false", self.config())
        self.assertNotIn("multi_agent = true", self.config())

    def test_minihub_executor_cannot_ask_the_owner(self):
        self.prepare()
        self.assertEqual(codex_rules.features(("default_mode_request_user_input", "x"), name="maps-1",
                                              state_dir=self.state, bus="maps"), ("x",))


class ThreadTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubcxt-")

    def tearDown(self):
        platforms.current.rmtree(self.state)

    def rules(self, name):
        path = os.path.join(self.state, "sessions", name, codex_rules.RULES)
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)["deny"]

    def test_full_access_by_default(self):
        for mode in (None, "bypassPermissions"):
            got = codex_rules.thread({"cwd": "/w", "approvalPolicy": "on-request", "sandbox": "workspace-write"},
                                     name="maps", mode=mode, state_dir=self.state)
            self.assertEqual((got["approvalPolicy"], got["sandbox"], got["cwd"]), ("never", "danger-full-access", "/w"))

    def test_each_mode_is_codexs_approval_policy_and_sandbox(self):
        # the probe of 2026-11-05 (codex 0.162.1, a fake Responses API): untrusted asks for every command and every
        # edit (Claude's default); on-request + workspace-write edits the folder and runs commands in the sandbox, asks
        # to go beyond it (acceptEdits); read-only + on-request writes nothing without asking (plan); never +
        # read-only refuses writes without asking (dontAsk)
        want = {"acceptEdits": ("on-request", "workspace-write"), "default": ("untrusted", "workspace-write"),
                "manual": ("untrusted", "workspace-write"), "plan": ("on-request", "read-only"),
                "dontAsk": ("never", "read-only")}
        for mode, pair in want.items():
            got = codex_rules.thread({"cwd": "/w", "approvalPolicy": "never", "sandbox": "danger-full-access"},
                                     name="maps", mode=mode, state_dir=self.state)
            self.assertEqual((got["approvalPolicy"], got["sandbox"]), pair, mode)

    def test_a_sandboxed_session_can_still_run_hub_commands(self):
        # `hub …` writes the core's spool: a writable root of the workspace-write sandbox (it must exist); read-only
        # modes ask for it like Claude's plan asks for Bash; full access needs nothing
        spool = os.path.join(os.path.realpath(self.state), "spool")
        got = codex_rules.thread({"config": {"mcp_servers": {"a": {}}}}, name="maps", mode="acceptEdits",
                                 state_dir=self.state)
        self.assertEqual(got["config"], {"mcp_servers": {"a": {}}, "sandbox_workspace_write": {"writable_roots": [spool]}})
        self.assertTrue(os.path.isdir(spool))
        # default (untrusted) asks for every such command anyway and runs an approved one outside the sandbox
        for mode in ("default", "plan", "dontAsk", None):
            got = codex_rules.thread({}, name="maps", mode=mode, state_dir=self.state)
            self.assertNotIn("config", got, mode)

    def test_the_session_reports_its_real_mode(self):
        proc = Proc()
        for mode in ("plan", "acceptEdits", "default", None):
            proc.start_params = codex_rules.thread({}, name="maps", mode=mode, state_dir=self.state)
            self.assertEqual(codex_rules.info(proc), {"mode": mode or "bypassPermissions"})

    def test_minihub_rules_file_written_and_removed(self):
        work = os.path.join(self.state, "work")
        codex_rules.thread({}, name="maps", minihub=True, cwd=work, state_dir=self.state,
                           agent_file=os.path.join(self.state, "agents", "maps", "CLAUDE.md"))
        deny = self.rules("maps")
        from core.minihub import _rule_path
        self.assertIn(f"Edit(/{_rule_path(work)}/**)", deny)   # Windows: C:\x is /c/x, as Claude Code compares it
        self.assertIn("Bash(git commit:*)", deny)
        codex_rules.thread({}, name="maps", minihub=False, cwd=work, state_dir=self.state)
        self.assertEqual(self.rules("maps"), [])   # the file always exists: without it the hook on Codex refuses


def patch(*lines):
    return "*** Begin Patch\n" + "\n".join(lines) + "\n*** End Patch"


class HookTest(unittest.TestCase):
    """The hook on Codex calls (the shape is from the 2026-10-19 probe: tool_name Bash | apply_patch, tool_input.command)."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubcxh-")
        self.work = os.path.join(self.state, "work")
        os.makedirs(os.path.join(self.work, "sub"))
        from core import minihub
        self.rules = minihub.deny(self.work, keep=(os.path.join(self.state, "agents", "maps", "CLAUDE.md"), self.state))

    def tearDown(self):
        platforms.current.rmtree(self.state)

    def why(self, tool, command, rules=None, cwd=None):
        return deny_hint.reason({"command": command}, tool, cwd or self.work, self.rules if rules is None else rules)

    def test_minihub_patch_inside_own_folder_is_denied(self):
        for p in (patch("*** Add File: x.txt", "+x"),
                  patch("*** Update File: sub/y.py", "@@", "-a", "+b"),
                  patch("*** Delete File: sub/y.py"),
                  patch(f"*** Add File: {self.work}/z.txt", "+x"),
                  patch("*** Update File: /tmp/elsewhere.txt", "*** Move to: sub/moved.txt", "@@", "-a", "+b")):
            self.assertEqual(self.why("apply_patch", p), deny_hint.MINIHUB_HINT, p)

    def test_minihub_patch_with_an_indented_header_is_denied(self):
        # release review 2026-11-09: Codex trims the spaces before a file line, so an indented one is applied too
        other = tempfile.mkdtemp(prefix="hubcxo-")
        self.addCleanup(platforms.current.rmtree, other)
        for p in (patch("  *** Update File: sub/y.py", "@@", "-a", "+b"),
                  patch(f"*** Add File: {other}/ok.txt", "+x", "\t*** Update File: sub/y.py", "@@", "-a", "+b")):
            self.assertEqual(self.why("apply_patch", p), deny_hint.MINIHUB_HINT, p)

    def test_minihub_patch_outside_own_folder_passes(self):
        other = tempfile.mkdtemp(prefix="hubcxo-")
        self.addCleanup(platforms.current.rmtree, other)
        self.assertIsNone(self.why("apply_patch", patch(f"*** Add File: {other}/x.txt", "+x")))
        self.assertIsNone(self.why("apply_patch", patch("*** Add File: ../outside.txt", "+x")))

    def test_executor_patch_passes(self):
        self.assertIsNone(self.why("apply_patch", patch("*** Add File: x.txt", "+x"), rules=[]))

    def test_minihub_git_commit_and_push_denied(self):
        for c in ("git commit -m x", "cd sub && git push origin main", "/usr/bin/git commit -am y"):
            self.assertEqual(self.why("Bash", c), deny_hint.MINIHUB_HINT, c)
        for c in ("git status", "git log --grep commit", "echo 'git commit'"):
            self.assertIsNone(self.why("Bash", c), c)
        self.assertIsNone(self.why("Bash", "git commit -m x", rules=[]))

    def test_kill_tools_and_sleep_as_before_on_codex(self):
        self.assertIsNotNone(self.why("Bash", "pgrep -l systemd", rules=[]))
        self.assertEqual(self.why("Bash", "sleep 60", rules=[]), deny_hint.SLEEP_HINT)
        self.assertIsNone(self.why("apply_patch", patch("*** Add File: pkill.txt", "+pkill"), rules=[]))

    @unittest.skipIf(SHELL is None, "no shell Codex runs a hook in here (tests/fakes/can.hook_shell)")
    def test_hook_command_from_hooks_json_end_to_end(self):
        """The command from the core's hooks.json, run the way Codex runs it: through a shell, input on stdin, the session's environment."""
        home = os.path.join(self.state, "codex-home")
        os.makedirs(home)
        codex_rules.prepare_home(home)
        with open(os.path.join(home, "hooks.json")) as f:
            command = json.load(f)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        codex_rules.thread({}, name="maps", minihub=True, cwd=self.work, state_dir=self.state,
                           agent_file=os.path.join(self.state, "agents", "maps", "CLAUDE.md"))
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_SESSION": "maps", "CODEX_HOME": home}

        def run(tool, command_text):
            hook = {"hook_event_name": "PreToolUse", "cwd": self.work, "tool_name": tool,
                    "tool_input": {"command": command_text}, "permission_mode": "bypassPermissions"}
            out = subprocess.run([*SHELL, command], input=json.dumps(hook), capture_output=True, text=True,
                                 env=env, timeout=30).stdout
            return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out.strip() else None
        self.assertEqual(run("apply_patch", patch("*** Add File: forbidden.txt", "+x")), "deny")
        self.assertEqual(run("Bash", "pgrep -l systemd"), "deny")
        self.assertIsNone(run("Bash", "ls"))
        env["HUB_SESSION"] = "maps-1"   # an executor: no rules of its own
        codex_rules.thread({}, name="maps-1", cwd=self.work, state_dir=self.state)
        self.assertIsNone(run("apply_patch", patch("*** Add File: forbidden.txt", "+x")))

    @unittest.skipIf(SHELL is None, "no shell Codex runs a hook in here (tests/fakes/can.hook_shell)")
    def test_a_failure_by_the_windows_line_is_a_deny_not_an_exit_code(self):
        """The core's hook line in Windows' form (Claude Code's exec form made a line, codex_rules._hook_line), run as
        Codex runs it: a session without its rules file is refused by a deny on stdout with exit 0 — Codex on Windows
        let a hook's exit 2 through (winstand, 2026-11-05), so the refusal may not rest on the exit code."""
        from unittest import mock
        home = os.path.join(self.state, "codex-home")
        os.makedirs(home)
        if codex_rules._hook_line([sys.executable, deny_hint.__file__]) is None:
            # a hook line of plain words: a path with a space, or with "@" (Homebrew's python@3.14 on macOS)
            self.skipTest("this machine's paths make no plain hook line")
        exe = {"type": "command", "command": sys.executable, "args": ["-X", "utf8", deny_hint.__file__]}
        with mock.patch.object(platforms.current, "hook_command", return_value=exe):
            codex_rules.prepare_home(home)
        with open(os.path.join(home, "hooks.json")) as f:
            (hook,) = json.load(f)["hooks"]["PreToolUse"][0]["hooks"]
        self.assertEqual(hook["command"], f"{sys.executable} -X utf8 {deny_hint.__file__}")
        given = {"hook_event_name": "PreToolUse", "cwd": self.work, "tool_name": "Bash",
                 "tool_input": {"command": "ls"}, "permission_mode": "bypassPermissions"}
        r = subprocess.run([*SHELL, hook["command"]], input=json.dumps(given), capture_output=True, text=True,
                           env=self.codex_env("nobody"), timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        spec = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(spec["permissionDecision"], "deny")
        self.assertIn("rules could not be read", spec["permissionDecisionReason"])

    def test_a_failure_at_import_is_a_deny_too(self):
        """The hook out of its core (a half-updated one): its import fails before run() — on Codex a deny with exit 0,
        not a traceback with code 1, on which Codex runs the call; with Claude — as before, an error."""
        alone = os.path.join(self.state, "alone", "hooks")
        os.makedirs(alone)
        shutil.copy(deny_hint.__file__, alone)
        given = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}})
        r = subprocess.run([sys.executable, os.path.join(alone, "deny_hint.py")], input=given, capture_output=True,
                           text=True, env=self.codex_env(), timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        spec = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(spec["permissionDecision"], "deny")
        self.assertIn("ModuleNotFoundError", spec["permissionDecisionReason"])
        claude = {k: v for k, v in os.environ.items() if k != "CODEX_HOME"}
        r = subprocess.run([sys.executable, os.path.join(alone, "deny_hint.py")], input=given, capture_output=True,
                           text=True, env=claude, timeout=30)
        self.assertEqual((r.returncode, r.stdout), (0, ""))   # no rule's word in the call: it ends before the import
        r = subprocess.run([sys.executable, os.path.join(alone, "deny_hint.py")], input=given.replace("ls", "pkill x"),
                           capture_output=True, text=True, env=claude, timeout=30)
        self.assertEqual((r.returncode, r.stdout), (1, ""))

    def hook_run(self, env, tool="Bash", command_text="pgrep -l systemd"):
        """deny_hint.py the way both hosts call it: (exit code, decision from the JSON, stderr)."""
        hook = {"hook_event_name": "PreToolUse", "cwd": self.work, "tool_name": tool,
                "tool_input": {"command": command_text}, "permission_mode": "bypassPermissions"}
        r = subprocess.run([sys.executable, deny_hint.__file__], input=json.dumps(hook), capture_output=True,
                           text=True, env=env, timeout=30)
        decision = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] if r.stdout.strip() else None
        return r.returncode, decision, r.stderr

    def codex_env(self, name="maps"):
        return {**os.environ, "HUB_STATE_DIR": self.state, "HUB_SESSION": name,
                "CODEX_HOME": os.path.join(self.state, "codex-home")}

    def test_codex_without_rules_file_refuses_every_call(self):
        """Pilot 2026-10-19: Codex blocks only on code 2 or deny — if the mini-hub's rules could not be read, that is a
        refusal, not a silent "no rules" (otherwise the mini-hub edits its own folder and commits). A deny with exit 0:
        Codex on Windows let a hook's exit 2 through (winstand, 2026-11-05)."""
        env = self.codex_env()
        code, decision, err = self.hook_run(env, "Bash", "ls")
        self.assertEqual((code, decision), (0, "deny"), err)
        self.assertIn("rules could not be read", err)
        os.makedirs(os.path.join(self.state, "sessions", "maps"))
        with open(os.path.join(self.state, "sessions", "maps", codex_rules.RULES), "w") as f:
            f.write("{not json")
        self.assertEqual(self.hook_run(env, "apply_patch", patch("*** Add File: x.txt", "+x"))[:2], (0, "deny"))
        with open(os.path.join(self.state, "sessions", "maps", codex_rules.RULES), "w") as f:
            json.dump({"deny": [123]}, f)   # a broken list: a rule is not a string
        self.assertEqual(self.hook_run(env, "apply_patch", patch("*** Add File: x.txt", "+x"))[:2], (0, "deny"))
        codex_rules.thread({}, name="maps", cwd=self.work, state_dir=self.state)   # not a mini-hub: empty rules
        self.assertEqual(self.hook_run(env, "Bash", "ls")[:2], (0, None))

    def test_a_session_in_a_codex_pool_account_folder_is_on_codex_too(self):
        """A session of the Codex account pool has its account's folder as CODEX_HOME, not <state>/codex-home: its rules
        hold all the same (a mini-hub's, and no rules file is a refusal), and a hook that fails before run() refuses."""
        pool = os.path.join(self.state, "codex-pool", "acct-2")
        os.makedirs(pool)
        env = {**self.codex_env(), "CODEX_HOME": pool, **ON_CODEX_ENV}   # what the host gives (hosts/codex._env)
        code, decision, err = self.hook_run(env, "Bash", "ls")
        self.assertEqual((code, decision), (0, "deny"), err)
        self.assertIn("rules could not be read", err)
        codex_rules.thread({}, name="maps", minihub=True, cwd=self.work, state_dir=self.state,
                           agent_file=os.path.join(self.state, "agents", "maps", "CLAUDE.md"))
        self.assertEqual(self.hook_run(env, "Bash", "git commit -m x")[:2], (0, "deny"))
        self.assertEqual(self.hook_run(env, "apply_patch", patch("*** Add File: " + os.path.join(self.work, "a.txt"),
                                                                 "+x"))[:2], (0, "deny"))
        self.assertEqual(self.hook_run(env, "Bash", "ls")[:2], (0, None))
        alone = os.path.join(self.state, "alone", "hooks")
        os.makedirs(alone)
        shutil.copy(deny_hint.__file__, alone)
        given = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}})
        r = subprocess.run([sys.executable, os.path.join(alone, "deny_hint.py")], input=given, capture_output=True,
                           text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_the_core_model_gets_no_tool_at_all(self):
        """The core's own model call (core/codexmodel.ask: the watcher, push, voice, the recap) is a thread whose input is
        other people's text and which only answers: HUB_CORE_MODEL=1 refuses every tool, the reason names the core's
        model — whatever rules file there is (here one of an ordinary session that would let `ls` through), and with no
        session and no state folder at all (the call has neither)."""
        os.makedirs(os.path.join(self.state, "sessions", "maps"))
        codex_rules.thread({}, name="maps", cwd=self.work, state_dir=self.state)   # not a mini-hub: nothing denied
        env = {**self.codex_env(), **ON_CODEX_ENV}
        self.assertEqual(self.hook_run(env, "Bash", "ls")[:2], (0, None))   # an ordinary Codex session: let through
        bare = {k: v for k, v in env.items() if k not in ("HUB_SESSION", "HUB_STATE_DIR")}
        for e in ({**env, "HUB_CORE_MODEL": "1"}, {**bare, "HUB_CORE_MODEL": "1"}):
            for tool, text in (("Bash", "ls"), ("exec_command", "ls"), ("apply_patch", patch("*** Add File: x.txt", "+x")),
                               ("view_image", "x.png"), ("web_search", "q"), ("update_plan", "p"),
                               ("mcp__srv__tool", "{}"), ("Task", "x")):
                hook = {"hook_event_name": "PreToolUse", "cwd": self.work, "tool_name": tool,
                        "tool_input": {"command": text}}
                r = subprocess.run([sys.executable, deny_hint.__file__], input=json.dumps(hook), capture_output=True,
                                   text=True, env=e, timeout=30)
                out = json.loads(r.stdout)["hookSpecificOutput"]
                self.assertEqual((r.returncode, out["permissionDecision"]), (0, "deny"), (tool, r.stderr))
                self.assertIn("core's model", out["permissionDecisionReason"], tool)
        # not Codex's call (a stray variable in a Claude session's environment): the hook does not refuse it
        claude = {k: v for k, v in os.environ.items() if k not in ("HUB_ON_CODEX", "CODEX_HOME")}
        self.assertEqual(self.hook_run({**claude, "HUB_CORE_MODEL": "1"}, "Bash", "ls")[:2], (0, None))

    def test_codex_without_the_state_folder_is_a_refusal_not_silence(self):
        """The mark says the call is Codex's; no HUB_STATE_DIR — the rules cannot be found: a refusal."""
        env = {**{k: v for k, v in self.codex_env().items() if k != "HUB_STATE_DIR"}, **ON_CODEX_ENV}
        code, decision, err = self.hook_run(env, "Bash", "ls")
        self.assertEqual((code, decision), (0, "deny"), err)

    def test_the_host_marks_its_session_whatever_the_folder_is(self):
        from hosts.codex import CodexHost
        host = CodexHost(self.state, codex=(sys.executable, FAKE_CODEX), env={}, all_sessions=True)
        pool = os.path.join(self.state, "codex-pool", "acct-2")
        for home in (None, pool):
            env = host._env("maps", None, {}, home=home)
            self.assertEqual(env["CODEX_HOME"], home or host.home)
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertTrue(deny_hint.on_codex())

    def test_a_claude_session_is_not_marked_even_when_the_hubs_environment_is(self):
        """The mark of a Codex session that leaked into a child (a nested `claude -p`, an installer) is not passed on
        to a Claude session: its hook would look for a rules file that is not there and refuse every call."""
        from hosts.streamjson import StreamHost
        host = StreamHost(self.state, claude=(sys.executable,))   # not run: only its environment
        with mock.patch.dict(os.environ, {"HUB_ON_CODEX": "1", "HUB_CORE_MODEL": "1"}):   # and the core's model call's mark
            self.assertNotIn("HUB_ON_CODEX", host.launch_env("maps"))
            self.assertNotIn("HUB_CORE_MODEL", host.launch_env("maps"))

    def test_a_claude_session_with_a_stray_codex_home_is_not_codex(self):
        """The owner's environment may carry a CODEX_HOME of its own: with Claude the hook must stay silent, not refuse
        every call for a rules file that was never written."""
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_SESSION": "maps",
               "CODEX_HOME": os.path.join(self.state, "personal-codex")}
        env.pop("HUB_ON_CODEX", None)
        self.assertEqual(self.hook_run(env, "Bash", "ls")[:2], (0, None))
        self.assertEqual(self.hook_run(env, "Bash", "pgrep -l systemd")[:2], (0, "deny"))

    def test_claude_session_needs_no_rules_file(self):
        """On Claude the mini-hub's rules are held by --disallowedTools, there is no file — the hook stays silent, as
        before; a leftover file (the session was on Codex) is not read on Claude."""
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_SESSION": "maps"}
        env.pop("CODEX_HOME", None)
        self.assertEqual(self.hook_run(env, "Bash", "ls")[:2], (0, None))
        self.assertEqual(self.hook_run(env, "Bash", "pgrep -l systemd")[:2], (0, "deny"))
        codex_rules.thread({}, name="maps", minihub=True, cwd=self.work, state_dir=self.state,
                           agent_file=os.path.join(self.state, "agents", "maps", "CLAUDE.md"))
        self.assertEqual(self.hook_run(env, "Bash", "git commit -m x")[:2], (0, None))

    def test_hook_failure_is_a_refusal(self):
        """A failure of the hook itself (an exception) is a deny with exit 0 and the reason: Codex runs the call on code 1
        (pilot 2026-10-19) and, on Windows, on code 2 too (winstand, 2026-11-05); Claude also holds permissions.deny."""
        import io
        from unittest import mock
        hook = json.dumps({"tool_name": "Bash", "tool_input": {"command": "pgrep x"}})
        home = tempfile.mkdtemp(prefix="hubcxh-")
        codex = {"HUB_STATE_DIR": home, "CODEX_HOME": os.path.join(home, "codex-home"), "HUB_SESSION": "maps"}
        for boom in (RuntimeError("broke"), AttributeError("x"), KeyError("y")):
            with mock.patch.dict(os.environ, codex), mock.patch.object(deny_hint, "reason", side_effect=boom), \
                    mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(hook.encode()))), \
                    mock.patch.object(sys, "stdout", io.StringIO()) as out, \
                    mock.patch.object(sys, "stderr", io.StringIO()) as err:
                self.assertEqual(deny_hint.run(), 0, boom)
            spec = json.loads(out.getvalue())["hookSpecificOutput"]
            self.assertEqual(spec["permissionDecision"], "deny")
            self.assertIn("the call was not run", spec["permissionDecisionReason"])
            self.assertIn("the call was not run", err.getvalue())
        for broken in ("", "{", "[1]"):   # input that does not parse — a refusal too
            with mock.patch.dict(os.environ, codex), \
                    mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(broken.encode()))), \
                    mock.patch.object(sys, "stdout", io.StringIO()) as out, \
                    mock.patch.object(sys, "stderr", io.StringIO()):
                self.assertEqual(deny_hint.run(), 0, broken)
            self.assertEqual(json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny", broken)
        # with Claude (no hub CODEX_HOME) the hook stays as before: input that does not parse — silence, code 0; its
        # deny rules hold the bans
        clean = {k: v for k, v in os.environ.items() if k != "CODEX_HOME"}
        for broken in ("", "{", "[1]"):
            with mock.patch.dict(os.environ, clean, clear=True), \
                    mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(broken.encode()))), \
                    mock.patch.object(sys, "stdout", io.StringIO()) as out:
                self.assertEqual(deny_hint.run(), 0, broken)
            self.assertEqual(out.getvalue(), "")


CMD = {"threadId": "t", "turnId": "u", "itemId": "call_1", "startedAtMs": 1, "kind": "command",
       "reason": "needs to write outside the folder", "command": "/bin/bash -lc 'echo X > /home/x/out.txt'",
       "cwd": "/home/x/work", "commandActions": [{"type": "unknown", "command": "echo X > /home/x/out.txt"}],
       "availableDecisions": ["accept", "cancel"]}
PATCH_ITEM = {"type": "fileChange", "id": "call_2", "status": "inProgress",
              "changes": [{"path": "/home/x/work/notes.txt", "kind": {"type": "add"}, "diff": "hello\n"}]}


class CardTest(unittest.TestCase):
    """Codex's approval requests (schema 0.162.1) are permission cards, like Claude Code's can_use_tool; the owner's
    answer goes back as Codex's decision."""

    def card(self, proc, method, params, rid=7):
        facts = codex_rules.request(proc, "maps", rid, method, params)
        (fact,) = facts
        self.assertEqual(fact[:2], ("permission.requested", "maps"))
        self.assertIn(fact[2]["request_id"], proc.asks)
        return fact[2]

    def answer(self, proc, data, behavior, message=None):
        rid, method, kept = proc.asks.pop(data["request_id"])
        response = {"behavior": behavior, **({"message": message} if message else {}),
                    **({"updatedInput": data["input"]} if behavior == "allow" else {})}
        return codex_rules.answer_request(proc, method, kept, response)

    def test_a_command_approval_is_a_bash_card(self):
        proc = Proc()
        data = self.card(proc, "item/commandExecution/requestApproval", CMD)
        self.assertEqual((data["tool"], data["interactive"]), ("Bash", False))
        self.assertEqual(data["input"]["command"], "echo X > /home/x/out.txt")   # what the model wrote, not the wrapper
        self.assertEqual(data["input"]["cwd"], "/home/x/work")
        self.assertEqual(data["why"], "needs to write outside the folder")
        self.assertNotIn("auto", data)
        self.assertEqual(self.answer(proc, data, "allow"), {"decision": "accept"})
        data = self.card(proc, "item/commandExecution/requestApproval", CMD)
        self.assertEqual(self.answer(proc, data, "deny"), {"decision": "decline"})   # the turn goes on, as with Claude

    def test_a_file_change_card_names_the_file_and_the_diff(self):
        # the request carries only itemId; the paths and the diff came in item/started of the step (hosts/codex keeps
        # it in proc.items)
        proc = Proc(items={"call_2": PATCH_ITEM})
        data = self.card(proc, "item/fileChange/requestApproval",
                         {"threadId": "t", "turnId": "u", "itemId": "call_2", "startedAtMs": 1, "reason": None})
        self.assertEqual(data["tool"], "Edit")
        self.assertEqual(data["input"]["file_path"], "/home/x/work/notes.txt")
        self.assertEqual(data["input"]["changes"], [{"path": "/home/x/work/notes.txt", "kind": "add"}])
        self.assertIn("hello", data["input"]["diff"])
        self.assertIsNone(data["why"])
        self.assertEqual(self.answer(proc, data, "allow"), {"decision": "accept"})
        # the step is unknown (an older host): still a card, by the item id
        data = self.card(Proc(), "item/fileChange/requestApproval", {"itemId": "call_9", "reason": "x"})
        self.assertEqual((data["tool"], data["input"]), ("Edit", {"item": "call_9"}))

    def test_a_permissions_request_grants_what_was_asked_or_nothing(self):
        asked = {"network": {"enabled": True}, "fileSystem": None}
        proc = Proc()
        data = self.card(proc, "item/permissions/requestApproval",
                         {"threadId": "t", "turnId": "u", "itemId": "c", "startedAtMs": 1, "cwd": "/home/x/work",
                          "reason": "needs the network", "permissions": asked})
        self.assertEqual(data["tool"], "Permissions")
        self.assertEqual(data["input"]["permissions"], {"network": {"enabled": True}})
        self.assertEqual(self.answer(proc, data, "allow"), {"permissions": asked, "scope": "turn"})
        data = self.card(proc, "item/permissions/requestApproval", {"permissions": asked, "cwd": "/w"})
        self.assertEqual(self.answer(proc, data, "deny"), {"permissions": {}, "scope": "turn"})

    def test_legacy_approvals_too(self):
        proc = Proc()
        data = self.card(proc, "execCommandApproval", {"conversationId": "t", "callId": "c",
                                                       "command": ["bash", "-lc", "rm -r build && echo ok"],
                                                       "cwd": "/w", "reason": None, "parsedCmd": []})
        self.assertEqual((data["tool"], data["input"]["command"]), ("Bash", "bash -lc 'rm -r build && echo ok'"))
        self.assertEqual(self.answer(proc, data, "allow"), {"decision": "approved"})
        data = self.card(proc, "applyPatchApproval", {"conversationId": "t", "callId": "c", "reason": None,
                                                      "fileChanges": {"/w/a.txt": {"type": "add", "content": "x"}}})
        self.assertEqual((data["tool"], data["input"]["file_path"]), ("Edit", "/w/a.txt"))
        self.assertEqual(self.answer(proc, data, "deny"), {"decision": "denied"})

    def test_other_requests_are_not_this_seams(self):
        proc = Proc()
        self.assertIsNone(codex_rules.request(proc, "maps", 1, "mcpServer/elicitation/request", {}))
        self.assertIsNone(codex_rules.answer_request(proc, "item/tool/requestUserInput", [], {"behavior": "allow"}))
        self.assertEqual(proc.asks, {})

    def test_auto_allow_marks_the_request_for_the_core(self):
        # hub.json permissions.auto_allow: the host marks the request, the core answers yes itself (reducer), as with
        # Claude; without the setting, or without a host to ask, the card goes to the owner
        self.assertEqual(self.card(Proc(host=Host(auto=True)), "item/commandExecution/requestApproval", CMD)["auto"],
                         "allow")
        self.assertNotIn("auto", self.card(Proc(host=Host(auto=False)), "item/commandExecution/requestApproval", CMD))
        self.assertNotIn("auto", self.card(Proc(), "item/commandExecution/requestApproval", CMD))
        # plan (read-only): a request is to go beyond the mode at all — the owner's, like Claude's ExitPlanMode
        proc = Proc(host=Host(auto=True))
        proc.start_params = {"approvalPolicy": "on-request", "sandbox": "read-only"}
        self.assertNotIn("auto", self.card(proc, "item/commandExecution/requestApproval", CMD))
        self.assertNotIn("auto", self.card(proc, "item/fileChange/requestApproval", {"itemId": "x"}))
        # but its own `hub …` (the spool is outside a read-only sandbox) — by the setting, as before; not a chain
        hub = {**CMD, "commandActions": [{"type": "unknown", "command": "hub task note 'step done'"}]}
        self.assertEqual(self.card(proc, "item/commandExecution/requestApproval", hub)["auto"], "allow")
        hub = {**CMD, "commandActions": [{"type": "unknown", "command": "hub gist x; rm -r build"}]}
        self.assertNotIn("auto", self.card(proc, "item/commandExecution/requestApproval", hub))

    def test_the_owners_reason_for_a_refusal_reaches_the_turn(self):
        # Claude Code hands the deny message to the model; Codex's decision has no text: the host steers it into the
        # running turn. The core's default text (no reason given) is not steered: Codex already says "rejected"
        host = Host()
        proc = Proc(host=host)
        data = self.card(proc, "item/commandExecution/requestApproval", CMD)
        self.assertEqual(self.answer(proc, data, "deny", "write it into the project folder"), {"decision": "decline"})
        self.assertEqual(len(proc.backlog), 1)
        self.assertIn("write it into the project folder", proc.backlog[0][0]["text"])
        self.assertEqual(host.flushed, 1)
        for default in codex_rules.DEFAULT_DENIALS:   # the core's default text, today's and an earlier core's
            data = self.card(proc, "item/commandExecution/requestApproval", CMD)
            self.answer(proc, data, "deny", default)
            self.assertEqual(len(proc.backlog), 1)

    def test_the_default_refusal_text_is_the_cores(self):
        # the owner refused without a reason: the text the fold puts into the answer is one the host knows
        from core import reducer
        st = reducer.empty()
        for seq, (t, data) in enumerate([("session.requested", {"cwd": "/w", "session_id": "s"}),
                                         ("session.started", {}),
                                         ("permission.requested", {"request_id": "r1", "tool": "Bash", "input": {}}),
                                         ("permission.answered", {"request_id": "r1", "behavior": "deny"})], 1):
            st, acts, _ = reducer.reduce(st, {"seq": seq, "ts": 100 + seq, "type": t, "entity": "w", "data": data})
        (r,) = [a for a in acts if a["do"] == "respond"]
        self.assertIn(r["response"]["message"], codex_rules.DEFAULT_DENIALS)


class HubCardTest(unittest.TestCase):
    """A hub on Codex, a session in a mode that asks (the fake codex_h.py asks like codex 0.162.1): the request is a
    card in the core, the owner's answer is Codex's decision."""

    def setUp(self):
        from core.daemon import Hub
        from hosts.codex import CodexHost
        self.state = tempfile.mkdtemp(prefix="hubcxc-")
        self.host = CodexHost(self.state, codex=(sys.executable, FAKE_H), env={"FAKE_TURN_S": "0.2"},
                              all_sessions=True, default_model="gpt-def")
        self.hub = Hub(self.state, self.host, registry_dir=os.path.join(self.state, "registry"))
        self.hub.start()

    def tearDown(self):
        self.hub.host.kill_server()
        time.sleep(0.2)
        platforms.current.rmtree(self.state)

    def cli(self, type, entity=None, **data):
        from core import spool
        spool.put(os.path.join(self.state, "spool"), type, entity, data)

    def until(self, pred, timeout=15.0):
        end = time.time() + timeout
        while time.time() < end:
            self.hub.tick()
            if pred():
                return
            time.sleep(0.03)
        self.fail(f"did not wait; {self.hub.state['sessions']}")

    def sess(self):
        return self.hub.state["sessions"].get("a", {})

    def said(self):
        from core import journal
        return [e["data"]["text"] for e in journal.read(self.hub.journal.path)
                if e["type"] == "session.said" and e.get("entity") == "a"]

    def start(self, mode="default"):
        self.cli("session.requested", "a", cwd=self.state, session_id="sid-a", mode=mode)
        self.until(lambda: self.sess().get("state") == "idle")
        self.assertEqual(self.sess()["info"]["mode"], mode)

    def ask(self, text):
        self.cli("message.received", to="a", text=text, channel="web", owner=True)
        self.until(lambda: self.sess().get("pending"))   # a permission card (questions are asks)
        return self.sess()["pending"][0]

    def test_a_command_card_allowed_by_the_owner(self):
        self.start()
        ask = self.ask("/approve_cmd")
        self.assertEqual(ask["tool"], "Bash")
        self.assertEqual(ask["input"]["command"], "echo X > /home/x/out.txt")
        self.cli("permission.answered", "a", request_id=ask["request_id"], behavior="allow", channel="web", owner=True)
        self.until(lambda: any(t.startswith("decision:") for t in self.said()))
        self.assertIn('{"decision": "accept"}', self.said()[-1])

    def test_a_file_change_card_names_the_file(self):
        self.start("acceptEdits")
        ask = self.ask("/approve_patch")
        self.assertEqual(ask["tool"], "Edit")
        # by the real path, as the code reads it (macOS's TMPDIR is a link: /var → /private/var)
        self.assertEqual(os.path.realpath(ask["input"]["file_path"]),
                         os.path.realpath(os.path.join(self.state, "notes.txt")))
        self.cli("permission.answered", "a", request_id=ask["request_id"], behavior="deny",
                 message="not this file", channel="web", owner=True)
        self.until(lambda: any(t.startswith("decision:") for t in self.said()))
        got = self.said()[-1]
        self.assertIn('{"decision": "decline"}', got)
        self.assertIn("not this file", got)   # the owner's reason went into the running turn
        self.assertEqual(getattr(self.host.procs["a"], "items", {}), {})   # the turn ended: the steps are not kept

    def test_the_host_keeps_a_file_steps_paths_until_it_ends(self):
        # the steps are kept only while they can still be asked about: a finished step, a finished turn and a dead
        # process do not accumulate them
        from hosts.codex import CodexProcess

        class Popen:
            pid, stdout = 1, iter(['{"method": "x"}\n'])

            def poll(self):
                return None
        popen = Popen()
        proc = self.host.procs["b"] = CodexProcess(popen, "sid-b", False)
        try:
            self.steps(proc, popen)
        finally:
            self.host.procs.pop("b")

    def steps(self, proc, popen):
        proc.thread = "t"
        step = {"type": "fileChange", "id": "call_5", "changes": [{"path": "/w/a", "kind": {"type": "add"}}]}
        started = {"method": "item/started", "params": {"threadId": "t", "item": step}}
        self.host._line("b", started)
        self.assertEqual(proc.items, {"call_5": step})
        self.host._line("b", {"method": "item/completed", "params": {"threadId": "t", "item": step}})
        self.assertEqual(proc.items, {})
        self.host._line("b", started)
        self.host._line("b", {"method": "turn/completed", "params": {"threadId": "t", "turn": {"status": "interrupted"}}})
        self.assertEqual(proc.items, {})
        self.host._line("b", started)
        self.host.reader("b", popen)()   # its stdout is over: the process is gone (after its lines, in their order)
        self.host.poll()
        self.assertEqual(proc.items, {})

    def test_a_subagents_file_step_is_kept_with_its_own_thread(self):
        # a subagent (its own thread, slice C) asks for its file step by id too; it outlives the session's turn
        from hosts.codex import CodexProcess
        from hosts import codex_rules

        class Popen:
            pid, stdout = 1, iter([])

            def poll(self):
                return None
        proc = self.host.procs["b"] = CodexProcess(Popen(), "sid-b", False)
        try:
            proc.thread = "t"
            from hosts import codex_items
            codex_items._work(proc).parent_of["child"] = "call_6"   # the step that spawned it (slice C)
            step = {"type": "fileChange", "id": "call_7", "changes": [{"path": "/w/b", "kind": {"type": "add"}}]}
            self.host._line("b", {"method": "item/started", "params": {"threadId": "child", "item": step}})
            self.host._line("b", {"method": "turn/completed", "params": {"threadId": "t", "turn": {"status": "completed"}}})
            self.assertEqual(proc.items, {"call_7": step})
            tool, inp = codex_rules._card(proc, "item/fileChange/requestApproval", {"threadId": "child", "itemId": "call_7"})
            self.assertEqual((tool, inp["file_path"]), ("Edit", "/w/b"))
            proc.backlog = []
            codex_rules.answer_request(proc, "item/fileChange/requestApproval", {"threadId": "child", "itemId": "call_7"},
                                       {"behavior": "deny", "message": "not this one"})
            self.assertEqual(proc.backlog, [])   # Codex tells the subagent; no turn of the session for its refusal
            self.host._line("b", {"method": "turn/completed", "params": {"threadId": "child",
                                                                       "turn": {"status": "completed"}}})
            self.assertEqual(proc.items, {})
            # a subagent closed without its turn's end, and a thread the session left (/clear): their steps go too
            self.host._line("b", {"method": "item/started", "params": {"threadId": "child", "item": step}})
            self.host._line("b", {"method": "thread/closed", "params": {"threadId": "child"}})
            self.assertEqual(proc.items, {})
            self.host._line("b", {"method": "item/started", "params": {"threadId": "t", "item": step}})
            proc.thread = "t2"
            self.host._line("b", {"method": "turn/started", "params": {"threadId": "t2", "turn": {"id": "u2"}}})
            self.assertEqual(proc.items, {})
        finally:
            self.host.procs.pop("b")

    def test_auto_allow_answers_without_a_card(self):
        self.host.auto_allow_on = lambda: True
        self.start("acceptEdits")
        self.cli("message.received", to="a", text="/approve_perms", channel="web", owner=True)
        self.until(lambda: any(t.startswith("decision:") for t in self.said()))
        self.assertIn('"scope": "turn"', self.said()[-1])
        self.assertIn('"enabled": true', self.said()[-1])
        self.assertFalse(self.sess().get("pending"))


class HostTest(unittest.TestCase):
    """The Codex host with the fake: the hook is trusted — the session starts; not trusted — host.error, no process."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubcxs-")

    def tearDown(self):
        platforms.current.rmtree(self.state)

    def host(self, env=None):
        from hosts.codex import CodexHost
        return CodexHost(self.state, codex=(sys.executable, FAKE_CODEX), env=env or {}, all_sessions=True)

    def test_untrusted_hook_session_not_spawned(self):
        path = os.path.join(self.state, "replies.json")
        with open(path, "w") as f:
            json.dump({"hooks/list": {"data": []}}, f)
        host = self.host({"FAKE_CODEX_REPLIES": path})
        with self.assertRaises(RuntimeError):
            host.spawn("maps", "sid-1", cwd=self.state)
        self.assertNotIn("maps", host.procs)

    def test_trusted_hook_session_spawns_without_ask_for_bus_executor(self):
        argv = os.path.join(self.state, "argv")
        host = self.host({"FAKE_ARGV_DIR": argv})
        try:
            self.assertEqual(host.spawn("maps-1", "sid-1", cwd=self.state, bus="maps"), "spawned")
            end = time.time() + 10
            files = []
            while time.time() < end and not files:
                time.sleep(0.05)
                files = [f for f in os.listdir(argv) if f.startswith("codex-maps-1-")] if os.path.isdir(argv) else []
            with open(os.path.join(argv, files[-1])) as f:
                got = f.read()
            self.assertNotIn("default_mode_request_user_input", got)
        finally:
            host.kill_server()


if __name__ == "__main__":
    unittest.main()
