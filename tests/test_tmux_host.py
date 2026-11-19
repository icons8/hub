"""Host on a real tmux with a fake claude: paste, Enter, window, startup. No model, deterministic."""
import tests  # noqa: F401 — hide the machine's settings (~/.config/hub-core), see tests/__init__.py
import os
import shutil
import sys
import tempfile
import time
import unittest
import uuid

from core import spool
from hosts.tmux import TmuxHost

from tests.fakes.cleanup import cleanup  # noqa: E402

FAKE = os.path.join(os.path.dirname(__file__), "fakes", "fake_claude.py")


def wait_for(pred, timeout=8.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        got = pred()
        if got:
            return got
        time.sleep(step)
    return pred()


@unittest.skipUnless(shutil.which("tmux"), "no tmux")
class TmuxHostTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubtest-")
        self.spool = os.path.join(self.state, "spool")
        self.host = TmuxHost(self.state, claude=(sys.executable, FAKE), socket=f"hubtest-{uuid.uuid4().hex[:8]}",
                             env={"FAKE_REGISTRY_DIR": os.path.join(self.state, "registry"), "FAKE_TURN_S": "0.3"})
        self.seen = []

    def tearDown(self):
        cleanup(self.host, self.state)

    def events(self):
        for path, ev in spool.take(self.spool):
            os.unlink(path)
            self.seen.append(ev)
        return self.seen

    def wait_event(self, type, **match):
        def found():
            for ev in self.events():
                if ev["type"] == type and all(ev["data"].get(k) == v for k, v in match.items()):
                    return ev
        ev = wait_for(found)
        self.assertIsNotNone(ev, f"no {type} {match}; there was: {[(e['type'], e['data']) for e in self.seen]}")
        return ev

    def count(self, type, **match):
        self.events()
        return sum(1 for e in self.seen if e["type"] == type and all(e["data"].get(k) == v for k, v in match.items()))

    def test_spawn_is_a_command_not_typed_line(self):
        """Failure 3: the launch is not typed into the shell, so the session starts on the first try and a long name
        is not cut."""
        name = "s-" + "x" * 300
        self.host.spawn(name, "sid-1", cwd=self.state)
        ev = self.wait_event("session.started", session_id="sid-1", source="startup")
        self.assertEqual(ev["entity"], name)
        self.assertEqual(ev["src"], "hook")
        self.assertIsNotNone(self.host.info(name))

    def test_the_sessions_secrets_stay_off_every_command_line(self):
        # `tmux -e KEY=…` put the session's environment (its login token among it) on the tmux client's command line:
        # any user of the machine reads it in `ps` or /proc/<pid>/cmdline. The pane's program gets it from a 0600 file
        # that is removed before claude starts
        import json
        from unittest import mock
        from core import perms
        dump = os.path.join(self.state, "dump.json")
        code = ("import json, os, sys; m = os.umask(0); "
                "json.dump({'argv': sys.argv, 'cmdline': open('/proc/self/cmdline', 'rb').read().decode() "
                "if os.path.exists('/proc/self/cmdline') else '', 'env': dict(os.environ), 'umask': m}, "
                "open(os.environ['DUMP'], 'w'))")
        # a launch file a pane never read (its program did not start) goes when the host starts: it holds a login
        stale = os.path.join(self.state, "launch", "launch-old.json")
        os.makedirs(os.path.dirname(stale))
        with open(stale, "w") as f:
            f.write("{}")
        os.utime(stale, (time.time() - 3600, time.time() - 3600))
        host = TmuxHost(self.state, claude=(sys.executable, "-I", "-c", code), socket=self.host.socket,
                        env={"DUMP": dump, "CLAUDE_CODE_OAUTH_TOKEN": "login-s3cret-probe"})
        self.assertFalse(os.path.exists(stale))
        calls, real = [], host._tmux
        host._tmux = lambda *a, **kw: (calls.append(a), real(*a, **kw))[1]
        project = os.path.join(self.state, "project")   # the pane's folder is the session's project: its json.py
        os.makedirs(project)                            # does not replace the loader's json
        with open(os.path.join(project, "json.py"), "w") as f:
            f.write("raise SystemExit('the project json.py ran')\n")
        with mock.patch.dict(os.environ, {perms.UMASK_ENV: "0o27"}):
            host.spawn("e", "sid-e", cwd=project)
        got = wait_for(lambda: os.path.exists(dump) and os.path.getsize(dump) and json.load(open(dump)))
        self.assertTrue(got)
        line = " ".join(str(x) for a in calls for x in a)
        self.assertNotIn("login-s3cret-probe", line)
        self.assertNotIn("-e", [x for a in calls for x in a])
        self.assertNotIn("login-s3cret-probe", " ".join(got["argv"]) + got["cmdline"])
        self.assertEqual(got["env"].get("CLAUDE_CODE_OAUTH_TOKEN"), "login-s3cret-probe")
        self.assertEqual((got["env"].get("HUB_SESSION"), got["env"].get("HUB_STATE_DIR")), ("e", self.state))
        self.assertIn("TMUX", got["env"])   # the pane's own environment (TERM, TMUX) stays
        self.assertEqual(got["umask"], 0o027)
        left = [n for _, _, fs in os.walk(self.state) for n in fs if n.startswith("launch-")]
        self.assertEqual(left, [])   # the loader removed its file

    def test_type_delivers_with_marker(self):
        self.host.spawn("a", "sid-a", cwd=self.state)
        self.wait_event("session.started")
        self.assertEqual(self.host.type("a", "[hub:m7] hello\nsecond line", message="m7"), "typed")
        ev = self.wait_event("turn.started", marker="m7")
        self.assertEqual(ev["data"]["prompt_head"], "[hub:m7] hello\nsecond line")
        self.wait_event("turn.stopped")

    def test_lost_enter_retry_presses_enter_only(self):
        """Failure 2: Enter was lost after /reload-plugins. The retry sees the marker on screen and presses only Enter,
        so the message goes once and without doubled text."""
        self.host.spawn("a", "sid-a", cwd=self.state)
        self.wait_event("session.started")
        self.host.type("a", "/reload-plugins")
        wait_for(lambda: "plugins reloaded" in self.host.screen("a"))
        self.host.type("a", "[hub:m9] x", message="m9")
        time.sleep(0.8)
        self.assertEqual(self.count("turn.started", marker="m9"), 0)
        self.assertEqual(self.host.type("a", "[hub:m9] x", message="m9"), "enter only")
        ev = self.wait_event("turn.started", marker="m9")
        self.assertEqual(ev["data"]["prompt_head"], "[hub:m9] x")
        time.sleep(0.5)
        self.assertEqual(self.count("turn.started", marker="m9"), 1)

    def test_lost_enter_with_collapsed_long_paste(self):
        """Live finding 29.09: a long paste is collapsed into "[Pasted text …]". The marker is typed in letters and
        visible, so the retry presses only Enter and the text goes whole and once."""
        self.host.spawn("a", "sid-a", cwd=self.state)
        self.wait_event("session.started")
        self.host.type("a", "/reload-plugins")
        wait_for(lambda: "plugins reloaded" in self.host.screen("a"))
        body = "\n".join(f"line {i}" for i in range(40))
        self.host.type("a", "[hub:m5] " + body, message="m5")
        self.assertTrue(wait_for(lambda: "[hub:m5] [Pasted text #2 +40 lines]" in self.host.screen("a")))
        self.assertEqual(self.host.type("a", "[hub:m5] " + body, message="m5"), "enter only")
        ev = self.wait_event("turn.started", marker="m5")
        self.assertEqual(ev["data"]["prompt_head"], ("[hub:m5] " + body)[:200])
        time.sleep(0.5)
        self.assertEqual(self.count("turn.started", marker="m5"), 1)

    def test_dialog_swallows_typed_text(self):
        """Failure 4 as it is: text typed into an open window is lost. That is why the core does not type into the window."""
        self.host.spawn("a", "sid-a", cwd=self.state)
        self.wait_event("session.started")
        self.host.type("a", "/dialog")
        wait_for(lambda: "DIALOG: choose" in self.host.screen("a"))
        self.host.type("a", "[hub:m3] x", message="m3")
        self.host._tmux("send-keys", "-t", "=hub-a:", "1")
        self.wait_event("turn.stopped")
        time.sleep(0.5)
        self.assertEqual(self.count("turn.started", marker="m3"), 0)

    def test_kill_and_resume_under_same_name(self):
        self.host.spawn("a", "sid-a", cwd=self.state)
        self.wait_event("session.started", source="startup")
        pid1 = self.host.info("a")["pid"]
        self.host.kill_server()  # like a reboot: tmux and everything in it is gone
        self.assertTrue(wait_for(lambda: not self.host.alive("a")))
        self.assertEqual(self.host.sessions(), [])
        self.host.spawn("a", "sid-a", resume=True, cwd=self.state)
        self.wait_event("session.started", source="resume", session_id="sid-a")
        self.assertNotEqual(self.host.info("a")["pid"], pid1)
        self.assertEqual(self.host.sessions(), ["a"])

    def test_exit_by_owner_reports_session_ended(self):
        self.host.spawn("a", "sid-a", cwd=self.state)
        self.wait_event("session.started")
        self.host.type("a", "/exit")
        self.wait_event("session.ended", reason="prompt_input_exit")
        self.assertTrue(wait_for(lambda: not self.host.alive("a")))


if __name__ == "__main__":
    unittest.main()
