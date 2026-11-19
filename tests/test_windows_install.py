"""Installation on Windows (deploy/windows): PowerShell finds Python and Claude Code, edits PATH and registers the
scheduler task, while setup.py writes the files, which is what we test without Windows. A reinstall does not overwrite
hub.json (like deploy/mac/install.sh), the hub session is enabled, and the hub's CLAUDE.md arrives even without
permission to create symbolic links."""
import tests  # noqa: F401 — hide the machine's settings
import codecs
import datetime
import json
import ntpath
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import platforms
from core import zones

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETUP = os.path.join(REPO, "deploy", "windows", "setup.py")
PS1 = os.path.join(REPO, "deploy", "windows", "install.ps1")


def setup():
    return runpy.run_path(SETUP)


class Config(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="win-state-")
        self.cfg = os.path.join(tempfile.mkdtemp(prefix="win-cfg-"), "hub.json")

    def test_hub_session_on_and_state_inside(self):
        self.assertTrue(setup()["write_config"](self.cfg, self.state))
        with open(self.cfg, encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertEqual(cfg["hub"], {"enabled": True, "name": "hub-core", "cwd": os.path.join(self.state, "hub-home")})
        self.assertEqual(cfg["timezone"], "local")
        self.assertEqual(cfg["projects"], {})   # no shared sandbox: a session without a project lives in its own folder

    def test_rerun_keeps_the_owners_settings(self):
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({"title": "my hub"}, f)
        self.assertFalse(setup()["write_config"](self.cfg, self.state))
        with open(self.cfg, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"title": "my hub"})


class HubHome(unittest.TestCase):
    def test_without_symlinks_the_files_point_to_the_repo(self):
        # Windows without administrator rights and developer mode does not create symbolic links (06.10, test stand:
        # hub-home was empty, so the hub session had neither its CLAUDE.md nor ONBOARDING.md). Then an @path import is
        # used: the text always comes from the repo
        state = tempfile.mkdtemp(prefix="win-home-")
        with mock.patch("os.symlink", side_effect=OSError("privilege not held")):
            setup()["hub_home"](REPO, state)
        home = os.path.join(state, "hub-home")
        for name in ("CLAUDE.md", "ONBOARDING.md"):
            with open(os.path.join(home, name), encoding="utf-8") as f:
                self.assertIn("@" + os.path.join(REPO, "hub-home", name).replace("\\", "/"), f.read())

    def test_rerun_replaces_old_files(self):
        state = tempfile.mkdtemp(prefix="win-home-")
        s = setup()
        s["hub_home"](REPO, state)
        s["hub_home"](REPO, state)
        with open(os.path.join(state, "hub-home", "CLAUDE.md"), encoding="utf-8") as f:
            self.assertTrue(f.read())


class Scripts(unittest.TestCase):
    def test_hub_command_and_launcher_use_the_hubs_python(self):
        state = tempfile.mkdtemp(prefix="win-bin-")
        py = r"C:\Program Files\Python312\python.exe"
        hub_cmd, launcher = setup()["scripts"](r"C:\x\agents-hub-core", state, py, encoding="utf-8")
        with open(hub_cmd, encoding="utf-8", newline="") as f:
            self.assertEqual(f.read(), f'@"{py}" "C:\\x\\agents-hub-core\\bin\\hub" %*\r\n')
        with open(launcher, encoding="utf-8") as f:
            text = f.read()
        self.assertIn(f'"{py}" "C:\\x\\agents-hub-core\\bin\\hub-up"', text)
        self.assertIn(ntpath.join(state, "logs", "hub-up.log"), text)

    def test_a_user_folder_outside_the_code_page_goes_through_its_variable(self):
        # a non-Latin user name on an English Windows (OEM 437): the install stopped with UnicodeEncodeError
        name = "\u0395\u03bb\u03ad\u03bd\u03b7 \u03a0\u03b1\u03c0\u03ac"   # Greek letters: no code page of an English Windows holds them
        user = "C:\\Users\\" + name   # the folder and the name apart: the public-tree pattern reads "Users\\u…" as a user's name
        env = {"LOCALAPPDATA": user + "\\AppData\\Local", "APPDATA": user + "\\AppData\\Roaming", "USERPROFILE": user}
        state = tempfile.mkdtemp(prefix="win-bin-")
        with open(os.path.join(state, "instance.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "work2", "ports": {"web": 8790}}, f)
        repo, py = env["LOCALAPPDATA"] + "\\hub-core\\code", env["LOCALAPPDATA"] + "\\hub-core\\app\\python.exe"
        hub_cmd, launcher = setup()["scripts"](repo, state, py, encoding="cp437", environ=env,
                                               config=env["APPDATA"] + "\\hub-core", instance="work2")
        for path in (hub_cmd, launcher):
            with open(path, "rb") as f:
                text = f.read().decode("cp437")
            self.assertNotIn("\\Users\\", text.replace(state, ""), path)
            self.assertIn('"%LOCALAPPDATA%\\hub-core\\app\\python.exe" ', text)
        self.assertIn('set "HUB_CONFIG_DIR=%APPDATA%\\hub-core"', text)
        # read back by the move of a hub from git (core/appmove): the folder as it is
        from core import appmove
        with mock.patch.dict(os.environ, env):
            self.assertEqual(appmove.repo_from_cmd(launcher, "cp437"), repo)
        with self.assertRaises(UnicodeEncodeError):   # a folder of no variable stays a refusal, never a "?" in a path
            setup()["scripts"]("D:\\" + user[9:], state, py, encoding="cp437", environ=env)

    def test_paths_the_pipe_cannot_hold_do_not_fail_the_install(self):
        # install.ps1 pipes setup.py's output away (| Out-Null): Python writes a pipe in the ANSI code page, and the
        # paths of a user named in other letters stopped the install after the files were written
        d = tempfile.mkdtemp(prefix="win-pipe-")
        self.addCleanup(platforms.current.rmtree, d)
        local = os.path.join(d, "Ελένη")   # the .cmd files name it as %LOCALAPPDATA% on Windows
        state = os.path.join(local, "hub-core")
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8",)}
        r = subprocess.run([sys.executable, SETUP, REPO, state, os.path.join(d, "cfg", "hub.json")], capture_output=True,
                           env={**env, "PYTHONIOENCODING": "cp1252", "LOCALAPPDATA": local}, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(state, "hub-up.cmd")))

    def test_launcher_does_not_redirect_into_the_log(self):
        """The `>>` redirection in cmd does not open a file while someone else is writing to it (hub alive: its parts
        write; hub killed: whatever inherited the handle and outlived it), and the command does not run, so a
        scheduler task retry would not bring the hub up (20.10). hub-up opens the log itself, with the --log key and
        shared access."""
        state = tempfile.mkdtemp(prefix="win-bin-")
        _, launcher = setup()["scripts"](r"C:\x\agents-hub-core", state, r"C:\py\python.exe", encoding="utf-8")
        with open(launcher, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn(">", text)
        self.assertIn(f'--log "{ntpath.join(state, "logs", "hub-up.log")}"', text)

    def test_launcher_tells_hub_up_it_is_the_scheduler(self):
        """The launcher is only for the scheduler task: --scheduled, so that the retry every 5 minutes does not start
        a hub the owner stopped (`hub stop` / `hub off`; tests/test_hub_stop)."""
        state = tempfile.mkdtemp(prefix="win-bin-")
        _, launcher = setup()["scripts"](r"C:\x\agents-hub-core", state, r"C:\py\python.exe", encoding="utf-8")
        with open(launcher, encoding="utf-8") as f:
            self.assertIn(" --scheduled", f.read())


class Installer(unittest.TestCase):
    """install.ps1 runs without administrator rights and takes the user name from whoami (06.10, test stand: under SSH
    USERDOMAIN is not the right account, Register-ScheduledTask 0x80070534)."""

    def setUp(self):
        with open(PS1, encoding="utf-8-sig") as f:
            self.text = f.read()

    def test_no_admin_and_user_from_whoami(self):
        self.assertNotIn("RunAs", self.text)
        self.assertNotIn("-RunLevel Highest", self.text)
        self.assertNotIn("$env:USERDOMAIN", self.text)
        self.assertIn("whoami", self.text)

    def test_task_brings_the_hub_back_after_it_was_killed(self):
        """A session ran `taskkill /IM python.exe`: hub-up, its supervisor and all parts died, and the hub stayed down
        until a manual start. The scheduler's RestartCount does not help: the task "succeeded" (a conhost code) rather
        than failing to start. A retry every 5 minutes: if the task is still running, IgnoreNew skips it; if the hub was
        started outside the task, the second hub-up exits by itself (tests/test_hub_stop)."""
        self.assertIn("-RepetitionInterval (New-TimeSpan -Minutes 5)", self.text)
        self.assertIn("-Trigger @($trigger, $again)", self.text)
        self.assertIn("-MultipleInstances IgnoreNew", self.text)

    def test_reinstall_lifts_the_stop_before_starting_the_task(self):
        # a reinstall stops the live hub with `hub stop`, which leaves a mark; a launcher with --scheduled would exit
        # at once because of it, and the hub would stay down after the install
        lift = self.text.index('Remove-Item (Join-Path $State "stopped")')
        self.assertLess(self.text.index('& $Python "$Run\\bin\\hub" stop'), lift)
        self.assertLess(lift, self.text.index("Start-ScheduledTask -TaskName $Task"))

    def test_paths_are_not_read_back_from_pythons_output(self):
        """Windows PowerShell 5.1 reads a program's output in the console code page, Python writes it in the ANSI one:
        for a user with a non-ASCII name the PATH entry and the task came back garbled (winstand) — the hub command was not found
        and the hub did not start at sign-in. The paths are where setup.py writes them."""
        self.assertNotRegex(self.text, r"\$HubCmd,\s*\$Launcher\s*=\s*&")
        self.assertIn('$HubCmd = Join-Path $State "bin\\hub.cmd"', self.text)
        self.assertIn('$Launcher = Join-Path $State "hub-up.cmd"', self.text)
        s = runpy.run_path(os.path.join(REPO, "deploy", "windows", "setup.py"))
        state = tempfile.mkdtemp(prefix="win-bin-")
        self.assertEqual(s["scripts"](r"C:\x\core", state, r"C:\py\python.exe", encoding="utf-8"),
                         (os.path.join(state, "bin", "hub.cmd"), os.path.join(state, "hub-up.cmd")))

    def test_a_line_on_stderr_does_not_stop_the_reinstall(self):
        # under $ErrorActionPreference = "Stop" PowerShell 5.1 turns stderr of a command with 2>$null into an exception:
        # the reinstall over a running task died on `hub stop` saying hub-up is not running (winstand)
        self.assertIn('try { & $Python "$Run\\bin\\hub" stop 2>$null | Out-Null } catch { }', self.text)

    def test_powershell_51_reads_it_as_utf8(self):
        with open(PS1, "rb") as f:
            self.assertTrue(f.read().startswith(b"\xef\xbb\xbf"))   # without a BOM Windows PowerShell 5.1 reads ANSI


def task_xml(arguments, encoding="utf-8", errors="strict"):
    """A scheduler task as `schtasks /Query /XML` prints it: the action install.ps1 registers."""
    text = ('<?xml version="1.0" encoding="UTF-16"?>\r\n<Task version="1.4" '
            'xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\r\n  <Actions Context="Author">\r\n'
            '    <Exec>\r\n      <Command>conhost.exe</Command>\r\n'
            f'      <Arguments>{arguments}</Arguments>\r\n      <WorkingDirectory>C:\\hub</WorkingDirectory>\r\n'
            '    </Exec>\r\n  </Actions>\r\n</Task>\r\n')
    return text.encode(encoding, errors)


class TheTaskOfThisHub(unittest.TestCase):
    """The scheduler task «Agents Hub» is one name per machine: `hub uninstall` of a second hub (another state folder)
    deleted the first one's task, `hub on` ran it, and install.ps1 overwrote any task of that name with -Force (tests
    research 03.11). The task is this hub's only if its action runs this state folder's launcher."""
    STATE = r"C:\Users\a\AppData\Local\hub-core"
    OURS = r'--headless cmd.exe /c "C:\Users\a\AppData\Local\hub-core\hub-up.cmd"'

    def unit(self, state, xml, code=0):
        from platforms import windows
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return mock.Mock(returncode=code, stdout=xml)
        with mock.patch.object(windows.subprocess, "run", run):
            got = windows.service_unit(state)
        self.assertEqual([c[:4] for c in calls], [["schtasks", "/Query", "/TN", "Agents Hub"]])
        return got

    def test_the_task_of_this_state_folder_is_ours(self):
        self.assertEqual(self.unit(self.STATE, task_xml(self.OURS))["id"], "Agents Hub")
        self.assertEqual(self.unit(self.STATE.upper() + "\\", task_xml(self.OURS))["id"], "Agents Hub")
        self.assertIsNotNone(self.unit(self.STATE, task_xml(self.OURS.replace('"', "&quot;"))))   # XML may escape

    def oem(self):
        """The console's OEM code page (`schtasks` writes in it); outside Windows — cp866 in its place."""
        try:
            codecs.lookup("oem")
        except LookupError:
            def find(name):
                return codecs.lookup("cp866") if name == "oem" else None
            codecs.register(find)
            self.addCleanup(codecs.unregister, find)

    def test_the_console_code_page_of_schtasks(self):
        # schtasks prints the task in the console's OEM code page, not in the UTF-16 it declares; a letter that page
        # lacks comes as «?» (winstand 03.11)
        self.oem()
        ours = rf'--headless cmd.exe /c "{self.STATE}\hub-up.cmd"'
        self.assertIsNotNone(self.unit(self.STATE, task_xml(ours, "utf-16")))     # if it ever does write UTF-16
        letter = next((c for c in "\u4e2d\u0e01" if c.encode("oem", "replace") == b"?"), None)
        if letter:
            state = self.STATE.replace("\\a\\", f"\\{letter}\\")
            xml = task_xml(f'--headless cmd.exe /c "{state}\\hub-up.cmd"', "oem", "replace")
            self.assertIn(b"?", xml)
            self.assertIsNotNone(self.unit(state, xml))
            self.assertIsNone(self.unit(state.replace(letter, letter * 2), xml))   # another hub's folder
        # Cyrillic in cp866 that is valid UTF-8 as well (the bytes E1 A0 AF are three cp866 letters): read as UTF-8 only, the path was lost
        state = self.STATE.replace("\\a\\", "\\" + bytes([0xE1, 0xA0, 0xAF]).decode("cp866") + "\\")
        self.assertIsNotNone(self.unit(state, task_xml(f'--headless cmd.exe /c "{state}\\hub-up.cmd"', "oem", "replace")))

    def test_the_task_of_another_hub_is_not_ours(self):
        from core import uninstall
        for state in (r"C:\dev\hub-state", r"C:\Users\a\AppData\Local\hub", r"C:\Users\a\AppData\Local\hub-core\x"):
            self.assertIsNone(self.unit(state, task_xml(self.OURS)), state)
        longer = r'--headless cmd.exe /c "C:\Users\a\AppData\Local\hub-core\hub-up.cmd.old"'
        self.assertIsNone(self.unit(self.STATE, task_xml(longer)))
        self.assertIsNone(self.unit(self.STATE, task_xml(self.OURS), code=1))   # no such task
        # without a unit `hub uninstall` only stops this hub: no step deletes a scheduler task
        with tempfile.TemporaryDirectory() as home:
            steps = uninstall.plan(r"C:\dev\hub-state", os.path.join(home, "cfg"), home=home, unit=None)
        self.assertFalse([s for s in steps if "schtasks" in s.get("argv", [])])

    def test_uninstall_deletes_the_task_then_waits_for_the_hub(self):
        # `hub stop` as a step failed on a hub already stopped (`hub off`, then `hub uninstall`: «not everything came
        # off») and returned before hub-up had gone, so --purge met the files it still held
        import platforms
        from core import uninstall
        from platforms import windows
        unit = {"kind": "task", "id": "Agents Hub", "path": None}
        with mock.patch.object(platforms.current, "uninstall_service", windows.uninstall_service), \
                mock.patch.object(platforms.current, "uninstall_path", return_value=[]), \
                tempfile.TemporaryDirectory() as home:
            steps = uninstall.plan(self.STATE, os.path.join(home, "cfg"), home=home, unit=unit)
        self.assertEqual([(s["do"], s.get("argv", [""])[0]) for s in steps[:2]], [("run", "schtasks"), ("halt", "")])
        self.assertFalse([s for s in steps if "stop" in s.get("argv", [])])

    def test_install_leaves_a_task_of_another_hub_alone(self):
        with open(PS1, encoding="utf-8-sig") as f:
            text = f.read()
        check = text.index("windows.service_unit(sys.argv[2])")
        # the root folder only, where Register-ScheduledTask puts it and `schtasks /TN` looks: a task of the same name
        # in a subfolder is not in the way
        self.assertIn('if (Get-ScheduledTask -TaskPath "\\" -TaskName $Task', text[check - 300:check])
        # before anything is written: the files (setup.py), PATH, the task
        for later in ('& $Python "$Run\\deploy\\windows\\setup.py"', 'Set-ItemProperty "HKCU:\\Environment"',
                      "Register-ScheduledTask -TaskName $Task"):
            self.assertLess(check, text.index(later), later)
        self.assertRegex(text[check:check + 200], r"\$Run \$State\s+if \(\$LASTEXITCODE -eq 3\) \{")
        self.assertIn("exit 1", text[check:text.index('& $Python "$Run\\deploy\\windows\\setup.py"')])


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "Windows PowerShell 5.1")
class OutputReadBack(unittest.TestCase):
    """install.ps1's output read by a program (deploy/oneline/setup.py writes it into install.log) is UTF-8, its own and
    its Pythons': a dash came out as "?" and a non-Latin user name as "????" (the one-line install's log, 16.11)."""

    def test_a_dash_and_other_letters_come_back_as_they_are(self):
        with open(PS1, encoding="utf-8-sig") as f:
            text = f.read()
        head = text[:text.index("$Run = ")]
        d = tempfile.mkdtemp(prefix="win-ps-out-")
        self.addCleanup(platforms.current.rmtree, d)
        path = os.path.join(d, "probe.ps1")
        word = "\u2014 \u0395\u03bb\u03ad\u03bd\u03b7"
        py = "import sys; print('\\u2014 \\u0395'); print('\\u2014', file=sys.stderr)"
        with open(path, "w", encoding="utf-8-sig") as f:   # with its BOM, as install.ps1: PowerShell 5.1 reads it so
            # what PowerShell reads from a Python and says itself (install.ps1: "Python: ... (3.14.8)")
            f.write(head + f'Write-Host "{word}"\n$v = & "{sys.executable}" -c "{py}"\nWrite-Host "py: $v"\n')
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                            path], capture_output=True, stdin=subprocess.DEVNULL, timeout=120,
                           env={**env, "HUB_INSTALL_OUTPUT": "utf-8"},
                           creationflags=0x08000000)   # its own console, as setup.py starts it
        out, err = (r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace"))
        self.assertEqual(out.splitlines(), [word, "py: \u2014 \u0395"], out + err)
        self.assertEqual(err.splitlines(), ["\u2014"], out + err)   # a Python's own stderr goes to the reader as it is


class LocalZone(unittest.TestCase):
    def test_local_is_the_machines_time(self):
        # on Windows the IANA zone name cannot be found without extra packages: the installer writes "local", the
        # machine's local time
        self.assertEqual(datetime.datetime.now(zones.zone("local")).utcoffset(),
                         datetime.datetime.now().astimezone().utcoffset())


if __name__ == "__main__":
    unittest.main()
