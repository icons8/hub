"""Module API 2.10: a core module with the message.rush right sends an urgent message (`now`), typed into the session in
the middle of its turn like the hub's `--now`; the daemon checks the right before the fact is written, a module without
it is refused, and so is a plugin."""
import tests  # noqa: F401 — ignore this machine's settings
import json
import os
import tempfile
import unittest

from core import modules, version
from core.daemon import Hub

NOW = 1_800_000_000.0


class RushModule(unittest.TestCase):
    def setUp(self):
        self.clock = [NOW]
        self.h = Hub(tempfile.mkdtemp(prefix="rush-"), type("H", (), {"alive": lambda s, n: True,
                                                                       "poll": lambda s: []})(),
                     registry_dir=tempfile.mkdtemp(), clock=lambda: self.clock[0])
        self.h.execute = lambda a: None
        self.h.modules.found = []
        self.h.modules.written = {"relay": {"capabilities": ["message.rush"]}, "other": {"capabilities": ["http"]}}
        owner = {"channel": "cli", "owner": True}
        self.h.record("session.requested", "maps", {"cwd": "/w", "session_id": "s-maps", **owner})
        self.h.record("session.started", "maps", {})
        self.h.record("turn.started", "maps", {"marker": None, "prompt_head": "long work"})
        self.h.record("turn.tool", "maps", {"id": "t1", "name": "Bash", "input": {"command": "make"}})

    def fact(self, sender, now=True, src="module"):
        """A message from a module process (hubmod.fact) or a plugin: the core names the sender (spool.outsider)."""
        self.clock[0] += 1
        data = {"to": "maps", "text": "the build finished", "channel": "plugin", "owner": False, "sender": sender}
        if now:
            data["now"] = True
        item = {"type": "message.received", "entity": None, "src": src, "ts": self.clock[0], "data": data}
        path = os.path.join(self.h.state_dir, f"spool-{self.clock[0]}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(item, f)
        before = set(self.h.state["messages"])
        self.h.spooled(path, item)
        new = set(self.h.state["messages"]) - before
        return self.h.state["messages"][new.pop()] if new else None

    def test_the_right_exists_and_is_a_2_10_right(self):
        self.assertIn("message.rush", modules.CAPABILITIES)
        self.assertEqual(version.manifest_since("message.rush"), "2.10")

    def test_a_module_with_the_right_is_typed_in_mid_turn(self):
        msg = self.fact("mod:relay")
        self.assertIsNotNone(msg)
        self.assertTrue(msg.get("injected"))
        self.assertEqual(msg["state"], "delivered")

    def test_a_module_without_the_right_is_refused(self):
        self.assertIsNone(self.fact("mod:other"))
        self.assertIn("message.rush", self.h.rush_admits("mod:other"))
        self.assertIn("message.rush", self.h.rush_admits("mod:unknown"))
        self.assertIsNone(self.h.rush_admits("mod:relay"))

    def test_without_now_the_message_waits_for_the_turn_as_before(self):
        msg = self.fact("mod:other", now=False)
        self.assertIsNotNone(msg)
        self.assertFalse(msg.get("injected"))

    def test_a_plugin_may_not_rush(self):
        self.assertIsNone(self.fact("ext:relay", src="ext:relay"))

    def test_an_urgent_task_from_a_module_is_refused_even_with_the_right(self):
        # `hub task new <session> … --now` from a module process: delegation.created with now, named mod:<name>
        for name in ("mod:relay", "mod:other"):
            self.clock[0] += 1
            data = {"session": "maps", "text": "rebuild the index", "now": True, "channel": "plugin", "owner": False,
                    "sender": name}
            item = {"type": "delegation.created", "entity": None, "src": "module", "ts": self.clock[0], "data": data}
            path = os.path.join(self.h.state_dir, f"spool-{self.clock[0]}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(item, f)
            self.h.spooled(path, item)
            self.assertEqual(self.h.state["delegations"], {}, name)
            self.assertFalse([m for m in self.h.state["messages"].values() if m.get("injected")], name)

    def test_the_right_is_for_urgent_messages_only_not_for_inserting_an_ordinary_one(self):
        # message.inject from a module is refused even with the right (reducer.by_watch_or_owner): the right lets a module
        # send an urgent message of its own, it does not let it push an ordinary one past the queue
        queued = self.fact("mod:relay", now=False)
        self.assertEqual(queued["state"], "queued")
        self.clock[0] += 1
        item = {"type": "message.inject", "entity": queued["id"], "src": "module", "ts": self.clock[0],
                "data": {"channel": "plugin", "owner": False, "sender": "mod:relay"}}
        path = os.path.join(self.h.state_dir, f"spool-{self.clock[0]}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(item, f)
        self.h.spooled(path, item)
        self.assertFalse(self.h.state["messages"][queued["id"]].get("injected"))
        self.assertTrue(self.fact("mod:relay").get("injected"))   # and the urgent one passes as before

    def test_a_mailbox_file_with_null_data_does_not_crash_the_daemon(self):
        # "data": null is a valid mailbox file (spool.malformed); the check of `now` must not read it as an object
        self.clock[0] += 1
        item = {"type": "message.received", "entity": None, "src": "cli", "ts": self.clock[0], "data": None}
        path = os.path.join(self.h.state_dir, f"spool-{self.clock[0]}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(item, f)
        self.h.spooled(path, item)   # used to raise AttributeError and leave the file in the mailbox
        self.assertFalse(os.path.exists(path))
        self.assertFalse([m for m in self.h.state["messages"].values() if m.get("injected")])


if __name__ == "__main__":
    unittest.main()
