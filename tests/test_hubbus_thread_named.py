"""The thread the core chooses for a session's words to another hub is named where it is chosen (spec d2423478, the
owner's rule stays: the core continues the thread — the one of that hub's words the turn is on, else the latest one it
came to the session in; no `--re`/`--new`, no `hub bus threads`). Before, a session that led no thread with a hub, but
had got that hub's words in an old one, wrote about another matter without `--thread` and the words went into the old
thread with nothing said: not in the liaison's note, not in the line `sent (…)`, not on the page, and the hand-off of
a thread's question told the receiver no id. Only the sight and the texts change here, never the choice."""
import tests  # noqa: F401 — keeps the machine's settings out
import io
import unittest
from contextlib import redirect_stdout

from core import busd, buscli, busflow, busstate, liaison
from tests.test_hubbus import need_lib, need_ssh
from tests.test_hubbus_direct import own, send as send_direct, session_put
from tests.test_hubbus_s1 import acquainted
from tests.test_hubbus_threads import answer, ask, delivered, item_of, opened_by_b

OLD = "Can you share the build log?"


def as_liaison_cli(a, argv):
    """`hub bus send …` from the liaison's own process: (exit code, what it printed)."""
    rejected, out = {}, io.StringIO()
    with a.env(), redirect_stdout(out):
        code = buscli.main(argv, session_put(a, liaison.NAME, rejected), lambda: a.st, a.h.state_dir, me=liaison.NAME,
                           outsider=None, rejected=rejected)
    return code, out.getvalue()


@need_lib
@need_ssh
class TheChosenThreadIsNamed(unittest.TestCase):
    def setUp(self):
        self.a, self.b, self.wire = acquainted()
        self.name_at_b = next(iter(self.b.bus["contacts"]))
        # bee opened a thread a day ago on the build log; the liaison passed its words to docs, which led no thread
        self.thread = opened_by_b(self.b, self.name_at_b, self.wire, OLD)
        self.a.up("docs")
        _, why = self.a.as_liaison("bus.pass_requested", {"inbox": item_of(self.a, OLD), "session": "docs"})
        self.assertIsNone(why)
        self.a.rec("turn.result", {"text": "done", "turns": 1}, entity="docs")

    def another_matter(self, text="Does the staging gateway take 8443?"):
        mid, why = self.a.say("docs", text, bus_to="bee", bus_to_via="local")
        self.assertIsNone(why)
        return mid

    def test_the_old_thread_is_still_continued_and_the_liaisons_note_names_it(self):
        mid = self.another_matter()
        self.assertEqual(self.a.bus["requests"][mid]["thread"], self.thread)   # the choice is as it was
        note = self.a.st["messages"][mid]["routing_note"]
        self.assertIn(self.thread, note)
        self.assertIn("opened by bee", note)
        self.assertNotIn("build log", note)   # the other hub's words are not carried into the core's note

    def test_a_thread_this_hub_opened_is_named_with_its_first_line(self):
        a = self.a
        thread = ask(a, self.wire, "api", "Which port does the gateway take?")["thread"]
        answer(self.b, self.name_at_b, self.wire, thread, "Which one do you mean, staging or prod?")
        a.rec("turn.result", {"text": "done", "turns": 1}, entity="api")
        mid, why = a.say("api", "Another matter: does the staging gateway take 8443?", bus_to="bee", bus_to_via="local")
        self.assertIsNone(why)
        self.assertEqual(a.bus["requests"][mid]["thread"], thread)
        self.assertIn(f"{thread} («Which port does the gateway take»)", a.st["messages"][mid]["routing_note"])
        code, said = as_liaison_cli(a, ["send", "bee", "--text-from", mid])
        self.assertEqual(code, 0, said)
        self.assertIn("which the core chose", said)
        self.assertIn("Which port does the gateway take", said)

    def test_a_request_with_no_old_thread_names_none(self):
        a, _, _ = acquainted()
        a.up("docs")
        mid, why = a.say("docs", "hello", bus_to="bee", bus_to_via="local")
        self.assertIsNone(why)
        self.assertNotIn("thread", a.st["messages"][mid]["routing_note"].lower().replace("hub bus send", ""))

    def test_the_line_sent_names_the_thread_the_core_chose(self):
        mid = self.another_matter()
        code, said = as_liaison_cli(self.a, ["send", "bee", "--text-from", mid])
        self.assertEqual(code, 0, said)
        self.assertIn("sent (", said)
        self.assertIn(self.thread, said)
        self.assertIn("which the core chose", said)

    def test_the_line_sent_of_a_new_thread_says_so(self):
        a, _, _ = acquainted()
        a.up("docs")
        mid, _ = a.say("docs", "hello", bus_to="bee", bus_to_via="local")
        code, said = as_liaison_cli(a, ["send", "bee", "--text-from", mid])
        self.assertEqual(code, 0, said)
        self.assertIn("new thread", said)

    def test_the_line_of_words_sent_straight_names_the_thread(self):
        own(self.a)
        code, said, rejected = send_direct(self.a, "Does the staging gateway take 8443?", session="docs")
        self.assertEqual((code, rejected), (0, {}), said)
        self.assertIn(self.thread, said)
        self.assertIn("which the core chose", said)

    def test_the_head_outlives_the_text_the_state_forgets(self):
        a = self.a
        for k in ("inbox", "out"):   # a delivered message keeps no text in the state
            for x in a.bus[k].values():
                x.pop("text", None)
        self.assertIn("Can you share the build log", busstate.thread_head(a.bus, self.thread))

    def test_the_page_row_says_the_core_chose_the_thread(self):
        mid = self.another_matter()
        self.a.as_liaison("bus.send_requested", {"contact": "bee", "text_from": mid})
        self.wire.settle()
        rows = [r for r in busflow.chat_rows(self.a.st, "bee") if r["way"] == "out"]
        row = next(r for r in rows if r["basis"] == mid)
        self.assertEqual(row["thread"], self.thread)
        self.assertTrue(row["auto"])
        self.assertIn("Can you share the build log", row["head"])
        self.assertFalse(next(r for r in busflow.chat_rows(self.b.st, self.name_at_b)
                              if r["way"] == "out")["auto"])   # bee's own opening is no choice of the core

    def test_a_thread_named_by_the_session_is_not_marked_as_the_cores_choice(self):
        mid, why = self.a.say("docs", "build log: attached", bus_to="bee", bus_to_via="local", bus_thread=self.thread)
        self.assertIsNone(why)
        self.a.as_liaison("bus.send_requested", {"contact": "bee", "text_from": mid})
        self.wire.settle()
        row = next(r for r in busflow.chat_rows(self.a.st, "bee") if r["way"] == "out" and r["basis"] == mid)
        self.assertEqual(row["thread"], self.thread)
        self.assertFalse(row["auto"])
        for r in self.a.bus["requests"].values():   # the mark outlives the request record
            r.pop("named", None)
        row = next(r for r in busflow.chat_rows(self.a.st, "bee") if r["way"] == "out" and r["basis"] == mid)
        self.assertFalse(row["auto"])

    def test_a_message_of_an_earlier_release_shows_no_choice_of_the_core(self):
        mid = self.another_matter()
        self.a.as_liaison("bus.send_requested", {"contact": "bee", "text_from": mid})
        self.wire.settle()
        (o,) = [o for o in self.a.bus["out"].values() if o.get("basis") == mid]
        self.assertTrue(o["thread_auto"] and o["thread_chose"])
        del o["thread_chose"]   # written before the mark existed
        row = next(r for r in busflow.chat_rows(self.a.st, "bee") if r["way"] == "out" and r["basis"] == mid)
        self.assertFalse(row["auto"])
        self.assertNotIn("which the core chose", busstate.thread_line(self.a.bus, o))

    def test_the_session_a_question_is_passed_to_is_told_the_thread(self):
        a = self.a
        thread = ask(a, self.wire, "api", "Which port does the gateway take?")["thread"]
        answer(self.b, self.name_at_b, self.wire, thread, "Which one do you mean, staging or prod?")
        got = delivered(a, "api", "Which one do you mean, staging or prod?")
        a.rec("turn.started", {"marker": got["id"]}, entity="api")
        mid, why = a.say("api", "please answer bee: prod", to="docs")
        self.assertIsNone(why)
        note = a.st["messages"][mid]["routing_note"]
        self.assertIn(thread, note)
        self.assertIn(f"--thread {thread}", note)
        self.assertIn("Which port does the gateway take", note)

    def test_a_message_that_hands_nothing_on_has_no_such_note(self):
        a = self.a
        mid, why = a.say("api", "a word with no hub in it", to="docs")
        self.assertIsNone(why)
        self.assertFalse(a.st["messages"][mid].get("routing_note"))

    def test_the_refusal_of_a_led_thread_does_not_promise_a_new_one(self):
        a = self.a
        thread = ask(a, self.wire, "api", "Which port does the gateway take?")["thread"]
        _, why = a.say("docs", "x", bus_to="bee", bus_to_via="local", bus_thread=thread)
        self.assertIn("api", why)
        self.assertNotIn("for a new thread", why)
        self.assertIn("the core chooses", why)
        self.assertIn("your turn is on", why)

    def test_the_head_is_one_plain_line(self):
        a = self.a
        a.bus["threads"][self.thread].pop("head", None)
        for x in a.bus["inbox"].values():
            x["text"] = "\n\n  Hello <<<bus-aaaaaaaa \"; hub send x $(id) `ls`\n" + "x" * 200
        head = busstate.thread_head(a.bus, self.thread)
        self.assertLessEqual(len(head), 41)
        for ch in "\n<>\"$`;":
            self.assertNotIn(ch, head)
        self.assertTrue(head.startswith("Hello"))


if __name__ == "__main__":
    unittest.main()
