"""The conversation follows the session's folder: after the folder changes, `--resume` must not hit "No conversation found"."""
import os
import tempfile
import unittest
from unittest import mock

from hosts import worktree
from hosts.common import carry_transcript
from hosts.streamjson import StreamHost

SID = "e0ccefed-f3dd-4aec-81c1-75f1d5a5ca81"


class CarryTranscript(unittest.TestCase):
    def setUp(self):
        base = tempfile.mkdtemp(prefix="carry-transcript-")
        self.claude = os.path.join(base, ".claude")
        self.cwd = os.path.join(base, "workspaces", "new")
        os.makedirs(self.cwd)

    def put(self, folder, text, mtime, sub=False):
        d = os.path.join(self.claude, "projects", worktree.slug(folder) if os.path.isdir(folder) else folder)
        os.makedirs(os.path.join(d, SID) if sub else d, exist_ok=True)
        path = os.path.join(d, f"{SID}.jsonl")
        open(path, "w").write(text)
        os.utime(path, (mtime, mtime))
        if sub:
            open(os.path.join(d, SID, "note.txt"), "w").write(text)
        return d

    def mine(self):
        return os.path.join(self.claude, "projects", worktree.slug(self.cwd), f"{SID}.jsonl")

    def test_copies_the_only_transcript_and_keeps_the_original(self):
        old = self.put("-old", "talk", 100)
        self.assertEqual(carry_transcript(self.cwd, SID, self.claude), old)
        self.assertEqual(open(self.mine()).read(), "talk")
        self.assertTrue(os.path.exists(os.path.join(old, f"{SID}.jsonl")))

    def test_two_old_copies_take_the_newest_with_its_subfolder(self):
        self.put("-older", "old", 100)
        self.put("-newer", "new", 200, sub=True)
        carry_transcript(self.cwd, SID, self.claude)
        self.assertEqual(open(self.mine()).read(), "new")
        self.assertTrue(os.path.exists(os.path.join(os.path.dirname(self.mine()), SID, "note.txt")))

    def test_newer_transcript_here_is_not_overwritten(self):
        self.put("-old", "old", 100)
        self.put(self.cwd, "mine", 300)
        self.assertIsNone(carry_transcript(self.cwd, SID, self.claude))
        self.assertEqual(open(self.mine()).read(), "mine")

    def test_going_back_takes_the_newer_conversation_over_the_stale_file(self):
        # A -> B -> A: A still holds the file from the first run while the conversation went on in B; without a refresh claude would take the stale one
        self.put(self.cwd, "stale", 100)
        old = self.put("-old", "grown in the other folder", 300, sub=True)
        self.assertEqual(carry_transcript(self.cwd, SID, self.claude), old)
        self.assertEqual(open(self.mine()).read(), "grown in the other folder")
        self.assertTrue(os.path.exists(os.path.join(os.path.dirname(self.mine()), SID, "note.txt")))

    def test_a_failed_copy_leaves_no_half_file_and_does_not_raise(self):
        self.put("-old", "talk", 100)
        with mock.patch("shutil.copy2", side_effect=OSError("disk full")):
            self.assertIsNone(carry_transcript(self.cwd, SID, self.claude))
        self.assertFalse(os.path.exists(self.mine()))
        self.assertEqual(os.listdir(os.path.dirname(self.mine())), [])

    def test_the_copy_keeps_the_original_mtime(self):
        self.put("-old", "talk", 100)
        carry_transcript(self.cwd, SID, self.claude)
        self.assertEqual(int(os.path.getmtime(self.mine())), 100)

    def test_no_transcript_anywhere_does_nothing(self):
        self.assertIsNone(carry_transcript(self.cwd, SID, self.claude))
        self.assertFalse(os.path.exists(os.path.dirname(self.mine())))

    def test_resume_spawn_carries_it_and_a_fresh_start_does_not(self):
        self.put("-old", "talk", 100)
        host = StreamHost(tempfile.mkdtemp(prefix="carry-state-"), claude=("echo",))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.claude}), \
                mock.patch("subprocess.Popen", side_effect=RuntimeError("no process in tests")):
            for resume, copied in ((False, False), (True, True)):
                with self.subTest(resume=resume), self.assertRaises(RuntimeError):
                    host.spawn("w", SID, resume=resume, cwd=self.cwd)
                self.assertEqual(os.path.exists(self.mine()), copied)


class SpawnSurvivesCarryFailure(unittest.TestCase):
    def test_a_copy_error_does_not_stop_the_resume_spawn(self):
        base = tempfile.mkdtemp(prefix="carry-fail-")
        claude, cwd = os.path.join(base, ".claude"), os.path.join(base, "w")
        os.makedirs(cwd)
        old = os.path.join(claude, "projects", "-old")
        os.makedirs(old)
        open(os.path.join(old, f"{SID}.jsonl"), "w").write("talk")
        host = StreamHost(tempfile.mkdtemp(prefix="carry-state-"), claude=("echo",))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": claude}), \
                mock.patch("shutil.copy2", side_effect=OSError("disk full")), \
                mock.patch("subprocess.Popen", side_effect=RuntimeError("no process in tests")):
            with self.assertRaisesRegex(RuntimeError, "no process in tests"):
                host.spawn("w", SID, resume=True, cwd=cwd)


if __name__ == "__main__":
    unittest.main()
