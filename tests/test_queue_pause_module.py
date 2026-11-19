"""A core module with the queue.pause right (Module API 2.10) sets a pause of the executor queue of its own and lifts only
that one; the daemon checks the right before the fact is written, a module without it is refused."""
import tests  # noqa: F401 — ignore this machine's settings
import json
import os
import tempfile
import time
import unittest

from core import modules, version
from core.daemon import Hub


class QueuePauseModule(unittest.TestCase):
    def setUp(self):
        self.h = Hub(tempfile.mkdtemp(prefix="qpause-"), type("H", (), {"alive": lambda s, n: True,
                                                                         "poll": lambda s: []})(),
                     registry_dir=tempfile.mkdtemp())
        self.h.execute = lambda a: None
        self.h.modules.found = []
        self.h.modules.written = {"builds": {"capabilities": ["queue.pause"]}, "other": {"capabilities": ["net"]}}

    def fact(self, name, kind, why=""):
        """hubmod.fact of a module process: the core names the sender (spool.outsider)."""
        item = {"type": kind, "entity": None, "src": "module", "ts": time.time(),
                "data": {"why": why, "channel": "plugin", "owner": False, "sender": f"mod:{name}"}}
        path = os.path.join(self.h.state_dir, f"spool-{name}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(item, f)
        self.h.spooled(path, item)

    def test_the_right_exists_and_is_a_2_10_right(self):
        self.assertIn("queue.pause", modules.CAPABILITIES)
        self.assertEqual(version.manifest_since("queue.pause"), "2.10")

    def test_a_module_with_the_right_pauses_and_lifts_its_own_pause(self):
        self.fact("builds", "task.queue_paused", "heavy build")
        self.assertEqual(self.h.state["queue_paused"]["by"], "mod:builds")
        self.assertEqual(self.h.state["queue_paused"]["why"], "heavy build")
        self.fact("builds", "task.queue_resumed")
        self.assertNotIn("queue_paused", self.h.state)

    def test_a_module_without_the_right_is_refused(self):
        self.fact("other", "task.queue_paused", "x")
        self.assertNotIn("queue_paused", self.h.state)
        self.assertIn("queue.pause", self.h.queue_pause_admits("mod:other"))
        self.assertIn("queue.pause", self.h.queue_pause_admits("mod:unknown"))
        self.assertIsNone(self.h.queue_pause_admits("mod:builds"))

    def test_a_module_cannot_lift_the_owners_pause(self):
        self.h.record("task.queue_paused", None, {"why": "triage", "channel": "cli", "owner": True})
        self.assertEqual(self.h.state["queue_paused"]["by"], None)
        self.fact("builds", "task.queue_resumed")
        self.assertEqual(self.h.state["queue_paused"]["why"], "triage")
