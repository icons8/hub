"""Folding in the feed: "more" only on a message taller than two thirds of the feed, a message once shown whole stays whole,
an answered reply neither folds nor grays by default, and every reply of a turn stays in view in "Brief"."""
import tests  # noqa: F401
import unittest
from tests.test_web_e2e import PageInChrome


class FeedFold(PageInChrome):
    # the rows with this text: how tall the message is, whether "more" is offered and the row's colour against a fresh reply
    PROBE = r"""((part) => {
      const feed = document.querySelector('main .feed');
      return [...document.querySelectorAll('main .feed .row')].filter((r) => r.textContent.includes(part)).map((r) => {
        const more = r.querySelector('.clamp-more'), box = more && more.previousElementSibling;
        // step: from one paragraph's top to the next — the cut ends on a whole paragraph or line, so up to this short of its budget
        const kids = box ? box.firstElementChild?.children || [] : [];
        return {more: !!more && !more.hidden && !!more.getClientRects().length, cut: box ? box.getBoundingClientRect().height : null,
                step: kids.length > 1 ? kids[1].getBoundingClientRect().top - kids[0].getBoundingClientRect().top : null,
                feed: feed.clientHeight, gray: r.classList.contains('resolved'), color: getComputedStyle(r.querySelector('.b')).color};
      });
    })"""

    def session(self, name):
        self.ev("session.requested", name, session_id="s-" + name, cwd="/w", channel="cli", owner=True)
        self.ev("session.started", name, session_id="s-" + name)

    def hub_says(self, name, text):
        """A message from the hub to the session, taken by a turn (the feed shows a message once a turn takes it)."""
        m = self.j.append("message.received", None, {"to": name, "text": text, "channel": "session", "owner": False, "sender": "hub"})
        self.ev("turn.started", name, marker=f"m{m['seq']}")
        self.ev("turn.stopped", name)

    def probe(self, part):
        return {"eval": f"{self.PROBE}({part!r})"}

    def opened(self, url, wait, width, height, name):
        """Steps that open the page afresh in a window of this size: the fold is decided once, when a message is first
        shown, so the size must be set before the page loads — a resize after it does not cut anew. The first "shot" sets
        it (cdp.mjs sets the window size of a shot until the end of the steps); the last one is the page as it was checked."""
        return [{"shot": f"{self.profile}/{name}-before.png", "width": width, "height": height}, {"go": "about:blank"},
                {"go": url}, {"wait": wait}, {"sleep": 300}, {"shot": f"{self.profile}/{name}.png", "width": width, "height": height}]

    def test_more_only_for_a_message_taller_than_two_thirds_of_the_feed(self):
        self.session("maps")
        medium = "Medium brief. " + "A plain line of the brief that wraps. " * 16    # ~620 characters: a few lines on any screen
        tall = "\n\n".join(f"Tall brief, paragraph {i}: one more point to read." for i in range(60))
        self.hub_says("maps", medium)
        self.hub_says("maps", tall)
        for width in (390, 1440):
            got = self.page(*self.opened(f"http://127.0.0.1:{self.port}/#/s/maps", "Tall brief", width, 900, f"fold-{width}"),
                            self.probe("Medium brief"), self.probe("Tall brief"))
            mid, big = got[-2]["eval"], got[-1]["eval"]
            self.assertEqual(len(mid), 1, (width, mid))
            self.assertFalse(mid[0]["more"], (width, mid))     # was three lines and "more" past 280 characters
            self.assertEqual(len(big), 1, (width, big))
            self.assertTrue(big[0]["more"], (width, big))
            # cut to half the bar: a third of the feed at most, on a whole paragraph or line — less than a paragraph short of it
            short = big[0]["feed"] / 3 - big[0]["cut"]
            self.assertTrue(-1 <= short < big[0]["step"], (width, big))

    def test_a_short_lead_before_a_long_list_is_not_cut_to_one_line(self):
        # "Summary:" and then 60 points: the cut goes on inside the list, not after the first line
        self.session("maps")
        self.hub_says("maps", "Summary:\n\n" + "\n".join(f"- point {i} of the plan to read" for i in range(60)))
        got = self.page(*self.opened(f"http://127.0.0.1:{self.port}/#/s/maps", "Summary:", 390, 900, "list-390"), self.probe("Summary:"))
        big = got[-1]["eval"][0]
        self.assertTrue(big["more"], big)
        self.assertLess(abs(big["cut"] - big["feed"] / 3), 40, big)

    def test_a_message_shown_whole_stays_whole_when_new_ones_come(self):
        self.session("maps")
        # six one-line paragraphs: under two thirds of a tall window, over two thirds of a low one
        text = "Report.\n\n" + "\n\n".join(f"Point {i} worth reading slowly." for i in range(6))
        self.hub_says("maps", text)
        got = self.page(*self.opened(f"http://127.0.0.1:{self.port}/#/s/maps", "Report.", 1440, 900, "whole"), self.probe("Report."))
        self.assertFalse(got[-1]["eval"][0]["more"], got[-1])
        # the window gets smaller and new messages come: the decision stays
        for i in range(3):
            self.hub_says("maps", f"Next note {i}.")
        got = self.page({"eval": "1"}, {"shot": f"{self.profile}/whole-after.png", "width": 1440, "height": 420},
                        {"wait": "Next note 2"}, {"sleep": 300}, self.probe("Report."))
        self.assertFalse(got[-1]["eval"][0]["more"], got[-1])
        # opened afresh in the low window, the same message is folded: it is the decision that kept it whole
        got = self.page(*self.opened(f"http://127.0.0.1:{self.port}/#/s/maps", "Next note 2", 1440, 420, "whole-low"), self.probe("Report."))
        self.assertTrue(got[-1]["eval"][0]["more"], got[-1])

    def answered_turn(self):
        """A session reply, then the owner's next message taken by the session (the core's reactions.taken), with the
        "Reactions" module on, as on most hubs that have it."""
        cfg = self.web.config
        self.web.config = lambda: {**cfg(), "reactions": {"enabled": True}}
        self.session("a")
        self.ev("turn.started", "a", marker=None)
        self.ev("session.said", "a", text="Old long report. " + "Detail after detail. " * 40)
        self.ev("turn.stopped", "a")
        e = self.j.append("message.received", None, {"to": "a", "text": "got it, go on", "channel": "web", "owner": True})
        self.ev("turn.started", "a", marker=f"m{e['seq']}", prompt_head="got it")
        self.ev("session.said", "a", text="Fresh long report. " + "Detail after detail. " * 20)
        self.ev("turn.stopped", "a")

    def test_an_answered_reply_neither_folds_nor_grays_by_default(self):
        self.answered_turn()
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/s/a", "feed": "compact"}, {"wait": "Fresh long"},
                        {"sleep": 400}, self.probe("Old long"), self.probe("Fresh long"))
        old, fresh = got[-2]["eval"][0], got[-1]["eval"][0]
        self.assertFalse(old["more"], old)   # ~840 characters: the answered reply was cut to three lines
        self.assertFalse(old["gray"], old)
        self.assertEqual(old["color"], fresh["color"])

    def test_every_reply_of_a_turn_stays_in_view_in_brief(self):
        self.session("a")
        e = self.j.append("message.received", None, {"to": "a", "text": "what is in the logs?", "channel": "web", "owner": True})
        self.ev("turn.started", "a", marker=f"m{e['seq']}", prompt_head="what")
        self.ev("session.said", "a", text="The logs show two failed logins from one address.")
        self.ev("turn.tool", "a", name="Write", input={"file_path": "/w/memory.md"}, id="t1")
        self.ev("session.said", "a", text="Saved that to memory.")
        self.ev("turn.stopped", "a")
        rows = ("[...document.querySelectorAll('main .feed .row.out')].map((r) => r.textContent)"
                ".filter((x) => /failed logins|Saved that/.test(x)).length")
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/s/a", "feed": "compact"}, {"wait": "Saved that"},
                        {"shot": f"{self.profile}/turn-1440.png", "width": 1440, "height": 900}, {"sleep": 300},
                        {"eval": rows}, {"eval": "document.querySelector('main').innerText.includes('two failed logins')"})
        self.assertEqual(got[-2]["eval"], 2, got)
        self.assertTrue(got[-1]["eval"], got)


    def test_show_all_under_a_fold_is_a_button_sized_for_a_finger_in_clean(self):
        # "Show all" / "Show less": the header "More" button's look (12/16 in the text color, a chevron), a 44 px touch on a phone;
        # it was a gray 12 px "more" in small letters that read as a caption
        import json, os
        en = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "i18n", "en.json"), encoding="utf-8"))
        self.session("maps")
        self.hub_says("maps", "\n\n".join(f"Tall brief, paragraph {i}: one more point to read." for i in range(60)))
        look = r"""(() => { const b = document.querySelector('main .feed .clamp-more'), c = getComputedStyle(b), r = b.getBoundingClientRect();
          return {text: b.textContent, size: c.fontSize, weight: c.fontWeight, color: c.color, h: Math.round(r.height),
                  text1: getComputedStyle(b.closest('.row').querySelector('.md')).color}; })()"""
        got = self.page({"go": "about:blank", "look": None}, {"local": {"hub.theme": '{"id":"core:clean"}'}},
                        {"go": f"http://127.0.0.1:{self.port}/#/s/maps", "look": None}, {"wait": "Tall brief"},
                        {"shot": f"{self.profile}/more-390.png", "width": 390, "height": 844}, {"sleep": 400}, {"eval": look},
                        {"eval": "document.querySelector('main .feed .clamp-more').click(), 1"}, {"sleep": 300}, {"eval": look},
                        {"shot": f"{self.profile}/more-1440.png", "width": 1440, "height": 900}, {"sleep": 400}, {"eval": look})
        phone, opened, wide = got[6]["eval"], got[9]["eval"], got[12]["eval"]
        self.assertEqual(phone["text"], en["web.feed.show_all"], phone)
        self.assertEqual(opened["text"], en["web.feed.show_less"], opened)
        self.assertEqual((phone["size"], phone["weight"]), ("12px", "400"), phone)
        self.assertEqual(phone["color"], phone["text1"], phone)
        self.assertGreaterEqual(phone["h"], 44, phone)
        self.assertLess(wide["h"], 44, wide)   # a desktop has a pointer: the button keeps its line

    # a second "Reply" under a message: shown only when the rendered message is taller than the feed's visible area
    LOW = r"""((part) => {
      const feed = document.querySelector('main .feed');
      return [...document.querySelectorAll('main .feed .row.out')].filter((r) => r.textContent.includes(part)).map((r) => {
        const btn = r.querySelector('.reply-btn-bottom');
        return {feed: feed.clientHeight, body: r.querySelector('.md').getBoundingClientRect().height, count: r.querySelectorAll('.reply-btn-bottom').length,
                shown: !!btn && !!btn.getClientRects().length};
      });
    })"""

    def results_feed(self):
        """Task results from an executor into the hub feed (the `reply` kind): the first one with the quote, the rest "again"."""
        self.ev("session.requested", "hub", session_id="s-hub", cwd="/w", kind="hub")
        self.ev("session.started", "hub", session_id="s-hub")
        self.session("w")
        m = self.j.append("message.received", None, {"to": "hub", "text": "fix the site map", "channel": "web", "owner": True})
        self.ev("turn.started", "hub", marker=f"m{m['seq']}", prompt_head="fix")   # the hub took it: no "waiting" strip over the field
        self.ev("turn.stopped", "hub")
        d = self.j.append("delegation.created", None, {"session": "w", "words": "fix the site map", "context": "T-1", "for": f"m{m['seq']}",
                                                      "channel": "session", "owner": False, "sender": "hub"})
        first = [True]

        def put(texts):
            for text in texts:
                self.ev("turn.started", "w", marker=f"m{d['seq']}" if first else None)
                first.clear()
                self.ev("turn.result", "w", text=text, is_error=False)
                self.ev("turn.stopped", "w")
        return "hub", put

    def says_feed(self):
        """Session replies of the `out` kind."""
        self.session("a")

        def put(texts):
            for text in texts:
                self.ev("turn.started", "a", marker=None)
                self.ev("session.said", "a", text=text)
                self.ev("turn.stopped", "a")
        return "a", put

    def lines(self, marker, n):
        return marker + "".join(f"\n\nPoint {i}." for i in range(n - 1))

    def check_bottom_reply(self, feed_of):
        """The same message of n one-line paragraphs, put into the feed as `put(texts)`: the pilot measures the feed and the height
        of a paragraph, then messages just below and just above the feed's height are shown — the bottom button only under the second."""
        where, put = feed_of()
        url = f"http://127.0.0.1:{self.port}/#/s/{where}"
        put([self.lines("Pilot one.", 1), self.lines("Pilot three.", 3)])
        got = self.page(*self.opened(url, "Pilot three", 1440, 900, f"{where}-pilot"), {"eval": f"{self.LOW}('Pilot one.')"}, {"eval": f"{self.LOW}('Pilot three.')"})
        one, three = got[-2]["eval"][0], got[-1]["eval"][0]
        self.assertFalse(one["shown"] or three["shown"], (one, three))   # a short one never has the second button
        feed, step = one["feed"], (three["body"] - one["body"]) / 2
        below = int((feed - step / 2 - one["body"]) // step) + 1    # a message a half step under the feed's height
        above = below + 2                                            # and one a half step over it
        put([self.lines("Below the edge.", below), self.lines("Above the edge.", above), "Short one."])
        press = {"eval": "[...document.querySelectorAll('main .feed .reply-btn-bottom')].pop().click(), 1"}
        got = self.page(*self.opened(url, "Above the edge", 1440, 900, f"{where}-edge"), self.probe_low("Below the edge."),
                        self.probe_low("Above the edge."), self.probe_low("Short one."), press,
                        {"until": "!!document.querySelector('.reply-chip')?.textContent"}, {"eval": "document.querySelector('.reply-chip').textContent"})
        low, high, short = (got[-6 + i]["eval"][0] for i in range(3))
        self.assertLess(low["body"], low["feed"], low)
        self.assertGreater(high["body"], high["feed"], high)
        self.assertEqual((low["count"], short["count"]), (0, 0), (low, short))   # under the feed's height or short: only the first button
        self.assertTrue(high["shown"], high)
        self.assertIn("Above the edge.", got[-1]["eval"])                # the second button replies to the same message

    def probe_low(self, part):
        return {"eval": f"{self.LOW}({part!r})"}

    def test_a_second_reply_button_is_under_a_session_message_taller_than_the_feed(self):
        self.check_bottom_reply(self.says_feed)

    def test_a_second_reply_button_is_under_a_task_result_taller_than_the_feed(self):
        self.check_bottom_reply(self.results_feed)

    def check_clean_edges(self, feed_of, reactions):
        # "Copy" (and "Reaction") widen the room a row keeps at its end: the second "Reply" follows it, so the two stand one above the other
        edge = r"""(() => { const r = [...document.querySelectorAll('main .feed .row.out')].find((r) => r.textContent.includes('Tall report'));
          const right = (n) => Math.round(n.getBoundingClientRect().right);
          return {top: right(r.querySelector('.reply-btn')), low: right(r.querySelector('.reply-btn-bottom'))}; })()"""
        clean = {"local": {"hub.theme": '{"id":"core:clean"}'}}
        cfg = self.web.config
        self.web.config = lambda: {**cfg(), "reactions": {"enabled": reactions}}
        where, put = feed_of()
        put([self.lines("Tall report.", 60)])
        for width in (1440, 390):
            steps = [clean, *self.opened(f"http://127.0.0.1:{self.port}/#/s/{where}", "Tall report", width, 900, f"edge-{width}")]
            got = self.page(*steps, {"eval": edge})[-1]["eval"]
            self.assertEqual(got["top"], got["low"], (reactions, width, got))

    def test_clean_the_second_reply_ends_where_the_first_one_does_beside_copy(self):
        self.check_clean_edges(self.says_feed, False)

    def test_clean_the_second_reply_ends_where_the_first_one_does_beside_copy_and_reaction(self):
        self.check_clean_edges(self.says_feed, True)

    def test_clean_the_second_reply_of_a_task_result_ends_where_the_first_one_does(self):
        self.check_clean_edges(self.results_feed, False)

    def test_an_answered_reply_folded_to_two_lines_gets_no_second_reply_button(self):
        # folded to two lines the message is short on the page, though its text is tall: a button under it would be a second one for nothing
        cfg = self.web.config
        self.web.config = lambda: {**cfg(), "reactions": {"enabled": True}}
        self.session("a")
        self.ev("turn.started", "a", marker=None)
        self.ev("session.said", "a", text=self.lines("Old tall report.", 60))
        self.ev("turn.stopped", "a")
        e = self.j.append("message.received", None, {"to": "a", "text": "got it, go on", "channel": "web", "owner": True})
        self.ev("turn.started", "a", marker=f"m{e['seq']}", prompt_head="got it")
        self.ev("session.said", "a", text=self.lines("Fresh tall report.", 60))
        self.ev("turn.stopped", "a")
        steps = self.opened(f"http://127.0.0.1:{self.port}/#/s/a", "Fresh tall", 1440, 900, "answered")
        steps[2] = {**steps[2], "feed": "compact", "brief": {"resolved": True}}   # Brief with "Fold the answered"
        got = self.page(*steps, {"eval": "!!document.querySelector('main .feed .clamp-2')"}, self.probe_low("Old tall"), self.probe_low("Fresh tall"))
        self.assertTrue(got[-3]["eval"], got)                       # the old one is folded to two lines
        self.assertEqual(got[-2]["eval"][0]["count"], 0, got[-2])   # answered and folded: no second button
        self.assertEqual(got[-1]["eval"][0]["shown"], True, got[-1])   # the fresh one is open and tall: it has it

    def test_the_fold_is_measured_against_the_feed_as_it_stays(self):
        # an executor on a task from the hub reports into the hub's feed, whose field carries the task strip, put in after the feed on the first draw. The fold
        # measured the feed before it (taller, a higher bar), so a message just over the bar came whole — and a cut is decided
        # once, no later draw repairs it
        self.ev("session.requested", "hub", session_id="s-hub", cwd="/w", kind="hub")
        self.ev("session.started", "hub", session_id="s-hub")
        self.session("w")
        m = self.j.append("message.received", None, {"to": "hub", "text": "fix the site map", "channel": "web", "owner": True})
        self.ev("turn.started", "hub", marker=f"m{m['seq']}", prompt_head="fix")
        self.ev("turn.stopped", "hub")
        d = self.j.append("delegation.created", None, {"session": "w", "words": "fix the site map", "context": "T-1", "for": f"m{m['seq']}",
                                                      "channel": "session", "owner": False, "sender": "hub"})
        self.ev("turn.started", "w", marker=f"m{d['seq']}")
        self.ev("turn.result", "w", text="Took it.", is_error=False)
        self.ev("turn.stopped", "w")
        lines = lambda marker, n: marker + "".join(f"\n\nPoint {i}." for i in range(n - 1))

        def w_wrote(text):   # a word from the executor to the hub, taken by a turn: a message of another participant, measured for the fold
            e = self.j.append("message.received", None, {"to": "hub", "text": text, "channel": "session", "owner": False, "sender": "w"})
            self.ev("turn.started", "hub", marker=f"m{e['seq']}")
            self.ev("turn.stopped", "hub")

        def full(steps):   # the full feed: in "Brief" the executor's words would sit folded in a group row
            steps[2] = {**steps[2], "feed": "full"}
            return steps
        w_wrote(lines("Pilot one.", 1))
        w_wrote(lines("Pilot three.", 3))
        url = f"http://127.0.0.1:{self.port}/#/s/hub"
        tall = r"""((part) => [...document.querySelectorAll('main .feed .row')].filter((r) => r.textContent.includes(part))
                   .map((r) => ({feed: document.querySelector('main .feed').clientHeight, body: r.querySelector('.md').getBoundingClientRect().height})))"""
        got = self.page(*full(self.opened(url, "Pilot three", 1440, 900, "w-pilot")), {"eval": f"{tall}('Pilot one.')"}, {"eval": f"{tall}('Pilot three.')"})
        one, three = got[-2]["eval"][0], got[-1]["eval"][0]
        bar = max(one["feed"], 450) * 2 / 3                      # the fold's bar on this page (foldTall), the window is 900 high
        step = (three["body"] - one["body"]) / 2
        n = int((bar - one["body"]) // step) + 2                 # a message of one-line paragraphs just over the bar (under a step over it)
        w_wrote(lines("Over the bar.", n))
        got = self.page(*full(self.opened(url, "Over the bar", 1440, 900, "w-over")), self.probe("Over the bar."), {"eval": f"{tall}('Over the bar.')"})
        over = got[-2]["eval"][0]
        self.assertLess(bar, got[-1]["eval"][0]["body"], got[-1])             # it is over the bar as the page stays
        self.assertLess(got[-1]["eval"][0]["body"], bar + step, got[-1])      # though under a step over it
        self.assertTrue(over["more"], over)                                   # so it is cut, with "more" under it
        self.assertTrue(-1 <= one["feed"] / 3 - over["cut"] < step, over)   # to a third of the feed, on a whole paragraph

def load_tests(loader, standard, pattern):
    return unittest.TestSuite(FeedFold(name) for name in FeedFold.__dict__ if name.startswith("test_"))
