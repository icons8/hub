"""The refusal hook on every Bash call (hooks/deny_hint.py): a call with none of the rules' words in it ends before any
import of the hub's code (2026-11-11: 29 entries with an `if` were 29 python processes for one command Claude Code did not
parse, 20-37 s under load on Windows, the hook's timeout is 10 s)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import platforms
from core import minihub
from hooks import deny_hint
from hosts import common

HOOK = os.path.join(common.REPO, "hooks", "deny_hint.py")
ORDINARY = ("ls -la", "git status", "for f in a b; do echo $f; done", "cd /x; python3 - <<EOF\nprint(1)\nEOF",
            "python3 -m unittest tests.test_x", "grep -rn foo src | head", "echo 'hello world' > /tmp/x.txt",
            "npm run build && npm test", "git commit -m 'fix a bug'", "curl -s http://127.0.0.1:8787/api/hello")
# every shape of a refusal the hook has; each must be a call quiet() lets through to the full check
REFUSED = ("pkill -f server.py", "killall node", "pgrep -f mongod", "echo a && /usr/bin/pkill x",
           "ps aux | grep x | awk '{print $2}' | xargs kill -9", "xargs -r -n 1 /bin/kill",
           "taskkill //F //IM python.exe", "TASKKILL /F /FI \"IMAGENAME eq x\"", "wmic process where name='x' delete",
           "powershell -c \"Stop-Process -Name python\"", "pwsh -c 'spps -n python'",
           "powershell -c \"Invoke-CimMethod -MethodName Terminate\"",
           "pwsh -c \"$p.Terminate()\"", "kill 4321", "kill -9 -1", "kill $PPID", "/bin/kill -9 4322",
           "python3 -c 'import os; os.kill(4321, 9)'", "python3 - <<'EOF'\nimport os; os.killpg(4321, 15)\nEOF",
           "sleep 60", "cd x && sleep 1m 30s && ls", "for i in 1 2; do sleep 40; done", "while true; do sleep 45; done")
POWERSHELL = ("Stop-Process -Name python", "Get-Process python | Stop-Process", "spps -n python", "$p.Terminate()",
              "taskkill /F /IM python.exe", "Stop-Process -Id 4321")


# the words of the rules' regular expressions that are no trigger by themselves: a program that only runs another one, a
# suffix, or a word that matters only next to one of KEYWORDS (`xargs` before kill, `powershell -c` with Stop-Process in
# its string, `python` with os.kill, `git commit` is a mini-hub's rule: HUB_NESTED_DENY turns the early exit off)
NEUTRAL = {"cmd", "command", "commit", "delete", "elif", "else", "exe", "exec", "git", "nam", "name", "nice", "nohup",
           "powershell", "process", "processname", "push", "pwsh", "python", "smhd", "sudo", "terminate", "then", "time",
           "timeout", "xargs"}


def raw(command, tool="Bash", **before):
    return json.dumps({"hook_event_name": "PreToolUse", **before, "tool_name": tool, "tool_input": {"command": command}})


class Quiet(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda env=dict(os.environ): (os.environ.clear(), os.environ.update(env)))
        for name in ("HUB_ON_CODEX", "CODEX_HOME", "HUB_NESTED_DENY"):
            os.environ.pop(name, None)

    def test_an_ordinary_call_is_quiet(self):
        for command in ORDINARY:
            self.assertTrue(deny_hint.quiet(raw(command)), command)

    def test_no_refusal_is_hidden_by_it(self):
        """Whatever the full check refuses, quiet() does not stop: its words cover every rule."""
        with mock.patch.object(deny_hint, "hub_pids", lambda: {4321, 4322}), \
                mock.patch.object(deny_hint, "KILLS_BY_IMAGE", True):
            for command in REFUSED:
                self.assertIsNotNone(deny_hint.reason({"command": command}), command)
                self.assertFalse(deny_hint.quiet(raw(command)), command)
            for command in POWERSHELL:
                self.assertIsNotNone(deny_hint.reason({"command": command}, "PowerShell"), command)
                self.assertFalse(deny_hint.quiet(raw(command, "PowerShell")), command)

    def test_every_word_of_the_rules_is_covered(self):
        """A rule added to the hook without its word in KEYWORDS would be silenced on Claude by the early exit: every word
        in the rules' regular expressions is either covered or listed in NEUTRAL, with a reason."""
        import re
        words = set()
        for name in ("TOOL", "XARGS", "BY_IMAGE", "WRAPPER", "PS_BY_NAME", "BY_PID", "PS_BY_PID", "PY_KILL", "PYTHON",
                     "SLEEP", "GIT"):
            source = re.sub(r"\\[a-zA-Z]", " ", getattr(deny_hint, name).pattern)
            words |= {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z_-]{2,}", source)}
        self.assertIn("pkill", words)   # the extraction itself works
        loose = {w for w in words if deny_hint.quiet(raw(w))}
        self.assertEqual(loose - NEUTRAL, set(), "a rule's word the early exit does not cover (KEYWORDS)")
        self.assertEqual(NEUTRAL - words, set(), "NEUTRAL lists a word no rule has")

    def test_the_words_of_the_session_folder_are_not_the_command(self):
        """cwd and transcript_path come before tool_name: a project called sleep-tracker does not turn the exit off."""
        where = {"cwd": "/work/sleep-tracker/kill-switch", "transcript_path": "/home/x/.claude/projects/-pgrep/s.jsonl"}
        self.assertTrue(deny_hint.quiet(raw("ls", **where)))
        self.assertFalse(deny_hint.quiet(raw("sleep 60", **where)))

    def test_a_json_escape_or_the_other_hosts_is_the_full_check(self):
        self.assertTrue(deny_hint.quiet(raw("ls")))
        self.assertFalse(deny_hint.quiet('{"tool_input": {"command": "p\\u006bill x"}}'))
        os.environ["HUB_NESTED_DENY"] = json.dumps(["Edit(//x/y/**)"])
        self.assertFalse(deny_hint.quiet(raw("ls")))
        os.environ["HUB_NESTED_DENY"] = "[]"
        self.assertTrue(deny_hint.quiet(raw("ls")))
        os.environ["HUB_ON_CODEX"] = "1"
        self.assertFalse(deny_hint.quiet(raw("ls")))
        os.environ.pop("HUB_ON_CODEX")
        self.assertFalse(deny_hint.quiet(raw("*** Begin Patch", "apply_patch")))   # a guard that holds the tool, not the host


class Process(unittest.TestCase):
    """The hook as Claude Code runs it: a process fed the call on stdin."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hintquiet-")
        self.addCleanup(platforms.current.rmtree, self.state)

    def run_hook(self, command, tool="Bash", env=None, flags=()):
        base = {k: v for k, v in os.environ.items() if k not in ("HUB_ON_CODEX", "CODEX_HOME", "HUB_NESTED_DENY")}
        out = subprocess.run([sys.executable, *flags, HOOK], input=raw(command, tool).encode(), capture_output=True,
                             timeout=60, env={**base, "HUB_STATE_DIR": self.state, **(env or {})})
        self.assertEqual(out.returncode, 0, out.stderr)
        return out

    def test_a_quiet_call_imports_nothing_of_the_hub(self):
        """The saving: no platforms, no repo_rules, not even json and re."""
        out = self.run_hook("for f in a b; do echo $f; done", flags=("-X", "importtime"))
        self.assertEqual(out.stdout, b"")
        imported = out.stderr.decode("utf-8", "replace")
        for name in ("platforms", "hooks.repo_rules", "hooks.spec_guard", "json", "re"):
            self.assertNotIn(f"| {name}\n", imported, name)

    def test_a_call_with_a_word_goes_on_to_the_full_check(self):
        out = self.run_hook("ps | grep x | xargs kill -9")
        self.assertEqual(json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        out = self.run_hook("echo 'pkill x' # sleep 90")
        self.assertEqual(out.stdout, b"")

    def test_a_mini_hubs_shell_write_is_still_refused(self):
        """Its rules are not words of the command: HUB_NESTED_DENY turns the early exit off."""
        folder = os.path.join(self.state, "work")
        os.makedirs(folder)
        rules = json.dumps(minihub.deny(folder))
        out = self.run_hook(f"echo hi > {folder.replace(os.sep, '/')}/x.txt", env={"HUB_NESTED_DENY": rules})
        self.assertEqual(json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_codex_without_a_rules_file_is_a_refusal_even_for_a_quiet_call(self):
        out = self.run_hook("ls", env={"HUB_ON_CODEX": "1", "HUB_SESSION": "maps"})
        self.assertEqual(json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        out = self.run_hook("ls", env={"HUB_ON_CODEX": "1", "HUB_SESSION": "maps", "HUB_CORE_MODEL": "1"})
        self.assertEqual(json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
