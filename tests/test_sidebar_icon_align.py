"""List-row icons sit on the center of the FIRST line of the row (the name), not on the middle of a multi-line row:
status icon, unread dot, "waiting for you" tag, dismiss tag and the executors toggle."""
import tests  # noqa: F401
import json
import os

from tests.test_web_e2e import PageInChrome

# (name, extra event data); the page then marks them: unread, waiting, a guess, a long gist
FLEET = (("alpha", {"role": "agent"}), ("worker-1", {"executor": True, "raised_by": "alpha"}),
         ("worker-2", {"executor": True, "raised_by": "alpha"}),
         ("beta", {}), ("gamma", {}), ("delta", {}), ("epsilon", {}))

LONG = ("A long caption that does not fit on one line of a narrow list, so it wraps onto the second and the third line "
        "of the row, and the icons must still stay on the line of the name and not slide down with the text")

MEASURE = """(() => {
  const c = (r) => r.top + r.height / 2;
  return [...document.querySelectorAll('#list .srow')].map((row) => {
    const nm = row.querySelector('.nm');
    const first = c(nm.getBoundingClientRect());
    const out = { name: nm.textContent.trim(), h: Math.round(row.getBoundingClientRect().height), cls: row.className, d: {} };
    for (const k of ['.glyph', '.dot', '.tag']) {
      const n = row.querySelector(':scope > ' + k);
      if (n) out.d[k] = Math.round((c(n.getBoundingClientRect()) - first) * 10) / 10;
    }
    const tog = row.parentElement.querySelector(':scope > .fork-toggle');
    if (tog) out.d['.fork-toggle'] = Math.round((c(tog.getBoundingClientRect()) - first) * 10) / 10;
    return out;
  });
})()"""

PREP = """(async () => {
  const m = await import('/static/js/state.js');
  while (!(m.S.snap && m.S.snap.sessions && m.S.snap.sessions.length >= 7)) await new Promise((r) => setTimeout(r, 100));
  const S = m.S, by = (n) => S.snap.sessions.find((s) => s.name === n);
  const long = %s;
  // an answer to the owner: "Answered you" in "Now" (a result a session sent on its own is folded there, spec d2433840)
  by('beta').unread = true; by('beta').label = 'idle'; by('beta').owner_out = by('beta').owner_reply = 9; by('beta').seen = 0;
  by('gamma').status = 'waiting';
  by('delta').attention = { ask: 'Say yes to the merge' }; by('delta').unread = false;
  by('epsilon').gist = { text: long, ts: 1 }; by('epsilon').unread = true; by('epsilon').owner_out = by('epsilon').owner_reply = 9;
  by('worker-1').status = 'waiting';
  m.render && m.render();
  window.__ready = true; return 1;
})()""" % json.dumps(LONG)


class IconAlign(PageInChrome):
    def fleet(self):
        for name, extra in FLEET:
            self.ev("session.requested", name, session_id="s-" + name, cwd="/w", channel="cli", owner=True, **extra)
            self.ev("session.started", name, session_id="s-" + name)

    def rows(self, mode, width, theme=None):
        """Offsets (px) of every icon's center from the center of the name's line, per row."""
        self.fleet()
        steps = [{"go": f"http://127.0.0.1:{self.port}/#/sessions"}, {"local": {"hub.sidebar": mode}},
                 {"eval": "location.reload()"}, {"sleep": 800},
                 {"until": "document.querySelector('#list .srow')"},
                 {"eval": PREP}, {"until": "window.__ready"}, {"sleep": 700}]
        self.page(*steps)
        # the shot step is what sets the width and the theme; its frame is kept only in HUB_TEST_ICON_FRAMES (for a
        # review), otherwise it goes into the test's temporary state folder and is removed with it
        frames = os.environ.get("HUB_TEST_ICON_FRAMES")
        sh = {"shot": os.path.join(frames or self.state, f"{mode}-{width}-{theme or 'light'}.png"), "width": width}
        if theme:
            sh["theme"] = theme
        self.page(sh)
        return {r["name"]: r for r in self.page({"eval": MEASURE})[0]["eval"]}

    def check(self, mode, width, theme=None):
        rows = self.rows(mode, width, theme)
        want = {"beta": ".dot", "gamma": ".tag", "delta": ".tag"}   # the icon this row must have, or the test checks nothing
        for name, key in want.items():
            self.assertIn(key, rows[name]["d"], (mode, width, name, rows[name]))
        self.assertTrue(any(".fork-toggle" in r["d"] for r in rows.values()), rows)
        # 1 px: here the icons sit within 0.1 px of the name line and the old layout put them 1.6-2 px off; fonts and
        # rounding of other machines (headless-shell of a self-check) move a centered icon by a fraction of a pixel
        for name, r in rows.items():
            for key, dy in r["d"].items():
                self.assertLessEqual(abs(dy), 1, f"{mode} {width}px: {key} of {name} is {dy}px off the name line")

    def test_all(self):
        self.check("all", 1300)

    def test_all_narrow(self):
        self.check("all", 390)

    def test_now(self):   # "Now": the caption takes up to two lines, the icons stay on the first
        self.check("active", 1300)

    def test_now_narrow(self):
        self.check("active", 390, "dark")


def load_tests(loader, standard, pattern):   # only its own tests, not the ~420 inherited from the page
    import unittest
    return unittest.TestSuite(IconAlign(n) for n in IconAlign.__dict__ if n.startswith("test_"))
