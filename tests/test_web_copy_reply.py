""""Copy" stands beside "Reply" on a message, and a file gets "Reply" too: a session's file (its caption or its name is the
quote) and the owner's own message with only attachments (the names of its files)."""
import tests  # noqa: F401
import json
import os
import unittest

from tests.test_files_e2e import png
from tests.test_web_e2e import PageInChrome

EN = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "i18n", "en.json"), encoding="utf-8"))
SAID = "The report is ready: three charts and a table."
ASKED = "Check the totals in the March table."
CLEAN = {"local": {"hub.theme": '{"id":"core:clean"}'}}

# the buttons of the row with these words, against each other, and the size of the icon each one draws
GEOMETRY = r"""((words) => {
  const row = [...document.querySelectorAll('.feed .row')].find((r) => r.textContent.includes(words));
  const box = (n) => n && n.getBoundingClientRect(), b = (k) => row.querySelector(':scope > .b > .' + k);
  const copy = box(b('copy-btn')), reply = box(b('reply-btn'));
  // an inline ::before ignores its 16 x 16 and draws nothing: in the button it is a flex item (blockified)
  const icon = (n) => { const c = getComputedStyle(n, '::before'); return [c.display, c.width, c.height].join(' '); };
  return copy && reply && {gap: Math.round(reply.left - copy.right), top: Math.round(copy.top - reply.top),
    copy: [Math.round(copy.width), Math.round(copy.height)], icons: [icon(b('copy-btn')), icon(b('reply-btn'))]};
})(%s)"""

NEXT = "Next: the totals are fixed, and the March table is the last in the listing in the report."
# the reaction icon of Clean as the reactions module's PR places it: a 24 button 4 before "Reply"
ICON = ("(() => { const s = document.createElement('style'); s.textContent = 'html.th-clean .row > .b > .reply-btn ~ .react-add "
        "{ position: absolute; top: -2px; inset-inline-end: 28px; width: 24px; height: 24px; padding: 0; font-size: 0; }'; "
        "document.head.append(s); return 1; })()")
# what the buttons of the row with these words cover: each other and the lines of its text
CLASH = r"""((words) => {
  const row = [...document.querySelectorAll('.feed .row.out')].find((r) => r.querySelector('.md')?.textContent.includes(words));
  const btns = [...row.querySelectorAll(':scope > .b > :is(.copy-btn, .reply-btn, .react-add)')].map((n) => [n.className, n.getBoundingClientRect()]);
  const range = document.createRange(); range.selectNodeContents(row.querySelector('.md'));
  const lines = [...range.getClientRects()].filter((r) => r.width > 1);
  const hit = (a, b) => a.left < b.right - 0.5 && b.left < a.right - 0.5 && a.top < b.bottom - 0.5 && b.top < a.bottom - 0.5;
  const clash = [];
  btns.forEach(([n, r], i) => {
    btns.slice(i + 1).forEach(([m, q]) => hit(r, q) && clash.push(n + ' / ' + m));
    if (lines.some((l) => hit(r, l))) clash.push(n + ' / text');
  });
  return {byline: !!row.querySelector(':scope > .b > .byline'), buttons: btns.length, clash};
})(%s)"""

# the page has settled: fonts in, no finite animation or transition still running (the working dots loop for ever)
SETTLED = ("document.fonts.status === 'loaded' && !document.getAnimations().some((a) => a.playState === 'running' "
           "&& a.effect?.getComputedTiming().iterations !== Infinity)")
STUB = "navigator.clipboard.writeText = async (t) => { window.__copied = t; }; 1"
AFTER = r"""(() => {
  const b = [...document.querySelectorAll('.feed .row.out')].find((r) => r.textContent.includes('three charts')).querySelector('.copy-btn');
  return {copied: window.__copied, title: b.title, done: b.classList.contains('done')};
})()"""

# what of the line of a reply to you is on screen right before its buttons
DOT = r"""(() => {
  const b = document.querySelector('.feed .reply-box > .meta > .copy-btn'), n = b.previousSibling;
  return n.nodeType === 3 ? n.nodeValue.trim() : getComputedStyle(n).display === "none" ? "" : n.textContent.trim();
})()"""

# who "Reply" goes to and with what quote, after a press on the "Reply" of the row matched by a selector
PREP = "import('/static/js/state.js').then((m) => { window.__S = m.S; }), 1"
TARGET = "(({session, quote}) => ({session, quote}))(window.__S.replyTarget || {})"


class CopyReply(PageInChrome):
    def turn(self):
        self.ev("session.requested", "a", session_id="s-a", cwd="/w", channel="cli", owner=True)
        self.ev("session.started", "a", session_id="s-a")
        e = self.j.append("message.received", None, {"to": "a", "text": ASKED, "channel": "cli", "owner": True})
        self.ev("turn.started", "a", marker=f"m{e['seq']}", prompt_head="Check")
        self.ev("turn.stopped", "a")
        e = self.j.append("message.received", None, {
            "to": "a", "text": "", "channel": "cli", "owner": True,
            "attachments": [{"path": "/w/in/plan.pdf", "name": "plan.pdf", "mime": "application/pdf", "size": 1},
                            {"path": "/w/in/notes.txt", "name": "notes.txt", "mime": "text/plain", "size": 1}]})
        self.ev("turn.started", "a", marker=f"m{e['seq']}", prompt_head="")
        self.ev("session.said", "a", text=SAID)
        self.ev("turn.result", "a", text=SAID)
        self.ev("turn.stopped", "a")
        self.ev("owner.file", None, path="/w/report.md", caption=None, channel="session", owner=False, sender="a")

    def test_copy_stands_beside_reply_and_copies_the_whole_message(self):
        self.turn()
        for width in (390, 1440):
            got = self.page(CLEAN, {"go": f"http://127.0.0.1:{self.port}/#/s/a", "look": None}, {"wait": "three charts"},
                            {"shot": os.path.join(self.state, f"copy-{width}.png"), "width": width, "height": 900},
                            {"until": SETTLED}, {"eval": GEOMETRY % json.dumps("three charts")}, {"eval": GEOMETRY % json.dumps(ASKED)})
            for g, own in ((got[-2]["eval"], False), (got[-1]["eval"], True)):   # the session's message and the owner's own one
                self.assertTrue(g, (width, got))
                self.assertEqual(g["copy"], [24, 24], (width, g))                 # the same quiet 24 button as "Reply"
                self.assertEqual(g["icons"], ["block 16px 16px", "block 16px 16px"], (width, g))   # each draws its 16 icon
                if own and width < 761:   # beside a bubble nearly as wide as the phone: under "Reply", 4 apart
                    self.assertEqual((g["top"], g["gap"]), (28, -24), (width, g))
                    continue
                self.assertEqual(g["top"], 0, (width, g))                         # in its line
                self.assertEqual(g["gap"], 4, (width, g))                         # just before it
        got = self.page({"eval": STUB}, {"hover": ".feed .row.out > .b > .copy-btn"},
                        {"mclick": ".feed .row.out > .b > .copy-btn"}, {"until": f"{AFTER}.done"}, {"eval": AFTER},
                        {"until": f"!{AFTER}.done", "ms": 5000}, {"eval": AFTER})   # "Copied" goes back by itself
        self.assertEqual(got[-3]["eval"], {"copied": SAID, "title": EN["web.markdown.copied"], "done": True}, got)
        self.assertEqual(got[-1]["eval"], {"copied": SAID, "title": EN["web.quote.copy"], "done": False}, got)

    def test_a_file_and_an_own_message_with_only_files_get_reply(self):
        self.turn()
        for sel, quote in ((".feed .row.sent-file .reply-btn", "report.md"), (".feed .row.mine:not(:has(.own-text)) .reply-btn", "plan.pdf, notes.txt")):
            got = self.page(CLEAN, {"go": f"http://127.0.0.1:{self.port}/#/s/a", "look": None}, {"wait": "three charts"},
                            {"eval": PREP}, {"until": f"window.__S && document.querySelector('{sel}')"}, {"eval": f"document.querySelector('{sel}')?.title"},
                            {"hover": sel}, {"mclick": sel}, {"until": "!!window.__S.replyTarget"}, {"eval": TARGET})
            self.assertEqual(got[-5]["eval"], EN["web.feed.reply"], (sel, got))
            self.assertEqual(got[-1]["eval"], {"session": "a", "quote": quote}, (sel, got))
        # words only for "Copy": an own message with nothing but files has none
        got = self.page({"eval": "document.querySelectorAll('.feed .row.mine:not(:has(.own-text)) .copy-btn, .feed .row.sent-file .copy-btn').length"})
        self.assertEqual(got[-1]["eval"], 0, got)

    def test_the_buttons_of_a_reply_without_its_name_line_keep_off_each_other_and_the_text(self):
        # a session's second reply in a row has no name line; with reactions on there are three buttons. Clean's reaction icon
        # (the reactions module's PR) stands 4 before "Reply": the injected rule is that icon's place, so "Copy" must stand
        # further out. On a phone the end of the first line has no room: the buttons go to the time line under the text
        self.web.config = (lambda cfg: lambda: {**cfg(), "reactions": {"enabled": True}})(self.web.config)
        self.ev("session.requested", "a", session_id="s-a", cwd="/w", channel="cli", owner=True)
        self.ev("session.started", "a", session_id="s-a")
        e = self.j.append("message.received", None, {"to": "a", "text": ASKED, "channel": "cli", "owner": True})
        self.ev("turn.started", "a", marker=f"m{e['seq']}", prompt_head="Check")
        self.ev("session.said", "a", text=SAID)
        self.ev("session.said", "a", text=NEXT)
        self.ev("turn.result", "a", text=NEXT)
        self.ev("turn.stopped", "a")
        # the "Full" view shows both remarks of the turn one after the other: the second has no name line
        full = {"local": {**CLEAN["local"], "hub.feed": "full"}}
        for width in (390, 1440):
            got = self.page(full, {"go": f"http://127.0.0.1:{self.port}/#/s/a", "look": None}, {"wait": "listing in the"},
                            {"eval": ICON}, {"until": SETTLED}, {"shot": os.path.join(self.state, f"clash-{width}.png"), "width": width, "height": 900},
                            {"eval": CLASH % json.dumps("listing in the")})
            g = got[-1]["eval"]
            self.assertTrue(g and g["byline"] is False and g["buttons"] == 3, (width, got))
            self.assertEqual(g["clash"], [], (width, g))


    def test_a_quote_of_a_file_leads_to_the_file(self):
        # "Reply" on a session's file quotes its name, on the owner's message with only files — their names: a press on such a
        # quote lands on that row, not on an earlier line that mentions the file, and not on "not found"
        self.ev("session.requested", "a", session_id="s-a", cwd="/w", channel="cli", owner=True)
        self.ev("session.started", "a", session_id="s-a")
        e = self.j.append("message.received", None, {"to": "a", "text": ASKED, "channel": "cli", "owner": True})
        self.ev("turn.started", "a", marker=f"m{e['seq']}", prompt_head="Check")
        self.ev("session.said", "a", text="The totals go into report.md, I will send it.")
        self.ev("turn.result", "a", text="The totals go into report.md, I will send it.")
        self.ev("turn.stopped", "a")
        e = self.j.append("message.received", None, {
            "to": "a", "text": "", "channel": "cli", "owner": True,
            "attachments": [{"path": "/w/in/plan.pdf", "name": "plan.pdf", "mime": "application/pdf", "size": 1},
                            {"path": "/w/in/notes.txt", "name": "notes.txt", "mime": "text/plain", "size": 1}]})
        self.ev("turn.started", "a", marker=f"m{e['seq']}", prompt_head="")
        self.ev("owner.file", None, path="/w/report.md", caption=None, channel="session", owner=False, sender="a")
        self.ev("turn.result", "a", text="Got the files.")
        self.ev("turn.stopped", "a")
        for words, quote in (("Is it final?", "report.md"), ("Read them?", "plan.pdf, notes.txt")):
            e = self.j.append("message.received", None, {"to": "a", "text": words, "channel": "web", "owner": True, "quote": quote})
            self.ev("turn.started", "a", marker=f"m{e['seq']}")
            self.ev("turn.result", "a", text="Yes.")
            self.ev("turn.stopped", "a")
        press = "(() => { const q = [...document.querySelectorAll('.feed .in-reply')]; q[%d].click(); return q.length; })()"
        landed = "(() => { const r = document.querySelector('.feed .row.flash'); return r && [r.className, r.innerText]; })()"
        for i, cls, words in ((0, "sent-file", "report.md"), (1, "mine", "plan.pdf")):
            got = self.page({"go": "about:blank"}, {"go": f"http://127.0.0.1:{self.port}/#/s/a", "feed": "compact"}, {"wait": "Read them?"},
                            {"eval": press % i}, {"until": "!!document.querySelector('.feed .row.flash')", "ms": 5000}, {"eval": landed})
            self.assertTrue(got[-1]["eval"], (words, got))
            cls_got, text = got[-1]["eval"]
            self.assertIn(cls, cls_got.split(), (words, got))
            self.assertIn(words, text, (words, got))
            self.assertNotIn("I will send it", text, (words, got))   # not the earlier line that mentions the file

    # the place of each button of a row, by its class, and of the reactions under it: the row is matched by its words
    PLACES = r"""((words) => {
      const row = [...document.querySelectorAll('.feed .row')].find((r) => r.querySelector(':scope > .b')?.innerText.includes(words));
      const r = (n) => { if (!n) return null; const b = n.getBoundingClientRect(); return {l: Math.round(b.left), r: Math.round(b.right), t: Math.round(b.top), b: Math.round(b.bottom)}; };
      const at = (sel) => r(row.querySelector(sel));
      return {copy: at('.copy-btn'), reply: at('.reply-btn'), react: at(':scope > .b > .react-add'), reactions: at(':scope > .b > .reactions'),
              pad: getComputedStyle(row.querySelector(':scope > .b')).paddingInlineEnd, vw: innerWidth};
    })(%s)"""

    def test_classic_keeps_one_order_one_gap_and_the_reactions_on_their_own_line(self):
        # Classic: "Copy" stands before "Reply" everywhere (the "replied to your message" line too); on the name line the grid's
        # gap alone parts the buttons (Copy · Reply · Reaction evenly); the reactions of the owner's own message keep their own
        # line under "Copy" and "Reply"
        self.reactions_module(True)
        self.reacted_turn()
        self.ev("session.requested", "hub", session_id="s-hub", cwd="/w", kind="hub")
        self.ev("session.started", "hub", session_id="s-hub")
        m = self.j.append("message.received", None, {"to": "hub", "text": "fix the site map", "channel": "web", "owner": True})
        d = self.j.append("delegation.created", None, {"session": "a", "words": "fix the site map", "context": "T-1", "for": f"m{m['seq']}",
                                                      "channel": "session", "owner": False, "sender": "hub"})
        self.ev("turn.started", "a", marker=f"m{d['seq']}")
        self.ev("turn.result", "a", text="the site map is fixed", is_error=False)
        self.ev("turn.stopped", "a")
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/s/a"}, {"wait": "Report ready"}, {"until": SETTLED},
                        {"shot": os.path.join(self.state, "classic-1440.png"), "width": 1440, "height": 900},
                        {"eval": self.PLACES % json.dumps("Report ready")}, {"eval": self.PLACES % json.dumps("make a report")})
        out, own = got[-2]["eval"], got[-1]["eval"]
        self.assertTrue(out["copy"], out)                                       # "Copy" is there, beside "Reply"
        self.assertEqual((out["reply"]["l"] - out["copy"]["r"], out["react"]["l"] - out["reply"]["r"]), (8, 8), out)
        self.assertTrue(own["reactions"] and own["reactions"]["t"] >= own["reply"]["b"], own)   # its own line, under the buttons
        got = self.page({"go": "about:blank"}, {"go": f"http://127.0.0.1:{self.port}/#/s/hub"}, {"wait": "site map is fixed"}, {"until": SETTLED},
                        {"eval": self.PLACES % json.dumps("site map is fixed")})
        box = got[-1]["eval"]
        self.assertTrue(box["copy"] and box["reply"], box)
        self.assertLess(box["copy"]["r"], box["reply"]["l"], box)   # "Copy" before "Reply to …"

    def test_clean_a_copy_in_a_reply_to_you_shows_the_tick(self):
        self.ev("session.requested", "hub", session_id="s-hub", cwd="/w", kind="hub")
        self.ev("session.started", "hub", session_id="s-hub")
        self.ev("session.requested", "w", session_id="s-w", cwd="/w")
        self.ev("session.started", "w", session_id="s-w")
        m = self.j.append("message.received", None, {"to": "hub", "text": "fix the site map", "channel": "web", "owner": True})
        d = self.j.append("delegation.created", None, {"session": "w", "words": "fix the site map", "context": "T-1", "for": f"m{m['seq']}",
                                                      "channel": "session", "owner": False, "sender": "hub"})
        self.ev("turn.started", "w", marker=f"m{d['seq']}")
        self.ev("turn.result", "w", text="the site map is fixed", is_error=False)
        self.ev("turn.stopped", "w")
        mask = "(() => getComputedStyle(document.querySelector('.feed .reply-box .copy-btn'), '::before').maskImage)()"
        got = self.page(CLEAN, {"go": f"http://127.0.0.1:{self.port}/#/s/hub", "look": None}, {"wait": "site map is fixed"},
                        {"eval": STUB}, {"eval": mask}, {"hover": ".feed .reply-box .copy-btn"}, {"mclick": ".feed .reply-box .copy-btn"},
                        {"until": f"{mask}.includes('allow.svg')"}, {"eval": mask}, {"eval": "window.__copied"},
                        {"eval": DOT})
        self.assertIn("copy.svg", got[-7]["eval"], got)
        self.assertEqual(got[-1]["eval"], "", got)   # the dot before the buttons is hidden, as it was before "Copy"
        self.assertIn("allow.svg", got[-3]["eval"], got)   # the tick, as on any other "Copy"
        self.assertEqual(got[-2]["eval"], "the site map is fixed", got)

    def test_clean_phone_keeps_the_buttons_inside_and_the_text_wide(self):
        # a phone: a session's reply with reactions on keeps its full width (the buttons sit on the name line); the "Reply" of a
        # picture the session sent does not cover the picture; the owner's full-width bubble keeps "Copy" on the screen
        self.reactions_module(True)
        self.reacted_turn()
        pic = os.path.join(self.state, "pic.png")
        png(pic, 240, 160)
        self.ev("owner.file", None, path=pic, caption=None, channel="session", owner=False, sender="a")
        long = "Please check every total in the March and April tables, then the charts, and write down what differs. " * 3
        e = self.j.append("message.received", None, {"to": "a", "text": long, "channel": "web", "owner": True})
        self.ev("turn.started", "a", marker=f"m{e['seq']}")
        self.ev("turn.result", "a", text="Checked.")
        self.ev("turn.stopped", "a")
        img = ("(() => { const r = document.querySelector('.feed .row.sent-file'), i = r.querySelector('img'), b = r.querySelector('.reply-btn'); "
               "if (!i || !b) return null; const p = i.getBoundingClientRect(); let q = b.getBoundingClientRect(); "
               "const hit = (a) => q.left < a.right && a.left < q.right && q.top < a.bottom && a.top < q.bottom; "
               # a mini-hub's executor with a long name: its "<name> sent you" line wraps before "Reply", not under it
               "const w = document.createTreeWalker(r.querySelector('.meta'), NodeFilter.SHOW_TEXT); let n; "
               "while ((n = w.nextNode()) && !n.nodeValue.includes('Sent you')); if (n) n.nodeValue = 'an-executor-of-a-mini-hub-with-a-long-name sent you'; "
               "const g = document.createRange(); g.selectNodeContents(r.querySelector('.meta')); q = b.getBoundingClientRect(); "
               "return {img: [p.width > 0, p.height > 0], over: hit(p), meta: !!n && [...g.getClientRects()].some(hit)}; })()")
        for width in (390, 360, 320):
            got = self.page(CLEAN, {"go": "about:blank"}, {"touch": True}, {"go": f"http://127.0.0.1:{self.port}/#/s/a", "look": None},
                            {"wait": "March and April"}, {"shot": os.path.join(self.state, f"phone-{width}.png"), "width": width, "height": 900}, {"until": SETTLED},
                            {"eval": self.PLACES % json.dumps("Report ready")}, {"eval": img}, {"eval": self.PLACES % json.dumps("March and April")})
            out, pic_at, own = got[-3]["eval"], got[-2]["eval"], got[-1]["eval"]
            self.assertEqual(out["pad"], "0px", (width, out))
            self.assertTrue(pic_at and all(pic_at["img"]), (width, got[-2]))
            self.assertFalse(pic_at["over"], (width, pic_at))
            self.assertFalse(pic_at["meta"], (width, pic_at))
            self.assertGreaterEqual(min(own["copy"]["l"], own["reply"]["l"]), 0, (width, own))
            self.assertLessEqual(max(own["copy"]["r"], own["reply"]["r"]), own["vw"], (width, own))

def load_tests(loader, standard, pattern):
    return unittest.TestSuite(CopyReply(name) for name in CopyReply.__dict__ if name.startswith("test_"))
