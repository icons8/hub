"""An answer to a card with one question in the feed names the options not chosen along with the one chosen: "Chose X;
not chosen: Y · Z". The answer is read as whole labels (a label's own comma included, a shorter label inside a longer one
apart); what the page itself sent is known from its picks, so a comment after the pick hides nothing and own words
that start with a label ("Yes — later") stay "Answer: …"; a card with several questions stays "Answer: …" too. The
page's own pick counts only for the answer it really sent: a tap the core refused, or one that lost to the tap in
flight, does not color an answer recorded from elsewhere."""
import os
import tests  # noqa: F401
import unittest
from unittest import mock
from tests.test_web_e2e import PageInChrome


class FeedCardAnswer(PageInChrome):
    LINE = "(() => document.querySelector('.row.answer .own-text')?.textContent || null)()"
    LINES = "[...document.querySelectorAll('.row.answer .own-text')].map((n) => n.textContent)"
    ROW = {"until": "!!document.querySelector('.row.answer .own-text')", "ms": 10000}
    SUBMIT = "document.querySelector('.ask-card').requestSubmit(); return 1; })()"

    def ask(self, labels, rid="r1", question="Which?", multi=False, more=()):
        if rid == "r1":
            self.ev("session.requested", "a", session_id="s-a", cwd="/w", channel="cli", owner=True)
            self.ev("session.started", "a", session_id="s-a")
            m = self.j.append("message.received", None, {"to": "a", "text": "plan the detour", "channel": "web", "owner": True})
            self.ev("turn.started", "a", marker=f"m{m['seq']}")
        qs = [{"question": question, "header": "Which", "multiSelect": multi,
               "options": [{"label": l, "description": ""} for l in labels]}]
        qs += [{"question": q, "header": "More", "multiSelect": False, "options": [{"label": l, "description": ""} for l in ls]}
               for q, ls in more]
        self.ev("permission.requested", "a", request_id=rid, tool="AskUserQuestion", interactive=True, input={"questions": qs})

    def card(self, labels, answer, multi=False):
        self.ask(labels, multi=multi)
        self.ev("permission.answered", "a", request_id="r1", behavior="allow", answers={"Which?": answer},
                channel="web", owner=True)
        self.ev("turn.stopped", "a")

    def sent(self):
        # what the page sent waits in the spool: into the journal, as the core would do
        from core import spool
        out = []
        for path, item in spool.take(os.path.join(self.state, "spool")):
            self.j.append(item["type"], item["entity"], item["data"], src="web")
            os.unlink(path)
            out.append(item)
        return out

    def test_an_answer_says_what_was_chosen_and_what_was_not(self):
        self.card(("Northern road", "Southern road", "Coast road"), "Northern road")
        for feed in ("compact", "full"):
            got = self.page({"go": "about:blank"}, {"go": f"http://127.0.0.1:{self.port}/#/s/a", "feed": feed},
                            {"wait": "Northern road"}, self.ROW, {"eval": self.LINE})
            self.assertTrue(got[-1]["eval"].startswith("Chose Northern road; not chosen: Southern road · Coast road ("),
                            (feed, got))
        # typed in the card instead of a pick: what was not chosen is not known, the row stays as it was
        self.ask(("Northern road", "Southern road"), rid="r2")
        self.ev("permission.answered", "a", request_id="r2", behavior="allow", answers={"Which?": "whichever is open"},
                channel="web", owner=True)
        got = self.page({"go": "about:blank"}, {"go": f"http://127.0.0.1:{self.port}/#/s/a"}, {"wait": "whichever is open"},
                        {"eval": self.LINES})
        self.assertTrue(any(x.startswith("Answer: whichever is open (") for x in got[-1]["eval"]), got)

    def test_a_chosen_label_with_a_comma_of_its_own_is_not_set_apart(self):
        # a multi-select answer is its labels joined with ", ": "Yes, today" is one label, not two
        self.card(("Yes, today", "Tomorrow", "Next week"), "Yes, today, Tomorrow", multi=True)
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/s/a"}, {"wait": "Next week"}, self.ROW, {"eval": self.LINE})
        self.assertIn("; not chosen: Next week (", got[-1]["eval"], got)

    def test_a_label_inside_a_longer_chosen_label_is_not_chosen(self):
        # "Yes" is its own option: choosing "Yes, today" does not choose it
        self.card(("Yes", "Yes, today", "Tomorrow"), "Yes, today")
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/s/a"}, {"wait": "Tomorrow"}, self.ROW, {"eval": self.LINE})
        self.assertIn("; not chosen: Yes · Tomorrow (", got[-1]["eval"], got)

    def test_a_pick_with_a_comment_from_this_page_keeps_the_pick(self):
        # the pick and a comment from the session's field go as "B — text": the page knows its pick, and a label named
        # in the comment is still not chosen
        self.ask(("Northern road", "Southern road", "Coast road"))
        self.web.decide_wait = 0
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/s/a"}, {"wait": "Coast road"},
                        {"eval": "(() => { const c = document.querySelector('.ask-card'); c.classList.add('aimed');"   # the session's field is aimed at the card
                                 " c.querySelectorAll('label.opt input')[1].click();"
                                 " return c.__comment('not the north, Coast road, too narrow'); })()"},
                        {"until": "!!document.querySelector('.ask-card .sent-line')", "ms": 5000})
        self.assertTrue(got[-2]["eval"], got)
        self.sent()
        got = self.page(self.ROW, {"eval": self.LINE})
        self.assertIn("Chose Southern road — not the north, Coast road, too narrow; not chosen: Northern road · Coast road (",
                      got[-1]["eval"], got)

    def test_own_words_that_start_with_a_label_stay_an_answer(self):
        # typed in the card: "Yes — but only on Monday" is not the pick "Yes" with a comment
        self.ask(("Yes", "No", "Later"))
        self.web.decide_wait = 0
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/s/a"}, {"wait": "Later"},
                        {"eval": "(() => { const t = document.querySelector('.ask-card [name=other0]'); t.value = 'Yes — but only on Monday';"
                                 " t.dispatchEvent(new Event('input', {bubbles: true})); " + self.SUBMIT},
                        {"until": "!!document.querySelector('.ask-card .sent-line')", "ms": 5000})
        self.assertTrue(got[-1]["until"], got)
        self.sent()
        got = self.page(self.ROW, {"eval": self.LINE})
        self.assertTrue((got[-1]["eval"] or "").startswith("Answer: Yes — but only on Monday ("), got)
        # the same answer seen from another page (the journal alone): the pick is not known either
        self.ask(("Yes", "No", "Later"), rid="r2")
        self.ev("permission.answered", "a", request_id="r2", behavior="allow", answers={"Which?": "Yes — but only on Monday"},
                channel="web", owner=True)
        got = self.page({"go": "about:blank"}, {"go": f"http://127.0.0.1:{self.port}/#/s/a"}, {"wait": "only on Monday"},
                        {"until": "document.querySelectorAll('.row.answer .own-text').length === 2", "ms": 10000},
                        {"eval": self.LINES})
        self.assertEqual([x.split(" (")[0] for x in got[-1]["eval"]], ["Answer: Yes — but only on Monday"] * 2, got)
        # a pick in the card, by contrast, is read: tapping an option is the answer
        self.ask(("Yes", "No", "Later"), rid="r3")
        got = self.page({"wait": "Later"}, {"eval": "document.querySelectorAll('.ask-card label.opt input')[2].click(), 1"},
                        {"until": "!!document.querySelector('.ask-card .sent-line')", "ms": 5000})
        self.assertTrue(got[-1]["until"], got)
        self.sent()
        got = self.page({"until": "document.querySelectorAll('.row.answer .own-text').length === 3", "ms": 10000}, {"eval": self.LINES})
        self.assertTrue(got[-1]["eval"][-1].startswith("Chose Later; not chosen: Yes · No ("), got)

    def test_a_card_with_several_questions_stays_an_answer(self):
        # the same labels in two questions: "Yes" to one and "No" to the other are not "not chosen" of each other, while
        # a card with one question beside it tells what was not chosen
        self.ask(("Yes", "No"), question="Deploy?", more=(("Notify?", ("Yes", "No")),))
        self.ev("permission.answered", "a", request_id="r1", behavior="allow", answers={"Deploy?": "Yes", "Notify?": "No"},
                channel="web", owner=True)
        self.ask(("Yes", "No"), rid="r2", question="Restart?")
        self.ev("permission.answered", "a", request_id="r2", behavior="allow", answers={"Restart?": "No"}, channel="web", owner=True)
        self.ev("turn.stopped", "a")
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/s/a"}, {"wait": "Restart?"},
                        {"until": "document.querySelectorAll('.row.answer .own-text').length === 2", "ms": 10000}, {"eval": self.LINES})
        self.assertEqual([x.split(" (")[0] for x in got[-1]["eval"]], ["Answer: Yes, No", "Chose No; not chosen: Yes"], got)


    ROADS = ("Northern road", "Southern road", "Coast road")
    TAP = "document.querySelectorAll('.ask-card label.opt input')[{}].click(), 1"

    def refused_tap(self, i=1):
        # a tap the core refused (409): the card opens again with the reason; the page sent nothing the core kept
        with mock.patch.object(self.web, "answer", return_value=(409, "Session no longer waits")):
            got = self.page({"go": f"http://127.0.0.1:{self.port}/#/s/a"}, {"wait": "Coast road"},
                            {"eval": self.TAP.format(i)},
                            {"until": "!!document.querySelector('.ask-card .error-text')?.textContent", "ms": 5000})
        self.assertTrue(got[-1]["until"], got)

    def test_a_refused_tap_does_not_color_an_answer_given_elsewhere(self):
        # tapped "Southern road", the core refused; then Telegram (another page) answered "Northern road": the row is
        # about the recorded answer, not the refused tap
        self.ask(self.ROADS)
        self.web.decide_wait = 0
        self.refused_tap(1)
        self.ev("permission.answered", "a", request_id="r1", behavior="allow", answers={"Which?": "Northern road"},
                channel="telegram", owner=True)
        got = self.page(self.ROW, {"eval": self.LINE})
        self.assertTrue((got[-1]["eval"] or "").startswith("Chose Northern road; not chosen: Southern road · Coast road ("), got)

    def test_more_context_after_a_refused_tap_stays_an_answer(self):
        # after the refusal "more context" goes out: it is no pick of a label — nothing is "not chosen"
        self.ask(self.ROADS)
        self.web.decide_wait = 0
        self.refused_tap(1)
        got = self.page({"eval": "document.querySelector('.ask-card .btn.more').click(), 1"},
                        {"until": "!!document.querySelector('.ask-card .sent-line')", "ms": 5000})
        self.assertTrue(got[-1]["until"], got)
        self.sent()
        got = self.page(self.ROW, {"eval": self.LINE})
        line = got[-1]["eval"] or ""
        self.assertTrue(line.startswith("Answer: "), got)
        self.assertNotIn("not chosen", line, got)
        # a tap on the next card is read as before: the refusal and "more context" left no mark on it
        self.ask(self.ROADS, rid="r2")
        got = self.page({"wait": "Coast road"}, {"eval": self.TAP.format(2)},
                        {"until": "!!document.querySelector('.ask-card .sent-line')", "ms": 5000})
        self.assertTrue(got[-1]["until"], got)
        self.sent()
        got = self.page({"until": "document.querySelectorAll('.row.answer .own-text').length === 2", "ms": 10000}, {"eval": self.LINES})
        self.assertTrue(got[-1]["eval"][-1].startswith("Chose Coast road; not chosen: Northern road · Southern road ("), got)

    def test_a_double_tap_and_a_tap_on_the_other_option_tell_the_answer_that_went(self):
        # two taps on "Northern road" and one on "Southern road" while the first is in flight: one answer goes, the
        # first one — the row tells that one, not the last tap
        self.ask(self.ROADS)
        self.web.decide_wait = 0
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/s/a"}, {"wait": "Coast road"},
                        {"eval": "(() => { const o = document.querySelectorAll('.ask-card label.opt input');"
                                 " o[0].click(); o[0].click(); o[1].click(); return 1; })()"},
                        {"until": "!!document.querySelector('.ask-card .sent-line')", "ms": 5000})
        self.assertTrue(got[-1]["until"], got)
        sent = [e["data"]["answers"] for e in self.sent() if e["type"] == "permission.answered"]
        self.assertEqual(sent, [{"Which?": "Northern road"}])
        got = self.page(self.ROW, {"eval": self.LINE})
        self.assertTrue((got[-1]["eval"] or "").startswith("Chose Northern road; not chosen: Southern road · Coast road ("), got)

    def test_own_words_made_of_labels_with_a_comma_are_not_a_pick_of_one(self):
        # "Yes, No" to a single-pick card is the owner's own words (two picks are impossible there); to a multi-pick card
        # it is two picks
        self.card(("Yes", "No", "Later"), "Yes, No")
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/s/a"}, self.ROW, {"eval": self.LINE})
        self.assertTrue((got[-1]["eval"] or "").startswith("Answer: Yes, No ("), got)
        self.ask(("Yes", "No", "Later"), rid="r2", multi=True)
        self.ev("permission.answered", "a", request_id="r2", behavior="allow", answers={"Which?": "Yes, No"},
                channel="web", owner=True)
        got = self.page({"until": "document.querySelectorAll('.row.answer .own-text').length === 2", "ms": 10000},
                        {"eval": self.LINES})
        self.assertTrue(got[-1]["eval"][-1].startswith("Chose Yes, No; not chosen: Later ("), got)


def load_tests(loader, standard, pattern):
    return unittest.TestSuite(FeedCardAnswer(name) for name in FeedCardAnswer.__dict__ if name.startswith("test_"))


if __name__ == "__main__":
    unittest.main()
