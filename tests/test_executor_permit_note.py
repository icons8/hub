"""An executor waits for a permission (a command, not a form): whoever raised it gets one line in its next message
("FYI", does not wake a turn), once per request. Before it, the raiser only found out by polling `hub status`."""
import tests  # noqa: F401 — ignore this machine's settings
import unittest

from core import executors, reducer

OWNER = {"channel": "cli", "owner": True}


class PermitNote(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "maps", cwd="/m", session_id="s-maps", role="agent", about="Maps", **OWNER)
        self.ev("session.started", "maps")
        self.assertIsNone(self.ev("task.run_requested", "maps-1", task="fix the build", channel="session", owner=False,
                                  sender="maps"))
        for type, entity, data in executors.events_for(self.st, "maps-1", self.st["runs"]["maps-1"],
                                                       cwd="/m/.claude/worktrees/maps-1", worktree=None):
            self.ev(type, entity, **data)
        self.ev("session.started", "maps-1")
        self.ev("turn.started", "maps-1", marker=None)

    def ev(self, type, entity=None, src=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 2000 + self.seq, "type": type,
                                                   "entity": entity, "data": data, **({"src": src} if src else {})})
        return why

    def notes(self, to="maps"):
        return [m for m in self.st["messages"].values() if m["session"] == to and "waits for permission" in m["text"]]

    def ask(self, rid="r1", tool="Bash", **extra):
        self.ev("permission.requested", "maps-1", request_id=rid, tool=tool, input={"command": "rm -rf build/old"},
                **extra)

    def test_the_raiser_hears_that_its_executor_waits_for_a_command(self):
        self.ask(why="ask rule Bash(rm -rf*)")
        (m,) = self.notes()
        self.assertTrue(m.get("fyi"))
        self.assertEqual(m["state"], "aside")                      # does not wake a turn
        self.assertIn("maps-1", m["text"])
        self.assertIn("rm -rf build/old", m["text"])
        self.assertIn("ask rule Bash(rm -rf*)", m["text"])

    def test_once_per_request_and_again_for_the_next_one(self):
        self.ask("r1")
        self.ask("r1")                                              # the same card again
        self.assertEqual(len(self.notes()), 1)
        self.ask("r2")
        self.assertEqual(len(self.notes()), 2)

    def test_the_note_is_taken_back_when_the_request_is_answered_before_it_is_delivered(self):
        self.ask("r1")
        self.ask("r2")
        self.assertEqual(len(self.notes()), 2)
        self.assertIsNone(self.ev("permission.answered", "maps-1", request_id="r1", behavior="allow", **OWNER))
        (m,) = self.notes()
        self.assertIn("rm -rf build/old", m["text"])
        self.assertEqual(self.st["sessions"]["maps-1"]["pending"][0]["request_id"], "r2")
        self.assertIsNone(self.ev("permission.answered", "maps-1", request_id="r2", behavior="deny", **OWNER))
        self.assertEqual(self.notes(), [])

    def test_a_note_already_given_to_a_carrier_stays(self):
        self.ask("r1")
        (m,) = self.notes()
        m["with"] = "m1"                      # picked up by a message on its way
        self.ev("permission.answered", "maps-1", request_id="r1", behavior="allow", **OWNER)
        self.assertEqual(len(self.notes()), 1)

    def test_requests_without_an_id_are_each_told(self):
        self.ev("permission.requested", "maps-1", tool="Bash", input={"command": "ls a"})
        self.ev("permission.requested", "maps-1", tool="Bash", input={"command": "ls b"})
        self.assertEqual(len(self.notes()), 2)

    def test_a_big_input_is_cut_per_field(self):
        self.ev("permission.requested", "maps-1", request_id="r5", tool="Write",
                input={"file_path": "/m/big.txt", "content": "x" * 5_000_000})
        (m,) = self.notes()
        self.assertIn("/m/big.txt", m["text"])
        self.assertLess(len(m["text"]), 700)

    def test_a_secret_across_the_cut_is_masked_and_a_flag_stays_visible(self):
        key = "sk-ant-api03-" + "abcdefghij" * 6
        self.ev("permission.requested", "maps-1", request_id="r7", tool="Edit",
                input={"file_path": "/m/x", "old_string": "q" * 60 + " " + key, "replace_all": True})
        (m,) = self.notes()
        self.assertNotIn("abcdefghij", m["text"])
        self.assertIn('"replace_all": true', m["text"])

    def test_the_id_a_clash_renamed_is_the_one_taken_back(self):
        reducer.new_message(self.st, f"m{self.seq + 1}x8", "maps", "someone else's aside note", 1, fyi=True)
        self.ask("r8")
        (m,) = self.notes()
        self.assertNotEqual(m["id"], f"m{self.seq}x8")
        self.ev("permission.answered", "maps-1", request_id="r8", behavior="allow", **OWNER)
        self.assertEqual(self.notes(), [])
        self.assertIn(f"m{self.seq - 1}x8", self.st["messages"])   # the other one is not touched

    def test_a_removed_raiser_is_not_told(self):
        self.st["sessions"]["maps"]["state"] = "removed"
        self.ask("r6")
        self.assertEqual(self.notes(), [])

    def test_a_form_an_automatic_allow_and_a_session_without_a_raiser_say_nothing(self):
        self.ev("permission.requested", "maps-1", request_id="q1", tool="AskUserQuestion", interactive=True,
                input={"questions": [{"question": "Which?", "options": [{"label": "a"}]}]})
        self.assertEqual(self.notes(), [])
        self.ev("permission.requested", "maps-1", src="host", request_id="a1", tool="Bash", auto="allow",
                input={"command": "make"})
        self.assertEqual(self.notes(), [])                         # the core allows it itself: nothing waits
        self.ev("permission.requested", "maps", request_id="r9", tool="Bash", input={"command": "ls"})
        self.assertEqual([m for m in self.st["messages"].values() if "waits for permission" in m["text"]], [])

    def test_a_tool_without_a_command_is_shown_by_its_input(self):
        self.ev("permission.requested", "maps-1", request_id="r4", tool="Edit", input={"file_path": "/m/.git/config"})
        (m,) = self.notes()
        self.assertIn("/m/.git/config", m["text"])

    def test_a_secret_in_the_command_is_not_copied_into_the_note(self):
        key = "sk-ant-api03-" + "abcdefghij" * 2
        self.ev("permission.requested", "maps-1", request_id="r3", tool="Bash", masked=True,
                input={"command": "curl -H 'Authorization: Bearer %s' x" % key})
        (m,) = self.notes()
        self.assertNotIn("sk-ant-api03", m["text"])


if __name__ == "__main__":
    unittest.main()
