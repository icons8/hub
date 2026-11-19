"""`claude -p` from inside a hub session runs with that session's toolset, not the owner's personal MCP and settings.
2026-10-05: an executor without a granted ms365 opened a nested `claude -p` and read Teams through the owner's
personal MCP, bypassing the hub's grants."""
import tests  # noqa: F401 - ignore the machine's settings
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import platforms
from hosts import streamjson
from tests.test_stop import git_bash
from tests.fakes import cut

SHIM = os.path.join(os.path.dirname(streamjson.__file__), "nested", "claude")
SHIM_NS = runpy.run_path(SHIM)
compose, login = SHIM_NS["compose"], SHIM_NS["login"]
SESSION = ["--plugin-dir", "/p/research", "--mcp-config", "/s/x/mcp.json"]


class Compose(unittest.TestCase):
    def test_a_print_call_gets_the_sessions_set_only(self):
        args = compose(["-p", "--allowedTools", "mcp__ms365", "read the chats"], SESSION)
        self.assertEqual(args[:2], ["--setting-sources", "project,local"])
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(args[-4:], ["-p", "--allowedTools", "mcp__ms365", "read the chats"])
        for flag in SESSION:
            self.assertIn(flag, args)

    def test_explicit_isolation_of_the_caller_stays(self):
        # eval runs set both settings and MCP themselves - leave them alone
        own = ["-p", "--setting-sources", "", "--strict-mcp-config", "--mcp-config", "/e/mcp.json", "x"]
        self.assertEqual(compose(own, SESSION), own)

    def test_subcommands_and_version_pass_through(self):
        for argv in (["mcp", "list"], ["--version"], []):
            self.assertEqual(compose(argv, SESSION), argv)


@cut.needs("gateway")
class Login(unittest.TestCase):
    """Nested claude on a pool token: Claude Code scrubs CLAUDE_CODE_OAUTH_TOKEN from the Bash environment, while the
    gateway address and secret stay - without a stub the nested `claude -p` got "Not logged in" (2026-10-21)."""

    def session(self, stub=True):
        from modules.gateway import gateway
        env = gateway.session_env({"HUB_ACCOUNT": "a"}, "http://127.0.0.1:1", "s", stub=stub)
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)   # as in the session's Bash
        return env, gateway.STUB

    def test_a_pool_session_on_the_gateway_gets_the_stub_back(self):
        env, stub = self.session()
        self.assertEqual(login(env)["CLAUDE_CODE_OAUTH_TOKEN"], stub)

    def test_an_own_login_of_the_call_stays(self):
        for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            env, stub = self.session()
            env[k] = "own"
            self.assertEqual(login(env)[k], "own")
            self.assertEqual(env.get("CLAUDE_CODE_OAUTH_TOKEN", "own"), "own", k)

    def test_the_computers_login_and_off_the_gateway_get_nothing(self):
        env, _ = self.session(stub=False)   # a pool account with this computer's own login: HUB_ACCOUNT is set, there is no stub
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", login(env))
        env, _ = self.session()
        env.pop("ANTHROPIC_BASE_URL")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", login(env))
        env, _ = self.session()
        env["ANTHROPIC_CUSTOM_HEADERS"] = "x-own: 1"   # the call pointed claude at its own address - the stub is not needed there
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", login(env))

    @unittest.skipIf(os.name == "nt", "a fake claude is an extensionless sh; the Windows wrapper is checked by WindowsShim")
    def test_the_shim_runs_the_real_claude_with_the_stub(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        fake = os.path.join(d, "claude")
        with open(fake, "w") as f:
            f.write('#!/bin/sh\necho "token=$CLAUDE_CODE_OAUTH_TOKEN"\n')
        os.chmod(fake, 0o755)
        env, stub = self.session()
        env.update(PATH=os.environ.get("PATH", ""), HUB_REAL_CLAUDE=fake)
        out = subprocess.run([sys.executable, SHIM, "-p", "x"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "token=" + stub, out.stderr)
        out = subprocess.run([sys.executable, SHIM, "--print", "x"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "token=" + stub, out.stderr)
        out = subprocess.run([sys.executable, SHIM, "auth", "status"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "token=", "subcommands — as is")


class Spawn(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.host = streamjson.StreamHost(self.dir, claude=["claude"])
        self.host.fresh_prompt = False   # without the `claude --help` probe
        self.host.tool_flags = lambda **_: ["--setting-sources", "project,local", "--plugin-dir", "/p/research",
                                            "--strict-mcp-config", "--mcp-config", "/s/x/mcp.json"]

    def spawn(self):
        popen = mock.MagicMock()
        popen.poll.return_value = None
        with mock.patch.object(streamjson.subprocess, "Popen", return_value=popen) as p, \
                mock.patch.object(streamjson.threading, "Thread"):
            self.host.spawn("x", "sid-x", cwd=self.dir)
        return p.call_args.kwargs["env"]

    def test_the_session_finds_the_shim_first_and_knows_its_set(self):
        env = self.spawn()
        self.assertEqual(env["PATH"].split(os.pathsep)[0], platforms.current.nested(SHIM, self.dir)[0])
        with open(env["HUB_NESTED_FLAGS"]) as f:
            self.assertEqual(json.load(f), SESSION)
        self.assertTrue(env["HUB_REAL_CLAUDE"])
        self.assertNotEqual(os.path.dirname(env["HUB_REAL_CLAUDE"]), os.path.dirname(SHIM))

    @unittest.skipIf(os.name == "nt", "a fake claude is an extensionless sh; the Windows wrapper is checked by WindowsShim")
    def test_inside_a_hub_session_the_real_claude_is_found_past_the_shim(self):
        # an eval run from a session: the wrapper is already first in its PATH - the real claude is looked up past it
        bin_dir = tempfile.mkdtemp()
        fake = os.path.join(bin_dir, "claude")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(fake, 0o755)
        with mock.patch.dict(os.environ, {"PATH": os.pathsep.join([os.path.dirname(SHIM), bin_dir])}):
            env = self.spawn()
        self.assertEqual(env["HUB_REAL_CLAUDE"], fake)
        other = tempfile.mkdtemp()   # another hub copy's wrapper (a working copy) is skipped too, otherwise a loop
        with open(os.path.join(other, "claude"), "w") as f, open(SHIM) as src:
            f.write(src.read())
        os.chmod(os.path.join(other, "claude"), 0o755)
        with mock.patch.dict(os.environ, {"PATH": os.pathsep.join([other, os.path.dirname(SHIM), bin_dir])}):
            self.host.procs.clear()
            self.assertEqual(self.spawn()["HUB_REAL_CLAUDE"], fake)
        self.assertEqual(env["PATH"], os.pathsep.join([platforms.current.nested(SHIM, self.dir)[0], bin_dir]))

    def test_the_shim_is_executable(self):
        self.assertTrue(os.access(SHIM, os.X_OK))


REAL = """import sys, time
time.sleep(1)
print("REAL", " ".join(sys.argv[1:]), flush=True)
sys.exit(3)
"""


@unittest.skipUnless(os.name == "nt", "Windows only")
class WindowsShim(unittest.TestCase):
    """The Windows wrapper: python3 there is most often the Microsoft Store stub, and exec is a new process, so the
    caller would get exit code 0 before the real claude finishes its answer."""

    def setUp(self, python=sys.executable):
        self.state = tempfile.mkdtemp()
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "real.py"), "w") as f:
            f.write(REAL)
        real = os.path.join(d, "claude.cmd")   # the "real" claude: sleeps 1 s, writes its arguments, exits with 3
        with open(real, "w") as f:
            f.write(f'@"{sys.executable}" "{os.path.join(d, "real.py")}" %*\n')
        stub = tempfile.mkdtemp()   # python3 as the Store stub: it is in PATH but is not Python
        with open(os.path.join(stub, "python3"), "w", newline="\n") as f:
            f.write("#!/bin/sh\necho 'Python was not found' >&2\nexit 9009\n")
        with mock.patch.object(platforms.current.sys, "executable", python):
            shim_dir, shim_env = platforms.current.nested(SHIM, self.state)
        path = os.pathsep.join([shim_dir, stub, os.environ["PATH"]])
        self.env = {**os.environ, **shim_env, "PATH": path, "HUB_REAL_CLAUDE": real}

    @unittest.skipUnless(git_bash(), "no Git Bash")
    def test_git_bash_gets_the_real_exit_code_after_the_answer(self):
        # like claude's Bash on Windows: `claude -p` from Git Bash
        out = subprocess.run([git_bash(), "-c", "claude -p γειά; echo rc=$?"], env=self.env, capture_output=True,
                             timeout=60).stdout.decode("utf-8", "replace").split()
        self.assertIn("rc=3", out)
        self.assertLess(out.index("REAL"), out.index("rc=3"))
        self.assertIn("--strict-mcp-config", out)

    def test_powershell_and_cmd_find_the_shim_too(self):
        out = subprocess.run(["cmd", "/c", "claude", "-p", "hi"], env=self.env, capture_output=True, timeout=60)
        self.assertEqual(out.returncode, 3)
        self.assertIn(b"--strict-mcp-config", out.stdout)

    def test_python_under_a_path_with_percent_and_cyrillic(self):
        # review: cmd reads the path in a .cmd file in the console code page and expands %...% in it - a user with a
        # non-ASCII name, a folder with "%": the wrapper did not start, and outside the OEM encoding the real claude
        # ran from PowerShell, bypassing the toolset
        venv = os.path.join(tempfile.mkdtemp(), "Χρήστης %PATH% venv")
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", venv], check=True, timeout=120)
        self.setUp(python=os.path.join(venv, "Scripts", "python.exe"))
        out = subprocess.run(["cmd", "/c", "claude", "-p", "hi"], env=self.env, capture_output=True, timeout=60)
        self.assertEqual(out.returncode, 3, out.stderr)
        if git_bash():
            out = subprocess.run([git_bash(), "-c", "claude -p hi; echo rc=$?"], env=self.env, capture_output=True,
                                 timeout=60).stdout.decode("utf-8", "replace").split()
            self.assertIn("rc=3", out)

    def test_hub_runs_past_the_store_stub(self):
        # 2026-10-06, test rig: `hub` from a session's Bash went through the shebang to the python3 stub and exited
        # with code 49 - the hub session could neither write nor read status. The `hub` wrapper uses the same Python
        # as the hub, from Git Bash and cmd
        env = {**self.env, "HUB_STATE_DIR": tempfile.mkdtemp()}
        env.pop("HUB_SESSION", None)
        shells = [["cmd", "/c", "hub", "stop"]] + ([[git_bash(), "-c", "hub stop"]] if git_bash() else [])
        for argv in shells:
            out = subprocess.run(argv, env=env, capture_output=True, timeout=60)
            self.assertIn("hub-up", out.stderr.decode("utf-8", "replace"), argv)


class WindowsShimFiles(unittest.TestCase):
    """Wrapper files are written on any OS: a check without Windows."""

    def test_hub_and_claude_shims_use_the_hubs_python(self):
        from platforms import windows
        state = tempfile.mkdtemp()
        d, env = windows.nested(SHIM, state)
        self.assertEqual(sorted(os.listdir(d)), ["claude", "claude.cmd", "hub", "hub.cmd"])
        self.assertEqual(env["HUB_NESTED_PYTHON"], sys.executable)
        self.assertTrue(os.path.samefile(env["HUB_NESTED_HUB"],
                                         os.path.join(os.path.dirname(platforms.__file__), "..", "bin", "hub")))
        with open(os.path.join(d, "hub"), newline="") as f:
            self.assertIn('"$HUB_NESTED_PYTHON" "$HUB_NESTED_HUB"', f.read())
        with open(os.path.join(d, "hub.cmd"), newline="") as f:
            self.assertIn('"%HUB_NESTED_PYTHON%" "%HUB_NESTED_HUB%" %*', f.read())


class LoginOfThisMachine(unittest.TestCase):
    """The nested claude -p runs without the user's Claude Code settings and Claude Code scrubs CLAUDE_CODE_OAUTH_TOKEN
    from the Bash environment: a session on this computer's login (settings.json env, catalog.user_env) gets that token
    back; a session on a pool account or at a foreign address does not; the call's own sign-in stays."""

    SETTINGS = {"CLAUDE_CODE_OAUTH_TOKEN": "tok-a", "HTTPS_PROXY": "https://proxy:1", "FOO": "x"}

    def environment(self, env, settings=SETTINGS):
        d = tempfile.mkdtemp(prefix="login-")
        with open(os.path.join(d, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"env": settings, "model": "opus"}, f)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": d}):
            return SHIM_NS["environment"](dict(env))

    def test_the_scrubbed_login_comes_back_from_the_user_settings(self):
        env = self.environment({"PATH": "/bin"})
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok-a")
        self.assertNotIn("FOO", env)            # only the login, not the rest of the owner's env
        self.assertNotIn("HTTPS_PROXY", env)    # not scrubbed: the session has it as the core gave it

    def test_what_the_call_already_has_stays(self):
        self.assertEqual(self.environment({"CLAUDE_CODE_OAUTH_TOKEN": "tok-b"})["CLAUDE_CODE_OAUTH_TOKEN"], "tok-b")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", self.environment({"ANTHROPIC_API_KEY": "k"}))

    def test_a_session_on_a_pool_account_keeps_what_the_core_gave_it(self):
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", self.environment({"HUB_ACCOUNT": "a"}))

    def test_the_owners_token_does_not_go_to_a_foreign_address(self):
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", self.environment({"ANTHROPIC_BASE_URL": "https://proxy.example.test"}))
        own = dict(self.SETTINGS, ANTHROPIC_BASE_URL="https://mine.example.test/")
        self.assertEqual(self.environment({"ANTHROPIC_BASE_URL": "https://mine.example.test"}, own)["CLAUDE_CODE_OAUTH_TOKEN"],
                         "tok-a")

    def test_on_the_account_gateway_with_this_computers_login(self):
        from core import profiles
        env = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9", "ANTHROPIC_CUSTOM_HEADERS": f"{profiles.SECRET_HEADER}: s"}
        self.assertEqual(self.environment(env)["CLAUDE_CODE_OAUTH_TOKEN"], "tok-a")

    def test_no_settings_or_a_broken_core_no_change(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": tempfile.mkdtemp(prefix="login-")}):
            self.assertEqual(SHIM_NS["environment"]({"PATH": "/bin"}), {"PATH": "/bin"})
        from core import catalog
        with mock.patch.object(catalog, "user_env", side_effect=RuntimeError("half-updated copy")):
            self.assertEqual(SHIM_NS["environment"]({"PATH": "/bin"}), {"PATH": "/bin"})

if __name__ == "__main__":
    unittest.main()
