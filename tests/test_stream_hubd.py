"""hubd with the stream-json host and a fake claude: delivery, questions, permissions, take/give, crash, restart.
No tmux and no terminal — runs on any OS."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import os
import shutil
import subprocess
import sys
import threading
import tempfile
import time
import unittest

from core import forms, journal, redact, spool
from core.daemon import Hub
from hosts.streamjson import StreamHost

FAKE = os.path.join(os.path.dirname(__file__), "fakes", "fake_claude_stream.py")


class StreamHubdTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubs-")
        self.hub = self.new_hub()
        self.hub.start()

    def tearDown(self):
        self.hub.host.kill_server()
        time.sleep(0.2)
        shutil.rmtree(self.state, ignore_errors=True)

    def new_hub(self):
        host = StreamHost(self.state, claude=(sys.executable, FAKE), env={"FAKE_TURN_S": "0.2"})
        return Hub(self.state, host, registry_dir=os.path.join(self.state, "registry"))

    def cli(self, type, entity=None, **data):
        spool.put(os.path.join(self.state, "spool"), type, entity, data)

    def until(self, pred, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            self.hub.tick()
            if pred():
                return
            time.sleep(0.03)
        self.fail(f"did not wait; {self.hub.state['sessions']} {self.hub.state['messages']}")

    def sess(self, name):
        return self.hub.state["sessions"].get(name, {})

    def events(self, type, **match):
        return [e for e in journal.read(self.hub.journal.path)
                if e["type"] == type and all(e["data"].get(k) == v for k, v in match.items())]

    def send(self, to, text):
        before = set(self.hub.state["messages"])
        self.cli("message.received", to=to, text=text)
        self.until(lambda: set(self.hub.state["messages"]) - before)
        (mid,) = set(self.hub.state["messages"]) - before
        return mid

    def delivered(self, mid):
        return self.hub.state["messages"][mid]["state"] == "delivered"

    def repo(self, dirty=False):
        """A specialist folder is a git repo with a commit (mini-hub, core/minihub.py: not git — a session by itself);
        dirty — with uncommitted changes: it will not become a mini-hub and works the old way."""
        d = tempfile.mkdtemp(prefix="hubs-repo-", dir=self.state)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@x"}
        for args in (("init", "-q"), ("commit", "-q", "--allow-empty", "-m", "x")):
            subprocess.run(["git", "-C", d, *args], check=True, capture_output=True, env=env)
        if dirty:
            with open(os.path.join(d, "wip.txt"), "w") as f:
                f.write("1")
        return d

    def new_session(self, name, talk=True, cwd=None, **extra):
        self.cli("session.requested", name, cwd=cwd or self.state, session_id=f"sid-{name}", **extra)
        # a specialist in a clean repo becomes a mini-hub at once, and the core parks a free one with nothing to do
        # (core/minihub.py)
        self.until(lambda: self.sess(name).get("state") in ("idle", "parked"))
        if talk:  # one turn: with claude -p the conversation appears on disk only after it
            mid = self.send(name, "hello")
            self.until(lambda: self.delivered(mid) and self.sess(name)["state"] == "idle")

    def test_old_claude_code_is_named_on_windows(self):
        # PR #18 review: the core hook on Windows is exec form, Claude Code older than 2.1.139 does not know it: the
        # session is forever "starting" with no explanation. It names the claude version itself — the session carries
        # the text for `hub status`
        self.hub.host.env["FAKE_CLAUDE_VERSION"] = "2.1.138"
        self.new_session("a", talk=False)
        self.until(lambda: self.sess("a").get("info"))
        problem = self.sess("a")["info"].get("problem")
        if os.name == "nt":
            self.assertIn("2.1.139", problem)
        else:
            self.assertIsNone(problem)

    def test_default_mode_is_bypass_and_message_delivered(self):
        self.new_session("a")
        mid = self.send("a", "hello")
        self.until(lambda: self.delivered(mid) and self.sess("a")["state"] == "idle")
        said = self.events("session.said")
        self.assertEqual(said[-1]["data"]["text"], "ok: hello")
        self.assertEqual(self.events("host.done", do="spawn")[0]["data"]["result"], "spawned")
        self.until(lambda: self.events("session.said"))
        # in bypassPermissions a write permission is not asked for
        mid = self.send("a", "/write")
        self.until(lambda: self.delivered(mid) and self.sess("a")["state"] == "idle")
        self.assertEqual(self.events("permission.requested"), [])

    def test_image_attachment_reaches_model_and_path_in_text(self):
        img = os.path.join(self.state, "attachments", "x", "shot.png")
        os.makedirs(os.path.dirname(img))
        with open(img, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        self.new_session("a")
        before = set(self.hub.state["messages"])
        self.cli("message.received", to="a", text="look", channel="web", owner=True,
                 attachments=[{"path": img, "name": "shot.png", "mime": "image/png", "size": 72}])
        self.until(lambda: any(e["data"]["text"] == "images seen: 1" for e in self.events("session.said")))
        heads = [e["data"]["prompt_head"] for e in self.events("turn.started")]
        self.assertTrue(any("Attachments:" in h and img in h for h in heads) or any(img in h for h in heads), heads[-1])

    def test_tool_calls_and_interrupt_are_facts(self):
        from core import views
        self.new_session("a")
        r = views.Reader()
        for ev in journal.read(self.hub.journal.path):
            r.feed(ev)
        self.assertIsNotNone(self.sess("a").get("info"))  # init: model, mode
        self.cli("session.interrupt_requested", "a", channel="web", owner=True)
        for _ in range(10):
            self.hub.tick()
            time.sleep(0.03)
        self.assertTrue(any(e["type"] == "core.rejected" and e["data"]["type"] == "session.interrupt_requested"
                            for e in journal.read(self.hub.journal.path)))  # a free session is not stopped

    def test_first_answer_visible_before_turn_ends(self):
        """The hub role: "first answer within seconds, before any work" — the model's intermediate text goes out at
        once, the turn result as a separate fact (for assignments)."""
        self.new_session("a")
        self.send("a", "/progress")
        self.until(lambda: any(e["data"]["text"] == "on it, working" for e in self.events("session.said")))
        results_then = len(self.events("turn.result"))
        self.until(lambda: len(self.events("turn.result")) > results_then)
        seqs = {e["data"]["text"]: e["seq"] for e in self.events("session.said")}
        last_result = self.events("turn.result")[-1]["seq"]
        self.assertLess(seqs["on it, working"], last_result)

    def test_question_is_a_card_and_the_answer_comes_as_a_message(self):
        # release 1 of the "tasks, specialists, executors" spec: a question as a form does not hold the turn — the
        # model gets at once "the question is shown as a card", the next message arrives without waiting for the
        # owner; his answer is a message
        self.new_session("a")
        mid = self.send("a", "/ask")
        self.until(lambda: self.sess("a").get("asks"))
        ask = self.sess("a")["asks"][0]
        later = self.send("a", "later")
        self.until(lambda: self.delivered(later))
        self.cli("permission.answered", "a", request_id=ask["request_id"], behavior="allow",
                 answers={"Which option?": "B"}, channel="web", owner=True)
        said = lambda: [e["data"]["text"] for e in self.events("session.said")]  # noqa: E731
        # the fake says back the first 50 characters: the answer's head and the question
        self.until(lambda: self.sess("a")["state"] == "idle" and any("answer to question" in t and "Which option?" in t
                                                                     for t in said()))
        self.assertEqual(said()[:3], ["ok: hello", "chosen: refused", "ok: later"])
        self.assertIn(ask["id"], said()[-1])
        self.assertTrue(self.delivered(mid))

    def test_owner_command_with_mark_at_end_is_recognised(self):
        # the core enters the owner's command first, the label at the end ("/ask\n\n[hub:mN]"): a fake recognizes it
        # that way too
        self.new_session("a")
        before = set(self.hub.state["messages"])
        self.cli("message.received", to="a", text="/ask", channel="web", owner=True)
        self.until(lambda: set(self.hub.state["messages"]) - before and self.sess("a").get("asks"))
        (mid,) = set(self.hub.state["messages"]) - before
        typed = [e for e in self.events("turn.started") if e["data"].get("prompt_head", "").startswith("/ask")]
        self.assertTrue(typed and typed[-1]["data"].get("marker") == mid, typed)
        self.cli("permission.answered", "a", request_id=self.sess("a")["asks"][0]["request_id"], behavior="allow",
                 answers={"Which option?": "A"}, channel="web", owner=True)
        self.until(lambda: self.delivered(mid) and self.sess("a")["state"] == "idle")

    def test_permission_in_default_mode_denied_with_reason(self):
        self.new_session("a", mode="default")
        self.send("a", "/write")
        self.until(lambda: self.sess("a")["state"] == "dialog")
        self.cli("permission.answered", "a", behavior="deny", message="not allowed")
        self.until(lambda: len(self.events("session.said")) >= 2)
        self.assertEqual(self.events("session.said")[-1]["data"]["text"], "denied: not allowed")

    def test_a_key_in_a_permission_request_stays_out_of_the_journal_and_the_command_runs_as_asked(self):
        token = "ghp_" + "Z" * 36
        self.new_session("a", mode="default")
        self.send("a", "/secret")
        self.until(lambda: self.sess("a")["state"] == "dialog")
        (req,) = self.sess("a")["pending"]
        self.assertEqual(req["input"]["command"], f"GH_TOKEN={redact.MASK} gh release create")   # what the owner sees
        self.cli("permission.answered", "a", request_id=req["request_id"], behavior="allow")
        self.until(lambda: len(self.events("session.said")) >= 2)
        self.assertEqual(self.events("session.said")[-1]["data"]["text"], "run as asked")
        with open(self.hub.journal.path, encoding="utf-8") as f:
            self.assertNotIn(token, f.read())
        self.assertEqual(self.hub.inputs, {})

    def test_crash_mid_turn_resumed_and_next_message_delivered(self):
        """Failures 1 and 8: the claude process died mid-turn — the core brings it back via --resume under the same
        name."""
        self.new_session("a")
        self.send("a", "/crash")
        self.until(lambda: self.events("session.exited"))
        self.until(lambda: self.sess("a").get("state") == "idle" and self.events("session.started", source="resume"))
        mid = self.send("a", "after the crash")
        self.until(lambda: self.delivered(mid))

    def test_hubd_restart_resumes_sessions(self):
        """hubd restarted (deploy, reboot): its claude children are gone; free sessions with nothing to do do not come
        back by themselves (core/parking, 2026-10-07: 22 sessions at once), a message brings the session back via
        --resume and arrives."""
        self.new_session("a")
        self.new_session("b")
        self.hub.host.kill_server()
        self.hub = self.new_hub()
        self.hub.start()
        self.until(lambda: self.sess("a").get("state") == "parked" and self.sess("b").get("state") == "parked")
        self.assertEqual(self.sess("a")["state"], "parked")
        mid = self.send("b", "after the restart")
        self.until(lambda: self.delivered(mid))
        self.assertEqual([e["entity"] for e in self.events("session.started", source="resume")], ["b"])

    def test_restart_during_a_turn_leaves_no_second_process(self):
        """2026-09-30 18:08: a hub restart mid-turn — the old claude process kept living next to the new one, both led
        one conversation for three minutes and wrote the same files. When stopping, the core kills its processes; and
        on finding at startup a live process of a past life (hubd was killed without stopping) — it kills it before
        bringing the session up."""
        from tests.fakes.procs import alive  # os.kill(pid, 0) on Windows is Ctrl+C, not a liveness check
        self.new_session("a")
        self.send("a", "/slow")
        self.until(lambda: self.sess("a")["state"] == "busy")
        old = self.sess("a")["pid"]
        self.hub.host.shutdown()                       # what hubd does on SIGTERM
        self.assertFalse(alive(old))
        self.hub = self.new_hub()
        self.hub.start()
        self.until(lambda: self.sess("a").get("state") == "idle" and self.sess("a")["pid"] != old)
        # hubd killed without stopping: the session process is left an orphan — the new hubd finds it by pid and
        # sessionId
        self.send("a", "/slow")
        self.until(lambda: self.sess("a")["state"] == "busy")
        orphan = self.sess("a")["pid"]
        self.hub.host.procs.clear()                    # the old host is gone without killing the children
        self.hub = self.new_hub()
        self.hub.start()
        self.until(lambda: self.sess("a").get("state") == "idle" and self.sess("a")["pid"] not in (old, orphan))
        self.assertFalse(alive(orphan))

    def test_a_busy_specialist_raises_an_executor_and_gets_its_result(self):
        """Spec "tasks, specialists, executors", release 2 — a live run with a fake claude: the specialist is busy and
        starts an executor for a task; it gets the task, answers, the result reaches the specialist; no forks."""
        self.new_session("ag", role="agent", about="profile", cwd=self.repo())
        self.until(lambda: self.sess("ag").get("minihub_since") and self.sess("ag")["state"] == "idle")   # mini-hub
        self.send("ag", "/slow")
        self.until(lambda: self.sess("ag")["state"] == "busy")
        self.cli("task.run_requested", "ag-task", task="count something", channel="session", owner=False,
                 sender="ag")
        self.until(lambda: self.sess("ag-task").get("state") == "idle"
                   and any(e["entity"] == "ag-task" for e in self.events("turn.result")))
        x = self.sess("ag-task")
        self.assertEqual((x["role"], x["raised_by"]), ("executor", "ag"))
        # the executor's result goes to the specialist who started it (the specialist is busy — the result waits in
        # its queue)
        self.assertTrue(any(m["session"] == "ag" and "ag-task" in m.get("origin", "")
                            for m in self.hub.state["messages"].values()))
        self.assertFalse([s for s in self.hub.state["sessions"].values() if s.get("role") == "fork"])

    def test_take_and_give(self):
        """hub take: the process is released, messages pile up; give — brought up via --resume, the accumulated ones
        are delivered."""
        self.new_session("a")
        self.cli("session.take_requested", "a")
        self.until(lambda: self.sess("a")["state"] == "held" and self.events("host.done", do="release"))
        self.assertFalse(self.hub.host.alive("a"))
        mid = self.send("a", "while with the owner")
        for _ in range(15):
            self.hub.tick()
            time.sleep(0.03)
        self.assertEqual(self.hub.state["messages"][mid]["state"], "queued")
        self.assertEqual(self.events("session.exited"), [])
        self.cli("session.give_requested", "a")
        self.until(lambda: self.delivered(mid))
        self.assertEqual(self.events("host.done", do="spawn")[-1]["data"]["result"], "spawned")

    def test_hub_take_cli_opens_resume_and_gives_back(self):
        """`hub take a`: the hub releases the session, claude --resume runs in the terminal with the same sessionId
        and the core hooks; after exit the session returns to the hub by itself."""
        self.new_session("a")
        stop = threading.Event()

        def loop():
            while not stop.is_set():
                self.hub.tick()
                time.sleep(0.05)
        t = threading.Thread(target=loop)
        t.start()
        try:
            env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CLAUDE": f"{sys.executable} {FAKE}"}
            out = subprocess.run([sys.executable, os.path.join(os.path.dirname(FAKE), "..", "..", "bin", "hub"),
                                  "take", "a"], env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                 timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            end = time.time() + 10
            while time.time() < end and self.sess("a").get("state") != "idle":
                time.sleep(0.05)
        finally:
            stop.set()
            t.join()
        kinds = [(e["type"], e["data"].get("source") or e["data"].get("do")) for e in journal.read(self.hub.journal.path)
                 if e["entity"] == "a" and e["type"] in ("session.take_requested", "host.done", "session.started",
                                                          "session.give_requested")
                 and e["data"].get("do") not in ("type",)]
        self.assertEqual(kinds, [("host.done", "spawn"), ("session.started", "startup"),
                                 ("session.take_requested", None),
                                 ("host.done", "release"), ("session.started", "resume"),
                                 ("session.give_requested", None), ("host.done", "spawn"), ("session.started", "resume")])
        self.assertEqual(self.sess("a")["state"], "idle")

    def test_message_with_sender_is_confirmed(self):
        """A label with a sender ([hub:mN from hub]) is confirmed by the hook — no repeats and no jam in the queue."""
        self.new_session("a")
        before = set(self.hub.state["messages"])
        self.cli("message.received", to="a", text="from the hub", channel="session", owner=False, sender="hub")
        self.cli("message.received", to="a", text="from the owner", channel="web", owner=True)
        self.until(lambda: len(set(self.hub.state["messages"]) - before) == 2
                   and all(self.delivered(m) for m in set(self.hub.state["messages"]) - before))
        heads = [e["data"]["prompt_head"] for e in self.events("turn.started")]
        self.assertIn("[hub:m", heads[-2])
        self.assertTrue(heads[-2].split("] ")[0].endswith("from hub"))
        self.assertTrue(heads[-1].split("] ")[0].endswith("from user"))

    def test_no_turns_restarts_fresh_with_same_id(self):
        """VM failure 2026-09-29: the session had no turns at all — after a hubd restart it is brought up with the same
        --session-id, not --resume (claude -p has no conversation yet, --resume fails)."""
        self.new_session("a", talk=False)
        self.hub.host.kill_server()
        self.hub = self.new_hub()
        self.hub.start()
        self.until(lambda: self.sess("a").get("state") == "parked")    # free — a message will bring it up (core/parking)
        mid = self.send("a", "hello")
        self.until(lambda: self.delivered(mid))
        self.assertEqual([e["data"]["source"] for e in self.events("session.started")], ["startup", "startup"])
        self.assertNotIn("failed", [self.sess("a")["state"]])

    def test_sleep_and_wake_by_message_keeps_session(self):
        """"Close" = put to sleep: the process is released; a message wakes the same session via --resume and is
        delivered."""
        self.new_session("a")
        self.cli("session.close_requested", "a")
        self.until(lambda: self.sess("a")["state"] == "asleep" and not self.hub.host.alive("a"))
        for _ in range(10):
            self.hub.tick()
            time.sleep(0.03)
        self.assertEqual(self.events("session.exited"), [])  # a sleeping one is not brought up by itself
        mid = self.send("a", "wake up")
        self.until(lambda: self.delivered(mid))
        started = self.events("session.started", source="resume")
        self.assertEqual([(e["entity"], e["data"]["session_id"]) for e in started], [("a", "sid-a")])

    def test_retry_timer_does_not_duplicate_on_stream(self):
        """The confirmation deadline passed (the hook was lost) — a retry does not write to stdin a second time."""
        self.new_session("a")
        host = self.hub.host
        self.assertEqual(host.type("a", "[hub:m1] x", message="m1"), "sent")
        self.assertEqual(host.type("a", "[hub:m1] x", message="m1"), "already sent")


if __name__ == "__main__":
    unittest.main()
