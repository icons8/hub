"""tools/compat.py — compatibility of the core with Claude Code versions (owner decision 2026-10-06: test on all Claude
Code versions and on Windows before a release). Without a real claude: fake version binaries accept or refuse a flag."""
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))
import compat  # noqa: E402
from tests.fakes import cut

# fake claude: --version, --help (does it know --system-prompt-snapshot), one stream-json turn; hooks — via --settings,
# like the real one; behavior comes from the JSON next to it
FAKE = textwrap.dedent('''\
    #!{python}
    import json, os, subprocess, sys, time
    conf = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "conf-" + os.path.basename(__file__))))
    args = sys.argv[1:]
    if args == ["--version"]:
        print(conf["version"] + " (Claude Code)"); sys.exit(0)
    if args == ["--help"]:
        print("--append-system-prompt-file" + (" --system-prompt-snapshot" if conf.get("snapshot") else "")); sys.exit(0)
    for flag in conf.get("refuse", []):
        if flag in args:
            print("error: unknown option '" + flag + "'", file=sys.stderr); sys.exit(1)
    if conf.get("hang"):
        time.sleep(60)
    if "-p" in args and "--settings" not in args:   # the accounts gateway self-check: a request to the fake API and init
        import urllib.request
        tools = conf.get("tools", ["Bash"])
        body = json.dumps(dict(tools=[dict(name=t, defer_loading=t == "Read") for t in tools])).encode()
        urllib.request.urlopen(urllib.request.Request(os.environ["ANTHROPIC_BASE_URL"] + "/v1/messages", data=body),
                               timeout=10).read()
        print(json.dumps(dict(type="system", subtype="init", tools=tools, claude_code_version=conf["version"])))
        sys.exit(0)
    settings = json.load(open(args[args.index("--settings") + 1]))
    def hook(event, **data):
        if conf.get("no_hooks"):
            return
        for h in settings["hooks"][event][0]["hooks"]:
            subprocess.run(h["command"], shell=True, input=json.dumps(dict(hook_event_name=event, session_id="s", **data)),
                           text=True)
    with open(os.environ["FAKE_ARGS"], "w") as f:
        json.dump(args, f)
    with open(os.environ["FAKE_ARGS"] + ".token", "w") as f:
        f.write(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""))
    hook("SessionStart", source="startup")
    msg = json.loads(sys.stdin.readline())
    hook("UserPromptSubmit", prompt=msg["message"]["content"])
    hook("Stop")
    print(json.dumps(dict(type="result", subtype="success", is_error=bool(conf.get("api_error")),
                          result="API Error: 400" if conf.get("api_error") else "ok")), flush=True)
    sys.stdin.read()
''')


@unittest.skipIf(os.name == "nt", "the fake claude is a script with a shebang")
class CompatRun(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.args = os.path.join(self.dir, "args.json")
        os.environ["FAKE_ARGS"] = self.args

    def fake(self, version, **conf):
        path = os.path.join(self.dir, version)
        with open(path, "w") as f:
            f.write(FAKE.format(python=sys.executable))
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        with open(os.path.join(self.dir, "conf-" + version), "w") as f:
            json.dump({"version": version, **conf}, f)
        return path

    def test_core_command_line_one_turn(self):
        result = compat.check(self.fake("2.1.291", snapshot=True), timeout=20)
        self.assertEqual(result["status"], compat.OK, result)
        with open(self.args) as f:
            args = json.load(f)
        for flag in ("--input-format", "--settings", "--append-system-prompt-file", "--system-prompt-snapshot",
                     "--effort", "--strict-mcp-config", "--permission-prompt-tool"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--model") + 1], "haiku")
        self.assertNotIn("--mcp-config", args)
        # the agent file and the core's addendum — one file, one flag (like the core)
        self.assertEqual(args.count("--append-system-prompt-file"), 1)
        self.assertNotIn("--append-system-prompt", args)

    @cut.needs("profile")
    def test_claude_runs_on_the_active_pool_account_like_sessions(self):
        # 2026-10-11: this computer has no login of its own — sessions run on pool tokens; the check ran without a login, every
        # version said "Not logged in", and the release did not go out
        state, cfg = tempfile.mkdtemp(), tempfile.mkdtemp()
        with open(os.path.join(state, "state.json"), "w") as f:
            json.dump({"state": {"account": {"active": "b"}}}, f)
        with open(os.path.join(cfg, "accounts.json"), "w") as f:
            json.dump({"accounts": [{"name": "a", "token": "tok-a"}, {"name": "b", "token": "tok-b"}]}, f)
        with mock.patch.dict(os.environ, {"HUB_STATE_DIR": state, "HUB_CONFIG_DIR": cfg}):
            result = compat.check(self.fake("2.1.291", snapshot=True), timeout=20)
        self.assertEqual(result["status"], compat.OK, result)
        with open(self.args + ".token") as f:
            self.assertEqual(f.read(), "tok-b")

    def test_without_a_pool_the_computer_login_is_used(self):
        state, cfg = tempfile.mkdtemp(), tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"HUB_STATE_DIR": state, "HUB_CONFIG_DIR": cfg, "CLAUDE_CODE_OAUTH_TOKEN": "own"}):
            compat.check(self.fake("2.1.291", snapshot=True), timeout=20)
        with open(self.args + ".token") as f:
            self.assertEqual(f.read(), "own")

    def test_old_version_without_snapshot_gets_no_snapshot_flag(self):
        result = compat.check(self.fake("2.1.218", refuse=["--system-prompt-snapshot"]), timeout=20)
        self.assertEqual(result["status"], compat.OK, result)

    def test_refusal_carries_claude_error_text(self):
        result = compat.check(self.fake("2.1.200", refuse=["-n"]), timeout=20)
        self.assertEqual(result["status"], compat.REFUSED)
        self.assertIn("unknown option '-n'", result["detail"])

    def test_error_result_is_refusal(self):
        result = compat.check(self.fake("2.1.201", api_error=True), timeout=20)
        self.assertEqual(result["status"], compat.REFUSED)
        self.assertIn("API Error", result["detail"])

    def test_silent_version_times_out(self):
        result = compat.check(self.fake("2.1.202", hang=True), timeout=2)
        self.assertEqual(result["status"], compat.TIMEOUT)
        self.assertIn("2 s", result["detail"])

    def test_hooks_must_fire(self):
        result = compat.check(self.fake("2.1.203", no_hooks=True), timeout=20)
        self.assertEqual(result["status"], compat.HOOKLESS)
        self.assertIn("turn.started", result["detail"])

    @cut.needs("gateway")
    def test_main_table_codes_and_minimum(self):
        self.fake("2.1.100", refuse=["-n"])
        self.fake("2.1.218")
        self.fake("2.1.291", snapshot=True)
        os.makedirs(os.path.join(self.dir, "2.1.5"))   # not a binary — not a version
        out = self.main("--versions-dir", self.dir, "--timeout", "20", "--versions", "2.1.100,2.1.218,2.1.291")
        self.assertEqual(out.returncode, 3, out.stdout + out.stderr)
        self.assertIn("2.1.100", out.stdout)
        self.assertIn("incompatible: 2.1.100", out.stdout)
        self.assertIn("CLAUDE_TESTED can be set to 2.1.218", out.stdout)
        self.assertIn("not checked", out.stdout)   # no second machine configured — honest, not a failure
        self.assertNotIn("2.1.5", out.stdout)
        self.assertIn("accounts gateway: will not turn on for 2.1.100", out.stdout)

    @cut.needs("gateway")
    def test_gateway_probe_per_version(self):
        self.fake("2.1.291", snapshot=True, tools=["Bash", "Read", "ToolSearch"])
        out = self.main("--versions-dir", self.dir, "--json", "--timeout", "20", "--versions", "2.1.291")
        got = json.loads(out.stdout)["results"]["2.1.291"]["gateway"]
        self.assertEqual((got["ok"], got["first_party"]), (True, True))

    def test_main_all_ok_json(self):
        self.fake("2.1.291", snapshot=True)
        out = self.main("--versions-dir", self.dir, "--json", "--timeout", "20", "--versions", "2.1.291")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["results"]["2.1.291"]["status"], "ok")

    def downloads(self, pointer):
        """The fake Anthropic downloads whose stable channel names `pointer`."""
        from tests.fakes import claude_downloads
        srv = claude_downloads.Server(claude_downloads.Key(), "2.1.400")
        srv.pointer = pointer.encode() + b"\n"
        self.addCleanup(srv.close)
        return {"HUB_CLAUDE_DOWNLOADS": srv.url}

    def test_by_default_only_the_stable_channel_version_is_checked(self):
        # owner decision 2026-11-11: test only on the latest Claude Code at the time of the release — not every version
        # the machine has: the folder gives the binary, the stable channel the version
        self.fake("2.1.218")
        self.fake("2.1.291", snapshot=True)
        out = self.main("--versions-dir", self.dir, "--json", "--timeout", "20", env=self.downloads("2.1.291"))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(list(json.loads(out.stdout)["results"]), ["2.1.291"])

    def test_no_stable_version_is_a_failed_check_not_an_empty_one(self):
        self.fake("2.1.291", snapshot=True)
        out = self.main("--versions-dir", self.dir, "--json", "--timeout", "20", env=self.downloads("<html>"))
        self.assertEqual(out.returncode, 2, out.stdout)
        self.assertIn("Claude Code stable", out.stderr)

    def tree(self):
        tree = os.path.join(self.dir, "tree")
        os.makedirs(os.path.join(tree, "core"))
        with open(os.path.join(tree, "core", "version.py"), "w", newline="") as f:
            f.write('PLUGIN_API = "1.9"\nCLAUDE_TESTED = "2.1.218"   # the tested one\nRUNTIME_MIN = 2\n')
        return os.path.join(tree, "core", "version.py")

    def test_pin_writes_the_stable_version_into_the_tree(self):
        path = self.tree()
        out = self.main("--pin", os.path.dirname(os.path.dirname(path)), env=self.downloads("2.1.330"))
        self.assertEqual(out.returncode, 0, out.stderr)
        with open(path, newline="") as f:
            self.assertEqual(f.read(), 'PLUGIN_API = "1.9"\nCLAUDE_TESTED = "2.1.330"   # the tested one\nRUNTIME_MIN = 2\n')

    def test_pin_without_a_version_refuses_and_leaves_the_tree(self):
        path = self.tree()
        for pointer in ("<html>blocked</html>", "2.1.330-beta"):
            out = self.main("--pin", os.path.dirname(os.path.dirname(path)), env=self.downloads(pointer))
            self.assertEqual(out.returncode, 2, out.stdout)
            with open(path) as f:
                self.assertIn('CLAUDE_TESTED = "2.1.218"', f.read())

    def test_missing_version_installed_by_npm_into_temp(self):
        npm = os.path.join(self.dir, "npm")
        src = self.fake("2.1.250")
        with open(npm, "w") as f:   # fake npm: puts claude into <prefix>/node_modules/.bin
            f.write(f'#!/bin/sh\nprefix=$3\nmkdir -p "$prefix/node_modules/.bin"\n'
                    f'cp "{src}" "$prefix/node_modules/.bin/claude"\n'
                    f'cp "{self.dir}/conf-2.1.250" "$prefix/node_modules/.bin/conf-claude"\necho "$@" > "{self.dir}/npm-args"\n')
        os.chmod(npm, 0o755)
        empty = tempfile.mkdtemp()
        res = compat.run_local(["2.1.250"], empty, timeout=20, npm=npm)
        self.assertEqual(res["2.1.250"]["status"], compat.OK, res)
        with open(os.path.join(self.dir, "npm-args")) as f:
            self.assertIn("@anthropic-ai/claude-code@2.1.250", f.read())

    def test_binary_must_be_the_version_it_claims(self):
        path = self.fake("2.1.260")
        os.rename(path, os.path.join(self.dir, "2.1.261"))   # the file is "2.1.261" but contains 2.1.260
        os.rename(os.path.join(self.dir, "conf-2.1.260"), os.path.join(self.dir, "conf-2.1.261"))
        res = compat.run_local(["2.1.261"], self.dir, timeout=20)
        self.assertEqual(res["2.1.261"]["status"], compat.ERROR)
        self.assertIn("2.1.260", res["2.1.261"]["detail"])

    def test_npm_failure_is_check_failure(self):
        res = compat.run_local(["2.1.250"], tempfile.mkdtemp(), timeout=5, npm="/bin/false")
        self.assertEqual(res["2.1.250"]["status"], compat.ERROR)
        self.assertEqual(compat.verdict({"mac": res})[0], 2)

    def main(self, *args, env=None):
        return subprocess.run([sys.executable, os.path.join(REPO, "tools", "compat.py"), *args], capture_output=True,
                              text=True, env={**os.environ, "HUB_COMPAT_SSH": "", "HUB_COMPAT_VERSIONS": "", **(env or {})},
                              timeout=120)

    def test_no_model_guard(self):
        out = self.main()   # the default versions folder holds a real claude: don't run it under HUB_NO_MODEL
        self.assertEqual(out.returncode, 2)
        self.assertIn("HUB_NO_MODEL", out.stderr)


class Verdict(unittest.TestCase):
    ok = {"status": "ok"}

    def test_codes(self):
        self.assertEqual(compat.verdict({"mac": {"2.1.291": self.ok}})[0], 0)
        self.assertEqual(compat.verdict({"mac": {"2.1.291": {"status": "timeout"}}})[0], 3)
        self.assertEqual(compat.verdict({"mac": {"2.1.291": {"status": "error"}}})[0], 2)
        self.assertEqual(compat.verdict({"mac": {}})[0], 2)   # no version at all — the check never ran

    def test_minimum_across_machines_skips_gap(self):
        machines = {"mac": {"2.1.100": self.ok, "2.1.200": self.ok, "2.1.291": self.ok},
                    "win": {"2.1.200": {"status": "refused", "detail": "x"}, "2.1.291": self.ok}}
        code, bad, minimum = compat.verdict(machines)
        self.assertEqual((code, minimum), (3, "2.1.291"))   # 2.1.100 passed, but there is a refusal above it
        self.assertEqual([(m, v) for m, v, _ in bad], [("win", "2.1.200")])

    def test_unreachable_machine_not_a_failure(self):
        machines = {"mac": {"2.1.291": self.ok}, "win": {"": {"status": "skipped", "detail": "no connection"}}}
        code, text = compat.report(machines)
        self.assertEqual(code, 0)
        self.assertIn("not checked", text)
        self.assertIn("CLAUDE_TESTED can be set to 2.1.291", text)

    def test_version_order_numeric(self):
        self.assertEqual(sorted(["2.1.30", "2.1.218", "2.1.9"], key=compat.vkey), ["2.1.9", "2.1.30", "2.1.218"])


@unittest.skipIf(os.name == "nt", "the fake ssh is a script with a shebang")
class Ssh(unittest.TestCase):
    def fake_ssh(self, body):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "ssh")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
        return path

    def test_unreachable_is_skipped(self):
        ssh = self.fake_ssh('echo "ssh: connect to host x port 22: Operation timed out" >&2; exit 255\n')
        res = compat.run_ssh("user@x", "C:\\hubrev\\compat", ssh=ssh)
        self.assertEqual(res[""]["status"], compat.SKIPPED)
        self.assertIn("timed out", res[""]["detail"])

    def test_remote_json_parsed(self):
        reply = json.dumps({"code": 0, "results": {"2.1.218": {"status": "ok", "detail": "ok", "seconds": 3}}})
        ssh = self.fake_ssh(f'case "$*" in *"echo ok"*) echo ok ;; *) echo "noise"; echo \'{reply}\' ;; esac\n')
        scp = self.fake_ssh(f'echo "$@" > "{os.path.dirname(ssh)}/scp-args"\n')
        res = compat.run_ssh("user@x", "C:\\hubrev\\compat", ssh=ssh, scp=scp)
        self.assertEqual(res, {"2.1.218": {"status": "ok", "detail": "ok", "seconds": 3}})
        with open(os.path.join(os.path.dirname(ssh), "scp-args")) as f:
            self.assertIn("user@x:C:/hubrev/compat.zip", f.read())

    def test_the_second_machine_checks_the_same_version(self):
        d = tempfile.mkdtemp()
        reply = json.dumps({"code": 0, "results": {"2.1.330": {"status": "ok", "detail": "ok", "seconds": 1}}})
        ssh = self.fake_ssh(f'case "$*" in *"echo ok"*) echo ok ;; *) printf %s "$*" > "{d}/args"; echo \'{reply}\' ;; esac\n')
        res = compat.run_ssh("user@x", "C:\\hubrev\\compat", ssh=ssh, scp=self.fake_ssh("true\n"), versions=["2.1.330"])
        self.assertEqual(res["2.1.330"]["status"], "ok")
        with open(f"{d}/args") as f:
            self.assertIn("--versions 2.1.330", f.read())

    def test_sleeping_machine_is_woken_for_the_check_and_put_back_to_sleep(self):
        # decision: the Windows stand is switched on only for tests, it is resource-hungry — the stand sleeps,
        # the check wakes it (HUB_COMPAT_SSH_WAKE), waits for ssh and afterwards puts it to sleep (HUB_COMPAT_SSH_SLEEP),
        # even if the run failed
        d = tempfile.mkdtemp()
        reply = json.dumps({"code": 0, "results": {"2.1.218": {"status": "ok", "detail": "ok", "seconds": 1}}})
        ssh = self.fake_ssh(f'[ -f "{d}/up" ] || exit 255; case "$*" in *"echo ok"*) echo ok ;; *) echo \'{reply}\' ;; esac\n')
        scp = self.fake_ssh("true\n")
        res = compat.run_ssh("user@x", "C:\\hubrev\\compat", ssh=ssh, scp=scp, wake=f"touch {d}/up",
                             sleep=f"touch {d}/down", wake_wait=20, poll=0.1)
        self.assertEqual(res["2.1.218"]["status"], "ok")
        self.assertTrue(os.path.exists(f"{d}/down"))
        os.remove(f"{d}/down")
        res = compat.run_ssh("user@x", "C:\\hubrev\\compat", ssh=ssh, scp=self.fake_ssh("exit 1\n"),
                             wake=f"touch {d}/up", sleep=f"touch {d}/down", wake_wait=20, poll=0.1)
        self.assertEqual(res[""]["status"], compat.ERROR)
        self.assertTrue(os.path.exists(f"{d}/down"))

    def test_machine_that_does_not_wake_is_a_failure(self):
        d = tempfile.mkdtemp()
        ssh = self.fake_ssh("exit 255\n")
        res = compat.run_ssh("user@x", "C:\\hubrev\\compat", ssh=ssh, wake="true", sleep=f"touch {d}/down",
                             wake_wait=0.5, poll=0.1)
        self.assertEqual(res[""]["status"], compat.ERROR)   # it was woken and not waited out — that is not "not checked"
        self.assertTrue(os.path.exists(f"{d}/down"))

    def test_machine_without_claude_is_a_failure_not_silence(self):
        # 2026-10-11: a new stand had no claude — the machine returned an empty result and vanished from the table, and the release would have passed
        reply = json.dumps({"code": 2, "results": {}})
        ssh = self.fake_ssh(f'case "$*" in *"echo ok"*) echo ok ;; *) echo \'{reply}\' ;; esac\n')
        res = compat.run_ssh("user@x", "C:\\hubrev\\compat", ssh=ssh, scp=self.fake_ssh("true\n"))
        self.assertEqual(res[""]["status"], compat.ERROR)
        self.assertEqual(compat.verdict({"x": res})[0], 2)

    def test_pool_token_goes_over_stdin_not_the_command_line(self):
        # claude's login on the second machine is this one's pool account (as for sessions); the token goes over ssh's stdin,
        # not on the command line and not to that machine's disk
        d = tempfile.mkdtemp()
        reply = json.dumps({"code": 0, "results": {"2.1.218": {"status": "ok", "detail": "ok", "seconds": 1}}})
        ssh = self.fake_ssh(f'case "$*" in *"echo ok"*) echo ok ;; *) printf %s "$*" > "{d}/args"; cat > "{d}/stdin"; '
                            f'echo \'{reply}\' ;; esac\n')
        res = compat.run_ssh("user@x", "C:\\hubrev\\compat", ssh=ssh, scp=self.fake_ssh("true\n"), token="tok-123")
        self.assertEqual(res["2.1.218"]["status"], "ok")
        with open(f"{d}/args") as f:
            args = f.read()
        self.assertIn("--token-stdin", args)
        self.assertNotIn("tok-123", args)
        with open(f"{d}/stdin") as f:
            self.assertEqual(f.read().strip(), "tok-123")

    def ssh_token(self, provider, active="b"):
        """The login that main() would send over ssh: the pool provider is the given one, the second machine is faked."""
        from core import profiles
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = tmp.name
        with open(os.path.join(state, "state.json"), "w") as f:
            json.dump({"state": {"account": {"active": active}}}, f)
        sent = []
        with mock.patch.dict(os.environ, {"HUB_STATE_DIR": state, "HUB_NO_MODEL": ""}), \
                mock.patch.object(profiles.Profiles, "provider", lambda self: provider), \
                mock.patch.object(profiles.Profiles, "serves", lambda self, gate: True), \
                mock.patch.object(compat, "run_ssh", lambda *a, **k: sent.append(k.get("token")) or {"2.1.218": {
                    "status": "ok", "detail": "", "seconds": 1}}), \
                mock.patch.object(compat, "run_local", lambda *a, **k: {"2.1.218": {"status": "ok", "detail": "", "seconds": 1}}), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = compat.main(["--ssh", "user@x", "--versions", "2.1.218"])
        return code, sent, out.getvalue() + err.getvalue()

    def test_over_ssh_goes_the_real_pool_token_not_the_gateway_stub(self):
        # 2026-10-30 (v1.6.0): with the active account on the accounts gateway the core's environment holds the gateway's stub;
        # the second machine has no gateway — "401 Invalid bearer token" on every version, --release code 5
        class Gateway:
            def token(self, name, session=None, kind="session"):
                return {"env": {"CLAUDE_CODE_OAUTH_TOKEN": "tok-real-b"}, "gateway": {"port": 18999, "secret": "s" * 20}}
        code, sent, shown = self.ssh_token(Gateway())
        self.assertEqual((code, sent), (0, ["tok-real-b"]))
        self.assertNotIn("tok-real-b", shown)

    def test_over_ssh_the_stub_is_never_sent(self):
        from core import profiles

        class Stub:
            def token(self, name, session=None, kind="session"):
                return {"env": {"CLAUDE_CODE_OAUTH_TOKEN": profiles.STUB}, "gateway": {"port": 18999, "secret": "s" * 20}}
        code, sent, shown = self.ssh_token(Stub())
        self.assertEqual((code, sent), (2, []))
        self.assertIn("stub", shown)

    def test_over_ssh_a_module_without_the_profile_login_stops_the_run(self):
        # the fallback is the old path: the module gave no login of the profile — not the machine's login, an error
        class Silent:
            name = "acc"

            def token(self, name, session=None, kind="session"):
                return {}

            def listing(self):
                return {"on": True}
        from core import profiles
        with mock.patch.object(profiles, "Module", Silent):
            code, sent, shown = self.ssh_token(Silent())
        self.assertEqual((code, sent), (2, []))
        self.assertIn("login for the check over ssh", shown)

    def test_over_ssh_without_a_pool_account_nothing_is_sent(self):
        class Empty:
            def token(self, name, session=None, kind="session"):
                return None
        code, sent, _ = self.ssh_token(Empty(), active=None)
        self.assertEqual((code, sent), (0, [None]))

    def test_token_from_stdin_replaces_the_machine_login(self):
        env = {"ANTHROPIC_API_KEY": "k", "PATH": "/bin"}
        compat.use_token(env, "tok-123")
        self.assertEqual(env, {"CLAUDE_CODE_OAUTH_TOKEN": "tok-123", "PATH": "/bin"})
        env = {"PATH": "/bin"}
        compat.use_token(env, None)
        self.assertEqual(env, {"PATH": "/bin"})


@unittest.skipIf(os.name == "nt", "publish.sh — sh")
class Publish(unittest.TestCase):
    """publish.sh: runs compat.py before pushing; non-zero — doesn't publish; --skip-compat — with an explicit line."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.repo = os.path.join(self.root, "repo")
        os.makedirs(os.path.join(self.repo, "tools"))
        with open(os.path.join(REPO, "tools", "publish.sh")) as f, \
                open(os.path.join(self.repo, "tools", "publish.sh"), "w") as g:
            g.write(f.read())
        for name in ("publish_allow.py", "publish-allow.txt", "publish-deny.txt", "publish_checks.py",
                     "publish_state.py", "release.py", "publish_remote.py", "private_check.py", "contributors.py",
                     "publish_guard.py"):
            with open(os.path.join(REPO, "tools", name)) as f, open(os.path.join(self.repo, "tools", name), "w") as g:
                g.write(f.read())
        # private_check reads the runner's earlier line forms from the language data of the repository it belongs to
        shutil.copytree(os.path.join(REPO, "i18n", "data"), os.path.join(self.repo, "i18n", "data"))
        with open(os.path.join(self.repo, "tools", "public_language.py"), "w") as g:
            g.write("raise SystemExit(0)\n")   # the language ratchet has its own test (test_publish)
        with open(os.path.join(self.repo, "tools", "oss_open.py"), "w") as g:
            g.write('OPEN_TARGETS = ("icons8/hub",)\n')   # the real list is read in test_publish
        with open(os.path.join(self.repo, "tools", "i18n.py"), "w") as g:
            g.write("raise SystemExit(0)\n")   # the translations gate has its own test (test_publish)
        # without its own deny patterns publish.sh doesn't publish (closed by default): a harmless one, in .gitignore, like the hub's
        with open(os.path.join(self.repo, ".publish-deny"), "w") as f:
            f.write("forbidden-name\n")
        with open(os.path.join(self.repo, ".gitignore"), "w") as f:
            f.write(".publish-deny\n__pycache__/\n")
        self.remote = os.path.join(self.root, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.remote], check=True)

    def commit(self, compat_code):
        with open(os.path.join(self.repo, "tools", "compat.py"), "w") as f:
            f.write(f"import sys\nprint('compatibility table')\nsys.exit({compat_code})\n")
        with open(os.path.join(self.repo, "CHANGES.md"), "w") as f:
            f.write(f"## {time.strftime('%Y-%m-%d')}\n\n- item\n")
        git = ["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run(["git", "init", "-q", "-b", "main", self.repo], check=True)
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-q", "-m", "x"], check=True)

    def publish(self, *flags):
        env = {**os.environ, "PUBLISH_NAME": "t", "PUBLISH_EMAIL": "t@example.com", "PATH": os.path.dirname(sys.executable) + ":/usr/bin:/bin"}
        return subprocess.run(["sh", os.path.join(self.repo, "tools", "publish.sh"), self.remote, *flags],
                              capture_output=True, text=True, env=env, timeout=60)

    def pushed(self):
        return bool(subprocess.run(["git", "-C", self.remote, "rev-parse", "--verify", "-q", "main"],
                                   capture_output=True).stdout)

    def test_incompatible_blocks_push(self):
        self.commit(3)
        out = self.publish()
        self.assertEqual(out.returncode, 5, out.stderr)
        self.assertIn("compatibility table", out.stdout)
        self.assertIn("not publishing", out.stderr)
        self.assertFalse(self.pushed())

    def test_compatible_pushes(self):
        self.commit(0)
        out = self.publish()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(self.pushed())

    def test_check_does_not_run_compat(self):
        self.commit(3)
        out = self.publish("--check")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("compatibility table", out.stdout)

    def test_skip_compat_loud(self):
        self.commit(3)
        out = self.publish("--skip-compat")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("skipped (--skip-compat)", out.stderr)
        self.assertTrue(self.pushed())


if __name__ == "__main__":
    unittest.main()
