"""A conversation deleted from disk (a disk cleanup removed old transcripts): `--resume` fails with "No conversation found"
on every raise and the session crashes in a loop that no restart ends. The host starts a new conversation with the same
--session-id instead, tells the session the old one is gone, and the core records it in the spawn fact."""
import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from hosts import worktree
from hosts.streamjson import StreamHost
from hosts.tmux import TmuxHost

SID = "9b0e4f1c-0d7a-4c55-8a0e-1f2a3b4c5d6e"


class Base(unittest.TestCase):
    def setUp(self):
        base = tempfile.mkdtemp(prefix="resume-lost-")
        self.claude = os.path.join(base, ".claude")
        self.cwd = os.path.join(base, "work")
        os.makedirs(self.cwd)
        # what the cleanup leaves: the conversation's subagents folder, no <id>.jsonl
        self.project = os.path.join(self.claude, "projects", worktree.slug(self.cwd))
        os.makedirs(os.path.join(self.project, SID, "subagents"))
        self.env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.claude})
        self.env.start()
        self.addCleanup(self.env.stop)

    def launch(self, host, resume=True):
        """The claude command line the host would start."""
        seen, real = [], subprocess.Popen

        def popen(cmd, *a, **k):
            if SID not in cmd:   # the host's own probes (`claude --help` of the stand-in program)
                return real(cmd, *a, **k)
            seen.append(list(cmd))
            raise RuntimeError("no process in tests")
        with mock.patch("subprocess.Popen", side_effect=popen), self.assertRaises(RuntimeError):
            host.spawn("w", SID, resume=resume, cwd=self.cwd)
        return seen[-1]


class StreamJson(Base):
    def host(self):
        self.state = tempfile.mkdtemp(prefix="resume-lost-state-")
        return StreamHost(self.state, claude=(sys.executable, "-c", "pass"))

    def prompt(self):
        with open(os.path.join(self.state, "sessions", "w", "system-prompt.md"), encoding="utf-8") as f:
            return f.read()

    def test_no_conversation_anywhere_starts_a_new_one_with_the_same_id(self):
        host = self.host()
        cmd = self.launch(host)
        self.assertNotIn("--resume", cmd)
        self.assertEqual(cmd[cmd.index("--session-id") + 1], SID)
        self.assertIn(SID, self.prompt())                 # the session is told its previous conversation is gone
        self.assertIn("no longer on disk", self.prompt())
        self.assertEqual(host.conversation_lost, {"w"})

    def test_a_conversation_in_another_project_folder_is_resumed(self):
        old = os.path.join(self.claude, "projects", "-old")
        os.makedirs(old)
        open(os.path.join(old, f"{SID}.jsonl"), "w").write("talk")
        host = self.host()
        with mock.patch("shutil.copy2", side_effect=OSError("disk full")):   # even when carrying it here fails
            cmd = self.launch(host)
        self.assertIn("--resume", cmd)
        self.assertNotIn("no longer on disk", self.prompt())
        self.assertEqual(host.conversation_lost, set())

    def test_no_projects_folder_at_all_is_not_a_lost_conversation(self):
        # a cleanup deletes old transcripts, not the folder: with no projects folder at all the conversations live
        # elsewhere (another config folder, a stand-in claude) and nothing is known to be lost — resume as asked
        from platforms import current
        current.rmtree(os.path.join(self.claude, "projects"))
        host = self.host()
        cmd = self.launch(host)
        self.assertIn("--resume", cmd)
        self.assertNotIn("no longer on disk", self.prompt())
        self.assertEqual(host.conversation_lost, set())

    def test_a_fresh_start_is_not_a_lost_conversation(self):
        host = self.host()
        cmd = self.launch(host, resume=False)
        self.assertIn("--session-id", cmd)
        self.assertNotIn("no longer on disk", self.prompt())
        self.assertEqual(host.conversation_lost, set())


class Tmux(Base):
    def test_no_conversation_anywhere_starts_a_new_one_with_the_same_id(self):
        host = TmuxHost.__new__(TmuxHost)
        host.claude, host.settings, host.state_dir, host.env, host.conversation_lost = ["claude"], "s.json", "/s", {}, set()
        host.alive = lambda name: False
        host.providers = lambda: ()
        host.tool_flags = lambda **_: []
        host.session_env = lambda: {}
        host.target = lambda name: name
        seen = []
        host._launch_file = lambda spec: "/s/launch.json"
        host._tmux = lambda *a, **k: seen.append(a)
        with mock.patch("hosts.tmux.user_env", return_value={}):
            self.assertEqual(host.spawn("w", SID, resume=True, cwd=self.cwd), "spawned")
        cmd = list(seen[-1])
        self.assertNotIn("--resume", cmd)                 # the window's claude starts anew, not into the crash loop
        self.assertEqual(cmd[cmd.index("--session-id") + 1], SID)
        self.assertEqual(host.conversation_lost, {"w"})


class SpawnFact(unittest.TestCase):
    def test_the_spawn_fact_says_the_conversation_was_lost_once(self):
        from core.daemon import Hub
        from core import journal
        host = types.SimpleNamespace(conversation_lost=set())

        def spawn(name, **_):
            host.conversation_lost.add(name)
            return "spawned"
        host.spawn = spawn
        h = Hub(tempfile.mkdtemp(prefix="resume-lost-d-"), host)
        own = {"channel": "cli", "owner": True}
        h.record("session.requested", "maps", {"cwd": "/m", "session_id": "s-m", **own})
        h.execute({"do": "spawn", "session": "maps", "session_id": "s-m", "cwd": "/m", "resume": True})
        done = [e for e in journal.read(h.journal.path) if e["type"] == "host.done"][-1]
        self.assertEqual(done["data"].get("conversation"), "lost")
        self.assertEqual(host.conversation_lost, set())

    def test_the_mark_of_a_launch_that_failed_does_not_reach_the_next_one(self):
        # the host marked the launch lost, then failed (no sign-in, Popen, the window): the next launch resumed fine
        from core.daemon import Hub
        from core import journal
        host = types.SimpleNamespace(conversation_lost=set())
        calls = []

        def spawn(name, **_):
            calls.append(name)
            if len(calls) == 1:
                host.conversation_lost.add(name)
                raise OSError("no process")
            return "spawned"
        host.spawn = spawn
        h = Hub(tempfile.mkdtemp(prefix="resume-lost-d-"), host)
        own = {"channel": "cli", "owner": True}
        h.record("session.requested", "maps", {"cwd": "/m", "session_id": "s-m", **own})
        h.execute({"do": "spawn", "session": "maps", "session_id": "s-m", "cwd": "/m", "resume": True})
        events = journal.read(h.journal.path)   # the error, then the core's own next raise of the session
        self.assertEqual([e["type"] for e in events if e["type"].startswith("host.")], ["host.error", "host.done"])
        done = [e for e in events if e["type"] == "host.done"]
        self.assertEqual(done[0]["data"]["result"], "spawned")
        self.assertNotIn("conversation", done[0]["data"])
        self.assertEqual(host.conversation_lost, set())


if __name__ == "__main__":
    unittest.main()
