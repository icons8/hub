"""The settings the hub starts sessions with (`claude --settings`): the core's hooks and a ban on killing processes by
pattern. 2026-10-05: `pkill -f server.py -u <name>` on macOS killed every process with the user name in its command
line - there, options after the pattern are not parsed, so `-u` and the name became two more patterns (PR #26).
`kill $(pgrep -f ...)` does the same damage. Checked on a live claude in bypassPermissions: deny holds, including
inside `$(...)` and after `&&`; the full path is a separate rule."""
import tests  # noqa: F401 - ignore the machine's settings
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

import platforms
from platforms import posix, windows
from hosts import common


class SessionSettings(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        with open(common.write_hook_settings(os.path.join(self.dir, "s.json"))) as f:
            self.settings = json.load(f)

    def test_killing_by_pattern_is_denied(self):
        deny = self.settings["permissions"]["deny"]
        for tool in ("pkill", "killall", "pgrep"):
            for path in ("", "/usr/bin/", "/bin/"):
                self.assertIn(f"Bash({path}{tool}:*)", deny)

    def test_hooks_stay(self):
        self.assertEqual(set(self.settings["hooks"]), set(common.HOOK_EVENTS) | {"PreToolUse"})

    def test_denied_call_gets_a_hint_hook(self):
        """2026-10-16: Claude Code's bare refusal ("Permission ... has been denied") does not say what to do instead, so
        sessions tried again (165 refusals in 92 sessions over 11 days). A PreToolUse hook refuses before deny and with a
        reason; checked on live claude 2.1.218 and 2.1.296: the reason reaches the model. deny stays - without the hook the
        ban is the same. One entry for all Bash calls, with no `if` (2026-11-11): a command Claude Code did not parse goes to
        every entry with an `if`, 29 python processes on Windows past the 10 s timeout - the hook itself ends at once when
        no rule's word is in the call (hooks/deny_hint.quiet)."""
        entries = self.settings["hooks"]["PreToolUse"]
        bash = [h for h in entries if h["matcher"] == "Bash"]
        self.assertEqual(len(bash), 1)
        self.assertNotIn("if", bash[0]["hooks"][0])
        # the PowerShell tool (Windows): the same hook on every call
        self.assertEqual([h["matcher"] for h in entries if h not in bash], ["PowerShell"])
        for h in entries:
            self.assertIn("deny_hint.py", json.dumps(h["hooks"][0]))

    def hook(self, command, tool="Bash", **more):
        # the hub state is temporary: the hook reads the hub's pid from HUB_STATE_DIR/hub-up.json, a live hub is untouched
        env = {**os.environ, "HUB_STATE_DIR": self.dir}
        out = subprocess.run([sys.executable, os.path.join(common.REPO, "hooks", "deny_hint.py")], capture_output=True,
                             input=json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool,
                                               "tool_input": {"command": command, **more}}).encode(), timeout=30,
                             env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout) if out.stdout.strip() else None

    def test_hint_says_how_to_find_and_kill_own_pid(self):
        for command in ("pgrep -f mongod", "kill $(pgrep -f server.py)", "echo a && /usr/bin/pkill x",
                        "killall node", "ls; pkill -f x -u me", "cd /srv\npkill -f server.py",
                        "if true; then pgrep x; fi", "{ pkill x; }", "! pgrep -q x", "for p in a; do pkill $p; done",
                        "timeout 5 pkill -f x", "sudo pkill x", "cat <<EOF\nbody\nEOF\npkill x", "ls # x\npgrep y"):
            got = self.hook(command)["hookSpecificOutput"]
            self.assertEqual((got["hookEventName"], got["permissionDecision"]), ("PreToolUse", "deny"), command)
            self.assertEqual(got["permissionDecisionReason"], platforms.current.KILL_HINT)
        self.assertIn("ps -axo pid,command | grep", posix.KILL_HINT)
        self.assertIn("kill <pid>", posix.KILL_HINT)
        self.assertIn("Get-CimInstance Win32_Process", windows.KILL_HINT)
        self.assertIn("Stop-Process -Id <pid>", windows.KILL_HINT)

    def test_killing_by_image_name_is_denied(self):
        """A session stopped its http server with `taskkill //F //IM python.exe` and killed the whole hub: the core, the
        page, the bot, the schedule and hub-up itself are python too. Killing by image name or pattern on Windows
        (taskkill /IM and /FI, Stop-Process -Name, Get-Process ... | Stop-Process, wmic ... delete) gets the same
        refusal as pkill: by pid only. taskkill and wmic exist only on Windows: on Mac and Linux they are not parsed
        (`KILLS_BY_IMAGE` in the hook); PowerShell (pwsh exists there too) and `xargs kill` work on any OS."""
        from unittest import mock
        from hooks import deny_hint
        # on any OS: a pid from a pattern search
        for command in ("ps aux | grep x | awk '{print $2}' | xargs -n 1 kill", "pgrep x | xargs sudo kill",
                        "ps | grep x | xargs -I {} kill -9 {}", "ps | grep x | xargs /bin/kill",
                        "ps -W | grep python | awk '{print $1}' | xargs kill -9", "ps | grep x | xargs -r taskkill //F //PID"):
            got = self.hook(command)
            self.assertIsNotNone(got, command)
            self.assertEqual(got["hookSpecificOutput"]["permissionDecisionReason"], platforms.current.KILL_HINT,
                             command)
        windows_only = ["cd /c/w; cp a b; taskkill //F //IM python.exe >/dev/null 2>&1; hub send-file a x",
                        "taskkill /F /IM python.exe /T", "taskkill //IM pythonw.exe", "taskkill -im node.exe -f",
                        "TASKKILL /im py*", "/c/Windows/System32/taskkill.exe //F //IM python.exe",
                        "taskkill //F //FI \"IMAGENAME eq python*\"", "ls && taskkill //f //im python.exe || true",
                        "wmic process where \"name='python.exe'\" delete",
                        "wmic process where name=\"python.exe\" call terminate",
                        "wmic.exe process where name=\"python.exe\" delete"]
        # a PowerShell script line from Bash (pwsh exists on Mac and Linux too): on any OS
        wrapped = ["cmd //c \"taskkill /F /IM python.exe\"", "cmd.exe /c taskkill /im python.exe",
                   "powershell -Command \"Stop-Process -Name python -Force\"",
                   "powershell.exe -NoProfile -c 'Stop-Process -Name python*'", "pwsh -c \"spps -n pythonw\"",
                   "powershell -c \"Get-Process python | Stop-Process -Force\"",
                   "powershell -c \"gps py* | kill\"", "powershell -c \"kill -Name python\"",
                   "powershell -c \"Get-CimInstance Win32_Process -Filter \\\"Name='python.exe'\\\" | "
                   "Invoke-CimMethod -MethodName Terminate\""]
        for on_windows in (True, False):
            with mock.patch.object(deny_hint, "KILLS_BY_IMAGE", on_windows):
                for command in windows_only:
                    self.assertEqual(deny_hint.reason({"command": command}),
                                     platforms.current.KILL_HINT if on_windows else None, command)
                for command in wrapped:
                    self.assertEqual(deny_hint.reason({"command": command}), platforms.current.KILL_HINT, command)
        for hint in (posix.KILL_HINT, windows.KILL_HINT):
            self.assertIn("that is how the hub dies", hint)
        for word in ("taskkill /IM", "Stop-Process -Name", "remember the PID"):
            self.assertIn(word, windows.KILL_HINT)
        # the PowerShell tool: the same parsing, the command is a PowerShell script right away
        for command in ("Stop-Process -Name python -Force", "Get-Process python* | Stop-Process",
                        "taskkill /F /IM python.exe"):
            for on_windows in (True, False):
                with mock.patch.object(deny_hint, "KILLS_BY_IMAGE", on_windows):
                    self.assertEqual(deny_hint.reason({"command": command}, "PowerShell"),
                                     platforms.current.KILL_HINT, command)

    def test_killing_the_hub_by_pid_is_denied(self):
        """By pid is allowed, but not the hub's own process: hub-up, its supervisor and the core (hub-up.json)."""
        from hooks import deny_hint
        with open(os.path.join(self.dir, "hub-up.json"), "w") as f:
            json.dump({"pid": 4321, "service": None, "core": 4322, "parent": 4320}, f)
        for command in ("taskkill //F //PID 4322", "kill -9 4321", "kill 5555 4321", "taskkill /PID 4320 /T /F",
                        "powershell -c \"Stop-Process -Id 4320 -Force\"", "ls; kill -TERM 4322"):
            got = self.hook(command)
            self.assertIsNotNone(got, command)
            self.assertEqual(got["hookSpecificOutput"]["permissionDecisionReason"], deny_hint.HUB_PID_HINT, command)
        for command in ("kill 5555", "kill -9 5555", "taskkill //F //PID 5555", "taskkill /PID 5555 /T",
                        "powershell -c \"Stop-Process -Id 5555\"", "kill %1", "kill $!", "kill $(cat server.pid)"):
            self.assertIsNone(self.hook(command), command)
        # an "is it alive" check and a signal list kill nobody: signal 0 is only a check, -l only prints
        for command in ("kill -0 4321", "kill -0 4321 4322", "kill -l 4321", "kill -L 4321", "kill -s 0 4321",
                        "kill -n 0 4321", "kill -s0 4321", "/bin/kill -0 4321", "ls; kill -0 4322 && echo alive", "kill -0 5555 || true"):
            self.assertIsNone(self.hook(command), command)
        # while the next link with a real kill is still refused
        for command in ("kill -0 -9 4321", "kill -s 0 -s KILL 4321", "taskkill -0 4321", "kill -0 4321 -9",
                        "kill -0 4321 && kill 4321", "kill -l; kill -9 4322", "kill -0 5555 4321 ; kill -TERM 4321"):
            self.assertIsNotNone(self.hook(command), command)
        self.assertIsNone(self.hook("Stop-Process -Id 5555", tool="PowerShell"))
        self.assertIsNotNone(self.hook("Stop-Process -Id 4322", tool="PowerShell"))

    def test_killing_everyone_or_the_hub_group_is_denied(self):
        # the audit of 03.11 (probe P6): `kill -9 -1` is every process of the user, the hub too; 0 is the caller's own
        # group; -<pgid> is a whole group; $PPID is the session's claude; os.kill from python past the `kill` rule
        from hooks import deny_hint
        with open(os.path.join(self.dir, "hub-up.json"), "w") as f:
            json.dump({"pid": 4321, "service": None, "core": 4322, "parent": 4320}, f)
        for command in ("kill -9 -1", "kill -KILL -1", "kill -- -1", "kill 0", "kill -9 0", "kill -s KILL 0",
                        "kill -- -4321", "kill -9 -4321", "kill -TERM -- -4320", "kill $PPID", "kill -9 ${PPID}",
                        "/bin/kill -9 -1", "python3 -c \"import os; os.kill(4321, 9)\"",
                        "python -c 'import os, signal; os.killpg(4321, signal.SIGTERM)'",
                        "python3 -c 'import os; os.kill(-1, 9)'", "(kill 4321)", "x=$(kill 4321)", "echo `kill -9 4322`",
                        "kill -9 -1 2>/dev/null", "/usr/bin/python3 -I -c 'import os; os.kill(4322, 15)'",
                        "python3 -Ic 'import os; os.kill(4321, 9)'", "python3 - <<'EOF'\nimport os; os.kill(4321, 9)\nEOF",
                        "echo 'import os; os.kill(4321, 9)' | python3",
                        "python3 -c 'import os, signal; os.kill(4321, signal.Signals(9))'"):
            got = self.hook(command)
            self.assertIsNotNone(got, command)
            self.assertEqual(got["hookSpecificOutput"]["permissionDecisionReason"], deny_hint.HUB_PID_HINT, command)
        for command in ("kill -9 -5555", "kill -- -5555", "kill -1 5555", "kill -9 5555", "kill $!", "kill %1",
                        "python3 -c 'import os; os.kill(5555, 9)'", "python3 -c 'print(1)'", "python3 x.py 4321",
                        "kill -l 0", "kill -0 0", "python3 -c 'import os; os.kill(4321, 0)'",
                        "kill 43210", "kill 4321.5", "kill 5555 0</dev/null", "kill $mine 0>&-",
                        "kill 4321>/dev/null"):   # the shell takes 4321 there as a descriptor: kill gets no pid
            self.assertIsNone(self.hook(command), command)

    def test_kill_hook_is_silent_on_ordinary_commands(self):
        for command in ("python -m http.server 8000", "py -3 -m http.server 8765 &", "grep -n taskkill hosts/common.py",
                        "echo taskkill /IM python.exe", "echo 'taskkill //F //IM python.exe'",
                        "git commit -m \"taskkill /IM python.exe is denied\"", "tasklist //FI \"IMAGENAME eq python.exe\"",
                        "taskkill /?", "powershell -NoProfile -Command \"Get-Process python\"",
                        "powershell -c \"Get-CimInstance Win32_Process | ? { $_.CommandLine -like '*server*' } | "
                        "fl ProcessId,CommandLine\"", "cmd //c dir", "wmic process list brief",
                        "find . -name '*.py' | xargs grep -l kill", "cat kill-notes.md", "ls # taskkill //IM python.exe",
                        "cat <<EOF\ntaskkill //F //IM python.exe\nEOF", "skill python"):
            self.assertIsNone(self.hook(command), command)
        for command in ("Get-Process python", "Get-Content kill.txt", "Write-Host 'Stop-Process -Name python'"):
            self.assertIsNone(self.hook(command, tool="PowerShell"), command)

    def test_long_sleep_in_front_gets_a_hint(self):
        """2026-10-16: a foreground `sleep N` - 240 times in ~185 sessions, a line in the prompt addendum did not help.
        The whole command or its first link, from 30 s, gets a refusal with a hint; there is no deny on sleep - without
        the hook the call would go through as before."""
        from hooks import deny_hint
        for command in ("sleep 30", "sleep 60 && tail -5 log", "sleep 120; hub status", "  sleep 1m", "sleep 20 15",
                        "sleep 0.5m\ncat out", "sleep 300 | cat", "sleep 45 || true", "sleep 60  # waiting for the build",
                        "sleep 60 >/dev/null", "sleep 120 2>&1 && tail log", "cd /srv/app && sleep 90 && tail -5 log",
                        "sleep 60 &>/dev/null; ls"):
            got = self.hook(command)["hookSpecificOutput"]
            self.assertEqual((got["permissionDecision"], got["permissionDecisionReason"]),
                             ("deny", deny_hint.SLEEP_HINT), command)
        for word in ("run_in_background", "Monitor", "until", "hub wait"):
            self.assertIn(word, deny_hint.SLEEP_HINT)

    def test_loop_with_long_sleep_in_front_gets_the_hint(self):
        """Eval 2026-10-17: "wait for the pipeline" - 6 of 6 sessions waited with a foreground loop `for/until ... do ...
        sleep 40; done`, holding the turn just like `sleep 240`. A sleep of 30 s or more in a for/while/until loop (in
        the body or the condition) gets the same refusal; a short sleep in a loop, a loop in the background and a sleep
        outside a loop that is not the first link pass."""
        from hooks import deny_hint
        for command in ("until [ -f done ]; do sleep 60; done", "while true; do sleep 300; done",
                        "for i in 1 2 3 4 5; do ./ci.sh status | grep -q passed && break; sleep 40; done",
                        "for i in $(seq 1 10); do\n  ./ci.sh status\n  sleep 30\ndone",
                        "until ./ci.sh status | grep -q passed; do sleep 1m; done; ./ci.sh status",
                        "cd svc && until grep -q ok deploy/status.txt 2>/dev/null; do sleep 45; done",
                        "while ! test -f out; do echo wait; sleep 60 # waiting\ndone", "while sleep 40; do ls; done",
                        "for i in 1 2; do if [ -f x ]; then break; else sleep 90; fi; done",
                        "for f in a b; do (sleep 40); done", "while :; do { sleep 40; }; done",
                        "if true; then while true; do sleep 60; done; fi", "time while true; do sleep 60; done",
                        "for i in 1 2; do\n  sleep 30 \\\n  ; done", "for i in 1; do ls; done; for j in 2; do sleep 40; done",
                        "for i in 1; do while true; do ls; done; sleep 50; done"):
            got = self.hook(command)["hookSpecificOutput"]
            self.assertEqual((got["permissionDecision"], got["permissionDecisionReason"]),
                             ("deny", deny_hint.SLEEP_HINT), command)
        for command in ("until [ -f done ]; do sleep 5; done", "for i in 1 2 3; do sleep 10; ls; done",
                        "while true; do sleep 60; done &", "for i in 1 2; do sleep 60 & done; wait",
                        "for i in 1; do echo 'sleep 60'; done", "for i in 1; do echo sleep 60; done",
                        "for i in 1; do ls; done; echo x # sleep 60", "while read l; do echo $l; done <<EOF\nsleep 60\nEOF",
                        "for i in 1; do sleep $N; done", "for i in 1; do ls; done && sleep 2",
                        "for i in 1; do ls; done; sleep 60; echo done", "for f in a; do ls; done; sleep 45 && ./notify-done.sh",
                        "for i in 1; do echo do sleep 60; done", "for i in 1; do echo [ -f done ]; done; sleep 60",
                        "for i in 1; do sleep 32>log; done", "while read l; do echo $l; done<f.txt; sleep 60",
                        "for f in a; do ls; done>out.txt; sleep 60"):
            self.assertIsNone(self.hook(command), command)
        self.assertIsNone(self.hook("until [ -f x ]; do sleep 60; done", run_in_background=True))
        self.assertIsNone(self.hook("until [ -f x ]; do sleep 60; done", timeout=10000))

    def test_short_and_background_sleep_pass(self):
        for command in ("sleep 5", "sleep 29 && ls", "sleep 2; sleep 3", "echo a; sleep 60",
                        "sleep 600 & echo $!", "sleep $N", "sleep infinity", "sleepy 60", "ls; # sleep 60",
                        "sleep 60 && curl -s localhost:8080/health &", "sleep 60 | tee log &", "sleep 60&&make&",
                        "echo 'sleep 60'", "make && sleep 60", "sleep 60 && cat <<EOF &\nbody\nEOF",
                        "sleep 60 && grep -q x <<< s &", "sleep 60 && echo $((1<<2)) &\nls", "sleep 32>log",
                        "sleep 302>/dev/null && ls", "sleep 60 && # it's\n make 'x' &"):
            self.assertIsNone(self.hook(command), command)
        self.assertIsNone(self.hook("sleep 600 && make", run_in_background=True))
        self.assertIsNone(self.hook("sleep 60; echo done", timeout=10000))
        self.assertIsNotNone(self.hook("sleep 60; echo done", timeout=120000))

    def test_the_hook_scripts_claude_calls_are_plain_files_of_the_code(self):
        """claude calls the hooks by path: they are .py files of the repo (the packages carry the code as .py with
        bytecode since 2026-11-01; an older .deb compiled it to C and had to keep these scripts by name)."""
        used = {os.path.relpath(arg, common.REPO).replace(os.sep, "/")
                for groups in self.settings["hooks"].values() for g in groups for h in g["hooks"]
                for arg in h.get("args") or shlex.split(h["command"]) if arg.endswith(".py")}
        self.assertEqual(used, {"hooks/hub_hook.py", "hooks/deny_hint.py"})
        for rel in used:
            self.assertTrue(os.path.isfile(os.path.join(common.REPO, rel)), rel)
        # the seed of the app (and so of the .deb) is the whole repo cloned on a release tag: the hooks come with it
        with open(os.path.join(common.REPO, "tools", "package", "deb", "build-in.sh")) as f:
            self.assertIn("code-seed", f.read())

    def test_hint_hook_is_silent_on_other_commands(self):
        """A command claude could not parse is passed to the hook on all rules (2.1.218 and 2.1.296: `for i in 1; do echo
        $i; done` - nine calls): the hook does not refuse by itself - without those programs in the command it stays
        silent; quoted text, a heredoc (a commit message) and a comment are not a command."""
        for command in ("ls", "echo pgrep", "grep -n pkill hosts/common.py", "kill 4242", "ps -axo pid,command",
                        "for f in *.py; do grep -l pkill $f; done", "for i in 1; do echo $i; done", "pkill_notes.sh",
                        "cat docs/pgrep.md", "echo do pkill later", "echo 'a; pkill b'", 'echo "x; pgrep y"',
                        "ls  # then pkill", "echo Done! pgrep not needed",
                        "git commit -m \"$(cat <<'EOF'\nBan on `pkill -f x`;\npgrep too\nEOF\n)\"",
                        "for f in a; do cat <<EOF\npkill x\nEOF\ndone"):
            self.assertIsNone(self.hook(command), command)


if __name__ == "__main__":
    unittest.main()
