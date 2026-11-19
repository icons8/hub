"""The chevron of an opened executors badge ("2 ⌄") and of an opened chat group of the feed does not blink on background
redraws: the Clean theme's rotate transition does not start again (the badge node stays the same too)."""
import tests  # noqa: F401

from tests.test_web_e2e import PageInChrome

# the list redraws (what every snapshot does) several times while the frames of the chevron are written down
SAMPLE = """(() => {
  const chev = () => document.querySelector('#list li[data-s="maps"] > .fork-toggle .chev');
  const first = chev(), head = document.querySelector('#list .sgroup-h');
  window.__chev = first;
  window.__log = [];
  let n = 0;
  const tick = () => {
    if (n % 4 === 1 && n < 20) dispatchEvent(new Event('hub-redraw'));
    const c = chev();
    window.__log.push({ head: !!head && document.querySelector('#list .sgroup-h') === head, same: c === first, here: !!c && c.isConnected, anim: c ? c.getAnimations().length : -1,
                        tf: c ? getComputedStyle(c).transform : '', down: c ? c.dataset.down || '' : '', text: c ? c.textContent : '' });
    if (++n < 30) requestAnimationFrame(tick); else window.__done = true;
  };
  requestAnimationFrame(tick);
  return 1;
})()"""

# the same for the chevron of an opened chat group in the feed (the feed builds new nodes on a redraw, so the mark must be there from the start)
CHAT = """(() => {
  window.__log = [];
  import('/static/js/state.js').then((m) => {
    let n = 0;
    const tick = () => {
      if (n % 4 === 1 && n < 20) m.render();
      const c = document.querySelector('.chat-group .chat-head .chat-chev');
      window.__log.push({ here: !!c, anim: c ? c.getAnimations().length : -1, tf: c ? getComputedStyle(c).transform : '' });
      if (++n < 30) requestAnimationFrame(tick); else window.__done = true;
    };
    requestAnimationFrame(tick);
  });
  return 1;
})()"""


class ForkChevron(PageInChrome):
    def stand(self):
        for name, extra in (("maps", {}), ("maps-x", {"executor": True, "raised_by": "maps"}),
                            ("maps-y", {"executor": True, "raised_by": "maps"})):
            self.ev("session.requested", name, session_id="s-" + name, cwd="/w", channel="cli", owner=True, **extra)
            self.ev("session.started", name, session_id="s-" + name)
            if extra:
                self.ev("turn.started", name, marker=None, prompt_head="long work")

    def frames(self, theme):
        self.stand()
        tog = '#list li[data-s="maps"] > .fork-toggle'
        go = {"go": f"http://127.0.0.1:{self.port}/#/sessions"}
        if theme == "clean":
            go["look"] = None   # a browser with no theme chosen: Clean
        else:
            self.page({"local": {"hub.theme": '{"id":"core:classic"}'}})
        got = self.page(go, {"until": f"!!document.querySelector('{tog}')"},
                        {"eval": f"(() => {{ const t = document.querySelector('{tog}'); if (t.getAttribute('aria-expanded') !== 'true') t.click(); return 1; }})()"},
                        {"until": f"document.querySelector('{tog}')?.getAttribute('aria-expanded') === 'true'"},
                        {"sleep": 600},
                        {"eval": "document.documentElement.classList.contains('th-clean')"},
                        {"eval": SAMPLE}, {"until": "window.__done", "ms": 5000}, {"eval": "window.__log"})
        self.assertEqual(got[5]["eval"], theme == "clean", got[5])
        return got[-1]["eval"]

    def check(self, log, clean):
        self.assertEqual(len(log), 30)
        for i, f in enumerate(log):
            self.assertTrue(f["here"] and f["same"], (i, f))        # the same node in the same place
            self.assertTrue(f["head"], (i, f))                      # an opened group heading is kept too
            self.assertEqual(f["anim"], 0, (i, f))                  # no transition running or restarted
            self.assertEqual(f["text"], "▾", (i, f))
            if clean:
                self.assertEqual(f["tf"], "matrix(0, 1, -1, 0, 0, 0)", (i, f))   # turned down on every frame

    def test_the_opened_badge_chevron_stays_in_clean(self):
        self.check(self.frames("clean"), True)

    def test_the_opened_badge_chevron_stays_in_classic(self):
        self.check(self.frames("classic"), False)

    def test_the_opened_chat_group_chevron_stays_turned_in_clean(self):
        self.ev("session.requested", "a", session_id="s-a", cwd="/w", channel="cli", owner=True)
        self.ev("session.started", "a", session_id="s-a")
        for text in ("REPLIED: one", "REPLIED: two"):
            e = self.j.append("message.received", None, {"to": "a", "text": text, "channel": "session", "owner": False, "sender": "hub-core"})
            self.ev("turn.started", "a", marker=f"m{e['seq']}", prompt_head=text)
            self.ev("session.said", "a", text="done")
            self.ev("turn.result", "a", text="done")
            self.ev("turn.stopped", "a")
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/s/a", "feed": "compact", "look": None},
                        {"until": "!!document.querySelector('.chat-head')"},
                        {"eval": "document.querySelector('.chat-head').click(); 1"},
                        {"until": "document.querySelector('.chat-head')?.getAttribute('aria-expanded') === 'true'"}, {"sleep": 600},
                        {"eval": CHAT}, {"until": "window.__done", "ms": 5000}, {"eval": "window.__log"})
        for i, f in enumerate(got[-1]["eval"]):
            self.assertEqual(f, {"here": True, "anim": 0, "tf": "matrix(0, 1, -1, 0, 0, 0)"}, i)


def load_tests(loader, standard, pattern):   # only its own tests, not the ~420 inherited from the page
    import unittest
    return unittest.TestSuite(ForkChevron(n) for n in ForkChevron.__dict__ if n.startswith("test_"))
