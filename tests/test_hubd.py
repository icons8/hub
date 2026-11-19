"""The whole hubd: core + tmux host + hooks + fake claude. Failure scenarios from the brief, section 8."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
import uuid

from core import journal, spool
from core.daemon import Hub
from hosts.tmux import TmuxHost

from tests.fakes.cleanup import cleanup  # noqa: E402

FAKE = os.path.join(os.path.dirname(__file__), "fakes", "fake_claude.py")


@unittest.skipUnless(shutil.which("tmux"), "no tmux")
class HubdTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubd-")
        self.registry = os.path.join(self.state, "registry")
        self.socket = f"hubtest-{uuid.uuid4().hex[:8]}"
        self.hub = self.new_hub()

    def tearDown(self):
        cleanup(self.hub.host, self.state)

    def new_hub(self):
        host = TmuxHost(self.state, claude=(sys.executable, FAKE), socket=self.socket,
                        env={"FAKE_REGISTRY_DIR": self.registry, "FAKE_TURN_S": "0.3"})
        return Hub(self.state, host, registry_dir=self.registry)

    def cli(self, type, entity=None, **data):
        spool.put(os.path.join(self.state, "spool"), type, entity, data)

    def until(self, pred, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            self.hub.tick()
            if pred():
                return True
            time.sleep(0.05)
        self.fail(f"timed out; state: {self.hub.state['sessions']} {self.hub.state['messages']}")

    def sess(self, name):
        return self.hub.state["sessions"].get(name, {}).get("state")

    def msg(self, mid):
        return self.hub.state["messages"].get(mid, {})

    def events(self, type, **match):
        return [e for e in journal.read(self.hub.journal.path)
                if e["type"] == type and all(e["data"].get(k) == v for k, v in match.items())]

    def only_message(self):
        (mid,) = self.hub.state["messages"]
        return mid

    def new_session(self, name):
        self.cli("session.requested", name, cwd=self.state, session_id=f"sid-{name}")
        self.until(lambda: self.sess(name) == "idle")

    def test_message_waits_for_dialog_then_delivered_once(self):
        """Failure 4: a session on a window — the message waits and is not typed; the window closed, the turn ended — delivered."""
        self.hub.start()
        self.new_session("a")
        self.hub.host.type("a", "/dialog")  # a person at the terminal, the model opened AskUserQuestion
        self.until(lambda: self.sess("a") == "dialog")
        self.cli("message.received", to="a", text="important")
        self.until(lambda: self.hub.state["messages"])
        mid = self.only_message()
        for _ in range(20):
            self.hub.tick()
            time.sleep(0.05)
        self.assertEqual(self.msg(mid)["state"], "queued")
        self.assertEqual(self.events("host.done", do="type"), [])
        self.hub.host._tmux("send-keys", "-t", "=hub-a:", "1")  # the window was closed
        self.until(lambda: self.msg(mid).get("state") == "delivered")
        self.assertEqual(len(self.events("turn.started", marker=mid)), 1)
        self.assertEqual(len(self.events("host.done", do="type")), 1)

    def test_reboot_sessions_resumed_under_same_names(self):
        """Reboot: tmux and hubd are gone; the new hubd brings sessions up through --resume under the same names,
        a message that did not arrive before the reboot is delivered once. An idle free session (b) does not come up
        by itself — the first message brings it up (core/parking, 2026-10-07)."""
        self.hub.start()
        self.new_session("a")
        self.new_session("b")
        self.hub.host.type("b", "hello")  # b had a turn too — it comes up through --resume
        self.until(lambda: self.events("turn.stopped"))
        self.hub.host.type("a", "/dialog")
        self.until(lambda: self.sess("a") == "dialog")
        self.cli("message.received", to="a", text="after the reboot")
        self.until(lambda: self.hub.state["messages"])
        mid = self.only_message()
        self.hub.host.kill_server()  # the machine went down
        self.hub = self.new_hub()     # hubd started by systemd after boot
        self.hub.start()
        self.until(lambda: self.sess("a") == "idle" and self.sess("b") == "parked")
        self.assertEqual(sorted(self.hub.host.sessions()), ["a"])
        self.cli("message.received", to="b", text="wake up")
        self.until(lambda: self.sess("b") == "idle" and "b" in self.hub.host.sessions())
        resumed = self.events("session.started", source="resume")
        self.assertEqual(sorted(e["entity"] for e in resumed), ["a", "b"])
        self.assertEqual({e["data"]["session_id"] for e in resumed}, {"sid-a", "sid-b"})
        self.until(lambda: self.msg(mid).get("state") == "delivered")
        self.assertEqual(len(self.events("turn.started", marker=mid)), 1)

    def test_start_after_an_unclean_stop_writes_a_snapshot(self):
        """Mac reboot on 2026-10-02: hubd was killed without a snapshot — the page and the watchdog rebuilt everything
        from the last snapshot with the new code and showed a message the core never created ("sending", attempt 2).
        Now at start after an unclean stop the core writes a snapshot of its state — readers check against it."""
        self.hub.start()
        self.new_session("a")
        self.hub.host.kill_server()
        self.hub = self.new_hub()
        self.hub.start()
        events = journal.read(self.hub.journal.path)
        starts = [e["seq"] for e in events if e["type"] == "core.started"]
        snaps = [e for e in events if e["type"] == "core.snapshot" and e["data"].get("why") == "start"]
        self.assertEqual(len(starts), 2)
        self.assertEqual([e["seq"] for e in snaps], [starts[1] - 1])
        self.assertIn("a", snaps[0]["data"]["state"]["sessions"])

    def lost_busy_session(self, clean):
        """A session on a window (a busy one), then the machine goes down: after a clean stop (hubd wrote its stop snapshot) or not."""
        self.hub.host.type("a", "/dialog")
        self.until(lambda: self.sess("a") == "dialog")
        if clean:
            self.hub.snapshot("stop")   # as hubd's stop() does, the last journal line
        self.hub.host.kill_server()
        self.hub = self.new_hub()
        self.hub.start()
        self.until(lambda: self.events("session.exited", why="reconcile") and self.sess("a") == "idle")
        return self.events("session.exited", why="reconcile")[-1]

    def test_a_session_lost_with_no_clean_shutdown_is_marked_a_crash_after_a_clean_stop_it_is_not(self):
        """The hub said "a hub restart cut your turn" after a machine crash too: hubd tells the start that follows a stop with
        no stop snapshot (the same sign as the start snapshot), and the lost turn's continuation says so."""
        self.hub.start()
        self.new_session("a")
        self.assertNotIn("crash", self.lost_busy_session(clean=True)["data"])
        (clean,) = [m for m in self.hub.state["messages"].values() if m["session"] == "a" and m["id"].endswith("x3")]
        self.assertTrue(clean["text"].startswith("A hub restart cut off"), clean["text"])
        self.assertTrue(self.lost_busy_session(clean=False)["data"]["crash"])
        crashed = [m for m in self.hub.state["messages"].values() if m["session"] == "a" and m["id"].endswith("x3")]
        self.assertIn("without a clean shutdown", crashed[-1]["text"])
        # later in the same life one session lost on its own is not "after a crash"
        self.hub.host.type("a", "/dialog")
        self.until(lambda: self.sess("a") == "dialog")
        self.assertFalse(self.hub.after_crash)

    def test_start_touches_the_pulse_for_hub_status(self):
        """`hub status` reads hubd.alive: a start that takes long (a snapshot, git) before its first tick left the old pulse,
        and the status said "hubd is not running?" about a hubd that was starting."""
        self.hub.start()
        beat = time.time() - 600
        os.utime(os.path.join(self.state, "hubd.alive"), (beat, beat))
        self.hub = self.new_hub()
        self.hub.start()
        # portable: the pulse was set 600 s back, the check is a margin of five minutes, not a second
        self.assertGreater(os.path.getmtime(os.path.join(self.state, "hubd.alive")), beat + 300)

    def test_start_says_since_when_the_core_was_down_and_what_waits_in_the_spool(self):
        """The line after start-up: when the previous hubd last showed signs of life —
        the heartbeat or the last event, whichever is later; how many messages wait in the mailbox — the core has
        not recorded them yet."""
        self.hub.start()
        self.hub.tick()
        last = journal.read(self.hub.journal.path)[-1]["ts"]
        beat = time.time() - 600
        os.utime(os.path.join(self.state, "hubd.alive"), (beat, beat))   # heartbeat older than the last event
        self.cli("message.received", text="are you there?", channel="web", owner=True)
        self.cli("turn.tool", "a", name="Read")
        self.hub = self.new_hub()
        self.hub.start()
        first, again = self.events("core.started")
        self.assertEqual((first["data"]["down_since"], first["data"]["spooled"]), (None, 0))   # first start
        self.assertEqual((again["data"]["down_since"], again["data"]["spooled"]), (last, 1))
        later = time.time() + 5
        os.utime(os.path.join(self.state, "hubd.alive"), (later, later))  # heartbeat later than the last event
        self.hub = self.new_hub()
        self.hub.start()
        self.assertAlmostEqual(self.events("core.started")[-1]["data"]["down_since"], later, places=2)

    def test_hubd_killed_mid_delivery(self):
        """The waiter is gone: hubd was killed after typing, before the confirmation. The new hubd takes the
        confirmation from the mailbox — delivered, no second typing."""
        self.hub.start()
        self.new_session("a")
        self.cli("message.received", to="a", text="x")
        self.until(lambda: self.hub.state["messages"] and self.msg(self.only_message()).get("state") == "typing")
        mid = self.only_message()
        del self.hub  # died, nobody reads the mailbox
        time.sleep(1.0)  # the hook had time to put turn.started into the mailbox
        self.hub = self.new_hub()
        self.hub.start()
        self.until(lambda: self.msg(mid).get("state") == "delivered")
        self.assertEqual(len(self.events("host.done", do="type")), 1)

    def test_claude_killed_is_resumed(self):
        """Failures 1 and 8: the claude process was killed (OOM) — the core sees it is gone and brings it up under the same name."""
        self.hub.start()
        self.new_session("a")
        self.hub.host.type("a", "hello")  # one turn: there is a conversation for --resume
        self.until(lambda: self.events("turn.stopped"))
        pid = self.hub.host.info("a")["pid"]
        os.kill(pid, signal.SIGKILL)
        self.until(lambda: self.events("session.exited"))
        self.until(lambda: self.sess("a") == "idle" and self.hub.state["sessions"]["a"]["pid"] != pid)
        self.assertEqual(self.events("session.started", source="resume")[0]["entity"], "a")

    def test_owner_exit_is_not_resumed(self):
        self.hub.start()
        self.new_session("a")
        self.hub.host.type("a", "/exit")
        self.until(lambda: self.sess("a") == "asleep")
        for _ in range(20):
            self.hub.tick()
            time.sleep(0.05)
        self.assertEqual(self.events("session.exited"), [])
        self.assertFalse(self.hub.host.alive("a"))

    def test_address_by_socket_reaches_same_session(self):
        """Failure 5: a message to a socket address from the registry is delivered to the same session."""
        self.hub.start()
        self.new_session("a")
        self.until(lambda: self.hub.state["sessions"]["a"].get("socket"))
        sock = self.hub.state["sessions"]["a"]["socket"]
        self.cli("message.received", to="uds:" + sock, text="over the socket")
        self.until(lambda: self.hub.state["messages"] and self.msg(self.only_message()).get("state") == "delivered")
        self.assertEqual(self.msg(self.only_message())["session"], "a")


if __name__ == "__main__":
    unittest.main()
