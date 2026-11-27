"""hub.json `schedule.shell`: "bash" runs schedule command jobs and the pre-checks of session jobs through Git Bash on
Windows, with `hub` first in PATH. The sessions that set a job write it in Bash (a pipe, `$(...)`, single quotes), and
cmd.exe reads that differently: a pipe outside double quotes is cmd's own pipe, `bash` on the PATH of the Task Scheduler
is the WSL launcher from System32, `bin/hub` has a python3 shebang cmd cannot run. Off by default: without the setting
everything goes to cmd.exe as before; without Git Bash too, with a line in the log. No real Git Bash or job here: the
search for it and Popen are replaced."""
import tests  # noqa: F401 — keep the machine's settings out
import io
import os
import shlex
import tempfile
import unittest
from unittest import mock

import platforms
from core import system
from modules.scheduler import clock as clockmod
from platforms import posix, windows

BASH = r"C:\Program Files\Git\bin\bash.exe"
# what a session writes: a pipe, command substitution, single and double quotes, `$` for bash
LINE = "n=$(printf 'a\\nb\\n' | grep -c .); hub notify --id milestone \"done $n\""


class Setup(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="schedshell-")
        self.addCleanup(platforms.current.rmtree, self.state)
        told = mock.patch.object(windows, "_JOB_SHELL_TOLD", set())   # one line per reason per process: each test its own
        told.start()
        self.addCleanup(told.stop)


class WindowsJobShell(Setup):
    def test_without_the_setting_a_job_goes_to_cmd_with_this_hubs_hub_first(self):
        # the clock puts <repo>/bin first, but its `hub` has no .cmd: cmd.exe went on along PATH to the `hub.cmd` of the
        # user's terminal, which sets its own hub's HUB_STATE_DIR — a second hub's job spoke to the main hub (2026-11-26)
        env = {"PATH": r"C:\main-hub\bin;C:\Windows", "HUB_SCHEDULE_JOB": "m"}
        with mock.patch.object(windows, "_git_bash", return_value=BASH) as found:
            for setting in (None, "", "cmd", "sh", "BASH"):
                args, shell, got = windows.job_shell(LINE, env, self.state, setting)
                self.assertEqual((args, shell), (LINE, True), setting)   # the line goes to cmd.exe as before
                first, rest = got["PATH"].split(os.pathsep, 1)
                self.assertEqual(rest, env["PATH"])
                self.assertEqual(os.path.commonpath([first, self.state]), self.state)   # this hub's own folder
                with open(os.path.join(first, "hub.cmd")) as f:
                    self.assertIn("HUB_NESTED_HUB", f.read())
                self.assertFalse(os.path.exists(os.path.join(first, "claude.cmd")))
                self.assertEqual(got["HUB_SCHEDULE_JOB"], "m")
        found.assert_not_called()   # Git Bash not even looked for without the setting

    def test_a_folder_of_hub_that_cannot_be_written_leaves_a_cmd_job_as_it_was(self):
        env = {"PATH": r"C:\Windows"}
        with mock.patch.object(windows, "own_hub", side_effect=PermissionError("locked")):
            self.assertEqual(windows.job_shell(LINE, env, self.state, None), (LINE, True, env))

    def test_bash_runs_the_command_whole_in_git_bash_with_hub_first_in_path(self):
        with mock.patch.object(windows, "_git_bash", return_value=BASH):
            args, shell, env = windows.job_shell(LINE, {"PATH": r"C:\Windows", "HUB_SCHEDULE_JOB": "m"}, self.state, "bash")
        self.assertEqual((args, shell), ([BASH, "-c", LINE], False))   # one argument: the quotes reach bash as written
        first, rest = env["PATH"].split(os.pathsep, 1)
        self.assertEqual(rest, r"C:\Windows")
        with open(os.path.join(first, "hub")) as f:
            self.assertIn("HUB_NESTED_HUB", f.read())
        self.assertEqual(os.path.normcase(env["HUB_NESTED_HUB"]),
                         os.path.normcase(os.path.join(os.path.dirname(os.path.dirname(windows.__file__)), "bin", "hub")))
        self.assertEqual(env["HUB_SCHEDULE_JOB"], "m")

    def assert_cmd_with_this_hubs_hub_first(self, got, env):
        args, shell, got = got
        self.assertEqual((args, shell), (LINE, True))
        first, rest = got["PATH"].split(os.pathsep, 1)
        self.assertEqual(rest, env["PATH"])
        self.assertEqual(os.path.commonpath([first, self.state]), self.state)
        self.assertTrue(os.path.isfile(os.path.join(first, "hub.cmd")))

    def test_without_git_bash_it_stays_cmd_with_this_hubs_hub_first_and_a_line_in_the_log(self):
        # a hub with only Codex sessions may have no Git Bash: its job went to cmd.exe with the env as it was, and found
        # the `hub.cmd` of another hub of the computer in the user's PATH (review of 2026-11-26)
        env = {"PATH": r"C:\other-hub\bin;C:\Windows"}
        with mock.patch.object(windows, "_git_bash", return_value=None), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assert_cmd_with_this_hubs_hub_first(windows.job_shell(LINE, env, self.state, "bash"), env)
        self.assertIn("Git Bash", err.getvalue())

    def test_the_line_in_the_log_comes_once_not_with_every_launch(self):
        with mock.patch.object(windows, "_git_bash", return_value=None), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            for _ in range(3):
                windows.job_shell(LINE, {}, self.state, "bash")
        self.assertEqual(err.getvalue().count("Git Bash"), 1)

    def test_a_folder_of_hub_that_cannot_be_written_leaves_the_job_in_cmd(self):
        env = {"PATH": r"C:\Windows"}
        with mock.patch.object(windows, "_git_bash", return_value=BASH), \
                mock.patch.object(windows, "nested", side_effect=PermissionError("locked")), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assert_cmd_with_this_hubs_hub_first(windows.job_shell(LINE, env, self.state, "bash"), env)
            with mock.patch.object(windows, "own_hub", side_effect=PermissionError("locked")):
                self.assertEqual(windows.job_shell(LINE, env, self.state, "bash"), (LINE, True, env))
        self.assertIn("locked", err.getvalue())

    def test_git_bash_is_the_one_next_to_git_not_the_wsl_launcher(self):
        git = os.path.join(self.state, "Git", "cmd", "git.exe")
        bash = os.path.join(self.state, "Git", "bin", "bash.exe")
        for path in (git, bash):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").close()
        wsl = r"C:\Windows\System32\bash.exe"
        which = {"git": git, "bash": wsl}
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(windows.shutil, "which", which.get):
            os.environ.pop("CLAUDE_CODE_GIT_BASH_PATH", None)
            self.assertTrue(os.path.samefile(windows._git_bash(), bash))   # realpath may lengthen a Windows short name
            which["git"] = os.path.join(self.state, "Git", "mingw64", "bin", "git.exe")   # the git a Git Bash PATH finds
            self.assertTrue(os.path.samefile(windows._git_bash(), bash))
            which.pop("git")
            self.assertIsNone(windows._git_bash())   # `bash` on PATH is not taken: it is WSL there

    def test_posix_ignores_the_setting(self):
        env = {"PATH": "/usr/bin"}
        self.assertEqual(posix.job_shell(LINE, env, self.state, "bash"), (LINE, True, env))
        args = ["/usr/bin/python3", "/opt/hub/audit.py"]
        self.assertEqual(posix.job_quote(args, "bash"), '"/usr/bin/python3" "/opt/hub/audit.py"')


class PrecheckQuoting(Setup):
    ARGS = [r"C:\Py 3\python.exe", "-X", "utf8", r"C:\hub a$b`c\audit.py", "C:\\end\\"]

    def test_under_bash_the_arguments_go_in_single_quotes_and_arrive_as_they_are(self):
        # in double quotes bash would eat `\$`, a backtick and the `\` before the closing quote
        with mock.patch.object(windows, "_git_bash", return_value=BASH):
            line = windows.job_quote(self.ARGS, "bash")
        self.assertEqual(shlex.split(line), self.ARGS)   # POSIX shlex reads single quotes as bash does
        self.assertNotIn('"', line)

    def test_without_the_setting_or_git_bash_the_quoting_is_the_previous_one(self):
        before = " ".join(f'"{a}"' for a in self.ARGS)
        with mock.patch.object(windows, "_git_bash", return_value=BASH):
            self.assertEqual(windows.job_quote(self.ARGS, None), before)
        with mock.patch.object(windows, "_git_bash", return_value=None):
            self.assertEqual(windows.job_quote(self.ARGS, "bash"), before)

    def test_the_stops_precheck_follows_the_setting(self):
        with mock.patch.object(platforms, "current", windows), mock.patch.object(windows, "_git_bash", return_value=BASH):
            self.assertEqual(system.stops_check({}), " ".join(f'"{a}"' for a in [*windows.python(), system.STOPS_SCRIPT]))
            on = system.stops_check({"schedule": {"shell": "bash"}})
            self.assertEqual(shlex.split(on), [*windows.python(), system.STOPS_SCRIPT])
            self.assertEqual(system.stops_check({"schedule": "bash"}), system.stops_check({}))   # wrong shape: off


class ClockUsesTheSetting(Setup):
    def run_both(self, cfg, found):
        """(Popen call of a command job, Popen call of a pre-check) with hub.json `cfg` and Git Bash at `found`."""
        clock = clockmod.Clock(self.state, config=lambda: cfg)
        st = {"sessions": {"s": {"cwd": self.state}}}
        with mock.patch.object(platforms, "current", windows), mock.patch.object(windows, "_git_bash", return_value=found), \
                mock.patch.object(clockmod.subprocess, "Popen") as popen, mock.patch("sys.stderr", new_callable=io.StringIO):
            clock.start({"id": "cmd", "command": LINE, "run_slot": 1.0}, 1.0)
            clock.check({"id": "chk", "check": LINE, "session": "s"}, 1.0, 1.0, st)
        return popen.call_args_list

    def test_without_the_setting_both_go_to_the_shell_as_before(self):
        for call in self.run_both({}, BASH):
            self.assertEqual((call.args, call.kwargs["shell"]), ((LINE,), True))

    def test_with_bash_both_go_to_git_bash(self):
        for call in self.run_both({"schedule": {"shell": "bash"}}, BASH):
            self.assertEqual((call.args, call.kwargs["shell"]), (([BASH, "-c", LINE],), False))
            self.assertTrue(os.path.isfile(os.path.join(call.kwargs["env"]["PATH"].split(os.pathsep)[0], "hub")))

    def test_with_bash_but_no_git_bash_both_go_to_the_shell_as_before(self):
        for call in self.run_both({"schedule": {"shell": "bash"}}, None):
            self.assertEqual((call.args, call.kwargs["shell"]), ((LINE,), True))


if __name__ == "__main__":
    unittest.main()
