"""Codex (experimental): a repo's hook on file edits (matcher Edit|Write) on a hub's Codex session
(hooks/edit_hooks.py, hosts/codex_set.edit_groups). Codex 0.162.1 runs such a hook on apply_patch, but with the patch in
tool_input.command; a guard reading tool_input.file_path let every edit through. Now the hook gets Claude Code's calls of
the patch (the forms checked against a real claude 2.1.309, probe 2026-11-05), and its refusal refuses the patch."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

import platforms

from hooks import edit_hooks
from hosts import codex_set

HERE = os.path.dirname(__file__)
FAKE_CODEX = os.path.join(HERE, "fakes", "fake_codex.py")

# a repo's guard: writes what it got, refuses a path with "denied" (exit 2), "jsondeny" (deny), "askme" (ask); after the
# edit "fmt" is reformatted, "postblock" blocks
GUARD = r'''
import json, os, sys
d = json.loads(sys.stdin.buffer.read().decode("utf-8"))
with open(os.environ["GUARD_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(d) + "\n")
fp = (d.get("tool_input") or {}).get("file_path") or ""
ev = d.get("hook_event_name")
if ev == "PreToolUse" and "denied" in fp:
    sys.stderr.write("no writes here: " + os.path.basename(fp))
    sys.exit(2)
if ev == "PreToolUse" and "jsondeny" in fp:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": "json says no"}}))
if ev == "PreToolUse" and "askme" in fp:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                             "permissionDecisionReason": "a layer rule"}}))
if ev == "PostToolUse" and fp.endswith("fmt.txt"):
    with open(fp, "w") as f:
        f.write("FORMATTED\n")
if ev == "PostToolUse" and "postblock" in fp:
    sys.stderr.write("lint failed")
    sys.exit(2)
if d.get("tool_name") == "Bash" and "deny-me" in str((d.get("tool_input") or {}).get("command")):
    sys.stdout.write("ignored-out")
    sys.stderr.write("no bash here")
    sys.exit(2)
if d.get("tool_name") == "Bash":
    sys.stdout.write("bash-out")
    sys.exit(3)
'''


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text if isinstance(text, str) else json.dumps(text))


def patch(*ops):
    return "*** Begin Patch\n" + "".join(ops) + "*** End Patch\n"


class Calls(unittest.TestCase):
    """A patch -> Claude's calls of it."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="hubcxed-"))
        self.addCleanup(platforms.current.rmtree, self.tmp)

    def p(self, rel):
        return os.path.join(self.tmp, rel)

    def test_add_is_a_write_of_the_whole_file(self):
        got = edit_hooks.calls(patch("*** Add File: sub/new.txt\n+hello\n+world\n"), self.tmp)
        self.assertEqual(got, [("Write", {"file_path": self.p(os.path.join("sub", "new.txt")),
                                          "content": "hello\nworld\n"}, self.p(os.path.join("sub", "new.txt")))])

    def test_update_is_an_edit_per_hunk(self):
        got = edit_hooks.calls(patch("*** Update File: a.txt\n@@\n one\n-two\n+TWO\n three\n@@ def f\n-x\n+y\n"),
                               self.tmp)
        self.assertEqual([c[:2] for c in got], [
            ("Edit", {"file_path": self.p("a.txt"), "old_string": "one\ntwo\nthree", "new_string": "one\nTWO\nthree",
                      "replace_all": False}),
            ("Edit", {"file_path": self.p("a.txt"), "old_string": "x", "new_string": "y", "replace_all": False})])

    def test_a_first_hunk_without_its_header_and_an_empty_context_line(self):
        got = edit_hooks.calls(patch("*** Update File: a.txt\n a\n\n-b\n+B\n*** End of File\n"), self.tmp)
        self.assertEqual(got[0][1]["old_string"], "a\n\nb")
        self.assertEqual(got[0][1]["new_string"], "a\n\nB")

    def test_a_move_is_a_write_of_the_new_path(self):
        write(self.p("c.txt"), "sea\nshore\n")
        got = edit_hooks.calls(patch("*** Update File: c.txt\n*** Move to: d/e.txt\n@@\n-sea\n+SEA\n"), self.tmp)
        dest = self.p(os.path.join("d", "e.txt"))
        self.assertEqual(got, [("Write", {"file_path": dest, "content": "SEA\nshore\n"}, dest)])

    def test_a_move_the_old_file_does_not_give_is_an_edit_of_the_new_path(self):
        got = edit_hooks.calls(patch("*** Update File: gone.txt\n*** Move to: d.txt\n@@\n-a\n+b\n"), self.tmp)
        self.assertEqual(got, [("Edit", {"file_path": self.p("d.txt"), "old_string": "a", "new_string": "b",
                                         "replace_all": False}, self.p("d.txt"))])

    def test_after_a_move_the_new_path_with_what_is_on_disk(self):
        write(self.p("d.txt"), "SEA\n")   # c.txt is gone after the patch
        got = edit_hooks.calls(patch("*** Update File: c.txt\n*** Move to: d.txt\n@@\n-sea\n+SEA\n"), self.tmp,
                               after=True)
        self.assertEqual(got, [("Write", {"file_path": self.p("d.txt"), "content": "SEA\n"}, self.p("d.txt"))])

    def test_a_delete_and_a_moved_files_old_path_are_no_edit(self):
        # Claude deletes and renames by Bash rm/mv, which an Edit|Write hook does not see (the repo's Edit rules do,
        # hooks/repo_rules); a Write of nothing there would be refused by "no Write over a file" guards
        write(self.p("c.txt"), "sea\n")
        text = patch("*** Delete File: b.txt\n", "*** Update File: c.txt\n*** Move to: d.txt\n")
        self.assertEqual([c[:2] for c in edit_hooks.calls(text, self.tmp)], [
            ("Write", {"file_path": self.p("d.txt"), "content": "sea\n"})])

    def test_two_anchors_of_one_hunk_and_a_crlf_file(self):
        write(self.p("c.txt"), "class A:\r\n    def g():\r\n        x\r\nclass B:\r\n    def g():\r\n        x\r\n")
        text = patch("*** Update File: c.txt\n*** Move to: d.txt\n@@ class B:\n@@     def g():\n-        x\n"
                     "+        y\n")
        self.assertEqual(edit_hooks.calls(text, self.tmp), [("Write", {
            "file_path": self.p("d.txt"),
            "content": "class A:\r\n    def g():\r\n        x\r\nclass B:\r\n    def g():\r\n        y\r\n"},
            self.p("d.txt"))])
        # a trailing anchor without lines is no Edit
        self.assertEqual(len(edit_hooks.calls(patch("*** Update File: c.txt\n@@ class B:\n@@     def g():\n"
                                                    "-        x\n+        y\n@@ end\n"), self.tmp)), 1)

    def test_lines_split_as_codex_splits_them(self):
        text = patch("*** Add File: a.txt\n+key=\u2028SECRET=1\x0bx\r\n+b\n")
        self.assertEqual(edit_hooks.calls(text, self.tmp)[0][1]["content"], "key=\u2028SECRET=1\x0bx\nb\n")

    def test_a_move_seeks_each_hunk_after_its_anchor_by_whole_lines(self):
        write(self.p("c.txt"), "def first():\n    xfoo()\n    foo()\ndef second():\n    foo()\n")
        got = edit_hooks.calls(patch("*** Update File: c.txt\n*** Move to: d.txt\n@@ def second():\n-    foo()\n"
                                     "+    bar()\n"), self.tmp)
        self.assertEqual(got[-1][1]["content"], "def first():\n    xfoo()\n    foo()\ndef second():\n    bar()\n")

    def test_an_absolute_path_and_one_outside_the_folder(self):
        out = os.path.join(os.path.dirname(self.tmp), "elsewhere.txt")
        got = edit_hooks.calls(patch(f"*** Add File: {out}\n+x\n*** Add File: ../up.txt\n+y\n"), self.tmp)
        self.assertEqual([c[2] for c in got], [out, os.path.join(os.path.dirname(self.tmp), "up.txt")])

    def test_what_codex_says_it_changed(self):
        out = "Exit code: 0\nWall time: 0 seconds\nOutput:\nSuccess. Updated the following files:\nA m1.txt\nM a.txt\nD b.txt\n"
        self.assertEqual(edit_hooks.changed(out, self.tmp), {self.p("m1.txt"), self.p("a.txt")})
        self.assertIsNone(edit_hooks.changed("Exit code: 1\nOutput:\nFailed to find expected lines", self.tmp))


class Matcher(unittest.TestCase):
    # what a real claude 2.1.309 ran for a Write and a Bash call (probe 2026-11-05)
    WRITE = {"Edit, Write": True, "^Write$": True, "Write|Edit|MultiEdit": True, ".*": True, "*": True, "": True,
             None: True, "MultiEdit": False, "NotebookEdit": False, " Edit ": False, "Ed.*": False, "dit$": False,
             "Ed": False, "edit": False, "Edit|Bash": False, "FileWriteTool": False, "apply_patch": False,
             "Edit,Bash": False}
    BASH = {".*", "*", "", None, "Edit|Bash", "Edit,Bash"}

    def test_as_claude_code_decides(self):
        for m, want in self.WRITE.items():
            self.assertEqual(edit_hooks.matches(m, "Write"), want, m)
            self.assertEqual(edit_hooks.matches(m, "Bash"), m in self.BASH, m)
        self.assertTrue(edit_hooks.matches("Ed.*", "Edit"))
        self.assertFalse(edit_hooks.matches("(", "Edit"))


class Groups(unittest.TestCase):
    def h(self, cmd):
        return {"type": "command", "command": cmd, "timeout": 30}

    def groups(self, matcher, event="PreToolUse"):
        g = {"hooks": [self.h("guard.sh")], **({"matcher": matcher} if matcher is not None else {})}
        return codex_set.edit_groups({event: [g]})[event]

    def test_an_edit_group_gets_the_script_on_apply_patch(self):
        (g,) = self.groups("Edit|Write")
        self.assertEqual(g["matcher"], "apply_patch")
        (h,) = g["hooks"]
        self.assertIn("edit_hooks.py", h["command"])
        self.assertIn(edit_hooks.b64("guard.sh"), h["command"])
        self.assertIn(edit_hooks.b64('["Edit|Write"]'), h["command"])
        self.assertEqual(h["timeout"], 30 * edit_hooks.RUNS + 5)

    def test_other_names_stay_as_they_were(self):
        rest, g = self.groups("Edit|Write|Bash", "PostToolUse")
        self.assertEqual(rest, {"matcher": "Bash", "hooks": [self.h("guard.sh")]})
        self.assertEqual(g["matcher"], "apply_patch")

    def test_every_tool_or_a_regex_gets_the_script_on_every_call(self):
        for m in (None, "*", ".*", "^Write$"):
            (g,) = self.groups(m)
            self.assertNotIn("matcher", g, m)
            self.assertIn("edit_hooks.py", g["hooks"][0]["command"])

    def test_on_windows_the_line_is_of_plain_words_and_reads_back(self):
        # Codex runs a line there, built of plain words only (codex_rules._hook_line): a base64 "=" padding was no
        # plain word, the script was not put in front and the guard got Codex's patch as is (winstand, 2026-11-05)
        from unittest import mock
        from hosts import codex_rules
        exe = {"type": "command", "command": "C:\\Py314\\python.exe", "args": ["-X", "utf8", "C:\\hub\\edit_hooks.py"]}
        for cmd in ("guard.sh", "python3 /x/g.py", "a", "ab"):   # every length mod 3: with and without padding
            g = {"matcher": "Edit|Write", "hooks": [self.h(cmd)]}
            with mock.patch.object(platforms.current, "hook_command", return_value=exe):
                (out,) = codex_set.edit_groups({"PreToolUse": [g]})["PreToolUse"]
            self.assertEqual(out["matcher"], "apply_patch", cmd)
            words = out["hooks"][0]["command"].split()
            self.assertEqual(words[:4], ["C:\\Py314\\python.exe", "-X", "utf8", "C:\\hub\\edit_hooks.py"])
            self.assertTrue(all(codex_rules.PLAIN.fullmatch(w) for w in words), words)
            self.assertEqual((json.loads(edit_hooks.unb64(words[4])), edit_hooks.unb64(words[5]), words[6]),
                             (["Edit|Write"], cmd, "30"))
        self.assertEqual(edit_hooks.unb64("Z3VhcmQuc2g="), "guard.sh")   # a padded word of an earlier line

    def test_not_an_edit_group_and_not_another_event(self):
        self.assertEqual(self.groups("Bash"), [{"matcher": "Bash", "hooks": [self.h("guard.sh")]}])
        self.assertEqual(self.groups("MultiEdit"), [{"matcher": "MultiEdit", "hooks": [self.h("guard.sh")]}])
        self.assertEqual(self.groups("Edit", "PermissionRequest"), [{"matcher": "Edit", "hooks": [self.h("guard.sh")]}])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="hubcxed-"))
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "-C", self.repo, "init", "-q"], check=True, capture_output=True)
        self.guard = os.path.join(self.tmp, "guard.py")
        write(self.guard, GUARD)
        self.log = os.path.join(self.tmp, "guard.log")
        if " " in sys.executable + self.guard:
            self.skipTest("a hook command of plain words: a path with a space")
        self.command = f"{sys.executable} {self.guard}"

    def settings(self, matcher="Edit|Write", events=("PreToolUse", "PostToolUse")):
        write(os.path.join(self.repo, ".claude", "settings.json"), {"hooks": {e: [
            {"matcher": matcher, "hooks": [{"type": "command", "command": self.command}]}] for e in events}})

    def thread_hooks(self):
        extra = codex_set.collect([], [], {}, {"HOME": os.path.join(self.tmp, "home")}, cwd=self.repo)
        return extra["hooks"]

    def codex_call(self, line, event, tool_input, tool="apply_patch", response=None):
        """Run a thread hook's command as Codex does (its shell is the platform's) with Codex's input."""
        hook = {"session_id": "s", "turn_id": "t", "transcript_path": None, "cwd": self.repo,
                "hook_event_name": event, "model": "m", "permission_mode": "bypassPermissions", "tool_name": tool,
                "tool_input": tool_input, "tool_use_id": "call_1", **({"tool_response": response} if response else {})}
        p = subprocess.run(platforms.current.hook_shell(line), input=json.dumps(hook).encode("utf-8"),
                           capture_output=True, cwd=self.repo, env={**os.environ, "GUARD_LOG": self.log}, timeout=60)
        out = p.stdout.decode("utf-8")
        return p.returncode, (json.loads(out) if out.strip().startswith("{") else out), p.stderr.decode("utf-8")

    def got(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def line(self, event="PreToolUse", matcher="apply_patch"):
        hooks = self.thread_hooks()
        (g,) = [g for g in hooks[event] if g.get("matcher") == matcher]
        return g["hooks"][0]["command"]


class Run(Base):
    def test_a_guard_gets_claudes_calls_and_its_refusal_refuses_the_patch(self):
        self.settings()
        write(os.path.join(self.repo, "a.txt"), "one\ntwo\n")
        text = patch("*** Add File: new.txt\n+hello\n", "*** Update File: a.txt\n@@\n one\n-two\n+TWO\n",
                     "*** Add File: denied.txt\n+x\n", "*** Delete File: old.txt\n")
        code, out, _ = self.codex_call(self.line(), "PreToolUse", {"command": text})
        self.assertEqual(code, 0)
        spec = out["hookSpecificOutput"]
        self.assertEqual(spec["permissionDecision"], "deny")
        self.assertIn("no writes here: denied.txt", spec["permissionDecisionReason"])
        self.assertIn(os.path.join(self.repo, "denied.txt"), spec["permissionDecisionReason"])
        got = self.got()
        self.assertEqual([(g["tool_name"], g["tool_input"]) for g in got], [
            ("Write", {"file_path": os.path.join(self.repo, "new.txt"), "content": "hello\n"}),
            ("Edit", {"file_path": os.path.join(self.repo, "a.txt"), "old_string": "one\ntwo",
                      "new_string": "one\nTWO", "replace_all": False}),
            ("Write", {"file_path": os.path.join(self.repo, "denied.txt"), "content": "x\n"})])
        self.assertEqual({k: got[0][k] for k in ("hook_event_name", "cwd", "session_id", "tool_use_id",
                                                 "permission_mode")},
                         {"hook_event_name": "PreToolUse", "cwd": self.repo, "session_id": "s",
                          "tool_use_id": "call_1", "permission_mode": "bypassPermissions"})

    def test_an_indented_file_line_is_refused_unread(self):
        # release review 2026-11-09: Codex trims the spaces before a file line, so after an Add the indented Update is
        # applied while this parser took it for the add's text — the guard would never see b.txt
        self.settings()
        for text in (patch("*** Add File: ok.txt\n+x\n  *** Update File: b.txt\n@@\n-b\n+B\n"),
                     patch("*** Delete File: gone.txt\n  *** Update File: b.txt\n@@\n-b\n+B\n")):
            code, out, _ = self.codex_call(self.line(), "PreToolUse", {"command": text})
            self.assertEqual(code, 0)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny", text)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"], edit_hooks.UNREAD)

    def test_a_patch_the_guard_lets_through_has_no_decision(self):
        self.settings()
        code, out, _ = self.codex_call(self.line(), "PreToolUse", {"command": patch("*** Add File: ok.txt\n+x\n")})
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(len(self.got()), 1)

    def test_a_json_deny_and_an_ask_refuse_too(self):
        self.settings()
        text = patch("*** Add File: jsondeny.txt\n+x\n*** Add File: askme.txt\n+y\n")
        _, out, _ = self.codex_call(self.line(), "PreToolUse", {"command": text})
        why = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("json says no", why)
        self.assertIn("a layer rule", why)

    def test_only_the_tools_the_matcher_names(self):
        self.settings(matcher="Write")
        write(os.path.join(self.repo, "a.txt"), "one\n")
        text = patch("*** Update File: a.txt\n@@\n-one\n+ONE\n*** Add File: b.txt\n+x\n")
        self.codex_call(self.line(), "PreToolUse", {"command": text})
        self.assertEqual([g["tool_name"] for g in self.got()], ["Write"])

    def test_after_the_patch_the_changed_files_a_formatter_and_a_block(self):
        self.settings()
        for n in ("fmt.txt", "postblock.txt"):
            write(os.path.join(self.repo, n), "x\n")
        text = patch("*** Update File: fmt.txt\n@@\n-x\n+y\n", "*** Add File: postblock.txt\n+z\n",
                     "*** Add File: failed.txt\n+w\n")
        done = ("Exit code: 0\nWall time: 0 seconds\nOutput:\nSuccess. Updated the following files:\nM fmt.txt\n"
                "A postblock.txt\n")
        code, out, _ = self.codex_call(self.line("PostToolUse"), "PostToolUse", {"command": text}, response=done)
        self.assertEqual(code, 0)
        self.assertNotIn("decision", out)   # Codex's block would put the reason in place of the patch's output
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("lint failed", ctx)
        fmt = os.path.join(self.repo, "fmt.txt")
        self.assertIn(f"PostToolUse hook modified {fmt} after your edit", ctx)
        got = self.got()
        self.assertEqual([g["tool_input"]["file_path"] for g in got], [fmt, os.path.join(self.repo, "postblock.txt")])
        self.assertEqual(got[0]["tool_response"]["filePath"], fmt)
        self.assertEqual(got[0]["tool_response"]["newString"], "y")
        self.assertEqual(got[1]["tool_response"]["type"], "create")

    def test_after_a_failed_patch_no_hook(self):
        self.settings()
        got = self.codex_call(self.line("PostToolUse"), "PostToolUse", {"command": patch("*** Add File: a.txt\n+x\n")},
                              response="Exit code: 1\nOutput:\nerror: patch failed")
        self.assertEqual(got, (0, "", ""))
        self.assertEqual(self.got(), [])

    def test_every_tool_group_gets_another_tool_as_codex_gives_it(self):
        self.settings(matcher="*")
        line = self.line(matcher=None)
        code, out, _ = self.codex_call(line, "PreToolUse", {"command": "echo hi"}, tool="Bash")
        self.assertEqual((code, out), (3, "bash-out"))
        self.assertEqual(self.got()[0]["tool_input"], {"command": "echo hi"})
        self.codex_call(line, "PreToolUse", {"command": patch("*** Add File: x.txt\n+x\n")})
        self.assertEqual(self.got()[1]["tool_name"], "Write")

    def test_another_tools_refusal_by_exit_2_goes_as_a_deny(self):
        # Codex on Windows let a hook's exit 2 through (winstand, 2026-11-05); a deny with exit 0 it holds everywhere
        self.settings(matcher="*")
        code, out, _ = self.codex_call(self.line(matcher=None), "PreToolUse", {"command": "deny-me"}, tool="Bash")
        self.assertEqual(code, 0)
        spec = out["hookSpecificOutput"]
        self.assertEqual(spec["permissionDecision"], "deny")
        self.assertIn("PreToolUse:Bash hook error: [", spec["permissionDecisionReason"])
        self.assertIn("no bash here", spec["permissionDecisionReason"])
        code, out, _ = self.codex_call(self.line("PostToolUse", matcher=None), "PostToolUse", {"command": "deny-me"},
                                       tool="Bash", response="x")
        self.assertEqual((code, out), (2, "ignored-out"))   # after the call: as it was, a non-blocking error

    def test_a_regex_group_skips_what_claude_would_not_match(self):
        self.settings(matcher="^Write$")
        self.codex_call(self.line(matcher=None), "PreToolUse", {"command": "echo hi"}, tool="Bash")
        self.assertEqual(self.got(), [])

    def test_one_command_under_two_groups_runs_once_per_call(self):
        # a real claude 2.1.309 ran the same command of "Edit|Write" and "*" once (probe 2026-11-05)
        write(os.path.join(self.repo, ".claude", "settings.json"), {"hooks": {"PreToolUse": [
            {"matcher": "Edit|Write|Bash", "hooks": [{"type": "command", "command": self.command}]},
            {"matcher": "*", "hooks": [{"type": "command", "command": self.command}]}]}})
        (g,) = self.thread_hooks()["PreToolUse"]
        self.assertNotIn("matcher", g)
        self.codex_call(g["hooks"][0]["command"], "PreToolUse", {"command": patch("*** Add File: x.txt\n+x\n")})
        self.codex_call(g["hooks"][0]["command"], "PreToolUse", {"command": "echo hi"}, tool="Bash")
        self.assertEqual([d["tool_name"] for d in self.got()], ["Write", "Bash"])

    def test_the_same_command_for_other_tools_too_runs_once(self):
        write(os.path.join(self.repo, ".claude", "settings.json"), {"hooks": {"PreToolUse": [
            {"matcher": "*", "hooks": [{"type": "command", "command": self.command, "async": True}]},
            {"matcher": "Bash", "hooks": [{"type": "command", "command": self.command}]},
            {"matcher": "Read", "hooks": [{"type": "command", "command": "other"}]}]}})
        hooks = self.thread_hooks()["PreToolUse"]
        self.assertEqual([g.get("matcher") for g in hooks], [None, "Read"])
        self.assertNotIn("async", hooks[0]["hooks"][0])   # the Bash group's run blocked: so does the one run
        self.codex_call(hooks[0]["hooks"][0]["command"], "PreToolUse", {"command": "echo hi"}, tool="Bash")
        self.assertEqual(len(self.got()), 1)

    def test_a_thread_started_before_the_list_of_matchers(self):
        self.assertEqual(edit_hooks._matchers("Edit|Write"), ["Edit|Write"])
        self.assertEqual(edit_hooks._matchers(""), [None])
        self.assertEqual(edit_hooks._matchers('["Write", null]'), ["Write", None])

    def test_a_hook_that_does_not_start_is_no_refusal(self):
        self.settings(events=("PreToolUse",))
        hook = {"hook_event_name": "PreToolUse", "cwd": self.repo, "tool_name": "apply_patch",
                "tool_input": {"command": patch("*** Add File: a.txt\n+x\n")}}
        old, runs = edit_hooks.run, edit_hooks.RUNS
        edit_hooks.run, edit_hooks.RUNS = (lambda *a: (None, b"", b"No such file")), 0.5   # a run gets less than its time
        self.addCleanup(setattr, edit_hooks, "run", old)
        self.addCleanup(setattr, edit_hooks, "RUNS", runs)
        self.assertIsNone(edit_hooks.patch(hook, ["Write"], "missing", 2.0))

    def test_no_run_past_the_patchs_time(self):
        self.settings(events=("PreToolUse",))
        hook = {"hook_event_name": "PreToolUse", "cwd": self.repo, "tool_name": "apply_patch",
                "tool_input": {"command": patch("*** Add File: a.txt\n+x\n")}}
        old, edit_hooks.RUNS = edit_hooks.RUNS, 0
        self.addCleanup(setattr, edit_hooks, "RUNS", old)
        got = edit_hooks.patch(hook, ["Write"], self.command, 5.0)
        self.assertIn("did not manage to check", got["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(self.got(), [])

    def test_codex_waits_as_long_as_the_script_may_take(self):
        self.assertEqual(edit_hooks.LONGEST + edit_hooks.MARGIN, codex_set.MAX_TIMEOUT)

    def test_what_is_not_a_patch_is_refused_before_and_left_after(self):
        self.settings()
        for given in ({"command": "echo not a patch"}, {"input": patch("*** Add File: x.txt\n+x\n")}):
            code, out, _ = self.codex_call(self.line(), "PreToolUse", given)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny", given)
            self.assertEqual(self.codex_call(self.line("PostToolUse"), "PostToolUse", given,
                                             response="Success. Updated the following files:\nA x.txt\n"), (0, "", ""))
        self.assertEqual(self.got(), [])

    def test_a_run_cut_by_the_patchs_time_refuses_it(self):
        # the patch's time is RUNS hook times: a 1.5 s hook (its own limit 3 s) on two files, 2.4 s for the patch — the
        # second run gets the ~0.9 s left and is cut: not a pass of the guard, a refusal
        slow = os.path.join(self.tmp, "slow.py")
        write(slow, "import sys, time\nsys.stdin.read()\ntime.sleep(1.5)\n")
        hook = {"hook_event_name": "PreToolUse", "cwd": self.repo, "tool_name": "apply_patch",
                "tool_input": {"command": patch("*** Add File: a.txt\n+x\n*** Add File: b.txt\n+y\n")}}
        old, edit_hooks.RUNS = edit_hooks.RUNS, 0.8
        self.addCleanup(setattr, edit_hooks, "RUNS", old)
        got = edit_hooks.patch(hook, ["Write"], f"{sys.executable} {slow}", 3.0)
        self.assertEqual(got["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("did not manage to check", got["hookSpecificOutput"]["permissionDecisionReason"])
        got = edit_hooks.patch({**hook, "hook_event_name": "PostToolUse",
                                "tool_response": "Success. Updated the following files:\nA a.txt\nA b.txt\n"},
                               ["Write"], f"{sys.executable} {slow}", 3.0)
        self.assertIn(os.path.join(self.repo, "b.txt"), got["hookSpecificOutput"]["additionalContext"])

    def test_a_broken_script_line_refuses_the_patch(self):
        # a deny with exit 0, not exit 2: Codex on Windows ran a patch a hook refused by exit 2 (winstand, 2026-11-05)
        for given in ({"hook_event_name": "PreToolUse", "tool_name": "apply_patch", "tool_input": {"command": "x"}},
                      "not json"):
            p = subprocess.run([sys.executable, codex_set.EDIT_HOOK, "not-base64!", "-"],
                               input=(given if isinstance(given, str) else json.dumps(given)).encode(),
                               capture_output=True, timeout=60)
            self.assertEqual(p.returncode, 0, given)
            spec = json.loads(p.stdout)["hookSpecificOutput"]
            self.assertEqual(spec["permissionDecision"], "deny")
            self.assertIn("The repo edit hook failed", spec["permissionDecisionReason"])
            self.assertIn("The repo edit hook failed", p.stderr.decode("utf-8"))
        after = {"hook_event_name": "PostToolUse", "tool_name": "apply_patch", "tool_input": {"command": "x"}}
        p = subprocess.run([sys.executable, codex_set.EDIT_HOOK, "not-base64!", "-"], input=json.dumps(after).encode(),
                           capture_output=True, timeout=60)
        self.assertEqual((p.returncode, p.stdout), (2, b""))


class ThroughTheHost(Base):
    """A real CodexHost with the fake app-server: the hook Codex gets in thread/start refuses a patch by the guard."""

    def test_the_repos_edit_guard_refuses_a_patch_codex_would_run(self):
        from core import spool
        from core.daemon import Hub
        from hosts.codex import CodexHost
        self.settings(events=("PreToolUse",))
        state = os.path.join(self.tmp, "state")
        os.makedirs(state)
        calls = os.path.join(state, "calls.jsonl")
        host = CodexHost(state, codex=(sys.executable, FAKE_CODEX), all_sessions=True, default_model="gpt-test",
                         env={"FAKE_TURN_S": "0.2", "FAKE_CODEX_CALLS": calls})
        hub = Hub(state, host, registry_dir=os.path.join(state, "registry"))
        self.addCleanup(host.kill_server)
        hub.start()
        spool.put(os.path.join(state, "spool"), "session.requested", "a", {"cwd": self.repo, "session_id": "sid-a"})
        end = time.time() + 10
        while hub.state["sessions"].get("a", {}).get("state") != "idle" and time.time() < end:
            hub.tick()
            time.sleep(0.03)
        with open(calls, encoding="utf-8") as f:
            (start,) = [json.loads(line)["params"] for line in f if '"thread/start"' in line]
        hooks = start["config"]["hooks"]
        # Codex runs every PreToolUse group whose matcher takes apply_patch or its aliases Edit, Write (probe)
        lines = [h["command"] for g in hooks["PreToolUse"] if edit_hooks.matches(g.get("matcher"), "apply_patch")
                 or edit_hooks.matches(g.get("matcher"), "Edit") for h in g["hooks"]]
        self.assertTrue(lines)
        text = patch("*** Add File: denied.txt\n+x\n")
        verdicts = [self.codex_call(line, "PreToolUse", {"command": text})[1] for line in lines]
        self.assertTrue(any(isinstance(v, dict) and v["hookSpecificOutput"].get("permissionDecision") == "deny"
                            for v in verdicts), verdicts)
        self.assertEqual(self.got()[0]["tool_input"]["file_path"], os.path.join(self.repo, "denied.txt"))
        self.assertTrue(all(k in hooks["state"] for k in codex_set.hook_trust(
            {e: v for e, v in hooks.items() if e != "state"})))


if __name__ == "__main__":
    unittest.main()
