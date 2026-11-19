"""The liaison needs no memory — its rules are the core's and come with each message (owner q1471777) — yet its turns
carried a median of 231 thousand tokens of an old conversation (four days, the owner's hub). After a turn past
liaison.CLEAR_AT the core types /clear into it, as the owner would: not while its own question is open, not while other
messages wait for it, not twice (spec d2505378, part B, /clear)."""
import tests  # noqa: F401 — keeps the machine's settings out
import unittest

from tests.test_hubbus import need_lib, need_ssh
from tests import test_hubbus_s1 as base
from core import liaison

Q = {"questions": [{"question": "Answer them?", "header": "Answer", "multiSelect": False,
                    "options": [{"label": "Yes", "description": "y"}, {"label": "No", "description": "n"}]}]}


def turn(a, context):
    a.rec("turn.started", {"marker": "x"}, entity=liaison.NAME)
    a.rec("turn.result", {"text": "—", "subtype": "success", "turns": 1, "context": context}, entity=liaison.NAME)


def clears(a):
    return [m for m in a.st["messages"].values() if m.get("session") == liaison.NAME and m.get("text") == "/clear"]


@need_lib
@need_ssh
class Clear(unittest.TestCase):
    def test_a_long_conversation_gets_clear_once(self):
        a = base.LiveHub("Hub A")
        turn(a, liaison.CLEAR_AT - 1)
        self.assertEqual(clears(a), [])
        turn(a, liaison.CLEAR_AT + 1)
        (m,) = clears(a)
        self.assertTrue(m.get("command") and m.get("builtin"))
        self.assertFalse(m.get("from_owner"))
        turn(a, liaison.CLEAR_AT + 2)                     # the /clear on its way: not a second one
        self.assertEqual(len(clears(a)), 1)

    def test_not_while_its_question_is_open(self):
        a = base.LiveHub("Hub A")
        a.rec("turn.started", {"marker": "x"}, entity=liaison.NAME)
        a.rec("permission.requested", {"request_id": "r1", "tool": "AskUserQuestion", "interactive": True, "input": Q},
              entity=liaison.NAME)
        a.rec("turn.result", {"text": "—", "subtype": "success", "turns": 1, "context": liaison.CLEAR_AT + 1},
              entity=liaison.NAME)
        self.assertEqual(clears(a), [])

    def test_not_while_messages_wait_for_it(self):
        a = base.LiveHub("Hub A")
        a.up("api")
        a.rec("turn.started", {"marker": "x"}, entity=liaison.NAME)
        a.say("api", "please pass this on", bus_to=None)
        a.rec("turn.result", {"text": "—", "subtype": "success", "turns": 1, "context": liaison.CLEAR_AT + 1},
              entity=liaison.NAME)
        self.assertEqual(clears(a), [])

    def test_not_on_a_commands_result_and_the_old_length_goes_with_clear(self):
        a = base.LiveHub("Hub A")
        a.rec("turn.result", {"text": "", "subtype": "success", "turns": 0, "context": liaison.CLEAR_AT + 1},
              entity=liaison.NAME)                      # a built-in command's result (no model turn) never asks for /clear
        self.assertEqual(clears(a), [])
        self.assertEqual(a.st["sessions"][liaison.NAME]["context"], liaison.CLEAR_AT + 1)
        a.rec("session.started", {"source": "clear"}, entity=liaison.NAME)
        self.assertIsNone(a.st["sessions"][liaison.NAME].get("context"))   # a new conversation: not the old length

    def test_not_while_it_holds_an_item_it_took(self):
        a, b, wire = base.acquainted()
        _, bid = base.sent_through(b, a, wire, "hello", session="maps", contact=next(iter(b.bus["contacts"])))
        m = a.bus["inbox"][bid]["message"]
        a.rec("turn.started", {"marker": m}, entity=liaison.NAME)
        a.rec("turn.result", {"text": "—", "subtype": "success", "turns": 1, "context": liaison.CLEAR_AT + 1},
              entity=liaison.NAME)
        self.assertEqual(clears(a), [])                 # b… is open and the liaison read it: its conversation may matter
        a.as_liaison("bus.done_requested", {"inbox": bid})
        turn(a, liaison.CLEAR_AT + 1)
        self.assertEqual(len(clears(a)), 1)

    def test_another_session_is_never_cleared(self):
        a = base.LiveHub("Hub A")
        a.up("api")
        a.rec("turn.started", {"marker": "x"}, entity="api")
        a.rec("turn.result", {"text": "—", "subtype": "success", "turns": 1, "context": 10 * liaison.CLEAR_AT},
              entity="api")
        self.assertEqual([m for m in a.st["messages"].values() if m.get("text") == "/clear"], [])


if __name__ == "__main__":
    unittest.main()
