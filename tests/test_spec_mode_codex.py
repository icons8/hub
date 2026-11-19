"""Spec mode on a hub's Codex session (slice 3): the same block as with Claude, held by the core's hook that stands on
every Codex call (hooks/deny_hint.py -> hooks/spec_guard.codex_reason). Codex edits by apply_patch (tool_name
apply_patch, the patch in tool_input.command): while the spec is open the patch may touch only the spec file; Delete
File and Move to are refused. The line of the prompt goes into the thread's developerInstructions. The model is not
called: the hook is run as Codex runs it, the thread is started against the fake Codex."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

import platforms
from tests import winsafe

from core import specmode
from hooks import deny_hint, spec_guard

HERE = os.path.dirname(__file__)
FAKE_CODEX = os.path.join(HERE, "fakes", "fake_codex.py")
SPEC = {"task": "d7", "path": "specs/d7/spec.md", "boss": "maps", "open": True}


def patch(*parts):
    return "*** Begin Patch\n" + "".join(parts) + "*** End Patch\n"


def add(path, text="x"):
    return f"*** Add File: {path}\n+{text}\n"


def update(path, move=None):
    return f"*** Update File: {path}\n" + (f"*** Move to: {move}\n" if move else "") + "@@\n-a\n+b\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="spec-cx-")
        self.addCleanup(platforms.current.rmtree, self.state)
        self.home = os.path.join(self.state, "codex-home")
        os.makedirs(self.home)
        specmode.write_projection(self.state, "maps-1", SPEC)
        self.path = specmode.abs_path(self.state, SPEC["path"])


class Guard(Base):
    def why(self, command, tool="apply_patch", name="maps-1", cwd="/w/x"):
        return spec_guard.codex_reason(tool, {"command": command}, self.state, name, cwd)

    def test_a_patch_may_touch_only_the_spec(self):
        self.assertIsNone(self.why(patch(add(self.path, "# Spec"))))
        self.assertIsNone(self.why(patch(update(self.path))))
        self.assertIsNone(self.why(patch(update(self.path), add(self.path))))
        why = self.why(patch(add("core/reducer.py")))
        self.assertIn("write only " + self.path, why)
        self.assertIn("hub send maps", why)
        self.assertTrue(self.why(patch(update("/w/x/core/reducer.py"))))
        # one foreign file refuses the whole patch: apply_patch is one call
        self.assertTrue(self.why(patch(add(self.path), add("/tmp/draft.md"))))
        self.assertTrue(self.why(patch(update(self.path), update("a.py"))))

    def test_an_indented_file_line_is_read_too(self):
        # release review 2026-11-09: Codex trims the spaces before a file line and applies it
        self.assertTrue(self.why(patch(add(self.path), "  *** Update File: a.py\n@@\n-a\n+b\n")))
        self.assertTrue(self.why(patch("\t*** Delete File: " + self.path + "\n")))

    def test_the_patch_paths_are_read_from_the_cwd(self):
        rel = os.path.relpath(self.path, self.state)
        self.assertIsNone(spec_guard.codex_reason("apply_patch", {"command": patch(add(rel))}, self.state, "maps-1",
                                                  self.state))
        self.assertTrue(spec_guard.codex_reason("apply_patch", {"command": patch(add(rel))}, self.state, "maps-1",
                                                os.path.join(self.state, "specs")))

    def test_delete_and_move_are_refused(self):
        self.assertTrue(self.why(patch(f"*** Delete File: {self.path}\n")))
        self.assertTrue(self.why(patch(update(self.path, move="/w/x/stolen.md"))))
        self.assertTrue(self.why(patch(update("/w/x/a.md", move=self.path))))
        self.assertTrue(self.why(patch(f"*** Delete File: a.py\n", add(self.path))))

    def test_a_patch_without_a_file_is_refused(self):
        for command in ("", "*** Begin Patch\n*** End Patch\n", "not a patch", "*** Begin Patch\n+x\n"):
            self.assertTrue(self.why(command), repr(command))
        self.assertTrue(spec_guard.codex_reason("apply_patch", {}, self.state, "maps-1", "/w/x"))
        self.assertTrue(spec_guard.codex_reason("apply_patch", {"command": ["x"]}, self.state, "maps-1", "/w/x"))

    def test_crlf_does_not_hide_a_path(self):
        self.assertIsNone(self.why(patch(add(self.path)).replace("\n", "\r\n")))
        self.assertTrue(self.why(patch(add("a.py")).replace("\n", "\r\n")))

    def test_a_link_to_the_spec_or_from_it_is_refused(self):
        link = os.path.join(self.state, "link.md")
        winsafe.symlink(self, self.path, link)
        self.assertIsNone(self.why(patch(add(link))))   # the same real file: it is the spec
        os.remove(self.path)
        winsafe.symlink(self, link, self.path)   # the spec itself turned into a link
        self.assertTrue(self.why(patch(add(self.path))))

    def test_other_tools_are_not_limited(self):
        for tool, inp in (("Bash", {"command": "sed -i s/a/b/ core/reducer.py"}), ("view_image", {"path": "/x"}),
                          ("mcp__jira__create", {}), ("request_user_input", {"questions": []})):
            self.assertIsNone(spec_guard.codex_reason(tool, inp, self.state, "maps-1", "/w/x"), tool)

    def test_accepted_opens_the_edits_at_once_no_projection_lets_it_go_a_broken_one_refuses(self):
        specmode.write_projection(self.state, "maps-1", {**SPEC, "open": False})
        self.assertIsNone(self.why(patch(add("core/reducer.py"))))
        self.assertIsNone(self.why(patch(add("core/reducer.py")), name="other"))
        with open(os.path.join(self.state, "sessions", "maps-1", specmode.PROJECTION), "w", encoding="utf-8") as f:
            f.write("{")
        self.assertIn("cannot be read", self.why(patch(add(self.path))))
        self.assertIn("cannot be read", self.why(patch(add(self.path)), name=None))


class Hook(Base):
    """The core's hook as Codex runs it: deny_hint.py with the hub's CODEX_HOME and a hook JSON on stdin."""

    def setUp(self):
        super().setUp()
        from hosts import codex_rules
        codex_rules.thread({}, name="maps-1", state_dir=self.state)   # the session's rules file, as the host writes it

    def run_hook(self, tool, tool_input, cwd=None, codex=True, session="maps-1"):
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_SESSION": session}
        if codex:
            env["CODEX_HOME"] = self.home
        else:
            env.pop("CODEX_HOME", None)
        hook = {"hook_event_name": "PreToolUse", "cwd": cwd or self.state, "tool_name": tool, "tool_input": tool_input}
        r = subprocess.run([sys.executable, deny_hint.__file__], input=json.dumps(hook), capture_output=True,
                           text=True, encoding="utf-8", env=env, timeout=60)
        out = json.loads(r.stdout)["hookSpecificOutput"] if r.stdout.strip() else {}
        return r.returncode, out.get("permissionDecision"), out.get("permissionDecisionReason")

    def test_the_core_hook_refuses_a_patch_beyond_the_spec(self):
        code, decision, why = self.run_hook("apply_patch", {"command": patch(add("core/reducer.py"))})
        self.assertEqual((code, decision), (0, "deny"))
        self.assertIn("write only " + self.path, why)
        self.assertEqual(self.run_hook("apply_patch", {"command": patch(add(self.path))}), (0, None, None))
        self.assertEqual(self.run_hook("apply_patch", {"command": patch(f"*** Delete File: {self.path}\n")})[:2],
                         (0, "deny"))

    def test_an_account_folder_of_the_codex_pool_is_held_too(self):
        # the hook's other rules once looked at CODEX_HOME == <state>/codex-home (deny_hint.on_codex); a session on a pool
        # account has its own folder — the spec block must not depend on it
        pool = os.path.join(self.state, "codex-pool", "acct-2")
        os.makedirs(pool)
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_SESSION": "maps-1", "CODEX_HOME": pool}
        hook = {"hook_event_name": "PreToolUse", "cwd": self.state, "tool_name": "apply_patch",
                "tool_input": {"command": patch(add("core/reducer.py"))}}
        r = subprocess.run([sys.executable, deny_hint.__file__], input=json.dumps(hook), capture_output=True,
                           text=True, encoding="utf-8", env=env, timeout=60)
        self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_a_path_or_a_cwd_the_hook_cannot_resolve_is_a_refusal(self):
        self.assertTrue(spec_guard.codex_reason("apply_patch", {"command": patch(add("a\x00b"))}, self.state, "maps-1",
                                                "/w/x"))
        self.assertIsNone(spec_guard.codex_reason("apply_patch", {"command": patch(add(self.path))}, self.state,
                                                  "maps-1", 5))   # a cwd that is not a string: the absolute path holds
        self.assertTrue(spec_guard.codex_reason("apply_patch", ["not", "a", "dict"], self.state, "maps-1", "/w/x"))

    def test_the_patch_regexes_read_a_crlf_patch_alike(self):
        from hooks import repo_rules
        text = patch(add("core/a.py"), update("core/b.py", move="core/c.py")).replace("\n", "\r\n")
        self.assertEqual(repo_rules.PATCH_PATH.findall(text), ["core/a.py", "core/b.py", "core/c.py"])
        self.assertEqual([p for _, p in repo_rules.PATCH_FILE.findall(text)], ["core/a.py", "core/b.py", "core/c.py"])

    def test_the_rest_goes_and_the_other_hosts_hook_does_not_read_it(self):
        self.assertEqual(self.run_hook("Bash", {"command": "ls"}), (0, None, None))
        # with Claude the guard is its own settings file (hosts/common.write_hook_settings) and Claude has no apply_patch:
        # a Bash call of a session in spec mode is not limited by deny_hint
        self.assertEqual(self.run_hook("Bash", {"command": "sed -i s/a/b/ a.py"}, codex=False), (0, None, None))
        # Claude never calls apply_patch, so the guard does not look at the host: the name is what it holds
        self.assertEqual(self.run_hook("apply_patch", {"command": patch(add("a.py"))}, codex=False)[:2], (0, "deny"))

    def test_a_broken_projection_is_a_refusal_with_the_exit_zero(self):
        with open(os.path.join(self.state, "sessions", "maps-1", specmode.PROJECTION), "w", encoding="utf-8") as f:
            f.write("{")
        self.assertEqual(self.run_hook("apply_patch", {"command": patch(add(self.path))})[:2], (0, "deny"))

    def test_the_spec_after_accept_is_not_held(self):
        specmode.write_projection(self.state, "maps-1", {**SPEC, "open": False})
        self.assertEqual(self.run_hook("apply_patch", {"command": patch(add("core/reducer.py"))}), (0, None, None))


class Thread(unittest.TestCase):
    """The prompt: an executor in spec mode gets the line in its thread, its raiser the rule; the others — as before."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="spec-cxt-")
        self.addCleanup(platforms.current.rmtree, self.state)
        self.calls = os.path.join(self.state, "calls.jsonl")

    def started(self, name, **kw):
        from hosts.codex import CodexHost
        host = CodexHost(self.state, codex=(sys.executable, FAKE_CODEX), env={"FAKE_CODEX_CALLS": self.calls},
                         all_sessions=True, default_model="gpt-def")
        try:
            self.assertEqual(host.spawn(name, f"sid-{name}", **kw), "spawned")
            end, start = time.time() + 10, []
            while time.time() < end and not start:
                time.sleep(0.05)
                if os.path.exists(self.calls):
                    with open(self.calls, encoding="utf-8") as f:
                        start = [c for c in map(json.loads, f) if c["method"] == "thread/start"]
            (call,) = start
            return call["params"]["developerInstructions"]
        finally:
            host.kill_server()

    def test_the_executor_gets_the_spec_line_with_its_path(self):
        text = self.started("maps-1", executor_of="maps", cwd=self.state, spec=SPEC)
        self.assertIn("Spec mode (task d7)", text)
        self.assertIn(os.path.join(self.state, "specs", "d7", "spec.md"), text)

    def test_after_accept_and_for_an_ordinary_session_there_is_no_line(self):
        self.assertNotIn("Spec mode", self.started("maps-1", executor_of="maps", cwd=self.state,
                                                  spec={**SPEC, "open": False}))

    def test_the_raiser_gets_the_rule(self):
        self.assertIn("Specs are on for you", self.started("maps", cwd=self.state, spec_boss=True))


if __name__ == "__main__":
    unittest.main()
