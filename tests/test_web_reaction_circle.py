"""A reaction badge in Clean stays the plate it was (28 high, round ends). Its emoji is moved to the optical center only where the
tuned offsets hold, Apple's emoji font (macOS, iOS): elsewhere it stays as drawn. A theme or plugin that adjusts a specific emoji with
`[data-emoji] { transform }` (API 1.31) moves it from where it is drawn: its rule replaces the offset, the two do not add up. The
offsets are tuned for one font, so the test does not measure the emoji's pixels."""
import tests  # noqa: F401

from tests.test_web_e2e import PageInChrome

LOOK = """(() => {
  const c = document.querySelector('.row.mine .react-chip'), e = c.querySelector('.react-emoji'), r = c.getBoundingClientRect();
  const s = getComputedStyle(c), es = getComputedStyle(e);
  return { h: r.height, w: r.width, radius: parseFloat(s.borderTopLeftRadius), translate: es.translate, transform: es.transform, display: es.display,
           apple: document.documentElement.classList.contains('th-clean-apple-emoji') };
})()"""


class ReactionPlate(PageInChrome):
    def test_the_emoji_moves_only_with_apples_font_and_a_themes_own_transform_replaces_the_offset(self):
        self.web.config = (lambda cfg: lambda: {**cfg(), "reactions": {"enabled": True}})(self.web.config)
        self.ev("session.requested", "a", session_id="s-a", cwd="/w")
        self.ev("session.started", "a")
        e = self.j.append("message.received", None, {"to": "a", "text": "make a report", "channel": "web", "owner": True})
        self.ev("turn.started", "a", marker=f"m{e['seq']}", prompt_head="make")
        self.ev("turn.stopped", "a")
        self.ev("message.reacted", None, target=f"m{e['seq']}", emoji="❤️", channel="web", owner=True)
        apple = "document.documentElement.classList.toggle('th-clean-apple-emoji', %s), 1"
        own = ("(() => { const s = document.createElement('style'); s.textContent = '[data-emoji=\"❤️\"] { transform: translateY(-1px) }'; "
               "document.head.append(s); return 1; })()")
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/s/a", "look": None},
                        {"until": "((c) => c.length === 1 && !c[0].getAnimations({subtree: true}).length && document.fonts.status === 'loaded')"
                                  "(document.querySelectorAll('.react-chip'))", "ms": 8000},
                        {"eval": "/mac|iphone|ipad/i.test(navigator.userAgentData?.platform || navigator.platform || '')"},
                        {"eval": LOOK}, {"eval": apple % "true"}, {"eval": LOOK}, {"eval": apple % "false"}, {"eval": LOOK},
                        {"eval": apple % "true"}, {"eval": own}, {"eval": LOOK})
        on_apple, first, mac, other, themed = got[2]["eval"], got[3]["eval"], got[5]["eval"], got[7]["eval"], got[10]["eval"]
        self.assertEqual(first["apple"], on_apple)          # the class follows the system's emoji font
        self.assertGreaterEqual(mac["h"], 28)
        self.assertGreater(mac["w"], mac["h"])              # a plate, not a circle
        self.assertGreaterEqual(mac["radius"], 14)          # round ends
        self.assertEqual(mac["display"], "inline-block")
        self.assertNotEqual(mac["transform"], "none")       # Apple's font: moved to its optical center
        self.assertEqual(mac["translate"], "none")
        self.assertEqual([other["transform"], other["translate"]], ["none", "none"])   # another font: as drawn
        self.assertEqual([themed["transform"], themed["translate"]], ["matrix(1, 0, 0, 1, 0, -1)", "none"])   # the theme's alone

def load_tests(loader, standard, pattern):   # only its own tests, not the ~420 inherited from the page
    import unittest
    return unittest.TestSuite(ReactionPlate(n) for n in ReactionPlate.__dict__ if n.startswith("test_"))
