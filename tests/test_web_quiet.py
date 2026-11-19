"""A page left open costs little: a working session's stopwatch ticking every second and a snapshot every second do not
make the browser lay the whole page out again. A profile of a long feed (about 2700 rows) showed the page busy all the
time: each snapshot read the input box's style right after the feed was rebuilt, the status line and the task list were
put in anew, Clean's sweep ran on every tick of the stopwatch and read the list's scroll. The counters below are kept by the
page itself (spies put in before its scripts); the old code fails each of them."""
import tests  # noqa: F401
import threading
import time
import unittest

from tests.test_web_e2e import PageInChrome

# put in before the page's scripts: what the checks count
SPY = r"""(() => {
  const spy = window.__spy = { wrapDirty: 0, sweeps: 0, listReads: 0 };
  // a read of the input box's --composer-wrap while the DOM has edits of the same task not yet laid out: a forced style pass
  const pending = new MutationObserver(() => {});
  pending.observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
  const gpv = CSSStyleDeclaration.prototype.getPropertyValue;
  CSSStyleDeclaration.prototype.getPropertyValue = function (name) {
    if (name === "--composer-wrap" && pending.takeRecords().length) spy.wrapDirty++;
    return gpv.call(this, name);
  };
  // a whole Clean sweep: it starts by marking the feed's bylines
  const qsa = Document.prototype.querySelectorAll;
  Document.prototype.querySelectorAll = function (sel) {
    if (sel === ".byline:not([data-clean])") spy.sweeps++;
    return qsa.call(this, sel);
  };
  // the list's scroll read (Clean's edge fade): a layout of the page when anything changed before it
  const top = Object.getOwnPropertyDescriptor(Element.prototype, "scrollTop");
  Object.defineProperty(Element.prototype, "scrollTop", { ...top, get() { if (this.id === "list") spy.listReads++; return top.get.call(this); } });
})()"""

# after the page settles: the edits of the status line, the task list and the bottom row of the input card
WATCH = r"""(() => {
  const d = document.querySelector('main .dock-inner'), c = window.__dock = { status: 0, todos: 0, fit: 0 };
  new MutationObserver((l) => { for (const m of l) {
    if (m.target.closest?.('.status')) c.status++;
    else if (m.target.closest?.('.todos')) c.todos++;
    else if (m.target === d) c.fit++;
  } }).observe(d, { subtree: true, childList: true, characterData: true, attributes: true });
  for (const k in window.__spy) window.__spy[k] = 0;
  return 1;
})()"""


# the stopwatch's spans: a text node put in place of another (a change of the tree) is counted
TREE = r"""(() => { window.__tree = 0; new MutationObserver((l) => { window.__tree += l.filter((m) => m.type === 'childList').length; })
  .observe(document.querySelector('main .row.working .elapsed'), { childList: true }); return 1; })()"""


class QuietPage(PageInChrome):
    def setUp(self):
        super().setUp()
        self.stop_executor_stand()   # the hub, maps and its busy executor maps-x: the stopwatch ticks in the list and the feed
        self.ev("session.said", "maps-x", text="First step done: `src/tile.py` reads the tiles.\n\n```python\ndef tile(x, y):\n    return x * y\n```")
        self.ev("turn.tool", "maps-x", id="t0", name="Read", input={"file_path": "/w/src/tile.py"})
        self.ev("turn.tool_result", "maps-x", id="t0", text="ok", error=False)

    def go(self, clean):
        return {"go": f"http://127.0.0.1:{self.port}/#/s/maps-x", "init": SPY, **({"look": None} if clean else {})}

    def feed_events(self, n, gap):
        """n snapshots while the page watches: a step of the working session every gap seconds."""
        def run():
            for i in range(n):
                time.sleep(gap)
                self.ev("turn.tool", "maps-x", id=f"q{i}", name="Bash", input={"command": f"make step-{i}"})
                self.ev("turn.tool_result", "maps-x", id=f"q{i}", text="ok", error=False)
        th = threading.Thread(target=run, daemon=True)
        th.start()
        return th

    def test_clean_does_not_sweep_the_page_on_every_tick_of_the_stopwatch(self):
        tick = "document.querySelector('main .row.working .elapsed')"
        got = self.page(self.go(True), {"until": "main.dataset.for === 'maps-x' && !!" + tick, "ms": 15000},
                        {"sleep": 1500}, {"eval": WATCH}, {"eval": f"{tick}.textContent"}, {"eval": TREE}, {"sleep": 4200},
                        {"eval": "[window.__spy.sweeps, window.__spy.listReads, window.__tree]"},
                        {"eval": f"[{tick}.textContent, {tick}.previousElementSibling?.className]"})
        self.assertTrue(got[1]["until"], got)
        # four ticks, no snapshot: no sweep, the list's scroll not read, the stopwatch's text nodes stay (no tree change)
        self.assertEqual(got[7]["eval"], [0, 0, 0], got)
        # the stopwatch still goes, and Clean still draws its "·" as a mark of its own
        self.assertNotEqual(got[8]["eval"][0], got[4]["eval"], got)
        self.assertFalse(got[8]["eval"][0].startswith(" · "), got)
        self.assertEqual(got[8]["eval"][1], "clean-sep", got)

    def test_snapshots_do_not_rebuild_the_status_line_or_force_a_style_pass_from_the_input_box(self):
        for clean in (False, True):
            with self.subTest(clean=clean):
                got = self.page({"go": "about:blank", "look": None}, self.go(clean),
                                {"until": "main.dataset.for === 'maps-x' && !!document.querySelector('main .dock-inner .status > *')", "ms": 15000},
                                {"sleep": 1500}, {"eval": WATCH})
                self.assertTrue(got[2]["until"], got)
                th = self.feed_events(5, 0.6)
                got = self.page({"sleep": 4500}, {"eval": "document.querySelectorAll('main .row.tool').length"},
                                {"eval": "[window.__spy.wrapDirty, window.__dock.status, window.__dock.todos, window.__dock.fit]"})
                th.join()
                self.assertGreaterEqual(got[1]["eval"], 3, got)   # the snapshots did reach the feed
                # the field's style is not read mid-redraw; the status line, the tasks and the bottom row of the card stand
                self.assertEqual(got[2]["eval"], [0, 0, 0, 0], got)

    def test_snapshots_do_not_highlight_the_same_code_or_build_date_formatters_again(self):
        count = ("(() => { const c = window.__again = { hl: 0, dates: 0 }, h = hljs.highlight; hljs.highlight = function (...a) { c.hl++; return h.apply(this, a); };"
                 " for (const k of ['toLocaleString', 'toLocaleDateString', 'toLocaleTimeString']) { const f = Date.prototype[k];"
                 " Date.prototype[k] = function (...a) { c.dates++; return f.apply(this, a); }; } return 1; })()")
        got = self.page(self.go(False), {"until": "main.dataset.for === 'maps-x' && !!document.querySelector('main .md code.hljs')", "ms": 15000},
                        {"sleep": 1000}, {"eval": count})
        self.assertTrue(got[1]["until"], got)
        th = self.feed_events(5, 0.6)
        got = self.page({"sleep": 4500}, {"eval": "document.querySelectorAll('main .row.tool').length"},
                        {"eval": "[window.__again.hl, window.__again.dates, !!document.querySelector('main .md code.hljs .hljs-keyword')]"})
        th.join()
        self.assertGreaterEqual(got[1]["eval"], 3, got)   # the snapshots did reach the feed
        self.assertEqual(got[2]["eval"], [0, 0, True], got)   # the code is still highlighted, from the kept markup

    def test_a_snapshot_keeps_the_feed_rows_that_did_not_change(self):
        # the rows are the same nodes: no new style and layout pass over the whole feed, a theme's marks stay
        watch = ("(() => { const inner = window.__inner = document.querySelector('main .feed-inner'), c = window.__rows = { gone: 0, came: 0 };"
                 " window.__first = inner.querySelector('[data-key]');"
                 " new MutationObserver((l) => { for (const m of l) { for (const n of m.removedNodes) if (n.dataset?.key) c.gone++;"
                 " for (const n of m.addedNodes) if (n.dataset?.key) c.came++; } }).observe(inner, { childList: true }); return 1; })()")
        for clean in (False, True):
            with self.subTest(clean=clean):
                got = self.page({"go": "about:blank", "look": None}, self.go(clean),
                                {"until": "main.dataset.for === 'maps-x' && document.querySelectorAll('main .feed-inner > [data-key]').length >= 2", "ms": 15000},
                                {"sleep": 1000}, {"eval": watch})
                self.assertTrue(got[2]["until"], got)
                th = self.feed_events(5, 0.6)
                got = self.page({"sleep": 4500}, {"eval": "document.querySelectorAll('main .row.tool').length"},
                                {"eval": "[window.__rows.gone, window.__rows.came >= 3, window.__inner === document.querySelector('main .feed-inner'),"
                                         " window.__first.isConnected]"})
                th.join()
                self.assertGreaterEqual(got[1]["eval"], 3, got)
                self.assertEqual(got[2]["eval"], [0, True, True, True], got)

    def test_another_look_puts_the_feed_and_the_status_line_in_anew(self):
        # the rows are kept while their markup is the same, and Clean's own marks in them (the "·" of the working row and
        # under the field) are not taken off by Clean: switched to Classic, none of them stay
        marks = "document.querySelectorAll('main .feed .clean-sep, main .dock-inner .status .clean-sep').length"
        got = self.page(self.go(True), {"until": f"main.dataset.for === 'maps-x' && {marks} > 0", "ms": 15000},
                        {"eval": "localStorage.setItem('hub.theme', JSON.stringify({id: 'core:classic'})),"
                                 " import('/static/js/themes.js').then((m) => { m.applySavedTheme(); window.dispatchEvent(new CustomEvent('hub-theme')); }), 1"},
                        {"until": "!document.documentElement.classList.contains('th-clean')", "ms": 5000}, {"sleep": 600},
                        {"eval": f"[{marks}, document.querySelector('main .row.working .elapsed').textContent.startsWith(' · ')]"})
        self.assertTrue(got[1]["until"] and got[3]["until"], got)
        self.assertEqual(got[-1]["eval"], [0, True], got)

    def test_clean_marks_on_the_list_rows_and_the_short_placeholder_stand_through_snapshots(self):
        # the list's merge erased every attribute it had not built (Clean's data-clean-*), set its own again, and Clean put
        # its marks back — every row restyled on every snapshot; the field's placeholder went long and short the same way
        churn = ("(() => { const c = window.__churn = { rows: 0, ph: 0 }, mine = /^(class|type|aria-current|data-clean-.*)$/;"
                 " new MutationObserver((l) => { for (const m of l) if (m.target.closest?.('.srow') && mine.test(m.attributeName)) c.rows++; })"
                 ".observe(document.getElementById('list'), { subtree: true, attributes: true });"
                 " new MutationObserver((l) => { c.ph += l.length; }).observe(document.querySelector('main form.composer textarea'), { attributes: true, attributeFilter: ['placeholder'] });"
                 " return document.querySelectorAll('#list .srow[data-clean-k]').length; })()")
        got = self.page(self.go(True), {"until": "main.dataset.for === 'maps-x' && !!document.querySelector('#list .srow[data-clean-k]')", "ms": 15000},
                        {"sleep": 1500}, {"eval": churn})
        self.assertTrue(got[1]["until"], got)
        self.assertGreaterEqual(got[3]["eval"], 2, got)   # Clean has marked the rows
        th = self.feed_events(5, 0.6)
        got = self.page({"sleep": 4500}, {"eval": "document.querySelectorAll('main .row.tool').length"},
                        {"eval": "[window.__churn.rows, window.__churn.ph, document.querySelectorAll('#list .srow[data-clean-k]').length > 0,"
                                 " document.querySelector('main form.composer textarea').placeholder.endsWith('…')]"})
        th.join()
        self.assertGreaterEqual(got[1]["eval"], 3, got)
        self.assertEqual(got[2]["eval"], [0, 0, True, True], got)

    def test_a_click_that_redraws_takes_the_selection_in_the_feed_off_and_the_snapshots_come(self):
        # a selection in the feed holds the live snapshots back (state.js renderLive). An action's redraw used to put the
        # feed in anew and the selection went with it; the feed keeps its rows now, so the click on "More" left it and the
        # page stood still for up to two minutes
        m = self.j.append("message.received", None, {"to": "maps", "text": "\n\n".join(f"Line {i} of a long brief." for i in range(60)),
                                                     "channel": "session", "owner": False, "sender": "hub"})
        self.ev("turn.started", "maps", marker=f"m{m['seq']}")
        self.ev("session.said", "maps", text="First answer: the brief is read.")
        self.ev("turn.result", "maps", text="First answer: the brief is read.")
        select = ("(() => { const p = [...document.querySelectorAll('main .feed .md p')].find((x) => x.textContent.includes('First answer'));"
                  " const r = document.createRange(); r.selectNodeContents(p); getSelection().removeAllRanges(); getSelection().addRange(r);"
                  " return getSelection().toString().length; })()")
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/s/maps", "init": SPY},
                        {"until": "main.dataset.for === 'maps' && !!document.querySelector('main .feed .clamp-more')", "ms": 15000},
                        {"sleep": 800}, {"eval": select},
                        {"eval": "document.querySelector('main .feed .clamp-more').scrollIntoView({ block: 'center' }), 1"},
                        {"mclick": "main .feed .clamp-more"}, {"sleep": 300}, {"eval": "getSelection().isCollapsed"})
        self.assertTrue(got[1]["until"], got)
        self.assertGreater(got[3]["eval"], 0, got)
        self.ev("turn.started", "maps", marker=None, prompt_head="more")
        self.ev("session.said", "maps", text="Second answer arrived after the click.")
        got = self.page({"until": "document.querySelector('main .feed').innerText.includes('Second answer arrived')", "ms": 6000})
        self.assertTrue(got[-1]["until"], got)   # the snapshot after the click is drawn, not held

    def test_a_new_icon_of_a_session_reaches_its_avatar_on_a_kept_reply_row(self):
        # Clean draws the replier's icon from the snapshot on the "w replied to your message" row; the row's markup does not
        # carry the icon, and the kept row kept the old face
        self.ev("session.requested", "w", session_id="s-w", cwd="/w", channel="cli", owner=True)
        self.ev("session.started", "w", session_id="s-w")
        m = self.j.append("message.received", None, {"to": "hub", "text": "fix the site map", "channel": "web", "owner": True})
        d = self.j.append("delegation.created", None, {"session": "w", "words": "fix the site map", "context": "T-1",
                                                      "for": f"m{m['seq']}", "channel": "session", "owner": False, "sender": "hub"})
        self.ev("turn.started", "w", marker=f"m{d['seq']}")
        self.ev("turn.result", "w", text="done: the map is fixed", is_error=False)
        self.ev("turn.stopped", "w")
        face = "(document.querySelector('main .feed .row .clean-av')?.style.getPropertyValue('--clean-av') || '')"
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/s/hub", "init": SPY, "look": None},
                        {"until": f"{face}.includes('/static/clean/a/')", "ms": 15000}, {"eval": face})
        self.assertTrue(got[1]["until"], got)
        self.assertNotIn("clay-teal-donut", got[-1]["eval"])   # no icon: a character by the name
        self.ev("session.iconed", "w", icon="🧭", channel="web", owner=True)   # the icon of a character: that character
        got = self.page({"until": f"{face}.includes('clay-teal-donut')", "ms": 6000}, {"eval": face})
        self.assertTrue(got[0]["until"], got)

    def test_an_emoji_of_ones_own_stays_an_emoji_and_the_character_follows_the_icon_in_clean(self):
        # a session whose icon is no character's (an emoji of the owner's own) keeps it in the list, on the reply row and in the
        # byline; a character takes its place only when the icon is a character's, and goes when the icon is not
        self.ev("session.requested", "w", session_id="s-w", cwd="/w", channel="cli", owner=True)
        self.ev("session.started", "w", session_id="s-w")
        self.ev("session.iconed", "w", icon="🦊", channel="web", owner=True)
        m = self.j.append("message.received", None, {"to": "hub", "text": "fix the site map", "channel": "web", "owner": True})
        d = self.j.append("delegation.created", None, {"session": "w", "words": "fix the site map", "context": "T-1",
                                                      "for": f"m{m['seq']}", "channel": "session", "owner": False, "sender": "hub"})
        self.ev("turn.started", "w", marker=f"m{d['seq']}")
        self.ev("session.said", "w", text="The map is fixed.")
        self.ev("turn.result", "w", text="done: the map is fixed", is_error=False)
        self.ev("turn.stopped", "w")
        # [the reply row's text, its character, the list row's emoji node text, the row's character]
        look = ("(() => { const r = document.querySelector('main .feed .row .clean-av'), l = document.querySelector('#list li[data-s=\"w\"]');"
                " return [r?.textContent, r?.dataset.cleanAv || '', l?.querySelector('.nm > .avatar')?.textContent,"
                " (l?.matches('.srow') ? l : l?.querySelector('.srow'))?.style.getPropertyValue('--clean-face') || '']; })()")
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/s/hub", "init": SPY, "look": None},
                        {"until": "!!document.querySelector('main .feed .row .clean-av') && !!document.querySelector('#list li[data-s=\"w\"] .nm > .avatar')", "ms": 15000},
                        {"eval": look})
        self.assertTrue(got[1]["until"], got)
        self.assertEqual(got[-1]["eval"], ["🦊", "", "🦊", ""], got[-1])   # the emoji, no character anywhere
        self.ev("session.iconed", "w", icon="🎨", channel="web", owner=True)   # a character's icon
        got = self.page({"until": f"({look})[1].includes('fuzzy-lime-star') && ({look})[3].includes('fuzzy-lime-star')", "ms": 6000}, {"eval": look})
        self.assertTrue(got[0]["until"], got)
        self.ev("session.iconed", "w", icon="🦊", channel="web", owner=True)   # back: the kept nodes lose the character
        got = self.page({"until": f"({look})[0] === '🦊' && ({look})[1] === '' && ({look})[3] === ''", "ms": 6000}, {"eval": look})
        self.assertTrue(got[0]["until"], got)
        self.assertEqual(got[-1]["eval"], ["🦊", "", "🦊", ""], got[-1])

    def test_the_byline_of_a_session_follows_its_icon_in_clean(self):
        # no icon: a character by the name; an emoji of one's own: the emoji; a character's icon: that character (the core's own
        # picture); the icon taken off: a character by the name again
        self.ev("session.requested", "w", session_id="s-w", cwd="/w", channel="cli", owner=True)
        self.ev("session.started", "w", session_id="s-w")
        m = self.j.append("message.received", None, {"to": "w", "text": "fix the site map", "channel": "web", "owner": True})
        self.ev("turn.started", "w", marker=f"m{m['seq']}")
        self.ev("session.said", "w", text="The map is fixed.")
        self.ev("turn.result", "w", text="done: the map is fixed", is_error=False)
        self.ev("turn.stopped", "w")
        by = ("(() => { const a = [...document.querySelectorAll('main .feed .byline')].find((x) => x.querySelector(':scope > b')?.textContent.trim() === 'w')"
              "?.querySelector('.av'); return a ? [a.classList.contains('emoji') ? a.textContent : '', a.dataset.cleanAv || '', a.style.backgroundImage] : null; })()")
        name = "/static/clean/a/"
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/s/w", "init": SPY, "look": None},
                        {"until": f"!!({by}) && ({by})[1].startsWith('{name}')", "ms": 15000}, {"eval": by})
        self.assertTrue(got[1]["until"], got)
        by_name = got[-1]["eval"][1]
        self.ev("session.iconed", "w", icon="🦊", channel="web", owner=True)
        got = self.page({"until": f"({by})?.[0] === '🦊'", "ms": 6000}, {"eval": by})
        self.assertTrue(got[0]["until"], got)
        self.assertEqual(got[-1]["eval"][1], "", got[-1])   # an emoji of one's own is not covered by a character
        self.ev("session.iconed", "w", icon="🎨", channel="web", owner=True)
        got = self.page({"until": f"({by})?.[2].includes('fuzzy-lime-star')", "ms": 6000}, {"eval": by})
        self.assertTrue(got[0]["until"], got)
        self.assertEqual(got[-1]["eval"][1], "", got[-1])
        self.ev("session.iconed", "w", icon="", channel="web", owner=True)
        got = self.page({"until": f"({by})?.[1] === '{by_name}'", "ms": 6000}, {"eval": by})
        self.assertTrue(got[0]["until"], got)

    def test_group_headings_stay_the_same_nodes_through_snapshots_in_clean(self):
        # a heading is kept while its markup is the same: the markup it was put in with, not the live one Clean has marked —
        # a heading put in anew under the pointer drops the click (reconcile.js); the first snapshot after the load counts
        for n in ("notes-bot", "web-design"):
            self.ev("session.requested", n, session_id="s-" + n, cwd="/w", channel="cli", owner=True, role="agent")
            self.ev("session.started", n, session_id="s-" + n)
        gone = ("(() => { window.__heads = 0; new MutationObserver((l) => { for (const m of l) for (const n of m.removedNodes)"
                " if (n.matches?.('.sgroup-h, .sgroup-hw, .fork-toggle') || n.querySelector?.('.sgroup-h, .sgroup-hw, .fork-toggle')) window.__heads++; })"
                ".observe(document.getElementById('list'), { subtree: true, childList: true }); return document.querySelectorAll('#list .sgroup-h').length; })()")
        got = self.page(self.go(True), {"until": "!!document.querySelector('#list [data-g=\"agents\"] .sgroup-h')", "ms": 15000}, {"eval": gone})
        self.assertTrue(got[1]["until"], got)
        th = self.feed_events(4, 0.6)
        got = self.page({"sleep": 3800}, {"eval": "window.__heads"})
        th.join()
        self.assertEqual(got[-1]["eval"], 0, got)

    def test_another_look_gives_the_field_the_cores_placeholder_again(self):
        # the core writes its placeholder when its own text or the look changes: a theme that put its own text there and does
        # not take it back (Clean does, a plugin theme may not) loses it with the look
        ph = "document.querySelector('main form.composer textarea').placeholder"
        got = self.page(self.go(False), {"until": f"main.dataset.for === 'maps-x' && !!{ph}", "ms": 15000}, {"eval": ph},
                        {"eval": "document.querySelector('main form.composer textarea').placeholder = 'Theme text', 1"},
                        {"eval": "window.dispatchEvent(new CustomEvent('hub-theme')), 1"}, {"sleep": 800}, {"eval": ph})
        self.assertTrue(got[1]["until"], got)
        self.assertEqual(got[-1]["eval"], got[2]["eval"], got)

def load_tests(loader, standard, pattern):
    return unittest.TestSuite(QuietPage(name) for name in QuietPage.__dict__ if name.startswith("test_"))
