"""A session's words and a turn's result reach the journal whole (a client report: a reply over 4000 characters broke off
mid-phrase on every client). Only a runaway output is cut — and marked with "cut"."""
import tests  # noqa: F401 — keeps the machine's settings out
import shutil
import tempfile
import unittest

from core import journal
from hosts.streamjson import StreamHost, StreamProcess


class Whole(unittest.TestCase):
    def facts(self, msg, kind):
        tmp = tempfile.mkdtemp(prefix="said-whole-")
        self.addCleanup(shutil.rmtree, tmp, True)
        host = StreamHost(tmp)
        popen = type("P", (), {"stdin": None, "poll": lambda self: None, "pid": 1})()
        host.procs["a"] = StreamProcess(popen)
        host.inbox.put(("a", msg))
        return [d for k, _, d in host.poll() if k == kind]

    def say(self, text):
        return self.facts({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}, "session.said")

    def test_a_long_reply_arrives_whole(self):
        text = "".join(f"Block {n}. " + "words " * 40 + "\n\n" for n in range(40))   # ~ 11000 characters
        self.assertGreater(len(text), 9000)
        (said,) = self.say(text)
        self.assertEqual(said["text"], text)
        self.assertNotIn("cut", said)

    def test_a_long_result_arrives_whole(self):
        text = "done " * 1800                                                  # 9000 characters
        (res,) = self.facts({"type": "result", "result": text, "num_turns": 1}, "turn.result")
        self.assertEqual(res["text"], text)
        self.assertNotIn("cut", res)
        self.assertNotIn("lessons", res)

    def test_a_runaway_output_is_cut_and_marked(self):
        text = "x" * (journal.TEXT_MAX + 500)
        (said,) = self.say(text)
        self.assertEqual((len(said["text"]), said["cut"]), (journal.TEXT_MAX, len(text)))
        (res,) = self.facts({"type": "result", "result": text, "num_turns": 1}, "turn.result")
        self.assertEqual((len(res["text"]), res["cut"]), (journal.TEXT_MAX, len(text)))
        self.assertEqual(res["lessons"], ["", ""])   # scanned whole: no lessons in it, so the cut lost none

    def test_the_cut_mark(self):
        self.assertTrue(journal.is_cut({"text": "x" * 4000}))                  # a journal written before the limit: cut at 4000, no mark
        self.assertTrue(journal.is_cut({"text": "x" * 90000, "cut": 120000}))
        self.assertFalse(journal.is_cut({"text": "x" * 9000}))
        self.assertFalse(journal.is_cut({"text": "short"}))
        self.assertFalse(journal.is_cut({}))


if __name__ == "__main__":
    unittest.main()
