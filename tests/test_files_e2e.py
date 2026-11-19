"""Files in a real browser (a report: opening a video showed nothing and there was no download button;
images were not shown either): headless Chrome, Files on a temporary folder, steps over CDP (tests/e2e/cdp.mjs).
A video plays and seeks, images show — in a markdown document too, a file has Download, closing works with Esc and the
cross, neighbouring files with the arrows, and the browser's Back returns the file."""
import tests  # noqa: F401 — ignore this machine's settings
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import zlib
from urllib.parse import quote

from core import i18n
from modules.files.server import Files
from modules.testchrome import chrome as testchrome

CHROME = testchrome.find()
HERE = os.path.dirname(os.path.abspath(__file__))
CDP = os.path.join(HERE, "e2e", "cdp.mjs")
CLIP = os.path.join(HERE, "e2e", "media", "clip.webm")  # 3 s, 64×36, VP9 — under a kilobyte
READY = bool(CHROME) and shutil.which("node")
CDP_WAIT = 60


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def png(path, w=40, h=30):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    rows = b"".join(b"\x00" + b"\x30\x80\xe0" * w for _ in range(h))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


@unittest.skipUnless(READY, f"needs node; {testchrome.INSTALL}")
class FilesInChrome(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="files-e2e-")
        self.root = os.path.join(self.dir, "demo")
        os.makedirs(os.path.join(self.root, "φάκελος βίντεο"))
        os.makedirs(os.path.join(self.root, "img"))
        os.makedirs(os.path.join(self.root, "docs"))
        shutil.copy(CLIP, os.path.join(self.root, "φάκελος βίντεο", "το βίντεό μου.webm"))
        for name in ("a.png", "b.png", "c.png"):
            png(os.path.join(self.root, "img", name))
        with open(os.path.join(self.root, "docs", "report.md"), "w") as f:
            f.write("# Report\n\n![shot](../img/a.png)\n\n![foreign](https://example.com/x.png)\n")
        self.files = Files(config=lambda: {"files": {"roots": {"demo": self.root}}}, state_dir=self.dir)
        self.server = self.files.serve(port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.stop_server)
        self.cdp = free_port()
        self.profile = tempfile.mkdtemp(prefix="files-e2e-chrome-")
        root = ["--no-sandbox"] if hasattr(os, "geteuid") and os.geteuid() == 0 else []
        self.chrome = subprocess.Popen([CHROME, "--headless=new", *testchrome.NO_AUDIO, *root, f"--remote-debugging-port={self.cdp}",
                                        f"--user-data-dir={self.profile}", "--window-size=1300,900",
                                        # the browser language is Russian: page texts are checked in Russian in the tests
                                        # (core/i18n.py human: a new hub with no setting takes the browser language)
                                        "--lang=ru", "--accept-lang=ru",
                                        "--disable-component-update", "--disable-background-networking",
                                        "--autoplay-policy=no-user-gesture-required",
                                        "--no-first-run", "--no-default-browser-check", "about:blank"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(self.stop_chrome)
        end = time.time() + CDP_WAIT
        while True:
            try:
                socket.create_connection(("127.0.0.1", self.cdp), 0.2).close()
                break
            except OSError:
                if self.chrome.poll() is not None:
                    self.skipTest(f"Chrome exited (code {self.chrome.returncode}) without opening CDP")
                if time.time() >= end:
                    self.skipTest(f"Chrome did not come up within {CDP_WAIT} s — is the machine overloaded?")
                time.sleep(0.1)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def stop_chrome(self):
        testchrome.stop(self.chrome, profile=self.profile)  # asked to close, else its own tree: only this Chrome
        from modules.testchrome import litter
        litter.own(self.profile)
        shutil.rmtree(self.profile, ignore_errors=True)

    def page(self, *steps):
        r = subprocess.run(["node", CDP, str(self.cdp), json.dumps(list(steps))], capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, f"the step failed: {r.stdout}\nscreen:\n{r.stderr[:1500]}")
        return json.loads(r.stdout)

    def url(self, rel):
        return f"http://127.0.0.1:{self.port}/#/demo/" + "/".join(quote(p) for p in rel.split("/"))

    @staticmethod
    def key(name):
        return {"eval": f"document.body.dispatchEvent(new KeyboardEvent('keydown', {{key: '{name}', bubbles: true}}))"}

    def test_the_reveal_button_is_named_after_the_hub_machines_file_manager_and_absent_without_a_desktop(self):
        """Owner 2026-11-10: "Finder" on a hub server was odd. The server says (data-reveal) whether its machine has a
        desktop and what it shows folders in: the button carries that name, an unknown manager is "Folder", no desktop
        - no button (the browser here is on the same machine)."""
        shots = os.environ.get("HUB_TEST_SHOTS")
        read = ("(b => b ? [b.textContent, b.title] : null)([...document.querySelectorAll('#acts button')]"
                ".find(b => !b.classList.contains('icon-only') && !/Edit|Save/.test(b.textContent)"
                " && b.title && /Finder|folder|Folder/i.test(b.title)))")
        for desktop, want in (("finder", "Finder"), ("files", "Files"), ("dolphin", "Dolphin"),
                              ("explorer", "Explorer"), ("folder", "Folder"), (None, None)):
            with self.subTest(desktop):
                self.files.desktop = desktop
                got = self.page({"go": "about:blank"},   # the same address would only change the hash: no new page
                                {"go": self.url("docs/report.md")},
                                {"until": "!!document.querySelector('#acts a.download')", "ms": 10000},
                                {"eval": read},
                                *([{"shot": os.path.join(shots, f"reveal-{desktop}.png"), "width": 1100, "height": 360}]
                                  if shots and desktop in ("dolphin", None) else []))
                self.assertTrue(got[2]["until"])
                self.assertEqual(got[3]["eval"] and got[3]["eval"][0], want)

    def test_an_error_banner_is_the_error_colour_on_the_same_plate_without_a_warning_stripe(self):
        """A theme carries only values for Files (no CSS): the error banner is told by the token, so that a theme's colours
        reach it, and not by a stripe of its own colour on the left."""
        show = ("(cls) => { const b = document.getElementById('banner'); b.textContent = 'x'; b.className = cls; const c = getComputedStyle(b); "
                "return [c.color, c.boxShadow]; }")
        probe = ("(() => { const d = document.createElement('i'); d.style.color = 'var(--destructive)'; document.body.append(d); "
                 "const c = getComputedStyle(d).color; d.remove(); return c; })()")
        got = self.page({"go": self.url("docs/report.md")}, {"until": "!!document.getElementById('banner')", "ms": 10000},
                        {"eval": f"({show})('fbanner on')"}, {"eval": f"({show})('fbanner on err')"}, {"eval": probe})
        (plain_color, plain_shadow), (err_color, err_shadow) = got[2]["eval"], got[3]["eval"]
        self.assertEqual(err_color, got[4]["eval"])
        self.assertNotEqual(err_color, plain_color)
        self.assertEqual(err_shadow, plain_shadow)   # the plate is the same: no stripe

    def test_a_spec_has_its_strip_with_accept_and_drop(self):
        """An executor's spec (core/specmode.py) opens under the "specs" root with a strip: who writes it, "Accept" and
        "Drop"; "Accept" takes the file as it is at the press (the browser is Russian here)."""
        from core import journal, spool
        from tests.test_spec_surfaces import spec_journal
        did = spec_journal(journal.Journal(os.path.join(self.dir, "journal.jsonl")), state=self.dir)
        self.files.spec_wait = 0
        shots = os.environ.get("HUB_TEST_SHOTS")
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/specs/{did}/spec.md"},
                        {"until": "!!document.querySelector('#view .specbar button')"},
                        {"eval": "document.querySelector('#view .specbar').textContent"},
                        *([{"shot": os.path.join(shots, "files-spec-strip.png"), "width": 1300, "height": 700}] if shots else []),
                        {"eval": "document.querySelector('#view .specbar button.primary').click(), 1"},
                        {"sleep": 800})
        self.assertTrue(got[1]["until"])
        self.assertIn(did, got[2]["eval"])
        self.assertIn("maps-1", got[2]["eval"])
        (ev,) = [e for _, e in spool.take(os.path.join(self.dir, "spool"))]
        self.assertEqual((ev["type"], ev["entity"]), ("delegation.spec_accepted", did))

    def test_the_specs_strip_has_no_buttons_while_its_executor_discusses_it(self):
        """Owner decision 2026-11-09 (core/specmode.awaiting): the executor is in a turn — the strip says it is writing,
        with no "Accept" / "Drop"; its result brings the buttons back without reloading the page (the 4 s poll)."""
        from core import journal
        from tests.test_spec_surfaces import spec_journal
        j = journal.Journal(os.path.join(self.dir, "journal.jsonl"))
        did = spec_journal(j, state=self.dir)
        j.append("message.received", None, {"to": "maps-1", "text": "why two slices?", "channel": "web", "owner": True})
        j.append("turn.started", "maps-1", {"marker": "m9999"})
        self.files.spec_wait = 0
        shots = os.environ.get("HUB_TEST_SHOTS")
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/specs/{did}/spec.md"},
                        {"until": "!!document.querySelector('#view .specbar')"},
                        {"eval": "document.querySelectorAll('#view .specbar button').length"},
                        *([{"shot": os.path.join(shots, "files-spec-strip-discussion.png"), "width": 1300, "height": 700}] if shots else []))
        self.assertEqual(got[2]["eval"], 0)
        j.append("turn.result", "maps-1", {"text": "Because of the migration."})
        j.append("turn.stopped", "maps-1", {})
        got = self.page({"until": "document.querySelectorAll('#view .specbar button').length === 2", "ms": 12000})
        self.assertTrue(got[0]["until"])

    def test_video_plays_seeks_and_downloads(self):
        """A clip with a space and non-Latin letters in its name opens in the player, seeking (Range) works; Download is an attachment."""
        got = self.page({"go": self.url("φάκελος βίντεο/το βίντεό μου.webm")},
                        {"until": "(v => v && v.readyState >= 1 && v.duration > 2)(document.querySelector('#view video'))",
                         "ms": 10000},
                        {"eval": "(v => { v.currentTime = 2; return 1; })(document.querySelector('#view video'))"},
                        {"until": "(v => !v.seeking && Math.abs(v.currentTime - 2) < 0.3)(document.querySelector('#view video'))",
                         "ms": 10000},
                        {"eval": "(a => a && [a.textContent, a.getAttribute('href'), a.download])"
                                 "(document.querySelector('#acts a.download'))"},
                        # a short window: the player fits wholly in the file window, its buttons are not cut off (a grid without rows used to stretch it)
                        {"shot": os.path.join(self.dir, "low.png"), "width": 1100, "height": 560},
                        {"eval": "(v => v.getBoundingClientRect().bottom <= document.querySelector('#view')"
                                 ".getBoundingClientRect().bottom + 1)(document.querySelector('#view video'))"})
        self.assertTrue(got[1]["until"], "the clip did not load")
        self.assertTrue(got[3]["until"], "the clip did not seek")
        label, link, name = got[4]["eval"]
        self.assertTrue(got[6]["eval"], "the player is taller than the file window")
        self.assertEqual((label, name), ("Download", "το βίντεό μου.webm"))
        self.assertIn("download=1", link)
        got = self.page({"eval": f"fetch({json.dumps(link)}).then(r => window.__dl = [r.status, r.headers.get('content-disposition')]), 1"},
                        {"until": "window.__dl"})
        code, cd = got[1]["until"]
        self.assertEqual(code, 200)
        self.assertTrue(cd.startswith("attachment;"), cd)

    def test_images_and_pictures_inside_markdown(self):
        """An image shows as an image; in a markdown report its own image by a relative path is visible, a foreign one is not."""
        shown = "(i => i && i.complete && i.naturalWidth)(document.querySelector('#view img'))"
        got = self.page({"go": self.url("img/b.png")}, {"until": shown, "ms": 8000},
                        {"go": self.url("docs/report.md")}, {"errors": 1500},
                        {"until": shown, "ms": 8000},
                        {"eval": "[...document.querySelectorAll('#view img')].map(i => i.getAttribute('src'))"})
        self.assertEqual(got[1]["until"], 40)
        self.assertEqual(got[4]["until"], 40, "the document image was not shown")
        self.assertEqual(len(got[5]["eval"]), 1, got[5]["eval"])  # https://example.com — removed
        self.assertIn("/raw?root=demo&path=img%2Fa.png", got[5]["eval"][0])

    def test_diagram_and_own_clip_inside_markdown(self):
        """A mermaid diagram in a document is drawn (its SVG is sanitized once more), the document's own clip stays a
        clip — media are removed only from the feed."""
        shutil.copy(CLIP, os.path.join(self.root, "docs", "clip.webm"))
        with open(os.path.join(self.root, "docs", "plan.md"), "w") as f:
            f.write("# Plan\n\n```mermaid\nflowchart LR\n  A[Hub] --> B[Specialist]\n```\n\n"
                    "<video src=\"clip.webm\" controls></video>\n")
        got = self.page({"go": self.url("docs/plan.md")},
                        {"until": "[...document.querySelectorAll('.mermaid-box svg')].some(s => s.textContent.includes('Specialist'))",
                         "ms": 15000},
                        {"eval": "[...document.querySelectorAll('#view video')].map(v => v.getAttribute('src'))"})
        self.assertTrue(got[1]["until"], "the diagram in the document is not drawn")
        self.assertEqual(len(got[2]["eval"]), 1, got[2]["eval"])
        self.assertIn("/raw?root=demo&path=docs%2Fclip.webm", got[2]["eval"][0])

    def test_a_report_in_its_sandbox_gets_its_own_files(self):
        """An html report is in a sandbox (an opaque origin, its requests are marked cross-site by the browser): refusing
        foreign sites does not affect its css and images."""
        import http.server
        from unittest import mock
        from urllib.parse import urlparse
        with open(os.path.join(self.root, "docs", "page.html"), "w") as f:
            f.write('<link rel="stylesheet" href="look.css"><img src="../img/c.png"><p>report</p>')
        with open(os.path.join(self.root, "docs", "look.css"), "w") as f:
            f.write("p { color: rgb(1, 2, 3) }")
        seen = []
        real = http.server.BaseHTTPRequestHandler.send_response

        def answered(handler, code, message=None):
            seen.append((urlparse(handler.path).path, code, handler.headers.get("Sec-Fetch-Site")))
            return real(handler, code, message)
        with mock.patch.object(http.server.BaseHTTPRequestHandler, "send_response", answered):
            self.page({"go": self.url("docs/page.html")}, {"until": "document.querySelector('#view iframe')"},
                      {"sleep": 1500})
        loaded = {p: (code, site) for p, code, site in seen if p.startswith("/render/")}
        self.assertEqual(loaded.get("/render/demo/docs/page.html", (0,))[0], 200, seen)
        self.assertEqual(loaded.get("/render/demo/docs/look.css"), (200, "cross-site"), seen)
        self.assertEqual(loaded.get("/render/demo/img/c.png"), (200, "cross-site"), seen)
        self.assertFalse([x for x in seen if x[1] == 403], seen)   # nobody was refused

    def test_table_with_a_long_unbroken_string_still_fits_its_columns(self):
        """A long string without spaces in a cell wraps instead of inflating the column: the second column does not run off the edge."""
        with open(os.path.join(self.root, "docs", "hosts.md"), "w") as f:
            f.write("| Host | Systems |\n|---|---|\n| `billing.example.com, api.example.com/" + "auth\\|user\\|siteApi/auth\\|api/auth\\|siteApi/users/user-info" * 4 + "` "
                    "| billing-service; paths /api/v2/billing, /api/v2/plans |\n")
        got = self.page({"go": self.url("docs/hosts.md")}, {"until": "!!document.querySelector('#view .md table')", "ms": 8000},
                        {"sleep": 300}, {"eval": "(t => [t.clientWidth, t.scrollWidth])(document.querySelector('#view .tbl'))"})
        client, scroll = got[-1]["eval"]
        self.assertGreater(client, 300, "the table is not drawn")
        self.assertLessEqual(scroll, client, "the table is wider than the window: the second column runs off the edge")

    def test_a_theme_can_set_the_width_of_the_text_column(self):
        """A theme's "measure" (e.g. "80ch") is the width of the line in the rendered text: a wide monospace font made the default
        68ch column a narrow strip with empty sides. No field or a value out of range (not "<number>ch", below 40, above 200) —
        the column as before."""
        with open(os.path.join(self.root, "docs", "long.md"), "w") as f:
            f.write("# Long\n\n" + "word " * 400 + "\n")
        colors = {"background": "#101010", "foreground": "#eeeeee", "primary": "#88aaff"}
        width = ("[document.documentElement.style.getPropertyValue('--measure'),"
                 " document.querySelector('#view .md.doc > p').getBoundingClientRect().width]")

        def column(theme):
            steps = []
            if theme is not None:
                # about:blank first: the same address again is a move inside the document, the seed would not run
                saved = json.dumps({"id": "own:t.json", "theme": {"name": "T", "colors": colors, **theme}})
                steps = [{"go": "about:blank"}, {"local": {"files.theme": saved}}]
            got = self.page(*steps, {"go": self.url("docs/long.md")},
                            {"until": "!!document.querySelector('#view .md.doc > p')", "ms": 8000}, {"eval": width})
            return got[-1]["eval"]
        default_var, default_px = column(None)
        self.assertEqual(default_var, "")
        self.assertGreater(default_px, 300)
        var, px = column({"measure": "80ch"})   # more than 80ch does not fit the 1300px window
        self.assertEqual(var, "80ch")
        self.assertGreater(px, default_px * 1.1, "the column did not widen")
        for bad in ("9999ch", "10ch", "wide", 110, "110px", "110ch; color: red", ""):
            var, px = column({"measure": bad})
            self.assertEqual((var, px), (default_var, default_px), bad)

    def test_the_hub_pages_text_size_reaches_files_through_the_cookie(self):
        """Owner complaint: "Text size" of the hub page did not work "across the whole hub". Files is another origin (its port): the
        page's localStorage is invisible to it, so the page leaves the choice in a cookie of the host (the ports share it; named by this Files port, so two hubs on
        one host do not clash) and Files takes it before the first paint — in the default look and in Classic. No cookie — the default step."""
        with open(os.path.join(self.root, "docs", "plain.md"), "w") as f:
            f.write("# Plain\n\nText\n")
        sizes = ("[...['#view .md.doc > p', '.fhome', '.crumbs'].map((q) => document.querySelector(q)).map((e) => e && parseFloat(getComputedStyle(e).fontSize)),"
                 " document.documentElement.style.getPropertyValue('--ui-scale')]")
        mine, other = "hub.textScale.%d" % self.port, "hub.textScale.%d" % (self.port + 1)
        ready = {"until": "!!document.querySelector('#view .md.doc > p')", "ms": 8000}
        for look, seed in (("default", []), ("Classic", [{"local": {"files.theme": '{"id":"core:classic"}'}}])):
            got = self.page({"go": "about:blank", "look": None}, *seed, {"go": self.url("docs/plain.md"), "look": None}, ready,
                            {"eval": sizes},
                            # the choice of another hub on this host (another "Files" port) is not ours
                            {"eval": f"document.cookie = '{other}=1.3333; path=/'; location.reload(); 1"}, {"sleep": 1000}, ready,
                            {"eval": sizes},
                            {"eval": f"document.cookie = '{mine}=1.3333; path=/'; location.reload(); 1"},
                            {"until": "document.documentElement.style.getPropertyValue('--ui-scale') === '1.3333'", "ms": 8000}, ready,
                            {"eval": sizes},
                            {"eval": f"document.cookie = '{mine}=; path=/; max-age=0'; document.cookie = '{other}=; path=/; max-age=0'; document.cookie"})
            evals = [g["eval"] for g in got if "eval" in g]   # sizes, the other hub's cookie set, sizes, ours set, sizes, both removed
            normal, foreign, large, left = evals[0], evals[2], evals[4], evals[5]
            self.assertEqual(normal[3], "", look)
            self.assertEqual(foreign, normal, look)
            self.assertEqual(large[3], "1.3333", look)
            for i, what in enumerate(("document text", "header", "breadcrumbs")):
                self.assertTrue(normal[i] and large[i] > normal[i] * 1.2, f"{look}: {what} {normal[i]} -> {large[i]}")
            self.assertEqual(left, "", look)

    def test_a_fresh_browser_gets_clean_and_classic_is_in_the_picker(self):
        """Clean, the core's default look, in "Files" too (every go of this test as in a fresh browser: a go script stays for
        the next pages of the run): its colors and font when this browser chose nothing; "Classic" in the
        picker at the top brings the earlier look at once and is remembered; the Clean plugin's choice moves to the core's."""
        with open(os.path.join(self.root, "docs", "plain.md"), "w") as f:
            f.write("# Plain\n\nText\n")
        look = ("[[...document.documentElement.classList].filter((c) => c.startsWith('th-clean')).join(' '),"
                " getComputedStyle(document.body).fontFamily.includes('Clean Geist'), document.querySelector('#themes select')?.value,"
                " localStorage.getItem('files.theme')]")
        pick = ("(() => { const s = document.querySelector('#themes select'); s.value = 'core:classic';"
                " s.dispatchEvent(new Event('change')); return 1; })()")
        got = self.page({"go": "about:blank", "look": None}, {"go": self.url("docs/plain.md"), "look": None},
                        {"until": "!!document.querySelector('#themes select')", "ms": 8000}, {"eval": look},
                        {"eval": pick}, {"until": "!document.documentElement.classList.contains('th-clean')"}, {"eval": look},
                        {"go": "about:blank", "look": None}, {"go": self.url("docs/plain.md"), "look": None},
                        {"until": "!!document.querySelector('#themes select')", "ms": 8000}, {"eval": look})
        self.assertEqual(got[3]["eval"], ["th-clean", True, "core:clean", None])
        self.assertEqual(got[6]["eval"], ["", False, "core:classic", '{"id":"core:classic"}'])
        self.assertEqual(got[-1]["eval"], ["", False, "core:classic", '{"id":"core:classic"}'])   # remembered
        # a link to a built-in theme is applied before the first paint, not after the list of themes comes
        early = ("new MutationObserver((_, o) => { if (!document.body) return; o.disconnect(); window.__early ="
                 " [...document.documentElement.classList].filter((c) => c.startsWith('th-clean')).join(' '); })"
                 ".observe(document, { childList: true, subtree: true })")   # when <body> begins: the first paint
        got = self.page({"go": "about:blank", "look": None},
                        {"go": self.url("docs/plain.md").replace("#", "?theme=core%3Aclean-dark#", 1), "look": None, "init": early},
                        {"until": "window.__early !== undefined"}, {"eval": "[window.__early, localStorage.getItem('files.theme')]"})
        self.assertEqual(got[-1]["eval"], ["th-clean th-clean-dark", '{"id":"core:clean-dark"}'])
        moved = json.dumps({"id": "kalimullin:clean:clean-dark.json", "theme": {"name": "Clean Dark", "colors": {
            "background": "#1c1c1f", "foreground": "#ececee", "primary": "#7d93ff"}}})
        got = self.page({"go": "about:blank", "look": None}, {"local": {"files.theme": moved}}, {"go": self.url("docs/plain.md"), "look": None},
                        {"until": "!!document.querySelector('#themes select')", "ms": 8000}, {"eval": look})
        self.assertEqual(got[-1]["eval"], ["th-clean th-clean-dark", True, "core:clean-dark", '{"id":"core:clean-dark"}'])

    def test_embedded_window_has_no_folder_column(self):
        """embed=1 (the embedded window of the hub page): a wide window has no folder column — the file starts at the left edge."""
        got = self.page({"go": self.url("docs/report.md") + "?embed=1"},
                        {"until": "!!document.querySelector('#view .md')", "ms": 8000},
                        {"eval": "[document.querySelector('.fside').getBoundingClientRect().width,"
                                 " document.querySelector('.fwin').getBoundingClientRect().left, innerWidth]"})
        side, left, width = got[-1]["eval"]
        # wider than the 1100px rule of files.css (on Windows the window frame takes ~16px of --window-size=1300)
        self.assertGreater(width, 1100)
        self.assertEqual(side, 0, "the folder column is visible")
        self.assertLess(left, 40, "an empty column on the left")

    def test_a_folder_from_the_embedded_window_shows_its_list(self):
        """In the embedded window the path above a file is clickable: a folder is no longer "just the file" but the whole
        Files view with the folder list (embed hid the list, and the folder opened as an empty panel)."""
        got = self.page({"go": self.url("docs/report.md") + "?embed=1"},
                        {"until": "!!document.querySelector('#view .md')", "ms": 8000},
                        {"eval": "[...document.querySelectorAll('#crumbs a')].find((a) => a.textContent === 'docs').click(), 1"},
                        {"until": "document.querySelector('#app').dataset.view === 'list'"
                                  " && document.querySelector('#list').innerText.includes('report.md')", "ms": 8000},
                        {"eval": "[document.querySelector('#list').getBoundingClientRect().width,"
                                 " document.querySelector('#app').dataset.embed || null]"})
        self.assertTrue(got[3]["until"], got)
        width, embed = got[-1]["eval"]
        self.assertGreater(width, 100, "the folder list is not visible")
        self.assertIsNone(embed)

    def test_only_the_hub_page_embeds_files(self):
        """The hub page (its port is HUB_WEB_PORT) embeds a file in its panel; a foreign site with the same markup does not."""
        import http.server
        from unittest import mock
        src = f"http://127.0.0.1:{self.port}/open?path=" + quote(os.path.join(self.root, "docs", "report.md")) + "&embed=1"

        class Page(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = f'<iframe src="{src}" width="800" height="500"></iframe>'.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        pages = []
        for _ in range(2):   # the hub page and a foreign site
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
            pages.append(srv.server_address[1])
        hub, foreign = pages
        with mock.patch.dict(os.environ, {"HUB_WEB_PORT": str(hub)}):
            got = self.page({"go": f"http://127.0.0.1:{hub}/"}, {"sleep": 1500}, {"frames": 1},
                            {"go": f"http://127.0.0.1:{foreign}/"}, {"sleep": 1500}, {"frames": 1})
        self.assertTrue(got[2]["frames"] and all(u.startswith(f"http://127.0.0.1:{self.port}/") for u in got[2]["frames"]),
                        got[2])
        self.assertTrue(got[5]["frames"] and all(u.startswith("chrome-error:") for u in got[5]["frames"]), got[5])

    def hub_page(self, query, sibling=False):
        """The hub page (its port is HUB_WEB_PORT) with report.md embedded the way md-panel does it (/open?…&embed=1 plus
        `query`); window.got keeps what the frame tells the page: [origin, data]. Returns the page port."""
        import http.server
        src = (f"http://127.0.0.1:{self.port}/open?path=" + quote(os.path.join(self.root, "docs", "report.md"))
               + "&embed=1" + query)
        body = (f'<iframe id="f" src="{src}" width="800" height="500"></iframe>'
                + ('<iframe id="s" srcdoc="<p>side</p>"></iframe>' if sibling else '') + '<script>window.got = [];'
                ' addEventListener("message", (e) => { if (e.source === f.contentWindow) got.push([e.origin, e.data]); });'
                '</script>').encode()

        class Page(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def to_frame(self, data):
        """A message from the hub page to the embedded window, addressed to the Files origin."""
        return {"eval": f"f.contentWindow.postMessage({json.dumps(data)}, 'http://127.0.0.1:{self.port}'), 1"}

    # the last thing the window told the page about itself
    STATE = "got.filter((m) => m[1].type === 'files:state').map((m) => m[1]).at(-1)"

    def test_the_hub_page_draws_the_header_of_the_embedded_window(self):
        """embed=1&chrome=hub (one header for every panel): the window hides its own header and tells the hub page the file,
        the path and the actions; the page runs an action by its id — a neighbour, the edit mode, Esc."""
        from unittest import mock
        with open(os.path.join(self.root, "docs", "notes.md"), "w") as f:
            f.write("# Notes\n")
        hub = self.hub_page("&chrome=hub")
        state = self.STATE
        with mock.patch.dict(os.environ, {"HUB_WEB_PORT": str(hub)}):
            got = self.page({"go": f"http://127.0.0.1:{hub}/"},
                            {"until": f"{state}?.title === 'report.md'", "ms": 8000},
                            {"eval": "[got.at(-1)[0], " + state + "]"},
                            self.to_frame({"type": "files:do", "id": "prev"}),
                            {"until": f"{state}?.title === 'notes.md'", "ms": 5000},
                            {"eval": "f.focus(), f.contentWindow.focus(), 1"},
                            {"key": "Escape"},
                            {"until": "got.some((m) => m[1].type === 'files:close')", "ms": 5000},
                            self.to_frame({"type": "files:do", "id": "edit"}),
                            {"until": f"{state}?.editing === true", "ms": 5000},
                            {"eval": f"{state}.actions.map((a) => a.id)"})
        self.assertTrue(all(g["until"] for g in got if "until" in g), got)
        origin, first = got[2]["eval"]
        self.assertEqual(origin, f"http://127.0.0.1:{self.port}")
        self.assertEqual([c["label"] for c in first["crumbs"]], [i18n.tr("en", "files.home"), "demo", "docs"])
        ids = [a["id"] for a in first["actions"]]
        for want in ("edit", "prev", "next", "download"):
            self.assertIn(want, ids)
        self.assertNotIn("close", ids)   # the panel has its own cross
        download = next(a for a in first["actions"] if a["id"] == "download")
        self.assertTrue(download["href"].startswith(f"http://127.0.0.1:{self.port}/raw?"), download)
        self.assertIn("download=1", download["href"])
        self.assertTrue(first["info"]["path"].endswith("report.md"), first["info"])
        # the list is navigation and reading: nothing that writes a file or starts a program can be asked for by id
        for banned in ("save", "cancel", "reveal"):
            self.assertNotIn(banned, ids)
            self.assertNotIn(banned, got[10]["eval"])
        self.assertNotIn("edit", got[10]["eval"])

    def test_only_the_hub_page_commands_the_embedded_window(self):
        """A page inside the window (an html report in its sandbox) cannot run the window's actions, an id that is not in
        the list does nothing; the hub page itself can."""
        from unittest import mock
        with open(os.path.join(self.root, "docs", "page.html"), "w") as f:
            f.write("<p>report</p><script>for (const w of [parent, top]) w.postMessage({type: 'files:do', id: 'edit'}, '*');"
                    "</script>")
        hub = self.hub_page("&chrome=hub")
        state = self.STATE
        with mock.patch.dict(os.environ, {"HUB_WEB_PORT": str(hub)}):
            got = self.page({"go": f"http://127.0.0.1:{hub}/"},
                            {"until": f"{state}?.title === 'report.md'", "ms": 8000},
                            self.to_frame({"type": "files:do", "id": "prev"}),
                            {"until": f"{state}?.title === 'page.html'", "ms": 5000},
                            {"sleep": 1500},
                            self.to_frame({"type": "files:do", "id": "nope"}),
                            self.to_frame({"type": "files:do", "id": "save", "path": "/etc/hosts"}),
                            self.to_frame({"type": "files:do", "id": "reveal"}),
                            {"sleep": 800},
                            {"eval": f"[{state}.editing, got.length]"},
                            self.to_frame({"type": "files:do", "id": "edit"}),
                            {"until": f"{state}?.editing === true", "ms": 5000})
        self.assertTrue(all(g["until"] for g in got if "until" in g), got)
        editing, _ = got[9]["eval"]
        self.assertFalse(editing, "a page inside the window switched on editing")

    def test_another_frame_of_the_hub_page_does_not_command_the_window(self):
        """A message from the hub page's own origin that does not come from the parent (another frame of the same page,
        which a plugin or a rendered message could hold) is not obeyed: the window takes only its parent's word."""
        from unittest import mock
        hub = self.hub_page("&chrome=hub", sibling=True)
        state = self.STATE
        side = ("document.getElementById('s').contentWindow.eval("
                "\"parent.document.getElementById('f').contentWindow.postMessage({type: 'files:do', id: 'edit'}, '*')\"), 1")
        with mock.patch.dict(os.environ, {"HUB_WEB_PORT": str(hub)}):
            got = self.page({"go": f"http://127.0.0.1:{hub}/"},
                            {"until": f"{state}?.title === 'report.md'", "ms": 8000},
                            {"eval": side},
                            {"sleep": 1500},
                            {"eval": f"{state}.editing"},
                            self.to_frame({"type": "files:do", "id": "edit"}),
                            {"until": f"{state}?.editing === true", "ms": 5000})
        self.assertTrue(got[1]["until"] and got[2]["eval"] == 1, got)
        self.assertFalse(got[4]["eval"], "another frame of the hub page switched on editing")
        self.assertTrue(got[6]["until"], got)   # the parent's own word still works

    def test_a_page_of_another_origin_is_told_nothing(self):
        """The window talks only to the hub page's exact origin (host and port of HUB_WEB_PORT), never to "*": the same
        frame embedded by a page of another origin (here: the hub page's port, opened as localhost, not as 127.0.0.1; the
        frame-ancestors of the window allow it, so only the window's own target origin stands) is told nothing: the same
        window on the hub page's own origin does tell it (the test above). Obeying only that origin: the check in the
        message handler, and a page inside the window cannot command it (the test below)."""
        from unittest import mock
        hub = self.hub_page("&chrome=hub")
        with mock.patch.dict(os.environ, {"HUB_WEB_PORT": str(hub)}):
            got = self.page({"go": f"http://localhost:{hub}/"},
                            {"sleep": 2500},
                            self.to_frame({"type": "files:do", "id": "prev"}),
                            {"sleep": 1500},
                            {"eval": "got.length"},
                            {"frames": 1})
        # no check that the frame is listed: localhost and 127.0.0.1 are different sites, and Chrome lists such a frame
        # (an out-of-process one) or not by its build; the window loads there — with "*" as the target the page gets 1
        self.assertEqual(got[4]["eval"], 0, "the window wrote to a page of another origin")

    def test_embedded_window_without_the_hub_header_stays_as_it_was(self):
        """embed=1 alone (an older md-panel): the window keeps its own header and tells the page nothing."""
        from unittest import mock
        hub = self.hub_page("")
        with mock.patch.dict(os.environ, {"HUB_WEB_PORT": str(hub)}):
            got = self.page({"go": f"http://127.0.0.1:{hub}/"}, {"sleep": 2500}, {"eval": "got.length"},
                            {"frames": 1})
        self.assertEqual(got[2]["eval"], 0)
        self.assertTrue(got[3]["frames"] and got[3]["frames"][0].startswith(f"http://127.0.0.1:{self.port}/"), got[3])

    def test_close_and_neighbours(self):
        """← → — neighbouring files of the folder, Esc and the cross — back to the folder, the browser's Back — the file again."""
        def on(name):  # the file is open: its path is in the line above it (before that the keys would go to the previous file)
            return {"until": f"(document.querySelector('#view .meta .path')?.textContent || '').replace(/\\\\/g, '/').endsWith('/{name}')"}
        got = self.page({"go": self.url("img/b.png")}, on("b.png"),
                        self.key("ArrowRight"), on("c.png"),
                        self.key("ArrowRight"), {"sleep": 300}, on("c.png"),  # the last one — nowhere further
                        self.key("ArrowLeft"), on("b.png"),
                        self.key("Escape"), {"until": "decodeURIComponent(location.hash) === '#/demo/img'"},
                        {"until": "document.querySelector('#app').dataset.view === 'list' && !document.querySelector('#acts .close')"},
                        {"eval": "history.back(), 1"}, on("b.png"),
                        {"eval": "document.querySelector('#acts .close').getAttribute('href')"})
        self.assertTrue(all(g["until"] for g in got if "until" in g), got)
        self.assertEqual(got[-1]["eval"], "#/demo/img")


if __name__ == "__main__":
    unittest.main()
