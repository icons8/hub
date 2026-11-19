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

def load_tests(loader, standard, pattern):
    return unittest.TestSuite(FeedFold(name) for name in FeedFold.__dict__ if name.startswith("test_"))
