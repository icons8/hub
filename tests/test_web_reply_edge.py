"""The "Reply" button of a session message sits at the right edge of the message itself, not at the edge of the feed."""
import tests  # noqa: F401
import unittest
from tests.test_web_e2e import PageInChrome


class ReplyEdge(PageInChrome):
    PROBE = r"""(() => {
      const rect = (n) => n.getBoundingClientRect();
      return [...document.querySelectorAll('.row.out')].filter((r) => r.querySelector(':scope > .b > .byline')).map((r) => {
        const b = r.querySelector(':scope > .b'), btn = rect(b.querySelector(':scope > .reply-btn')),
          meta = rect(b.querySelector(':scope > .meta')), text = rect(b.querySelector(':scope > .md'));
        return {btnRight: btn.right, messageRight: rect(b).right, rowRight: rect(r).right,
                afterMeta: btn.left >= meta.right - 1, aboveText: btn.bottom <= text.top + 1};
      });
    })()"""

    def message_in_header(self, session, text, width):
        """The first reply of a run carries the name line; the geometry of its row: the button against the message and the feed."""
        self.ev("session.requested", session, session_id="s-" + session, cwd="/w", channel="cli", owner=True)
        self.ev("session.started", session, session_id="s-" + session)
        self.ev("session.said", session, text=text)
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/s/{session}"}, {"wait": text[:6]},
                        {"shot": f"{self.profile}/{session}-{width}.png", "width": width, "height": 900}, {"sleep": 400},
                        {"eval": self.PROBE})[-1]["eval"]
        self.assertEqual(len(got), 1, got)
        m = got[0]
        self.assertTrue(m["afterMeta"], (width, m))    # beside the time, not over it
        self.assertTrue(m["aboveText"], (width, m))    # in the name line, above the text
        return m

    def test_the_button_stands_at_the_right_edge_of_its_own_message(self):
        for width in (390, 1440):
            short = self.message_in_header(f"short{width}", "Done.", width)
            self.assertAlmostEqual(short["btnRight"], short["messageRight"], delta=1, msg=(width, short))
            self.assertLess(short["messageRight"], short["rowRight"] - 40, (width, short))   # a short message does not span the feed
            long = self.message_in_header(f"long{width}", "A long reply about where the button stands in the feed. " * 12, width)
            self.assertAlmostEqual(long["btnRight"], long["messageRight"], delta=1, msg=(width, long))
            if width < 760:    # the text fills the feed here; on a wide screen a line stops at the reading measure, the message with it
                self.assertGreater(long["messageRight"], long["rowRight"] - 4, (width, long))


def load_tests(loader, standard, pattern):
    return unittest.TestSuite(ReplyEdge(name) for name in ReplyEdge.__dict__ if name.startswith("test_"))
