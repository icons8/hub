"""out_head: the start of the last result addressed to the owner (the same rule as owner_out), for the second "New"
line in the "Now" column of the session list. The core gives a fact — a short start of the text on one line; how to
show it is up to the page. A result to another session or the hub does not change it; the snapshot does not bloat
(truncation)."""
import tests  # noqa: F401
import unittest

from core import reducer, views

OWNER = {"channel": "web", "owner": True}
HUB = {"channel": "session", "owner": False, "sender": "hub"}


class OutHead(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        for n, kind in (("hub", "hub"), ("w", None)):
            self.ev("session.requested", n, cwd="/w", session_id=f"s-{n}", kind=kind, **OWNER)
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, _ = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                 "entity": entity, "data": data})
        return self.seq

    def owner_turn(self, text):
        m = self.ev("message.received", None, to="w", text="how are you?", **OWNER)
        self.ev("turn.started", "w", marker=f"m{m}")
        self.ev("turn.result", "w", text=text)

    def snap(self, name="w"):
        r = views.Reader()
        r.state = self.st
        return next(s for s in r.snapshot(now=1000)["sessions"] if s["name"] == name)

    def test_owner_result_head_one_line(self):
        self.owner_turn("## Done\n\nThe map   is fixed,\ntests are green.")
        s = self.snap()
        self.assertTrue(s["unread"])
        self.assertEqual(s["out_head"], "Done. The map is fixed, tests are green.")

    def test_head_has_no_markdown_marks(self):
        # a native client puts the field in a list row as is: "**bold**" must not reach it
        self.owner_turn("**Rechecked: the assembly data** holds, see [the table](http://x/t) and `plan.md`.\n- _item_ one\n"
                        "| a | b |\n|---|---|\n3 > 2, a * b, snake_case_name")
        self.assertEqual(self.snap()["out_head"], "Rechecked: the assembly data holds, see the table and plan.md. "
                                                  "item one 3 > 2, a * b, snake_case_name")

    def test_heading_and_table_do_not_glue_into_the_head(self):
        # a result that opens with a heading: the line says "Report. The map…", not "Report The map…"; the rows of a table
        # after the text do not trail as "a | b"
        self.owner_turn("# Result\nThe site map is rebuilt, 3 thousand addresses. Waiting for a «yes» to deploy.\n\n"
                        "| a | b |\n|---|---|\n| 1 | 2 |")
        self.assertEqual(self.snap()["out_head"], "Result. The site map is rebuilt, 3 thousand addresses. Waiting for a «yes» to deploy.")

    def test_head_seams_edges(self):
        for text, head in (("# Result", "Result"),                                        # a lone heading has no one after it
                           ("## **Result:**\ntext", "Result: text"),                   # already ends with a mark
                           ("| a | b |\n|---|---|\n| 1 | 2 |", "a | b"),           # a table that opens the result
                           ("Here:\n| a | b |\n|---|\n| 1 | 2 |\n\nTotal ok", "Here: Total ok"),
                           ("```bash\n# comment\n| wc\n```\nok", "comment wc ok"),  # code, not a heading or a table
                           ("#12 fixed\n#hash", "#12 fixed #hash"),                  # no space after "#": not a heading
                           ("2**10 and f(**kw)\n- a\n- b", "2**10 and f(**kw) a b"),
                           ("## Status\n| Task | Result |\n|--|--|\n| 1 | ok |", "Status. Task | Result"),   # a heading, then a table: its header row stays
                           ("## Title ##\nbody", "Title. body"),                      # the closing hashes of a heading
                           ("```x``` ok\n# H\nt", "x ok H. t"),                      # an inline span is not a fence
                           ("~~~\n# c\n| a\n~~~\n# H\nz", "c a H. z"),              # a tilde fence is a fence
                           ("", ""), ("|", ""), ("# ", ""), ("```", ""), ("# a\n# b", "a. b"),
                           ("# Result\r\ntext\r\n| a |\r\n", "Result. text")):
            self.assertEqual(reducer.out_head(text), head, text)

    def test_head_never_fails_on_garbage(self):
        import random
        rnd = random.Random(7)
        marks = ["#", " ", "\n", "\r\n", "|", "-", "`", "```", "~~~", "**", "_", "*", "a", "é", "[", "](", ")", ":", "\0", ">", "1."]
        for _ in range(2000):
            text = "".join(rnd.choice(marks) for _ in range(rnd.randint(0, 60)))
            self.assertLessEqual(len(reducer.out_head(text)), reducer.OUT_HEAD + 1, repr(text))   # the cut adds "…"
        for text in (None, 0, ["# a"], "x" * 200000, "# a\n" * 5000, "|" * 5000):
            self.assertLessEqual(len(reducer.out_head(text)), reducer.OUT_HEAD + 1)

    def test_marks_inside_code_and_numbers_stay(self):
        # review of the PR: "2**10", a glob in `code`, a fence line and a lone mark must not eat the text around them
        self.owner_turn("Counted 2**10 = 1024, rule `src/**/*.py` and `a_b_c`;\n```python\nx = 1")
        self.assertEqual(self.snap()["out_head"], "Counted 2**10 = 1024, rule src/**/*.py and a_b_c; x = 1")

    def test_plain_text_that_only_looks_like_markup_stays(self):
        # review: two exponents or globs outside `code`, dunder names, a year at the line start, ">=", an inline fence
        for plain in ("2**10 and 3**5", "src/**/*.py and lib/**/*.js", "__init__.py and __main__",
                      "see __name__ == '__main__'", "2024. was good", ">= 5 failed", "a * b and a_b_c", "x | y |",
                      "trap rm clean-*), then old clean-*", "files *_screen.py and print_*; _norm",
                      "(*) and (*)", "use [*] or [*]", 'rm "*" and "*"', "(_) and (_)", "x = 2 ** 3 and 4 ** 5"):
            self.assertEqual(reducer.strip_markup(plain), plain)
        self.assertEqual(reducer.strip_markup("**x** and __y z__ and *w* and ~~v~~"), "x and y z and w and v")
        self.assertEqual(reducer.strip_markup("say: *«Hi»* and *(maybe)*\n**"), "say: «Hi» and (maybe)\n")   # italics before a quote or a bracket, a lone "**"
        self.assertEqual(reducer.strip_markup("| a | b |"), " a | b")   # a table row loses its border, other lines keep a "|"

    def test_a_call_with_double_star_is_not_bold(self):
        """`f(**kwargs)` in a line keeps its stars even when a real bold follows it (the mark opened after "(" must not
        pair with the closing one of the next bold); a bold in brackets still loses its marks."""
        for text, plain in (("- f(**kwargs) and **bold**", "f(**kwargs) and bold"), ("f(**kwargs)** done", "f(**kwargs) done"),
                            ("f(**kwargs) and g(**opts) — **ok**", "f(**kwargs) and g(**opts) — ok"),
                            ("f(**kw) **a** **b**", "f(**kw) a b"), ("f(**kwargs)", "f(**kwargs)"),
                            ("(**note**) text", "(note) text"), ("(**a (b)** c)", "(a (b) c)")):
            self.assertEqual(reducer.strip_markup(text), plain, text)

    def test_strip_markup_ignores_the_marker_of_its_own_placeholder(self):
        self.assertEqual(reducer.strip_markup("a\x000\x00 `b`"), "a0 b")

    def test_head_markup_is_stripped_before_the_cut(self):
        # a mark that closes after the cut must not be left in the head
        self.owner_turn("**" + "words " * 60 + "**")
        head = self.snap()["out_head"]
        self.assertNotIn("*", head)
        self.assertTrue(head.endswith("…"))

    def test_gist_has_no_markdown_marks(self):
        self.ev("session.gist", "w", text="**Fixed** the map, see `map.py`", channel="session", sender="w")
        self.owner_turn("ok")
        self.ev("session.gist", "w", text="**Fixed** the map, see `map.py`", channel="session", sender="w")
        self.assertEqual(self.snap()["gist"]["text"], "Fixed the map, see map.py")

    def test_result_gist_has_no_markdown_marks(self):
        # the gist taken from the first line of a long result goes through the same cleaning as out_head
        self.owner_turn("_Fixed_ the map: see `map.py` and **two** files nearby, checked\n" + "words " * 120)
        self.assertEqual(self.snap()["gist"]["text"], "Fixed the map: see map.py and two files nearby, checked")

    def test_a_long_text_is_cleaned_only_as_far_as_its_head(self):
        # a long line of unpaired "**" made the cleaning regexes quadratic: a 100 000-character result held the fold for 10 s
        from unittest import mock
        lengths, real, link = [], reducer.strip_markup, reducer.LINK
        spy = mock.Mock(sub=lambda r, t: lengths.append(len(t)) or link.sub(r, t))   # LINK: quadratic on a run of "["
        with mock.patch.object(reducer, "strip_markup", lambda t: lengths.append(len(str(t or ""))) or real(t)), \
                mock.patch.object(reducer, "LINK", spy):
            self.owner_turn("**a " * 25000)
            self.ev("session.gist", "w", text="**b " * 25000, channel="session", sender="w")
            self.owner_turn("[" * 100000)
            self.owner_turn("**c " * 25000 + "\n" + "words " * 40)
        self.assertGreaterEqual(len(lengths), 5)   # out_head three times, the gist, the first line of the long result
        self.assertLessEqual(max(lengths), reducer.MD_SCAN)
        self.assertTrue(self.snap()["out_head"].startswith("c c c"))
        self.assertTrue(self.snap()["out_head"].endswith("…"))

    def test_long_result_is_cut(self):
        self.owner_turn("words " * 200)
        head = self.snap()["out_head"]
        self.assertLessEqual(len(head), reducer.OUT_HEAD + 1)
        self.assertTrue(head.endswith("…"))

    def test_result_to_hub_task_keeps_previous_head(self):
        self.owner_turn("first result to the owner")
        did = self.ev("delegation.created", None, session="w", for_session="hub", context="collect the log", **HUB)
        self.ev("turn.started", "w", marker=f"m{did}")
        self.ev("turn.result", "w", text="log collected")
        self.assertEqual(self.snap()["out_head"], "first result to the owner")

    def test_no_result_yet(self):
        self.assertIsNone(self.snap()["out_head"])
        self.assertIsNone(self.snap()["out_head_ts"])

    def test_head_ts_is_time_of_that_result(self):
        # out_head_ts is the ts of the event of that same result; another session's result changes neither the start nor the time
        self.owner_turn("first result to the owner")
        ts = 100 + self.seq
        self.assertEqual(self.snap()["out_head_ts"], ts)
        did = self.ev("delegation.created", None, session="w", for_session="hub", context="collect the log", **HUB)
        self.ev("turn.started", "w", marker=f"m{did}")
        self.ev("turn.result", "w", text="log collected")
        self.assertEqual(self.snap()["out_head_ts"], ts)
        self.owner_turn("second result to the owner")
        self.assertEqual(self.snap()["out_head_ts"], 100 + self.seq)

    def test_old_state_without_ts(self):
        # a state of an earlier version: out_head is there, the time is not — null, not an error
        self.owner_turn("result before the update")
        self.st["sessions"]["w"].pop("out_head_ts", None)
        s = self.snap()
        self.assertEqual(s["out_head"], "result before the update")
        self.assertIsNone(s["out_head_ts"])


if __name__ == "__main__":
    unittest.main()
