"""A critic from another model (modules/critic) is optional: off — no command and no rule; on — a rule in the session
instructions, Codex with explicit model and effort, read-only, an unavailable Codex falls back to Claude. No model is called."""
import tests  # noqa: F401 — ignore this machine's settings
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import platforms
from modules.critic import critic
from tests.fakes import lang


def draft():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "draft.md")
    with open(p, "w") as f:
        f.write("Move the cache to Redis, latency will halve.")
    return p


def launch_text(cmd):
    """What claude gets: the command line and the suffix file it names (the suffix goes as a file, PR #499)."""
    text = " ".join(cmd)
    if "--append-system-prompt-file" in cmd:
        with open(cmd[cmd.index("--append-system-prompt-file") + 1], encoding="utf-8") as f:
            text += " " + f.read()
    return text


class Review(unittest.TestCase):
    def test_codex_gets_pinned_model_effort_and_read_only(self):
        calls = []
        with mock.patch.object(critic.shutil, "which", lambda n, **k: "/x/" + n):
            who, out = critic.review({"critic": {"enabled": True, "model": "m-1", "effort": "medium"}}, draft(),
                                     task="speed up serving", run=lambda cmd, cwd, stdin=None: calls.append(cmd) or "«halved» with no measurement")
        cmd = calls[0]
        self.assertEqual(cmd[:2], ["/x/codex", "exec"])
        self.assertEqual(cmd[cmd.index("-m") + 1], "m-1")
        self.assertIn("model_reasoning_effort=medium", cmd)
        self.assertEqual(cmd[cmd.index("-s") + 1], "read-only")
        self.assertIn("--skip-git-repo-check", cmd)
        self.assertIn("speed up serving", cmd[-1])
        self.assertIn("Codex m-1", who)
        self.assertEqual(out, "«halved» with no measurement")

    def test_codex_unavailable_falls_back_to_read_only_claude(self):
        calls = []

        def run(cmd, cwd, stdin=None):
            calls.append((cmd, stdin))
            if "exec" in cmd:
                raise critic.Error("error: model is not supported when using ChatGPT account")
            return "no objections"
        with mock.patch.object(critic.shutil, "which", lambda n, **k: "/x/" + n):
            who, out = critic.review({"critic": {"enabled": True}}, draft(), run=run)
        claude, brief = calls[1]
        self.assertEqual(claude[-2], "--add-dir")   # the brief is not in the arguments: --add-dir would swallow it
        self.assertIn("Try to refute", brief)
        self.assertEqual(claude[claude.index("--model") + 1], "sonnet")
        self.assertEqual(claude[claude.index("--tools") + 1], "Read,Grep,Glob")
        self.assertIn("--strict-mcp-config", claude)
        self.assertIn("instead of Codex", who)

    def test_other_codex_errors_are_not_hidden(self):
        def run(cmd, cwd, stdin=None):
            raise critic.Error("network unavailable")
        with mock.patch.object(critic.shutil, "which", lambda n, **k: "/x/" + n), self.assertRaises(critic.Error):
            critic.review({"critic": {"enabled": True}}, draft(), run=run)

    def test_no_codex_or_claude_engine_goes_to_claude(self):
        for cfg, which in (({"enabled": True}, lambda n, **k: None if n == "codex" else "/x/" + n),
                           ({"enabled": True, "engine": "claude", "claude_model": "opus"}, lambda n, **k: "/x/" + n)):
            calls = []
            with mock.patch.object(critic.shutil, "which", which):
                critic.review({"critic": cfg}, draft(), run=lambda cmd, cwd, stdin=None: calls.append(cmd) or "ok")
            self.assertEqual(len(calls), 1)
            self.assertIn("-p", calls[0])

    def test_claude_skips_the_session_wrapper(self):
        # 16.10, Windows: from a session, `claude` in PATH is the hub wrapper (hosts/nested/claude → nested\claude.cmd); it needs
        # HUB_NESTED_PYTHON and HUB_NESTED_SHIM, but the critic strips them with the other HUB_* — cmd.exe got `@"" ""` and
        # failed with '"""" is not a command'. The critic sets the isolation itself: the real claude, bypassing the wrapper
        wrap, real = tempfile.mkdtemp(), tempfile.mkdtemp()
        for d, text in ((wrap, "#!/bin/sh\n# hosts/nested/claude (HUB_NESTED_FLAGS)\n"), (real, "#!/bin/sh\n")):
            for name in ("claude", "claude.cmd"):
                with open(os.path.join(d, name), "w") as f:
                    f.write(text)
                os.chmod(os.path.join(d, name), 0o755)
        with mock.patch.dict(os.environ, {"PATH": os.pathsep.join([wrap, real])}):
            os.environ.pop("HUB_REAL_CLAUDE", None)   # a run from a hub session carries the session's own
            cmd = critic.claude_cmd(critic.settings({}), [])
        self.assertEqual(os.path.dirname(cmd[0]), real)

    def test_the_real_claude_the_core_gave_the_session_is_taken_when_path_has_only_the_wrapper(self):
        # the hub starts claude by a full path (hub.json, ~/.local/bin off PATH under launchd): in the session's PATH
        # there is only the wrapper; the core gives the session HUB_REAL_CLAUDE. Before, the critic and the watchdog
        # line called a bare "claude" / which("claude") — the wrapper, which fails without HUB_NESTED_* on Windows
        from core import modelcall
        from modules.watch import watch
        wrap, real = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, wrap)
        self.addCleanup(platforms.current.rmtree, real)
        for d, text in ((wrap, "#!/bin/sh\n# hosts/nested/claude (HUB_NESTED_FLAGS)\n"), (real, "#!/bin/sh\n")):
            for name in ("claude", "claude.cmd"):
                with open(os.path.join(d, name), "w") as f:
                    f.write(text)
                os.chmod(os.path.join(d, name), 0o755)
        given = os.path.join(real, "claude")
        out = mock.Mock(stdout=json.dumps({"structured_output": {}}))
        for env, want in (({"HUB_REAL_CLAUDE": given}, given),
                          ({"HUB_REAL_CLAUDE": os.path.join(wrap, "claude")}, None),   # never the wrapper itself
                          ({"HUB_REAL_CLAUDE": "claude"}, None)):                     # not found by the core
            with self.subTest(env), mock.patch.dict(os.environ, {"PATH": wrap, "HUB_NO_MODEL": "", **env}):
                got = [critic.claude_cmd(critic.settings({}), [])[0], modelcall.command("p.md", "haiku", None)[0]]
                with mock.patch("subprocess.run", return_value=out) as run:
                    watch._claude("t", "p", {})
                got.append(run.call_args.args[0][0])
                for one in got:   # a bare name would be found through PATH: the wrapper
                    self.assertTrue(os.path.isabs(one), one)
                    self.assertNotEqual(os.path.dirname(one), wrap)
                    if want:
                        self.assertEqual(one, want)

    def test_missing_draft_is_an_error(self):
        with self.assertRaises(critic.Error):
            critic.review({"critic": {"enabled": True}}, "/nope/draft.md", run=lambda *a: "")

    def test_tests_never_call_a_model(self):
        with self.assertRaises(critic.Error) as e:
            critic._run(["true"], tempfile.gettempdir())
        self.assertIn("HUB_NO_MODEL", str(e.exception))

    def test_a_child_writing_in_the_console_code_page_does_not_crash_the_critic(self):
        # 15.10, Windows: hub critic crashed with UnicodeDecodeError (byte 0xad) in the subprocess reader thread — output is
        # read as UTF-8, but cmd.exe writes its errors in the console code page (cp866: Cyrillic en = 0xad); the cause was lost
        # ("code 1"). A child's error goes to the critic as text, not as a crash
        import sys
        said = lang.data("words", "no_data")   # Russian words of the language data with a Cyrillic en
        self.assertIn(b"\xad", said.encode("cp866"))
        try:
            err = said.encode("oem" if os.name == "nt" else "cp866")
        except UnicodeEncodeError:
            self.skipTest("the console code page has no Cyrillic")
        fake = lambda code: [sys.executable, "-c", code]   # noqa: E731
        with mock.patch.dict(os.environ, {"HUB_NO_MODEL": ""}), \
                mock.patch.object(critic, "model_env", lambda account: dict(os.environ)):
            with self.assertRaises(critic.Error) as e:
                critic._run(fake(f"import sys; sys.stderr.buffer.write({err!r}); sys.exit(1)"), tempfile.gettempdir(),
                            stdin="draft")
            self.assertNotEqual(str(e.exception), "code 1")
            if os.name == "nt":
                self.assertIn(said, str(e.exception))
            say = "«twice» without a measurement"   # a normal UTF-8 answer — as before
            self.assertEqual(critic._run(fake(f"import sys; sys.stdout.buffer.write({say.encode()!r})"),
                                         tempfile.gettempdir()), say)


class Command(unittest.TestCase):
    def run_main(self, argv, cfg):
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            return critic.main(argv, cfg), out.getvalue(), err.getvalue()

    def test_off_by_default_and_says_how_to_turn_on(self):
        code, _, err = self.run_main([draft()], {})
        self.assertEqual(code, 2)
        self.assertIn('"critic": {"enabled": true}', err)

    def test_bad_arguments(self):
        code, _, err = self.run_main([], {"critic": {"enabled": True}})
        self.assertEqual(code, 2)
        self.assertIn("hub critic <draft>", err)


class Prompt(unittest.TestCase):
    def command(self, cfg):
        from hosts import streamjson
        state = tempfile.mkdtemp()
        path = os.path.join(state, "hub.json")
        with open(path, "w") as f:
            json.dump(cfg, f)
        with mock.patch.dict(os.environ, {"HUB_CONFIG": path}):
            return launch_text(streamjson.StreamHost(state).command("s1", "id-1"))

    def test_rule_reaches_sessions_only_when_on(self):
        self.assertNotIn("hub critic", self.command({}))
        self.assertIn("hub critic", self.command({"critic": {"enabled": True}}))

    def test_listed_in_modules(self):
        from modules.web import modinfo
        state = tempfile.mkdtemp()
        m = next(x for x in modinfo.modules(state, {}, {}) if x["id"] == "critic")
        self.assertEqual(m["state"], modinfo.OFF)
        m = next(x for x in modinfo.modules(state, {"critic": {"enabled": True}}, {}) if x["id"] == "critic")
        self.assertEqual(m["state"], modinfo.ON)


class Login(unittest.TestCase):
    def test_the_nested_claude_gets_the_login_of_the_owners_settings(self):
        # 08.10 and 12.10: the session's Bash has no CLAUDE_CODE_OAUTH_TOKEN, the nested claude -p used the machine's stale
        # login ("OAuth session expired") — the critic failed. The login is taken from the Claude Code settings env, as for sessions and the watchdog
        from modules.critic import critic
        with mock.patch.dict(os.environ, {"HUB_X": "1", "KEEP": "k"}), \
                mock.patch("core.catalog.user_env", return_value={"CLAUDE_CODE_OAUTH_TOKEN": "tok-1"}):
            env = critic.model_env(None)
        self.assertEqual((env["CLAUDE_CODE_OAUTH_TOKEN"], env["KEEP"]), ("tok-1", "k"))
        self.assertNotIn("HUB_X", sorted(env))


if __name__ == "__main__":
    unittest.main()
