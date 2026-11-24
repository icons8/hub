"""A long paste the owner sent as a text file (the field's pasted-N.txt) shows in the feed as the apps' FeedTile: a 190 x 64 card
with its first words and its size, not a file line "pasted-1.txt · 3 KB". Another .txt stays a file line; an unreadable paste
keeps the "Pasted text" caption."""
import json
import os

import tests  # noqa: F401

from modules.web import attach
from tests.test_web_e2e import PageInChrome

with open(os.path.join(os.path.dirname(__file__), "..", "i18n", "en.json"), encoding="utf-8") as f:
    EN = json.load(f)

LOOK = """[...document.querySelectorAll('.feed .attach-list a')].map((a) => {
  const r = a.getBoundingClientRect(), t = a.querySelector('.pt-title'), s = a.querySelector('.pt-size');
  return { tile: a.classList.contains('paste-tile'), w: Math.round(r.width), h: Math.round(r.height),
           title: t ? t.textContent : '', size: s ? s.textContent : '', text: a.textContent, href: a.getAttribute('href'),
           label: a.getAttribute('aria-label') || '', top: Math.round(r.top), bottom: Math.round(r.bottom), inside: !t || (t.getBoundingClientRect().top >= r.top && s.getBoundingClientRect().bottom <= r.bottom + 0.5) };
})"""
# the card has read its file (or was told it cannot): its words are what the reader will show
SETTLED = "!!document.querySelector('.feed .paste-tile') && !document.querySelector('.feed .paste-tile[aria-busy]')"


class PasteTile(PageInChrome):
    def stand(self, files):
        self.ev("session.requested", "a", session_id="s-a", cwd="/w", channel="cli", owner=True)
        self.ev("session.started", "a", session_id="s-a")
        box = attach.root(self.state)
        os.makedirs(box, exist_ok=True)
        sent = []
        for name, body in files:
            path = os.path.join(box, name)
            with open(path, "wb") as f:
                f.write(body)
            sent.append({"path": path, "name": name, "mime": "text/plain", "size": len(body)})
        e = self.j.append("message.received", None, {"to": "a", "text": "look at this", "channel": "cli", "owner": True,
                                                     "attachments": sent})
        self.ev("turn.started", "a", marker=f"m{e['seq']}", prompt_head="look at this")
        self.ev("session.said", "a", text="read it")
        self.ev("turn.result", "a", text="read it")
        self.ev("turn.stopped", "a")

    def look(self, until):
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/s/a", "look": None}, {"wait": "read it"},
                        {"until": until, "ms": 5000}, {"eval": LOOK})
        return got[-1]["eval"]

    def test_a_paste_is_a_card_with_its_first_words_and_size(self):
        words = "Release notes   for\n\nthe week:\tthe feed keeps its place " + "and more words " * 300
        self.stand([("pasted-1.txt", words.encode())])
        got = self.look("document.querySelector('.feed .paste-tile .pt-title')?.textContent.startsWith('Release')")
        self.assertEqual(len(got), 1, got)
        tile = got[0]
        self.assertTrue(tile["tile"], tile)
        self.assertEqual((tile["w"], tile["h"]), (190, 64), tile)
        head = " ".join(words[:400].split())[:80]
        self.assertEqual(tile["title"], head)                       # whitespace folded, at most 80 characters
        self.assertRegex(tile["size"], r"KB$")
        self.assertNotIn("pasted-1.txt", tile["text"])
        self.assertIn("/api/file?path=", tile["href"])              # a click still gets the file
        self.assertTrue(tile["inside"], tile)                       # two lines of words and the size fit the card
        self.assertEqual(tile["label"], f'{EN["web.feed.pasted_text"]}: {head}, {tile["size"]}')   # read out as a paste

    def test_an_unreadable_paste_keeps_the_caption_and_another_txt_stays_a_file_line(self):
        self.stand([("pasted-2.txt", b"\x00\x01binary"), ("notes.txt", b"plain notes"), ("paste-config.txt", b"a = 1")])
        got = self.look(f"{SETTLED} && document.querySelectorAll('.feed .attach-list a').length === 3")
        paste, other, config = got
        self.assertTrue(paste["tile"], paste)
        self.assertEqual(paste["title"], EN["web.feed.pasted_text"])
        self.assertFalse(other["tile"], other)
        self.assertIn("notes.txt", other["text"])
        self.assertFalse(config["tile"], config)                    # only the field's own pasted-<N>.txt is a paste
        self.assertIn("paste-config.txt", config["text"])

    def test_a_gone_paste_is_asked_once_however_often_the_feed_redraws(self):
        self.stand([("pasted-5.txt", b"words of a deleted paste")])
        os.remove(os.path.join(attach.root(self.state), "pasted-5.txt"))
        self.look(SETTLED)
        # from here on the page counts its requests for the file (feed.js looks fetch up on every call)
        self.page({"eval": "window.__asked = 0, window.__fetch = window.fetch, window.fetch = (u, o) =>"
                           " (String(u).includes('pasted-5.txt') && window.__asked++, window.__fetch(u, o)), 1"})
        for i in range(8):                                          # each live event rebuilds the feed and its attachments
            self.ev("session.said", "a", text=f"line {i}")
            self.page({"until": f"document.querySelector('.feed').textContent.includes('line {i}')", "ms": 5000})
        got = self.page({"sleep": 300}, {"eval": "[window.__asked, document.querySelector('.feed .pt-title').textContent]"})[-1]["eval"]
        self.assertEqual(got, [0, EN["web.feed.pasted_text"]])     # a 404 is an answer: the caption stays, no request per redraw

    def test_on_a_right_to_left_page_the_words_and_size_keep_their_order(self):
        cfg = os.path.join(self.state, "hub-lang.json")
        with open(cfg, "w") as f:
            json.dump({"update": {"enabled": False}, "language": "ar"}, f)
        from unittest import mock
        env = mock.patch.dict(os.environ, {"HUB_CONFIG": cfg})
        env.start()
        self.addCleanup(env.stop)
        self.stand([("pasted-6.txt", b"Hello world:")])
        order = """(() => { const at = (n, i) => { const r = document.createRange(); r.setStart(n, i); r.setEnd(n, i + 1);
          return r.getBoundingClientRect().left; }, t = document.querySelector('.feed .pt-title').firstChild,
          s = document.querySelector('.feed .pt-size');
          return { dir: document.documentElement.dir, colon_after_words: at(t, t.length - 1) > at(t, 0),
                   size: getComputedStyle(s).unicodeBidi }; })()"""
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/s/a", "look": None}, {"wait": "read it"},
                        {"until": f"{SETTLED} && document.querySelector('.feed .pt-title')?.textContent === 'Hello world:'", "ms": 5000},
                        {"eval": order})[-1]["eval"]
        # not ":Hello world"; the size too takes its direction from its own text (an untranslated "1 KB" does not read "KB 1")
        self.assertEqual(got, {"dir": "rtl", "colon_after_words": True, "size": "plaintext"})

    def test_a_file_line_goes_under_the_cards_not_beside_them(self):
        # a wide own bubble has room for the cards and the file line in one row: the line stuck to the cards' top
        self.stand([("pasted-3.txt", b"first paste words"), ("notes.txt", b"plain notes"), ("pasted-4.txt", b"second paste words")])
        got = self.page({"local": {"hub.theme": '{"id":"core:clean"}'}}, {"go": f"http://127.0.0.1:{self.port}/#/s/a", "look": None},
                        {"wait": "read it"}, {"until": "document.querySelectorAll('.feed .attach-list a').length === 3", "ms": 5000},
                        {"shot": os.path.join(self.state, "tiles-1440.png"), "width": 1440, "height": 900}, {"sleep": 300}, {"eval": LOOK})[-1]["eval"]
        tiles, lines = [a for a in got if a["tile"]], [a for a in got if not a["tile"]]
        self.assertEqual((len(tiles), len(lines)), (2, 1), got)
        self.assertEqual(tiles[0]["top"], tiles[1]["top"], got)                      # the cards side by side
        self.assertEqual(lines[0]["top"] - max(t["bottom"] for t in tiles), 8, got)   # on a line of its own under them, at the list's gap


def load_tests(loader, standard, pattern):   # only its own tests, not the ~420 inherited from the page
    import unittest
    return unittest.TestSuite(PasteTile(n) for n in PasteTile.__dict__ if n.startswith("test_"))
