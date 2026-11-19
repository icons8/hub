"""Codex (experimental): Codex's sandbox on Windows for the hub's sessions that need one. An ordinary
user's codex.exe 0.162.1 has none in a fresh CODEX_HOME (windowsSandbox/readiness "notConfigured": every command of the
liaison, plan and dontAsk was "blocked by policy"); the core runs Codex's own setup (unelevated, no administrator) and
keeps its mode in config.toml; when the sandbox is still not ready, a session that needs it is not started — never
started with full access instead. tests/fakes/codex_w.py is that codex.exe."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import json
import os
import sys
import tempfile
import unittest

import platforms

from hosts import codex_rules, codex_sandbox, codex_set

FAKE_W = os.path.join(os.path.dirname(__file__), "fakes", "codex_w.py")


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubcxw-")
        self.addCleanup(platforms.current.rmtree, self.state)
        self.home = os.path.join(self.state, "codex-home")
        self.calls = os.path.join(self.state, "calls.jsonl")
        codex_set.write_config(self.home)

    def prepare(self, **env):
        codex_rules.prepare_home(self.home, codex=(sys.executable, FAKE_W),
                                 env={**os.environ, "CODEX_HOME": self.home, "FAKE_CODEX_CALLS": self.calls, **env})

    def sent(self, method):
        try:
            with open(self.calls, encoding="utf-8") as f:
                return [c["params"] for c in map(json.loads, f) if c["method"] == method]
        except OSError:
            return []

    def features(self, mode=None, liaison=False):
        return codex_rules.features(("x",), name="maps", mode=mode, liaison=liaison, state_dir=self.state)

    def config(self):
        with open(os.path.join(self.home, "config.toml"), encoding="utf-8") as f:
            return f.read()

    def test_windows_without_a_sandbox_sets_it_up_and_the_sandboxed_start(self):
        self.prepare()
        self.assertEqual(self.sent("windowsSandbox/setupStart"), [{"mode": "unelevated", "cwd": self.home}])
        self.assertIn('[windows]\nsandbox = "unelevated"', self.config())
        for mode in ("plan", "dontAsk", "acceptEdits", "default", None):
            self.assertEqual(self.features(mode), ("x",), mode)
        self.assertEqual(self.features(liaison=True), ("x",))
        # the hub starts again: the core rewrites config.toml, the setup's mode stays — no second setup
        codex_set.write_config(self.home)
        self.assertIn('[windows]\nsandbox = "unelevated"', self.config())
        self.prepare()
        self.assertEqual(len(self.sent("windowsSandbox/setupStart")), 1)
        self.assertEqual(self.features("plan"), ("x",))

    def test_a_failed_setup_refuses_the_sandboxed_not_full_access(self):
        self.prepare(FAKE_SETUP="fail")
        for mode in ("plan", "dontAsk", "acceptEdits", "default"):
            with self.assertRaises(RuntimeError) as e:
                self.features(mode)
            self.assertIn("the Codex sandbox is not ready", str(e.exception))
            self.assertIn("setup refused by policy", str(e.exception))
            self.assertIn(f"mode {mode}", str(e.exception))
        with self.assertRaises(RuntimeError) as e:
            self.features(liaison=True)
        self.assertIn("the liaison", str(e.exception))
        # full access needs no sandbox: those sessions start as before
        self.assertEqual(self.features(None), ("x",))
        self.assertEqual(self.features("bypassPermissions"), ("x",))

    def test_a_setup_without_an_answer_refuses_and_is_retried_later(self):
        from unittest import mock
        with mock.patch.object(codex_sandbox, "SETUP_TIMEOUT", 2):
            self.prepare(FAKE_SETUP="never")
        s = codex_sandbox._STATE[os.path.abspath(self.home)]
        self.assertFalse(s["ok"])
        self.assertIsNotNone(codex_sandbox.refusal(self.state, "read-only", "x", now=s["at"] + 1))   # no new attempt
        s["env"] = {**s["env"], "FAKE_SETUP": "ok"}   # it answers again
        self.assertIsNone(codex_sandbox.refusal(self.state, "read-only", "x", now=s["at"] + codex_sandbox.RETRY))
        self.assertEqual(len(self.sent("windowsSandbox/setupStart")), 2)

    def test_a_folder_with_a_mode_is_not_set_up_again(self):
        # elevated asks an administrator; a mode the folder has (updateRequired) is not downgraded by the core
        with open(os.path.join(self.home, "config.toml"), "a", encoding="utf-8") as f:
            f.write('\n[windows]\nsandbox = "elevated"\n')
        self.prepare(FAKE_READINESS="updateRequired")
        self.assertEqual(self.sent("windowsSandbox/setupStart"), [])
        self.assertIn('sandbox = "elevated"', self.config())
        with self.assertRaises(RuntimeError) as e:
            self.features("plan")
        self.assertIn("updateRequired", str(e.exception))

    def test_the_cores_own_mode_out_of_date_is_set_up_again(self):
        with open(os.path.join(self.home, "config.toml"), "a", encoding="utf-8") as f:
            f.write('\n[windows]\nsandbox = "unelevated"\n')
        self.prepare(FAKE_READINESS="updateRequired")
        self.assertEqual(self.sent("windowsSandbox/setupStart"), [{"mode": "unelevated", "cwd": self.home}])

    def test_a_retry_at_a_start_waits_for_the_setup_shortly(self):
        # the retry runs in hubd's tick: a setup that does not answer holds it RETRY_SETUP, not SETUP_TIMEOUT
        from unittest import mock
        with mock.patch.object(codex_sandbox, "SETUP_TIMEOUT", 2):
            self.prepare(FAKE_SETUP="never")
        s = codex_sandbox._STATE[os.path.abspath(self.home)]
        waits, real = [], codex_rules._rpc

        def rpc(codex, env, home, calls, timeout, **kw):
            waits.append((calls[0][0], timeout))
            return real(codex, env, home, calls, timeout, **kw)
        with mock.patch.object(codex_sandbox, "RETRY_SETUP", 1), mock.patch.object(codex_rules, "_rpc", rpc):
            self.assertIsNotNone(codex_sandbox.refusal(self.state, "read-only", "x", now=s["at"] + codex_sandbox.RETRY))
        self.assertIn(("windowsSandbox/setupStart", 1), waits)

    def test_a_refused_setup_is_named_at_once(self):
        # an error answer to setupStart ends the wait (its setupCompleted will not come) and gives its reason
        import time
        from unittest import mock
        t0 = time.monotonic()
        with mock.patch.object(codex_sandbox, "SETUP_TIMEOUT", 120):
            self.prepare(FAKE_SETUP="error")
        self.assertIn("setup is not allowed here", codex_sandbox._STATE[os.path.abspath(self.home)]["why"])
        self.assertLess(time.monotonic() - t0, 100)  # portable: the bound is the 120 s wait itself, not the machine's speed

    def test_a_trust_check_without_an_answer_is_not_taken_for_posix(self):
        # the first hooks/list may time out (slow first start after an update): the platform is asked again, not
        # assumed — a Windows Codex is set up, the sandboxed refused while it does not answer
        env = {**os.environ, "CODEX_HOME": self.home, "FAKE_CODEX_CALLS": self.calls}
        self.assertFalse(codex_sandbox.prepare(self.home, ("/nonexistent/codex",), env, {}))
        self.assertIsNotNone(codex_sandbox.refusal(self.state, "read-only", "x"))
        self.assertTrue(codex_sandbox.prepare(self.home, (sys.executable, FAKE_W), env, {}))
        self.assertEqual(len(self.sent("windowsSandbox/setupStart")), 1)
        self.assertIsNone(codex_sandbox.refusal(self.state, "read-only", "x"))

    def test_an_unanswered_retry_keeps_the_known_keys_path(self):
        self.prepare()
        home = os.path.abspath(self.home)
        t = codex_rules._TRUSTED[home]
        t["ok"], t["codex"] = False, ("/nonexistent/codex",)
        self.assertFalse(codex_rules.trusted(self.state, now=t["at"] + codex_rules.RETRY))
        self.assertEqual(codex_rules.flags_path(self.home), "C:\\<session-flags>\\config.toml")

    def test_the_trust_retry_brings_the_sandbox_its_platform(self):
        # the retry's own run answers the platform and readiness: the sandbox is checked by them, not asked again
        from unittest import mock
        self.assertFalse(codex_sandbox.prepare(self.home, ("/nonexistent/codex",), {**os.environ}, {}))
        s = codex_sandbox._STATE[os.path.abspath(self.home)]
        s["codex"], s["env"] = (sys.executable, FAKE_W), {**os.environ, "CODEX_HOME": self.home,
                                                          "FAKE_CODEX_CALLS": self.calls}
        with mock.patch.object(codex_sandbox, "_ask", wraps=codex_sandbox._ask) as ask:
            codex_sandbox.retried(self.home, {"platform": "windows", "readiness": "notConfigured"})
        self.assertIsNone(codex_sandbox.refusal(self.state, "read-only", "x"))
        self.assertEqual(ask.call_count, 1)   # only the readiness after the setup (a new process must say it)

    def test_the_trust_retry_readies_the_sandbox(self):
        # both checks failed at the host's start (Codex did not answer); the trust retry at a session start answers
        # — the sandbox is set up by that, before its own retry is due
        bad = {**os.environ, "CODEX_HOME": self.home, "FAKE_CODEX_CALLS": self.calls}
        codex_rules.prepare_home(self.home, codex=("/nonexistent/codex",), env=bad)
        home = os.path.abspath(self.home)
        t, s = codex_rules._TRUSTED[home], codex_sandbox._STATE[home]
        self.assertFalse(s["ok"])
        t["codex"] = s["codex"] = (sys.executable, FAKE_W)
        self.assertTrue(codex_rules.trusted(self.state, now=t["at"] + codex_rules.RETRY))
        self.assertIsNone(codex_sandbox.refusal(self.state, "read-only", "x", now=codex_sandbox._STATE[home]["at"]))

    def test_the_trust_retry_learns_the_keys_path_too(self):
        # a first trust check without an answer left no path: the retry at a start must bring the Windows one
        self.prepare()
        home = os.path.abspath(self.home)
        codex_rules._FLAGS[home] = None
        t = codex_rules._TRUSTED[home]
        t["ok"] = False
        self.assertTrue(codex_rules.trusted(self.state, now=t["at"] + codex_rules.RETRY))
        self.assertEqual(codex_rules.flags_path(self.home), "C:\\<session-flags>\\config.toml")

    def test_an_account_folder_has_its_own_sandbox_run_with_its_arguments(self):
        # the Codex pool: an account folder is its own CODEX_HOME — its own [windows] mode, set up by app-servers with
        # the folder's arguments (-c sqlite_home=<the hub folder>: no databases of its own there), its retries too; a
        # sandboxed session in it is refused by that folder's readiness, not the hub folder's
        from core import codexcli
        self.prepare()   # the hub folder: set up and ready
        acc = os.path.join(self.state, "moddata", "accounts", "codex", "codex-2")
        codex_set.write_config(acc)
        folder = codexcli.args(self.state, acc)
        self.assertTrue(folder)
        argv = os.path.join(self.state, "argv")

        def runs():
            out = []
            for name in sorted(os.listdir(argv)) if os.path.isdir(argv) else ():
                with open(os.path.join(argv, name), encoding="utf-8") as f:
                    out.append(json.load(f)["argv"])
            platforms.current.rmtree(argv)
            return out

        def with_folder(got):
            self.assertGreaterEqual(len(got), 1)
            for run in got:
                self.assertIn(folder, [run[i:i + len(folder)] for i in range(len(run))], run)
        codex_rules.prepare_home(acc, codex=(sys.executable, FAKE_W), args=folder,
                                 env={**os.environ, "CODEX_HOME": acc, "FAKE_CODEX_CALLS": self.calls,
                                      "FAKE_SETUP": "fail", "FAKE_ARGV_DIR": argv})
        got = runs()
        self.assertGreaterEqual(len(got), 2, got)   # the trust check and the sandbox's setup at least
        with_folder(got)
        refused = lambda home: codex_rules.features(("x",), name="maps", mode="plan", state_dir=self.state,   # noqa: E731
                                                    home=home)
        self.assertEqual(refused(self.home), ("x",))
        with self.assertRaises(RuntimeError) as e:
            refused(acc)
        self.assertIn("the Codex sandbox is not ready", str(e.exception))
        # its retry at a session start: the same folder's arguments
        s = codex_sandbox._STATE[os.path.abspath(acc)]
        s["env"], s["at"] = {**s["env"], "FAKE_SETUP": "ok"}, s["at"] - codex_sandbox.RETRY
        self.assertEqual(refused(acc), ("x",))
        with_folder(runs())
        # and the trust retry's answer readying it
        codex_sandbox._STATE[os.path.abspath(acc)]["ok"] = False   # a new record after that retry
        codex_sandbox.retried(acc, {"platform": "windows", "readiness": "notConfigured"})
        with_folder(runs())

    def test_posix_codex_needs_no_setup(self):
        # Linux's codex says "notConfigured" too (live 0.162.1): only initialize's platformFamily tells Windows
        self.prepare(FAKE_PLATFORM="unix")
        self.assertEqual(self.sent("windowsSandbox/setupStart"), [])
        self.assertNotIn("[windows]", self.config())
        self.assertEqual(self.features("plan"), ("x",))
        self.assertEqual(self.features(liaison=True), ("x",))


if __name__ == "__main__":
    unittest.main()
