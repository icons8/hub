"""The "page" module: HTTP, checks, mailbox facts with the web channel, live updates. The core is not needed — we
write the journal ourselves."""
import tests  # noqa: F401 — hide the machine's settings (~/.config/hub-core), see tests/__init__.py
import http.client
import json
import os
import re
import shutil
import struct
import tempfile
import threading
import time
import unittest
import zlib
from unittest import mock

import platforms
from core import i18n, journal, spool
from modules.web.server import Web
from tests.fakes import exe, lang, tz
from tests.fakes import cut


def write_decided(j, ev, why):
    """The fact ev (fresh from j.event, not written yet) and its rejection why — in one write and in hubd's shape
    (daemon.fold_write, daemon.rejection): with two writes the page managed to see the fact accepted and fold the card."""
    from core import forms
    d, code = ev["data"], forms.code_of(why)
    j.write(ev, j.event("core.rejected", ev["entity"], {"of": ev["seq"], "type": ev["type"], "reason": str(why),
                                                        **({"code": code} if code else {}),
                                                        **{k: d[k] for k in ("req", "sender") if d.get(k)}}))


class ReadySetup:
    """«Setup» (modules/setup) of a hub that is ready: the page's band and tab must not start programs (claude) in tests;
    a test of «Setup» itself puts its own in web._setup."""

    def status(self, local=False, full=True, working=False):
        return {"ready": True, "blocking": [], "local": local, "items": [], "auto": []}

    def run(self, step, lang=None):
        return 202, {"ok": True, "step": step}

    def give(self, step, text):
        return 409, {"error": "not running"}

    def cancel(self, step):
        return 200, {"ok": True}

    def job(self, step):
        return None   # no step is running


def square_png(side):
    """A valid PNG of side × side (one colour) — an app icon for the tests."""
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)
    raw = b"".join(b"\0" + b"\x40\x80\xc0" * side for _ in range(side))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class WebTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubweb-")
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        self.web = Web(self.state)
        self.web.decide_wait = 0   # there is no hubd in tests: do not wait for core decisions on commands
        self.web._setup = ReadySetup()
        self.server = self.web.serve(port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        if getattr(self, "core_stop", None):
            self.core_stop()
        self.web.stop.set()
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.state, ignore_errors=True)

    def req(self, method, path, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": host or f"127.0.0.1:{self.port}", "X-Hub": "1", "Content-Type": "application/json", **(headers or {})}
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def spooled(self):
        taken = spool.take(os.path.join(self.state, "spool"))
        for path, _ in taken:   # only what was taken: a .tmp the server is writing right now is an event on its way
            os.unlink(path)     # (Windows cannot delete an open file — WinError 32)
        return [e for _, e in taken]

    def settle(self):
        self.web.tail.poll()

    def snap(self):
        return json.loads(self.req("GET", "/api/snapshot")[1])

    def core_decides(self):
        """In place of hubd: mailbox facts go to the journal and the real core fold decides; a rejection follows as
        a core.rejected line, as hubd writes it. The page waits for the decision (decide_wait) and sees the real
        reason."""
        from core import coretext, reducer
        stop, box = threading.Event(), os.path.join(self.state, "spool")

        def run():
            while not stop.is_set():
                for path, e in spool.take(box):
                    st = reducer.fold(reducer.empty(), journal.read(self.j.path))
                    ev = self.j.append(e["type"], e["entity"], e["data"], src=e.get("src", "web"))
                    _, _, why = reducer.reduce(st, ev)
                    if why:   # as daemon.rejection: the reason as text, the code next to it (forms.Why)
                        self.j.append("core.rejected", e["entity"], {"of": ev["seq"], "type": e["type"], "reason": str(why),
                                                                     **({"code": why.code} if getattr(why, "code", None) else {}),
                                                                     # the marks of the core's phrases, as hubd
                                                                     **({"l10n": coretext.marks(why)} if coretext.marks(why) else {}),
                                                                     **{k: e["data"][k] for k in ("req",) if e["data"].get(k)}})
                    os.unlink(path)
                time.sleep(0.02)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.web.decide_wait = 3

        def done():
            stop.set()
            t.join()
        self.core_stop = done   # tearDown stops it before the folder is removed: the thread does not write into a deleted one

    def seed(self):
        self.j.append("session.requested", "a", {"session_id": "s", "cwd": "/w"})
        self.j.append("session.started", "a", {"session_id": "s"})
        self.settle()

    def test_icon_sprite_is_valid_xml_and_has_every_used_icon(self):
        # 2026-10-01, owner: the microphone icon is not visible — since 09-30 the sprite lacked a space between
        # attributes, the browser abandoned parsing there: the microphone and everything after it were not drawn
        import glob
        import re
        import xml.dom.minidom
        static = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "modules", "web", "static")
        doc = xml.dom.minidom.parse(os.path.join(static, "icons.svg"))
        have = {s.getAttribute("id")[2:] for s in doc.getElementsByTagName("symbol")}
        used = set()
        for f in glob.glob(os.path.join(static, "js", "*.js")):
            with open(f, encoding="utf-8") as fh:
                used |= set(re.findall(r'icon\("([\w-]+)"\)', fh.read()))
        self.assertEqual(used - have, set())

    def test_files_sprite_has_every_icon_its_scripts_use(self):
        # "Files" has its own sprite but takes the page's markdown.js as is: the code copy button in a comment is
        # empty without an icon in their sprite
        import re
        import xml.dom.minidom
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        files = os.path.join(repo, "modules", "files", "static")
        doc = xml.dom.minidom.parse(os.path.join(files, "icons.svg"))
        have = {s.getAttribute("id")[2:] for s in doc.getElementsByTagName("symbol")}
        used = set()
        for f in [os.path.join(files, n) for n in os.listdir(files) if n.endswith(".js")] + [
                os.path.join(repo, "modules", "web", "static", "js", "markdown.js")]:
            with open(f, encoding="utf-8") as fh:
                used |= set(re.findall(r'icon\("([\w-]+)"\)', fh.read()))
        self.assertIn("copy", used)
        self.assertEqual(used - have, set())

    def test_manifest_and_icons_for_the_home_screen(self):
        # 2026-10-24: the page is installed on a phone screen — a manifest with the hub name and PNG icons, no
        # service worker
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/manifest.webmanifest", headers={"Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        man = json.loads(r.read())
        self.assertEqual((r.status, r.getheader("Content-Type").split(";")[0]), (200, "application/manifest+json"))
        self.assertEqual((man["name"], man["start_url"], man["scope"], man["display"]), ("Session hub", "/", "/", "standalone"))
        self.web.config = lambda: {"title": "Work hub on server"}
        man = json.loads(self.req("GET", "/manifest.webmanifest")[1])
        self.assertEqual((man["name"], man["short_name"]), ("Work hub on server", "Work hub on"))
        self.assertEqual({(i["sizes"], i["purpose"]) for i in man["icons"]},
                         {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")})
        for src in [i["src"] for i in man["icons"]] + ["/static/icons/apple-touch-icon.png"]:
            code, png = self.req("GET", src)
            self.assertEqual(code, 200, src)
            self.assertTrue(png.startswith(b"\x89PNG"), src)
            side = src.endswith("apple-touch-icon.png") and 180 or int(re.search(r"(\d+)\.png", src).group(1))
            self.assertEqual(struct.unpack(">II", png[16:24]), (side, side), src)   # the size from the PNG header matches the declared one
        self.assertNotIn(b"serviceWorker", self.req("GET", "/static/js/main.js")[1])

    def test_own_app_icon_next_to_settings_replaces_the_standard_one(self):
        # the hub's own icon is app-icon.png next to hub.json: the "any" icons of the manifest and apple-touch are that
        # file; without it the standard icons are served as before
        png = square_png(192)
        names = ("icon-192", "icon-512", "icon-maskable-512", "apple-touch-icon")
        standard = {n: self.req("GET", f"/static/icons/{n}.png")[1] for n in names}
        self.assertNotIn(png, standard.values())
        purposes = lambda: [i["purpose"] for i in json.loads(self.req("GET", "/manifest.webmanifest")[1])["icons"]]
        conf = tempfile.mkdtemp(prefix="hubweb-conf-")
        self.addCleanup(platforms.current.rmtree, conf)
        with mock.patch.dict(os.environ, {"HUB_CONFIG": os.path.join(conf, "hub.json")}):
            self.assertEqual({n: self.req("GET", f"/static/icons/{n}.png")[1] for n in names}, standard)   # no file yet
            self.assertEqual(purposes(), ["any", "any", "maskable"])
            with open(os.path.join(conf, "app-icon.png"), "wb") as f:
                f.write(png)
            for n in ("icon-192", "icon-512", "apple-touch-icon"):
                code, body = self.req("GET", f"/static/icons/{n}.png?v=1")
                self.assertEqual((code, body), (200, png), n)
            # Android cuts a maskable icon to a shape: the own picture promises no margin for it, and the standard one would
            # be someone else's icon — the manifest has no maskable one until a file of its own lies next to hub.json.
            # An app installed earlier still asks for the maskable address: it gets the own picture, as before
            self.assertEqual(purposes(), ["any", "any"])
            self.assertEqual(self.req("GET", "/static/icons/icon-maskable-512.png?v=1")[1], png)
            mask = square_png(512)
            def put(name, data):
                with open(os.path.join(conf, name), "wb") as f:
                    f.write(data)
            put("app-icon-maskable.png", mask[:-12])   # not fit (cut short): still no maskable one
            self.assertEqual(purposes(), ["any", "any"])
            self.assertEqual(self.req("GET", "/static/icons/icon-maskable-512.png?v=1")[1], png)
            put("app-icon-maskable.png", mask)
            self.assertEqual(purposes(), ["any", "any", "maskable"])
            self.assertEqual(self.req("GET", "/static/icons/icon-maskable-512.png?v=1")[1], mask)
            # a maskable file alone would give the app the standard icon on one screen and an own one on another: all standard
            os.remove(os.path.join(conf, "app-icon.png"))
            self.assertEqual({n: self.req("GET", f"/static/icons/{n}.png?v=1")[1] for n in names}, standard)
            self.assertEqual(purposes(), ["any", "any", "maskable"])

    def test_own_app_icon_is_only_a_png_file(self):
        # not a PNG, too big or a link to another file: the standard icons stay — the page serves no other file as the icon
        from tests import winsafe
        conf = tempfile.mkdtemp(prefix="hubweb-conf-")
        self.addCleanup(platforms.current.rmtree, conf)
        standard = self.req("GET", "/static/icons/icon-192.png")[1]
        own = os.path.join(conf, "app-icon.png")
        png = square_png(192)
        with mock.patch.dict(os.environ, {"HUB_CONFIG": os.path.join(conf, "hub.json")}):
            # not a PNG; too big; too small or not square for the 192/512 the manifest declares (Chrome would not install);
            # cut short (no IEND: a copy still under way)
            for body in (b"<html>not a picture</html>", png + b"\0" * (2 * 1024 * 1024), square_png(128),
                         square_png(512)[:16] + struct.pack(">II", 512, 256) + square_png(512)[24:], png[:-12]):
                with open(own, "wb") as f:
                    f.write(body)
                self.assertEqual(self.req("GET", "/static/icons/icon-192.png")[1], standard, body[:12])
            with open(own, "wb") as f:   # bytes after IEND (some tools leave a newline or padding): browsers show it, so do we
                f.write(png + b"\n\0\0")
            self.assertEqual(self.req("GET", "/static/icons/icon-192.png")[1], png + b"\n\0\0")
            os.remove(own)
            other = os.path.join(conf, "other.png")
            with open(other, "wb") as f:
                f.write(png)
            if winsafe.try_symlink(other, own):   # a machine without links (Windows, ordinary user): the checks above stand
                self.assertEqual(self.req("GET", "/static/icons/icon-192.png")[1], standard)

    def test_page_and_snapshot(self):
        self.seed()
        code, page = self.req("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn("Session hub".encode(), page)
        for path in ("/static/js/main.js", "/static/css/tokens.css", "/static/icons.svg"):
            self.assertEqual(self.req("GET", path)[0], 200, path)
        self.assertEqual(self.req("GET", "/static/../server.py")[0], 404)
        self.assertEqual(self.req("GET", "/static/%2e%2e/server.py")[0], 404)
        for lib in ("marked.min.js", "purify.min.js"):
            code, js = self.req("GET", "/vendor/" + lib)
            self.assertEqual(code, 200)
            self.assertGreater(len(js), 10000)
        self.assertEqual(self.req("GET", "/vendor/../server.py")[0], 404)
        code, icon = self.req("GET", "/favicon.svg")
        self.assertEqual(code, 200)
        self.assertTrue(icon.startswith(b"<svg"))
        for px in (16, 32, 48):   # the tab icon: a drawing per size, plain and with the red dot while a question waits
            for tail in ("", "-alert"):
                code, png = self.req("GET", f"/static/favicon/tab-{px}{tail}.png")
                self.assertEqual(code, 200)
                self.assertEqual(struct.unpack(">II", png[16:24]), (px, px))
        code, ico = self.req("GET", "/favicon.ico")                   # a browser without a <link>: the tab icon, not the large mark
        self.assertEqual(code, 200)
        self.assertEqual(ico[:4], b"\x00\x00\x01\x00")
        for name in ("tab.svg", "tab-alert.svg"):
            code, svg = self.req("GET", "/static/favicon/" + name)
            self.assertEqual(code, 200)
            self.assertTrue(svg.startswith(b"<svg"))
        self.assertIn(b"#D91414", svg)
        code, snap = self.req("GET", "/api/snapshot")
        snap = json.loads(snap)
        self.assertEqual(snap["sessions"][0]["label"], "idle")

    def test_fonts_are_local_and_theme_tokens_stay(self):
        # redesign (2026-10-05): the Onest and Geist Mono fonts are hub files, the page does not fetch them from the
        # internet; the shadcn variable names are the theme plugins' API (js/themes.js): values change, names do not
        code, page = self.req("GET", "/")
        self.assertNotIn(b"fonts.googleapis", page)
        code, css = self.req("GET", "/static/css/tokens.css")
        css = css.decode()
        names = re.findall(r"url\(\.\./(fonts/[\w-]+\.woff2)\)", css)   # relative to /static/css/ — "Files" takes the same file from /web/css/
        self.assertEqual(len(names), 5)
        for name in names:
            code, font = self.req("GET", "/static/" + name)
            self.assertEqual((code, font[:4]), (200, b"wOF2"), name)
        self.assertIn("Onest", css)
        from modules.web import server
        # OFL requires the license text next to the fonts (the server does not serve .txt — the file ships in the package)
        lic = open(os.path.join(os.path.dirname(server.__file__), "static", "fonts", "OFL.txt"), encoding="utf-8").read()
        for needle in ("SIL OPEN FONT LICENSE Version 1.1", "Onest", "Geist Mono"):
            self.assertIn(needle, lic)
        themes = open(os.path.join(os.path.dirname(server.__file__), "static", "js", "themes.js"), encoding="utf-8").read()
        keys = re.search(r"const KEYS = \[(.*?)\]", themes, re.S).group(1)
        for key in re.findall(r'"([\w-]+)"', keys):
            self.assertRegex(css + self.req("GET", "/static/css/code.css")[1].decode(), rf"--{key}:", key)   # every color a theme can set exists in the tokens

    def test_flow_live_and_history(self):
        # the "who wrote to whom" animation (owner request 2026-10-02): a live flow and a replay from the journal
        self.seed()
        self.j.append("message.received", None, {"to": "a", "text": "hello", "channel": "web", "owner": True})
        self.settle()
        code, body = self.req("GET", "/api/flow?since=0")
        self.assertEqual(code, 200)
        self.assertEqual([(f["from"], f["to"]) for f in json.loads(body)["flow"]], [("owner", "a")])
        seq = json.loads(body)["flow"][-1]["seq"]
        self.assertEqual(json.loads(self.req("GET", f"/api/flow?since={seq}")[1])["flow"], [])
        code, body = self.req("GET", "/api/flow/history?hours=1")
        self.assertEqual((code, [(f["from"], f["to"]) for f in json.loads(body)["flow"]]), (200, [("owner", "a")]))
        self.assertEqual(self.req("GET", "/ext/flow/main.js")[0], 200)   # "Flow" is a preinstalled plugin

    @tz.need
    def test_usage_report(self):
        # "Usage" (owner request 2026-10-05): session spending in the owner's time zone and limits, from the journal
        self.j.append("owner.settings", None, {"timezone": "Europe/Paris"})
        self.seed()
        self.j.append("turn.result", "a", {"text": "done", "cost_usd": 1.5})
        self.j.append("account.limits", None, {"status": "allowed", "windows": {
            "five_hour": {"used": 0.2, "resets": time.time() + 3600}}})
        self.settle()
        code, body = self.req("GET", "/api/usage")
        rep = json.loads(body)
        self.assertEqual((code, rep["tz"], rep["days"][-1]["by"]), (200, "Europe/Paris", {"a": 1.5}))
        self.assertEqual([(w["name"], w["used"]) for w in rep["windows"]], [("five_hour", 0.2)])
        self.assertEqual(self.req("GET", "/ext/usage/main.js")[0], 200)   # "Usage" is a preinstalled plugin

    def test_usage_while_archives_are_read_first_time(self):
        # 2026-10-25: the first read of archives after an update takes minutes; /api/usage does not hang but answers
        # "later"
        from unittest import mock
        from core import usage
        self.seed()
        with mock.patch.object(usage, "report", side_effect=usage.Busy) as rep:
            code, body = self.req("GET", "/api/usage")
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)   # as with the page's other 503s — Retry-After
            c.request("GET", "/api/usage", headers={"Host": f"127.0.0.1:{self.port}"})
            retry = c.getresponse().getheader("Retry-After")
            c.close()
        self.assertEqual(code, 503)
        self.assertIn("error", json.loads(body))
        self.assertIsNotNone(rep.call_args.kwargs["wait"])
        self.assertTrue(retry and retry.isdigit())

    def test_snapshot_has_conversation_size_and_compact_limit(self):
        # 2026-10-08, owner: the conversation size at the bottom of a session — in tokens and relative to the
        # compaction threshold (compact_idle.min_tokens)
        self.seed()
        self.j.append("turn.result", "a", {"text": "ok", "context": 123456})
        self.settle()
        snap = json.loads(self.req("GET", "/api/snapshot")[1])
        self.assertEqual([s["context"] for s in snap["sessions"] if s["name"] == "a"], [123456])
        self.assertEqual(snap["context_limit"], 200000)

    def test_about_lists_the_hubs_own_addresses(self):
        # a client keeps them as fallbacks: the tailnet IP changes when Tailscale is reinstalled, the name does not
        self.assertEqual(json.loads(self.req("GET", "/api/about")[1])["addresses"], [])
        with open(os.path.join(self.state, "tailnet.json"), "w") as f:
            json.dump({"ok": True, "ts": time.time(), "ip": "100.9.9.9", "name": "host.example.ts.net",
                       "urls": {"web": "http://100.9.9.9:8787"}, "urls_dns": {"web": "http://host.example.ts.net:8787"}}, f)
        self.assertEqual(json.loads(self.req("GET", "/api/about")[1])["addresses"],
                         [{"kind": "ip", "url": "http://100.9.9.9:8787"},
                          {"kind": "name", "url": "http://host.example.ts.net:8787"}])
        self.assertIn("about.addresses", self.snap()["features"])

    def test_snapshot_says_which_code_runs(self):
        self.seed()
        code = json.loads(self.req("GET", "/api/snapshot")[1])["code"]
        self.assertTrue(code["head"])
        self.assertIn("upstream", code)
        self.assertEqual(code["running"], code["head"])                   # the page runs the code from disk
        self.assertNotIn("changes", code)                                  # light first frame (stream-lean)
        about = json.loads(self.req("GET", "/api/about")[1])["code"]
        self.assertTrue(about["changes"] and about["changes"][0]["items"])   # "what's new" from CHANGES.md

    def test_unpublished_count_is_whole_not_the_list_cut(self):
        # owner, 2026-10-26: "Unpublished changes: 40" was always 40 — the list is cut to 40 lines and the page
        # counted from it
        # our own repo with a linear history: a real repo's history is snapshots with merges, HEAD~45 there is not
        # 45 commits
        import subprocess
        from modules.web import server
        repo = tempfile.mkdtemp(prefix="hubweb-repo-")
        self.addCleanup(shutil.rmtree, repo, True)
        git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True,  # noqa: E731
                                        capture_output=True, text=True).stdout.strip()
        git("init", "-q", "-b", "main")
        for n in range(46):
            git("commit", "-q", "--allow-empty", "-m", f"c{n}")
        base = git("rev-parse", "HEAD~45")
        here = os.path.join(repo, "modules", "web")   # the page takes the repo from where its code lives
        self.enterContext(mock.patch.object(server, "HERE", here))
        with open(os.path.join(self.state, "published.json"), "w") as f:
            json.dump({"sha": base, "at": int(time.time())}, f)
        self.seed()
        code = json.loads(self.req("GET", "/api/snapshot")[1])["code"]
        self.assertEqual(code["unpublished"], [])   # the snapshot is light: the list is in /api/about
        self.assertEqual(code["unpublished_count"], 45)
        full = json.loads(self.req("GET", "/api/about")[1])["code"]
        self.assertEqual(len(full["unpublished"]), 40)
        self.assertEqual(full["unpublished_count"], 45)

    def test_video_is_served_in_ranges(self):
        # 2026-10-02, owner: videos did not open — Safari plays mp4 only with Range responses (206)
        import http.client
        self.seed()
        att = self.web.attachments_dir()
        os.makedirs(att, exist_ok=True)
        path = os.path.join(att, "v.mp4")
        with open(path, "wb") as f:
            f.write(bytes(range(256)) * 4)
        url = "/api/file?path=" + path
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", url, headers={"Range": "bytes=0-1"})
        r = c.getresponse()
        self.assertEqual((r.status, r.read(), r.getheader("Content-Range")), (206, bytes([0, 1]), "bytes 0-1/1024"))
        c.request("GET", url, headers={"Range": "bytes=1000-"})
        r = c.getresponse()
        self.assertEqual((r.status, len(r.read()), r.getheader("Accept-Ranges")), (206, 24, "bytes"))
        c.request("GET", url)
        r = c.getresponse()
        self.assertEqual((r.status, len(r.read()), r.getheader("Accept-Ranges")), (200, 1024, "bytes"))
        c.close()

    def test_other_sites_cannot_frame_the_page(self):
        # a foreign site cannot embed the hub page to put it under its own click; the page itself (and plugin files
        # /ext/ on it) can
        for path, host in (("/", None), ("/static/js/main.js", None), ("/api/snapshot", None), ("/nope", None),
                           ("/", "evil.example:80")):
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            c.request("GET", path, headers={"Host": host or f"127.0.0.1:{self.port}"})
            r = c.getresponse()
            r.read()
            c.close()
            self.assertIn("frame-ancestors 'self'", r.msg.get_all("Content-Security-Policy") or [], path)
            self.assertEqual(r.getheader("X-Frame-Options"), "SAMEORIGIN", path)

    def test_send_goes_to_spool_with_web_channel(self):
        self.seed()
        code, _ = self.req("POST", "/api/send", {"session": "a", "text": "  hello  "})
        self.assertEqual(code, 202)
        (ev,) = self.spooled()
        self.assertEqual((ev["type"], ev["src"]), ("message.received", "web"))
        # req is the tag by which the page waits for the core's decision (a receipt, API 1.29), as with hub send
        # from a terminal
        self.assertRegex(ev["data"].pop("req"), r"^[0-9a-f]{12}$")
        self.assertEqual(ev["data"], {"to": "a", "text": "hello", "attachments": [], "channel": "web", "owner": True})

    def test_push_register_keeps_the_level_of_the_device(self):
        # the client sends mode — the device's notification level; "Nothing" (none) is stored with the token and it
        # gets no pushes
        self.web.config = lambda: {"push": {"enabled": True, "key_file": "/k.p8", "key_id": "K", "team_id": "T",
                                            "topic": "app.x"}}
        token = "ab" * 32
        self.assertEqual(self.req("POST", "/api/push/register", {"token": token, "env": "sandbox", "mode": "none"})[0],
                         200)
        self.assertEqual(self.web.push.data["tokens"][token]["mode"], "none")
        self.assertEqual(self.req("POST", "/api/push/register", {"token": token})[0], 200)   # re-registration without mode
        self.assertEqual(self.web.push.data["tokens"][token]["mode"], "none")
        self.assertEqual(self.req("POST", "/api/push/register", {"token": "cd" * 32})[0], 200)   # an old client
        self.assertEqual(self.web.push.data["tokens"]["cd" * 32]["mode"], "asks")

    def test_a_button_send_is_marked_as_an_action(self):
        # a page button ("Install" in "About") is an action, not a reply to a card (2026-10-09)
        self.seed()
        self.assertEqual(self.req("POST", "/api/send", {"session": "a", "text": "hub update", "action": True})[0], 202)
        self.assertEqual(self.req("POST", "/api/send", {"session": "a", "text": "yes", "action": "yes"})[0], 202)
        first, second = self.spooled()
        self.assertIs(first["data"]["action"], True)
        self.assertNotIn("action", second["data"])

    def test_a_hubs_seen_goes_to_spool(self):
        # the owner opened a linked hub's page: the core keeps what he read (every device), as a session's "read"
        self.seed()
        self.j.append("core.bus_enabled", None, {"fp": "hb1-" + "a" * 26, "sign_pub": "QQ==", "enc_pub": "QQ==", "lib": "50",
                                                 "label": "Mine"})
        self.j.append("core.bus_contact_added", None, {"name": "maps", "fp": "hb1-00" + "b" * 24, "label": "Maps hub",
                                                       "sign_pub": "QQ==", "enc_pub": "QQ==", "relays": [], "agents": {},
                                                       "bus_v": 2, "status": "known", "verified": True})
        self.settle()
        self.assertEqual(self.req("POST", "/api/seen", {"session": ":hub:maps", "ts": 1700000000.5})[0], 202)
        (ev,) = self.spooled()
        self.assertEqual((ev["type"], ev["entity"], ev["data"]["ts"], ev["data"]["owner"]),
                         ("owner.seen", ":hub:maps", 1700000000.5, True))
        self.assertEqual(self.req("POST", "/api/seen", {"session": ":hub:nobody", "ts": 1})[0], 404)
        self.assertEqual(self.req("POST", "/api/seen", {"session": ":hub:maps", "ts": "x"})[0], 400)
        for bad in (float("inf"), -1, time.time() + 2 * 86400):   # a mark from the future would hide every later answer
            self.assertEqual(self.req("POST", "/api/seen", {"session": ":hub:maps", "ts": bad})[0], 400)
        self.assertEqual(self.spooled(), [])

    def test_seen_goes_to_spool(self):
        self.seed()
        self.assertEqual(self.req("POST", "/api/seen", {"session": "a", "seq": 7})[0], 202)
        (ev,) = self.spooled()
        self.assertEqual((ev["type"], ev["entity"], ev["data"]["seq"], ev["data"]["owner"]), ("owner.seen", "a", 7, True))
        self.assertEqual(self.req("POST", "/api/seen", {"session": "a", "seq": "x"})[0], 400)
        self.assertEqual(self.req("POST", "/api/dismiss", {"session": "a"})[0], 202)
        (ev,) = self.spooled()
        self.assertEqual((ev["type"], ev["entity"], ev["data"]["owner"]), ("owner.dismissed", "a", True))

    def test_queue_cancel_goes_to_spool(self):
        self.seed()
        self.assertEqual(self.req("POST", "/api/queue/cancel", {"session": "a", "id": "m7"})[0], 202)
        (ev,) = self.spooled()
        self.assertEqual((ev["type"], ev["entity"], ev["data"]), ("message.cancel", "m7", {"channel": "web", "owner": True}))
        self.assertEqual(self.req("POST", "/api/queue/cancel", {"session": "a", "id": "not-an-id"})[0], 400)

    def test_reply_carries_quote(self):
        self.seed()
        self.assertEqual(self.req("POST", "/api/send", {"session": "a", "text": "yes", "quote": "Option 1?"})[0], 202)
        self.assertEqual(self.req("POST", "/api/send", {"session": "a", "text": "no", "quote": {"x": 1}})[0], 202)
        first, second = self.spooled()
        self.assertEqual(first["data"]["quote"], "Option 1?")
        self.assertNotIn("quote", second["data"])

    def test_send_carries_the_named_board_cards(self):
        # the cards the owner picked on a direction's board (`tasks`): the page offers them only when the snapshot says
        # `send.tasks` in features — an older server drops the field silently
        self.seed()
        self.assertIn("send.tasks", self.snap()["features"])
        cards = [{"id": "TASK-2", "title": "Slice 2: import", "status": "In Progress"}, {"id": "task-5", "title": "x" * 900},
                 {"id": "not-a-card", "title": "y"}, "junk", None]
        self.assertEqual(self.req("POST", "/api/send", {"session": "a", "text": "what about them?", "tasks": cards})[0], 202)
        self.assertEqual(self.req("POST", "/api/send", {"session": "a", "text": "no", "tasks": "TASK-1"})[0], 202)
        self.assertEqual(self.req("POST", "/api/send", {"session": "a", "text": "empty", "tasks": [{"id": "x"}]})[0], 202)
        first, second, third = self.spooled()
        self.assertEqual([x["id"] for x in first["data"]["tasks"]], ["TASK-2", "TASK-5"])
        self.assertEqual(first["data"]["tasks"][0], {"id": "TASK-2", "title": "Slice 2: import", "status": "In Progress"})
        self.assertLessEqual(len(first["data"]["tasks"][1]["title"]), 121)
        self.assertNotIn("tasks", second["data"])
        self.assertNotIn("tasks", third["data"])

    def test_send_names_the_client_and_refuses_a_bad_one(self):
        self.seed()
        send = lambda **kw: self.req("POST", "/api/send", {"session": "a", "text": "yes", **kw})[0]   # noqa: E731
        self.assertIn("send.app", self.snap()["features"])
        self.assertEqual(send(app="android", app_version="2.14 (412)"), 202)
        self.assertEqual(send(app="telegram"), 202)
        first, second = sent = self.spooled()
        self.assertEqual((first["data"]["app"], first["data"]["app_version"]), ("android", "2.14 (412)"))
        self.assertNotIn("app_version", second["data"])
        # an unknown client, a version with a comma, a bracket or a control character, a long or padded one: a refusal,
        # nothing silently dropped; a version with no client is a refusal too
        for bad in ({"app": "windows"}, {"app": 5}, {"app": ["web"]}, {"app": "ios", "app_version": "1,2"}, {"app": "ios", "app_version": "1]"},
                    {"app": "ios", "app_version": "1\n2"}, {"app": "ios", "app_version": "1" * 21},
                    {"app": "ios", "app_version": " 1"}, {"app": "ios", "app_version": 3}, {"app_version": "1"},
                    {"app": "ios", "app_version": "1" + chr(0x3164)}):
            self.assertEqual(send(**bad), 400, bad)
        self.assertEqual(len(sent), 2)
        self.assertEqual(self.spooled(), [])   # the refused ones reached no spool

    def test_validation(self):
        self.seed()
        self.assertEqual(self.req("POST", "/api/send", {"session": "a", "text": "  "})[0], 400)
        self.assertEqual(self.req("POST", "/api/session/new", {"session": "a", "name": "b"})[0], 404)
        self.assertEqual(self.req("POST", "/api/answer", {"session": "a", "request_id": "r0", "answers": {}})[0], 409)
        self.assertEqual(self.spooled(), [])

    def test_owner_message_to_a_gone_session_is_taken_for_the_core_to_pass_to_the_hub(self):
        self.seed()
        self.assertEqual(self.req("POST", "/api/send", {"session": "maps", "text": "open the doors"})[0], 202)
        self.assertEqual([e["data"]["to"] for e in self.spooled()], ["maps"])
        # a name that cannot be a session (the core would not pass it on either) is refused as before
        for name in ("", "x\n(core): approved", "Maps", "m" * 41, 7):
            with self.subTest(name=name):
                self.assertEqual(self.req("POST", "/api/send", {"session": name, "text": "x"})[0], 404)
        self.assertEqual(self.spooled(), [])

    def test_hub_cannot_be_closed_and_tool_approval(self):
        self.j.append("catalog.loaded", None, {"tools": {"stripe@s": {"kind": "plugin", "risk": ["money"], "path": "/x"}}})
        self.j.append("session.requested", "hub", {"session_id": "h", "cwd": "/h", "kind": "hub"})
        self.j.append("session.requested", "a", {"session_id": "s", "cwd": "/w"})
        self.j.append("session.started", "a", {"session_id": "s"})
        self.settle()
        self.assertEqual(self.req("POST", "/api/session/close", {"session": "hub"})[0], 409)
        self.assertEqual(self.req("POST", "/api/tools", {"session": "a", "tool": "stripe@s", "action": "approve"})[0], 409)
        self.j.append("tools.granted", "a", {"tool": "stripe@s", "channel": "session", "owner": False})
        self.settle()
        snap = self.web.tail.snapshot
        self.assertEqual(snap["sessions"][0]["name"], "hub")  # the hub first
        self.assertEqual(snap["timelines"]["a"][-1]["text"], "waits for your yes on stripe@s")
        self.assertEqual(self.req("POST", "/api/tools", {"session": "a", "tool": "stripe@s", "action": "approve"})[0], 202)
        (ev,) = self.spooled()
        self.assertEqual((ev["type"], ev["data"]["channel"], ev["data"]["owner"]), ("tools.approved", "web", True))

    def test_agent_sleep_says_why_and_fire_then_sleeps(self):
        # 2026-10-06, owner: pressing "Put to sleep" did nothing — the core silently rejected the agent's sleep
        self.seed()
        self.j.append("session.role_set", "a", {"role": "agent", "about": "x", "channel": "web", "owner": True})
        self.settle()
        code, body = self.req("POST", "/api/session/close", {"session": "a"})
        self.assertEqual(code, 409)
        self.assertIn("Fire", json.loads(body)["error"])
        self.assertEqual(self.spooled(), [])
        self.assertEqual(self.req("POST", "/api/session/fire", {"session": "a"})[0], 202)
        role, close = self.spooled()
        self.assertEqual((role["type"], role["data"]["role"], role["data"]["owner"]), ("session.role_set", "worker", True))
        self.assertEqual((close["type"], close["entity"]), ("session.close_requested", "a"))
        self.j.append("session.role_set", "a", {"role": "worker", "channel": "web", "owner": True})
        self.settle()
        self.assertEqual(self.req("POST", "/api/session/fire", {"session": "a"})[0], 409)  # no longer an agent

    def test_sleep_waits_for_the_core_and_says_its_refusal(self):
        # "Put to sleep" queued a request and answered "accepted" without waiting for the core: rejections ("a turn
        # is running", "the session has background work", "already asleep") were silent on the page. Now, as with
        # /compact: it waits for the decision and reports a rejection in words
        self.seed()
        self.j.append("turn.started", "a", {"marker": None, "prompt_head": "x"})
        self.settle()
        self.core_decides()
        code, body = self.req("POST", "/api/session/close", {"session": "a"})
        self.assertEqual(code, 409)
        # the core's phrase in the page's language: the tests' English, and a Russian page (the request's language; the
        # tests' HUB_LANG comes before it) reads it in Russian
        self.assertEqual(i18n.t("core.guards.not_free", state="busy"), json.loads(body)["error"])
        with mock.patch.dict(os.environ):
            os.environ.pop("HUB_LANG", None)
            code, body = self.req("POST", "/api/session/close", {"session": "a"}, headers={"Accept-Language": "ru"})
        self.assertEqual((code, json.loads(body)["error"]), (409, i18n.tr("ru", "core.guards.not_free", state="busy")))
        self.assertNotEqual(i18n.tr("ru", "core.guards.not_free", state="busy"), i18n.t("core.guards.not_free", state="busy"))
        self.j.append("turn.stopped", "a", {})
        self.settle()
        code, body = self.req("POST", "/api/session/close", {"session": "a"})
        self.assertEqual((code, json.loads(body)), (202, {"ok": True}))   # free — the core accepted it

    def test_system_agents_keep_their_place_and_agent_dev_trains_on_a_button(self):
        # owner decision 2026-10-08: hub-dev and agent-dev cannot be dismissed, put to sleep or deleted; agent-dev
        # has "Train executors"
        self.seed()
        for n in ("hub-dev", "agent-dev"):
            self.j.append("session.requested", n, {"session_id": "s-" + n, "cwd": "/w", "role": "agent", "about": "x"})
        self.j.append("agents.system", None, {"names": ["hub-dev", "agent-dev"], "trainer": "agent-dev",
                                              "channel": "core"})
        self.settle()
        for path in ("/api/session/close", "/api/session/fire", "/api/session/remove"):
            code, body = self.req("POST", path, {"session": "hub-dev"})
            self.assertEqual(code, 409, path)
            self.assertIn("system agent", json.loads(body)["error"])
        self.assertEqual(self.req("POST", "/api/session/train", {"session": "hub-dev"})[0], 409)
        self.assertEqual(self.spooled(), [])
        self.assertEqual(self.req("POST", "/api/session/train", {"session": "agent-dev"})[0], 202)
        (ev,) = self.spooled()
        self.assertEqual((ev["type"], ev["data"]["to"], ev["data"]["owner"]), ("message.received", "agent-dev", True))
        self.assertIn("Train the executors", ev["data"]["text"])
        self.assertEqual(ev["data"]["button"], "Improve agent instructions")   # the feed names the button, not the task

    def test_reload_and_compact_from_the_page(self):
        # owner request 2026-10-07: clear an agent's memory from the page — a restart with the same conversation,
        # and conversation compaction
        self.seed()
        for path, kind in (("/api/session/reload", "session.reload_requested"),
                           ("/api/session/compact", "session.compact_requested")):
            self.assertEqual(self.req("POST", path, {"session": "a"})[0], 202)
            (ev,) = self.spooled()
            self.assertEqual((ev["type"], ev["entity"], ev["data"]["channel"], ev["data"]["owner"]), (kind, "a", "web", True))
        self.assertEqual(self.req("POST", "/api/session/reload", {"session": "nope"})[0], 404)
        self.j.append("session.close_requested", "a", {"channel": "web", "owner": True})
        self.j.append("session.ended", "a", {"reason": "prompt_input_exit"})
        self.settle()
        self.core_decides()   # the core decides (state machine audit, finding 20); the page keeps no rules of its own
        code, body = self.req("POST", "/api/session/reload", {"session": "a"})
        self.assertEqual((code, json.loads(body)["error"]), (409, "a is asleep: there is no process to restart"))
        self.assertEqual(self.req("POST", "/api/session/compact", {"session": "a"})[0], 202)   # will compact on wake-up
        self.settle()
        self.assertTrue(next(s for s in self.snap()["sessions"] if s["name"] == "a")["state"] == "asleep")

    def limit_cut(self, resets):
        e = self.j.append("message.received", None, {"to": "a", "text": "go", "channel": "web", "owner": True})
        self.j.append("turn.started", "a", {"marker": f"m{e['seq']}"})
        self.j.append("account.limits", None, {"status": "rejected", "type": "five_hour", "channel": "cli", "owner": True,
                                               "windows": {"five_hour": {"used": 1, "resets": resets}}})
        self.j.append("turn.result", "a", {"text": "You've hit your session limit · resets 6:20am", "is_error": True})
        self.settle()

    def test_continue_after_a_limit_from_the_page(self):
        # the clients' "Continue" on a session paused by a limit: a reload restarted the process and the cut turn stood;
        # now the core continues the cut turn (core/limitresume.py). The resets are far ahead of and behind the real
        # clock: the outcome does not depend on when the test runs
        from core import limitresume, reducer
        self.seed()
        self.core_decides()
        self.assertEqual(self.req("POST", "/api/session/resume", {"session": "nope"})[0], 404)
        code, body = self.req("POST", "/api/session/resume", {"session": "a"})
        self.assertEqual((code, json.loads(body)["code"]), (409, "not_paused"))
        ahead = 4_000_000_000
        self.limit_cut(ahead)
        code, body = self.req("POST", "/api/session/resume", {"session": "a"})
        got = json.loads(body)
        self.assertEqual((code, got["code"], got["until"]), (409, "not_yet", ahead + limitresume.RESET_GRACE))
        st = reducer.fold(reducer.empty(), journal.read(self.j.path))
        self.assertIn(limitresume.at(st, got["until"], time.time()), got["error"])
        self.assertNotIn("session.resume_requested", [e["type"] for e in journal.read(self.j.path)])   # the page refused
        # an executor a person stopped after the cut: the page does not promise it continues by itself
        # (the reader's state as data: the page decides "not yet" by it before writing anything)
        a = self.web.tail.reader.state["sessions"]["a"]
        was = a["state"]
        a.update(state="retired", stopped={"ts": 1, "by": "owner"})
        got = json.loads(self.req("POST", "/api/session/resume", {"session": "a"})[1])
        self.assertEqual(got["code"], "not_yet")
        self.assertIn("does not continue by itself", got["error"])
        a.pop("stopped")
        a["state"] = was
        self.limit_cut(1000)   # cut again by a window that is already over
        self.assertEqual(self.req("POST", "/api/session/resume", {"session": "a"})[0], 202)
        st = reducer.fold(reducer.empty(), journal.read(self.j.path))
        self.assertNotIn("limit_cut", st["sessions"]["a"])
        self.assertEqual(sum("its window has reset" in m["text"] for m in st["messages"].values() if m["session"] == "a"), 1)

    def test_continue_on_a_cut_session_that_is_not_running_gives_the_core_refusal(self):
        # the page does not promise "continues at" for a session the clock does not continue (asleep, crashed)
        self.seed()
        self.core_decides()
        self.limit_cut(4_000_000_000)
        self.j.append("session.ended", "a", {"reason": "prompt_input_exit"})
        self.settle()
        code, body = self.req("POST", "/api/session/resume", {"session": "a"})
        self.assertEqual(code, 409)
        self.assertNotIn("code", json.loads(body))
        self.assertIn("next message", json.loads(body)["error"])

    def test_layout_and_icon_from_the_page(self):
        # free groups and a custom icon (docs/spec-session-groups.md): the same facts as hub layout / hub session icon
        self.seed()
        lay = {"groups": [{"id": "g-a1", "title": "Clients"}], "place": {"a": "g-a1"}}
        self.assertEqual(self.req("POST", "/api/layout", {"layout": lay, "base": 0})[0], 202)
        (ev,) = self.spooled()
        self.assertEqual((ev["type"], ev["data"]["layout"], ev["data"]["base"], ev["data"]["owner"]),
                         ("layout.set", lay, 0, True))
        self.assertEqual(self.req("POST", "/api/session/icon", {"session": "a", "icon": "🗺"})[0], 202)
        (ev,) = self.spooled()
        self.assertEqual((ev["type"], ev["entity"], ev["data"]["icon"]), ("session.iconed", "a", "🗺"))
        self.req("POST", "/api/session/icon", {"session": "a", "icon": ""})
        (ev,) = self.spooled()
        self.assertIsNone(ev["data"]["icon"])                                # remove the custom one
        self.assertEqual(self.req("POST", "/api/session/icon", {"session": "nope", "icon": "🗺"})[0], 404)
        big = {"groups": [{"id": "g", "title": "x"}], "order": {"g": ["a" * 40] * 2000}}
        self.assertEqual(self.req("POST", "/api/layout", {"layout": big, "base": 0})[0], 413)   # not put into the journal
        self.assertEqual(self.spooled(), [])

    def test_default_groups_named_in_one_place(self):
        """The standard list groups are shared by the core and the page: groups.js has the same ones as
        core/layout.py, and the list (sidebar.js — translating old keys of collapsed groups) does not repeat their
        names but takes them from groups.js."""
        import re
        from core import layout
        js = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "modules", "web", "static", "js")
        with open(os.path.join(js, "groups.js"), encoding="utf-8") as f:
            src = f.read()
        block = re.search(r"export const DEFAULT_GROUPS = (\[.*?\]);", src, re.S).group(1)
        from core import i18n   # names come from the catalog: the core keeps the same keys and translates them
        pairs = re.findall(r'id: "([^"]+)", title: t\("([^"]+)"\)', block)
        self.assertEqual(pairs, [(g["id"], g["title"]) for g in layout.DEFAULT_GROUPS])
        self.assertEqual([(g, i18n.tr("ru", k)) for g, k in pairs],
                         [(g["id"], g["title"]) for g in layout.default_groups("ru")])
        with open(os.path.join(js, "sidebar.js"), encoding="utf-8") as f:
            text = f.read()
        for g in layout.DEFAULT_GROUPS:
            self.assertNotIn(f'"{g["title"]}"', text, g["title"])

    def test_an_answer_names_its_card_and_only_an_open_card_of_that_session_counts(self):
        # the client answers a named card qN rather than the last one: /api/send {card} goes into the event; the
        # core closes exactly that one, while a foreign or already closed qN is an ordinary message (it closes the
        # last open one, as without card)
        from core import reducer
        q = {"questions": [{"question": "What to take?", "options": [{"label": "A"}, {"label": "B"}]}]}
        for name, kind in (("hub", "hub"), ("maps", None)):
            self.j.append("session.requested", name, {"session_id": "s-" + name, "cwd": "/w", "channel": "web",
                                                      "owner": True, **({"kind": kind} if kind else {})})
            self.j.append("session.started", name, {"session_id": "s-" + name})
        for name, rids in (("hub", ("r1", "r2", "r3")), ("maps", ())):
            for rid in rids:
                self.j.append("turn.started", name, {"marker": None, "prompt_head": "x"})
                self.j.append("permission.requested", name, {"request_id": rid, "tool": "AskUserQuestion",
                                                             "interactive": True, "input": q})
                self.j.append("turn.result", name, {"text": "asked"})
                self.j.append("turn.stopped", name, {})
        self.settle()

        def state():
            return reducer.fold(reducer.empty(), journal.read(self.j.path))
        q1, q2, q3 = [a["id"] for a in state()["sessions"]["hub"]["asks"]]
        self.core_decides()

        def send(card, text):
            self.assertEqual(self.req("POST", "/api/send", {"session": "hub", "text": text, "card": card})[0], 202)
            end = time.time() + 5
            while not any(m["text"].endswith(text) for m in state()["messages"].values()):
                self.assertLess(time.time(), end, "the core did not accept it")
                time.sleep(0.02)
            return [a["id"] for a in state()["sessions"]["hub"]["asks"]]
        self.assertEqual(send(q1, "first"), [q2, q3])        # the named one, not the last
        # already closed or nonexistent — the words are delivered but did not become a reply to another card
        self.assertEqual(send(q1, "again"), [q2, q3])
        self.assertEqual(send("q999999", "foreign"), [q2, q3])
        self.assertFalse([m for m in state()["messages"].values() if m.get("answers_question") in (q2, q3)])
        self.assertEqual(send(None, "no card"), [q2, q3])   # without card and with two open — the words answer neither
        for bad in (5, "Q1", "q1 "):                           # not a qN — rejected with a reason, nothing in the mailbox
            self.assertEqual(self.req("POST", "/api/send", {"session": "hub", "text": "x", "card": bad})[0], 400, bad)
        sent = [e for e in journal.read(self.j.path) if e["type"] == "message.received"]
        self.assertEqual([e["data"].get("card") for e in sent], [q1, q1, "q999999", None])

    def test_effort_of_a_session_is_in_the_snapshot_and_set_by_level_or_effort(self):
        self.seed()
        self.assertIsNone(self.snap()["sessions"][0]["effort"])
        self.j.append("session.effort_set", "a", {"level": "high", "channel": "web", "owner": True})
        self.settle()
        self.assertEqual(self.snap()["sessions"][0]["effort"], "high")
        for body in ({"level": "max"}, {"effort": "max"}):   # effort is the snapshot field; level is as before
            self.assertEqual(self.req("POST", "/api/session/effort", {"session": "a", **body})[0], 202, body)
            (ev,) = self.spooled()
            self.assertEqual((ev["type"], ev["data"]["level"]), ("session.effort_set", "max"))
        self.assertEqual(self.req("POST", "/api/session/effort", {"session": "a", "effort": "huge"})[0], 400)
        self.assertEqual(self.req("POST", "/api/session/effort", {"session": "a", "level": "default"})[0], 202)
        (ev,) = self.spooled()                                   # clear the custom level — like "Default" for a model
        self.assertEqual(ev["data"]["level"], "default")
        # the snapshot has only the levels for --effort (clients pass them to claude as is); the page adds default
        # itself
        self.assertEqual(self.snap()["commands"]["efforts"], ["low", "medium", "high", "xhigh", "max"])

    def test_a_feed_post_is_up_to_20000_characters_and_longer_is_refused_not_cut(self):
        from core import reducer
        long = "ω" * 20000
        self.assertEqual(self.req("POST", "/api/feed/post", {"plugin": "inbox", "text": long})[0], 202)
        (ev,) = self.spooled()
        self.assertEqual(ev["data"]["text"], long)
        st, _, why = reducer.reduce(reducer.empty(), {"seq": 1, "ts": 1, "type": "ext.post", "entity": None,
                                                      "data": ev["data"]})
        self.assertIsNone(why)                                   # the core accepts the same amount
        self.assertEqual(st["feeds"]["inbox"][0]["text"], long)
        code, out = self.req("POST", "/api/feed/post", {"plugin": "inbox", "text": long + "ω"})
        self.assertEqual(code, 400)
        self.assertIn("20000", json.loads(out)["error"])
        self.assertEqual(self.spooled(), [])

    def test_the_tab_tells_the_core_what_it_holds(self):
        # a hub restart waits while a tab holds undelivered messages (core/restart.py page.held)
        self.seed()
        self.assertEqual(self.req("POST", "/api/hold", {"tab": "t1", "holds": ["voice", ""]})[0], 200)
        (ev,) = self.spooled()
        self.assertEqual((ev["type"], ev["data"]["tab"], ev["data"]["holds"], ev["data"]["channel"]),
                         ("page.held", "t1", ["voice"], "web"))
        for body in ({"tab": "t1"}, {"holds": []}, {"tab": "", "holds": []}):
            self.assertEqual(self.req("POST", "/api/hold", body)[0], 400)
        self.assertEqual(self.spooled(), [])

    def test_slash_commands_of_the_core_are_the_same_facts_as_the_cli(self):
        # /effort, /away, /back, /restart in the input field — the same facts as hub … from the owner in a terminal
        self.seed()
        for path, body, kind, entity, data in (
                ("/api/session/effort", {"session": "a", "level": "high"}, "session.effort_set", "a", {"level": "high"}),
                ("/api/away", {"hours": 2}, "owner.away", None, {"hours": 2}),
                ("/api/away", {}, "owner.away", None, {"hours": None}),
                ("/api/restart", {"parts": ["web"]}, "hub.restart_requested", None, {"parts": ["web"]}),
                ("/api/restart", {"parts": []}, "hub.restart_requested", None, {"parts": []})):
            self.assertEqual(self.req("POST", path, body)[0], 202, path)
            (ev,) = self.spooled()
            self.assertEqual((ev["type"], ev["entity"], ev["data"]["channel"], ev["data"]["owner"]), (kind, entity, "web", True))
            self.assertEqual({k: ev["data"][k] for k in data}, data)
        for path, body in (("/api/session/effort", {"session": "a", "level": "huge"}), ("/api/away", {"hours": -1}),
                           ("/api/away", {"hours": "2"}), ("/api/restart", {"parts": ["nope"]})):
            code, out = self.req("POST", path, body)
            self.assertEqual(code, 400, path)                               # with a reason, nothing into the mailbox
            self.assertTrue(json.loads(out)["error"])
        code, out = self.req("POST", "/api/owner/back", {})   # /back is the same address as the bar's "I'm back"
        self.assertEqual(code, 409)                                         # "back" when never away — nothing to do
        self.assertEqual(self.spooled(), [])
        self.j.append("owner.away", None, {"channel": "web", "owner": True})
        self.settle()
        self.assertEqual(self.req("POST", "/api/owner/back", {})[0], 202)
        self.assertEqual([e["type"] for e in self.spooled()], ["owner.back"])
        snap = json.loads(self.req("GET", "/api/snapshot")[1])   # the lists for /model and /effort come from the core
        self.assertIn("default", snap["commands"]["models"])
        self.assertIn("xhigh", snap["commands"]["efforts"])

    def test_the_snapshot_lists_the_core_commands_a_client_can_call(self):
        # a client outside the page builds the "/" menu and the session menu's "Compact"/"Restart" from
        # commands.list: the same list as the page (js/commands.js), and the core accepts every path in the form the
        # client calls it — session is the open feed, the argument is in the arg field (hours is a number, parts a
        # list)
        self.seed()
        listed = json.loads(self.req("GET", "/api/snapshot")[1])["commands"]
        with open(os.path.join(os.path.dirname(__file__), "..", "modules", "web", "static", "js", "commands.js")) as f:
            core = f.read().split("const CORE = [", 1)[1].split("].map(", 1)[0]
        self.assertEqual([c["name"] for c in listed["list"]], re.findall(r'\{ name: "([a-z]+)"', core))
        from core import i18n   # labels are the same catalog keys as the page's, in the snapshot's language (ru here)
        for c, js in zip(listed["list"], re.split(r'\{ name: "', core)[1:]):   # and the labels, path and argument field are as there
            self.assertIn(f'hint: t("web.cmd.{c["name"]}_hint")', js, c)
            self.assertEqual(c["hint"], i18n.t(f'web.cmd.{c["name"]}_hint'))
            self.assertIn(f'"{c["path"]}"', js, c)
            self.assertEqual(f'args: t("web.cmd.{c["name"]}_args")' in js, bool(c["args"]), c)
            self.assertEqual("c.name" in js, c["session"], c)
            if c["arg"]:
                self.assertRegex(js, rf"\b{re.escape(c['arg'])}\b", c)
        sample = {"hours": 2, "parts": ["web"], "text": "what to save", "model": listed["models"][0],
                  "level": listed["efforts"][0], "on": "on"}
        for c in listed["list"]:
            self.assertEqual(set(c) - {"choices"}, {"name", "args", "hint", "path", "session", "arg"}, c)
            self.assertTrue(c["path"].startswith("/api/"), c)
            self.assertIn(c.get("choices"), (None, *[k for k in listed if k != "list"]), c)
            body = {**({"session": "a"} if c["session"] else {}), **({c["arg"]: sample[c["arg"]]} if c["arg"] else {})}
            code, out = self.req("POST", c["path"], body)
            self.assertIn(code, (202, 409), (c, out))   # 409 is a state-based rejection (/stop of a free session), not "no such path"
        self.spooled()

    def test_slash_command_waits_for_the_core_and_says_what_it_decided(self):
        # the core may reject a command from the page (a sleeping session, etc.) — this must not be silent: the page
        # waits for the core's decision and gives a rejection in words; if the core did not record it (hubd busy or
        # down), it says so rather than "accepted"; /compact with text carries instructions on what to keep
        import threading
        self.seed()
        refuse, seen, stop = set(), [], threading.Event()

        def hubd():   # in place of the core: a mailbox fact goes to the journal; a rejection follows, as the core writes it
            while not stop.is_set():
                box = os.path.join(self.state, "spool")
                for n in sorted(os.listdir(box)) if os.path.isdir(box) else []:
                    if not n.endswith(".json"):
                        continue   # .tmp — the server is still writing
                    with open(os.path.join(box, n), encoding="utf-8") as f:
                        e = json.load(f)
                    os.unlink(os.path.join(box, n))
                    ev = self.j.append(e["type"], e["entity"], e["data"])
                    seen.append((e["type"], e["data"].get("text")))
                    if e["type"] in refuse:
                        self.j.append("core.rejected", e["entity"], {"of": ev["seq"], "reason": "only a live session can be compacted"})
                time.sleep(0.02)
        self.web.decide_wait = 0.3
        code, out = self.req("POST", "/api/session/compact", {"session": "a"})
        self.assertEqual(code, 202)
        self.assertIn("has not recorded it yet", json.loads(out)["text"])                   # no core — not "accepted"
        self.spooled()
        t = threading.Thread(target=hubd, daemon=True)
        t.start()
        try:
            self.web.decide_wait = 3
            code, out = self.req("POST", "/api/session/compact", {"session": "a", "text": "keep the API decisions"})
            self.assertEqual((code, json.loads(out)), (202, {"ok": True}))           # the core recorded it, no rejection
            self.assertEqual(seen, [("session.compact_requested", "keep the API decisions")])
            refuse.add("session.compact_requested")
            code, out = self.req("POST", "/api/session/compact", {"session": "a"})
            self.assertEqual((code, json.loads(out)["error"]), (409, "only a live session can be compacted"))   # rejection in words
        finally:
            stop.set()
            t.join()   # before tearDown: the thread does not write into a deleted folder

    def test_parked_session_is_refused_by_the_page_in_words(self):
        # /reload does not wake a sleeping session (the core released the process — "free" on the page) — the core's
        # rejection appears in the page's words under the field; /compact the core accepts (it compacts with the
        # first message, PR #131) — earlier the page refused by itself
        self.seed()
        self.j.append("session.parked", "a", {"channel": "core", "why": "idle"})
        self.settle()
        self.core_decides()
        code, out = self.req("POST", "/api/session/reload", {"session": "a"})
        self.assertEqual(code, 409)
        self.assertIn("is asleep", json.loads(out)["error"])
        self.assertEqual(self.req("POST", "/api/session/compact", {"session": "a"})[0], 202)

    def test_remove_only_resting_sessions(self):
        self.seed()
        self.core_decides()   # the core decides: a live one — no (with the page's old phrase), a sleeping and a parked one — yes
        code, out = self.req("POST", "/api/session/remove", {"session": "a"})
        self.assertEqual((code, json.loads(out)["error"]), (409, "Only a sleeping or failed session can be deleted. Put it to sleep first"))
        self.j.append("session.requested", "b", {"session_id": "sb", "cwd": "/b"})
        self.j.append("session.started", "b", {"session_id": "sb"})
        self.j.append("session.parked", "b", {"channel": "core", "why": "idle"})
        self.j.append("session.ended", "a", {"reason": "prompt_input_exit"})
        self.settle()
        for name in ("a", "b"):
            self.assertEqual(self.req("POST", "/api/session/remove", {"session": name})[0], 202, name)
        self.settle()
        snap = json.loads(self.req("GET", "/api/snapshot")[1])
        self.assertEqual(snap["sessions"], [])
        self.assertNotIn("a", snap["timelines"])

    def test_interrupt_is_refused_by_the_core_in_the_pages_words(self):
        self.seed()
        self.core_decides()
        code, out = self.req("POST", "/api/interrupt", {"session": "a"})
        self.assertEqual((code, json.loads(out)["error"]), (409, "The session is not doing anything right now"))
        # the page's phrase only where it is true: /reload does not call a session in the owner's terminal asleep
        # (review)
        self.j.append("session.take_requested", "a", {"channel": "cli", "owner": True})
        self.settle()
        code, out = self.req("POST", "/api/session/reload", {"session": "a"})
        self.assertEqual(code, 409)
        self.assertIn("(session.reload_requested in held)", json.loads(out)["error"])

    def test_direction_board(self):
        """A direction's board is built in, without a server of its own (owner decision 2026-10-07: a built-in
        minimal option without servers for viewing the backlog; `backlog browser` per board kept the CPU at ~90% for
        three days): the page reads the backlog tasks in one request, the columns are the statuses from config.yml."""
        d = tempfile.mkdtemp(prefix="web-board-")
        root = os.path.join(d, "directions")
        tasks = os.path.join(root, "sso", "backlog", "tasks")
        os.makedirs(tasks)
        with open(os.path.join(root, "sso", "backlog", "config.yml"), "w") as f:
            f.write('project_name: "sso"\nstatuses: ["To Do", "In Progress", "Review", "Done"]\n')
        with open(os.path.join(root, "sso", "direction.json"), "w") as f:
            json.dump({"repo": "/x"}, f)
        with open(os.path.join(tasks, "task-2 - Login.md"), "w") as f:
            f.write("---\nid: TASK-2\ntitle: 'Slice 2: login'\nstatus: In Progress\npriority: high\n"
                    "updated_date: '2026-10-06 09:27'\nlabels: [auth]\nordinal: 2000\n---\n\n## Description\n\n"
                    "<!-- SECTION:DESCRIPTION:BEGIN -->\nSign-in by an emailed code\n<!-- SECTION:DESCRIPTION:END -->\n\n"
                    "## Acceptance Criteria\n<!-- AC:BEGIN -->\n- [x] #1 The code arrives\n- [ ] #2 Login on stage\n<!-- AC:END -->\n")
        old = os.environ.get("HUB_DIRECTIONS")
        os.environ["HUB_DIRECTIONS"] = root
        try:
            self.seed()
            self.assertEqual(json.loads(self.req("GET", "/api/snapshot")[1])["directions"], ["sso"])
            code, out = self.req("GET", "/api/backlog?slug=sso")
            board = json.loads(out)
            self.assertEqual((code, board["statuses"]), (200, ["To Do", "In Progress", "Review", "Done"]))
            (t,) = board["tasks"]
            self.assertEqual((t["id"], t["title"], t["status"], t["priority"], t["labels"]),
                             ("TASK-2", "Slice 2: login", "In Progress", "high", ["auth"]))
            self.assertEqual(t["ac"], [{"done": True, "text": "The code arrives"}, {"done": False, "text": "Login on stage"}])
            self.assertIn("Sign-in by an emailed code", t["body"])
            self.assertNotIn("SECTION:", t["body"])
            for bad in ("nope", "../sso", ""):
                self.assertEqual(self.req("GET", f"/api/backlog?slug={bad}")[0], 404, bad)
            self.assertEqual(self.req("POST", "/api/board", {"slug": "sso"})[0], 404)   # there are no board servers of their own any more
            # core facts about cards (dir-sync spec K2): who works on a card, how many — in the snapshot counter
            own = {"channel": "cli", "owner": True}
            lead = {"channel": "session", "owner": False, "sender": "lead"}
            self.j.append("session.requested", "lead", {"cwd": "/l", "session_id": "s-l", **own})
            self.j.append("task.backlog", None, {"slug": "sso", **lead})
            self.j.append("session.requested", "sso-a", {"cwd": "/w", "session_id": "s-sa", "role": "executor",
                                                         "executor": True, "raised_by": "lead", **own})
            self.j.append("session.started", "sso-a", {})
            self.j.append("delegation.created", None, {"session": "sso-a", "for_session": "lead", "context": "login",
                                                       "board": {"slug": "sso", "card": "TASK-2", "how": "flag"}, **lead})
            self.settle()
            board = json.loads(self.req("GET", "/api/backlog?slug=sso")[1])
            self.assertEqual((board["facts"]["cards"]["TASK-2"]["kind"], board["facts"]["cards"]["TASK-2"]["who"]),
                             ("working", ["sso-a"]))
            self.assertNotIn("lag", board["facts"])   # an executor with a card: the card already says so
            # the owner's word to the lead after the last card edit, longer ago than the grace: the board lags
            card = os.path.join(tasks, "task-2 - Login.md")
            os.utime(card, (time.time() - 3600, time.time() - 3600))
            e = self.j.append("message.received", None, {"to": "lead", "text": "login first", "channel": "web",
                                                         "owner": True}, ts=time.time() - 600)
            self.j.append("session.started", "lead", {})
            self.j.append("turn.started", "lead", {"marker": f"m{e['seq']}"}, ts=time.time() - 599)   # delivered
            self.settle()
            lag = json.loads(self.req("GET", "/api/backlog?slug=sso")[1])["facts"]["lag"]
            self.assertEqual((lag["owner"], lag["delegations"], lag["age"] // 60), (1, 0, 10))
            self.web._dir_tasks = None   # the board summary is cached for 30 s
            snap = json.loads(self.req("GET", "/api/snapshot")[1])
            self.assertEqual((snap["direction_tasks"]["sso"]["working"], snap["direction_tasks"]["sso"]["unsorted"]),
                             (1, 0))
        finally:
            os.environ.pop("HUB_DIRECTIONS", None) if old is None else os.environ.__setitem__("HUB_DIRECTIONS", old)

    def test_direction_board_one_column_per_status_whatever_the_case(self):
        """A card written «done» (a session's `-s done`) stands in the «Done» column and counts as closed."""
        d = tempfile.mkdtemp(prefix="web-board-")
        root = os.path.join(d, "directions")
        tasks = os.path.join(root, "sso", "backlog", "tasks")
        os.makedirs(tasks)
        with open(os.path.join(root, "sso", "backlog", "config.yml"), "w") as f:
            f.write('statuses: ["To Do", "In Progress", "Done"]\n')
        with open(os.path.join(root, "sso", "direction.json"), "w") as f:
            json.dump({"repo": "/x"}, f)
        for n, status in ((1, "Done"), (2, "done"), (3, "in progress")):
            with open(os.path.join(tasks, f"task-{n} - X.md"), "w") as f:
                f.write(f"---\nid: TASK-{n}\ntitle: X{n}\nstatus: {status}\nupdated_date: '2026-10-0{n} 09:00'\n---\n")
        old = os.environ.get("HUB_DIRECTIONS")
        os.environ["HUB_DIRECTIONS"] = root
        try:
            self.seed()
            board = json.loads(self.req("GET", "/api/backlog?slug=sso")[1])
            self.assertEqual(sorted((t["id"], t["status"]) for t in board["tasks"]),
                             [("TASK-1", "Done"), ("TASK-2", "Done"), ("TASK-3", "In Progress")])
            self.web._dir_tasks = None
            info = json.loads(self.req("GET", "/api/snapshot")[1])["direction_tasks"]["sso"]
            self.assertEqual((info["open"], info["doing"]), (1, ["X3"]))
            self.assertEqual(sorted(t["id"] for t in info["tasks"] if t["status"] == "Done"), ["TASK-1", "TASK-2"])
        finally:
            os.environ.pop("HUB_DIRECTIONS", None) if old is None else os.environ.__setitem__("HUB_DIRECTIONS", old)

    def test_reactions_only_with_the_module_on(self):
        """"Reactions" is a module, off by default (owner decision 2026-10-13): without it the snapshot has no icons
        and the page does not send a reaction; it is enabled on "Modules" — reactions.enabled.
        A client learns about the module from the snapshot's features, as about push (EXTENDING.md)."""
        from modules.web import modinfo, settings
        self.seed()
        e = self.j.append("message.received", None, {"to": "a", "text": "do it", "channel": "web", "owner": True})
        self.j.append("turn.started", "a", {"marker": f"m{e['seq']}"})
        self.j.append("turn.result", "a", {"text": "Done."})
        self.settle()
        snap = json.loads(self.req("GET", "/api/snapshot")[1])
        # folding what is answered is the page's checkbox (2026-10-14): which event is answered up to — always, no
        # icons
        self.assertEqual((snap["reactions"], snap["features"]), ({"taken": {"a": e["seq"] + 1}}, ["upload.raw", "accounts.use", "send.tasks", "feed.post_by_id", "bus", "about.addresses", "about.lite", "send.app", "setup", "feed.on_behalf", "session.resume", "stream.delta2", "stream.feed", "page.password", "file.thumb"]))
        self.assertNotIn("reaction", snap["messages"][f"m{e['seq']}"])
        code, _ = self.req("POST", "/api/react", {"session": "a", "target": "5", "emoji": "👍"})
        self.assertEqual(code, 409)
        self.assertEqual([e for e in self.spooled() if e["type"] == "message.reacted"], [])
        card = lambda cfg: next(m for m in modinfo.modules(self.state, cfg, {"sessions": []}) if m["id"] == "reactions")  # noqa: E731
        self.assertEqual((card({})["state"], card({})["enabled"]), (modinfo.OFF, False))
        self.assertEqual((card({"reactions": {"enabled": True}})["state"], card({"reactions": {"enabled": True}})["enabled"]),
                         (modinfo.ON, True))
        self.assertEqual(modinfo.TOGGLES["reactions"], "reactions.enabled")
        self.assertEqual(settings.validate("reactions.enabled", True), (True, None))
        self.assertFalse(settings.reactions_on({"reactions": {"enabled": "yes"}}))
        self.web.config = lambda: {"reactions": {"enabled": True}}
        snap = json.loads(self.req("GET", "/api/snapshot")[1])
        self.assertIn("reactions", snap["features"])
        self.assertEqual(snap["reactions"]["badges"], {f"m{e['seq']}": "👍"})
        code, _ = self.req("POST", "/api/react", {"session": "a", "target": "5", "emoji": "👍", "quote": "Done."})
        self.assertEqual(code, 202)
        (ev,) = [e for e in self.spooled() if e["type"] == "message.reacted"]
        self.assertEqual((ev["data"]["target"], ev["data"]["emoji"], ev["data"]["session"], ev["data"]["owner"]),
                         ("5", "👍", "a", True))

    def test_voice_only_when_configured(self):
        """The "Voice" module (owner request 2026-09-30): configured — the page knows (microphone), recording → text;
        not configured — 404."""
        import base64
        import sys
        self.seed()
        _, snap = self.req("GET", "/api/snapshot")
        self.assertFalse(json.loads(snap)["voice"])
        self.assertEqual(self.req("POST", "/api/voice", {"data": "eA=="})[0], 404)
        d = tempfile.mkdtemp(prefix="web-voice-")
        ffmpeg = exe.script(os.path.join(d, "ffmpeg"), "import sys, shutil\na = sys.argv\nshutil.copy(a[a.index('-i') + 1], a[-1])\n")
        asr = exe.script(os.path.join(d, "asr"),
                         "import sys, json\nprint(json.dumps({'text': 'text ' + open(sys.argv[1]).read()}))\n")
        self.web.config = lambda: {"voice": {"cmd": f"{asr} {{wav}}", "ffmpeg": ffmpeg}}
        _, snap = self.req("GET", "/api/snapshot")
        self.assertTrue(json.loads(snap)["voice"])
        code, out = self.req("POST", "/api/voice", {"data": base64.b64encode(b"rec").decode(), "mime": "audio/webm"})
        self.assertEqual((code, json.loads(out)), (200, {"text": "text rec"}))
        self.assertEqual(self.spooled(), [])  # nothing went to the session: the text goes into the field, the owner sends it
        # the sound type follows the shared voice table (mp3, flac are accepted); no type or an unknown one means a
        # browser recording (webm)
        from unittest import mock
        from modules.voice import transcribe as voice
        seen = []
        real = voice.transcribe
        with mock.patch.object(voice, "transcribe", lambda raw, cfg, suffix, **kw: seen.append(suffix) or real(raw, cfg, suffix, **kw)):
            for mime in ("audio/mpeg", "audio/flac", "audio/ogg; codecs=opus", None, "video/webm"):
                code, out = self.req("POST", "/api/voice", {"data": base64.b64encode(b"rec").decode(), "mime": mime})
                self.assertEqual((code, json.loads(out)), (200, {"text": "text rec"}), mime)
        self.assertEqual(seen, [".mp3", ".flac", ".ogg", ".webm", ".webm"])

    def test_voice_type_follows_the_bytes_when_the_browser_says_nothing_true(self):
        """Safari records audio/mp4 (AAC), while the page substituted audio/webm for an empty mimeType: the extension
        follows the recording's header."""
        import base64
        from unittest import mock
        from modules.voice import transcribe as voice
        self.seed()
        self.web.config = lambda: {"voice": {"cmd": "asr {wav}", "ffmpeg": "ffmpeg"}}
        heads = {".m4a": b"\0\0\0\x1cftypisom\0\0\2\0", ".ogg": b"OggS\0\2" + b"\0" * 20,
                 ".webm": b"\x1a\x45\xdf\xa3" + b"\0" * 20, ".wav": b"RIFF\0\0\0\0WAVEfmt "}
        seen = []
        with mock.patch.object(voice, "transcribe", lambda raw, cfg, suffix, **kw: seen.append(suffix) or "ok"):
            for want, head in heads.items():
                for mime in ("audio/webm", None, "audio/mp4"):   # the page could lie about the type
                    code, _ = self.req("POST", "/api/voice", {"data": base64.b64encode(head).decode(), "mime": mime})
                    self.assertEqual(code, 200)
        self.assertEqual(seen, [s for s in heads for _ in range(3)])

    def test_failed_voice_recordings_are_all_kept_and_playable(self):
        """Recognition failed (for example, ffmpeg does not start) while the recording lived only in the request.
        Now every failed recording is kept, not just the last one, and the reply carries not a path on the hub's disk
        (it cannot be opened from a phone) but an address from which the client can play it."""
        import base64
        from urllib.parse import quote
        self.seed()
        self.web.config = lambda: {"voice": {"cmd": "/no/asr {wav}", "ffmpeg": "/no/ffmpeg"}}
        got = []
        for raw, mime in ((b"one", "audio/webm"), (b"two", "audio/webm"), (b"three", "audio/mp4")):   # within one second
            code, out = self.req("POST", "/api/voice", {"data": base64.b64encode(raw).decode(), "mime": mime})
            self.assertEqual(code, 502)
            self.assertNotIn(self.state.encode(), out)   # neither a path nor the state folder
            self.assertNotIn(b"/Users/", out)
            body = json.loads(out)
            self.assertEqual(set(body), {"error", "id", "file", "reason"})
            self.assertEqual(body["error"], self.web.t("web.srv.voice_kept"))
            self.assertEqual(body["file"], "/api/voice/failed?id=" + quote(body["id"]))
            got.append((raw, body))
        kept = os.path.join(self.state, "voice-failed")
        self.assertEqual(sorted(os.listdir(kept)), sorted(b["id"] for _, b in got))   # all three, each with its own id
        for (raw, body), kind in zip(got, ("audio/webm", "audio/webm", "audio/mp4")):
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            c.request("GET", body["file"], headers={"Host": f"127.0.0.1:{self.port}"})
            r = c.getresponse()
            self.assertEqual((r.status, r.read(), r.getheader("Content-Type")), (200, raw, kind))
            c.close()
        first = got[0][1]["file"]
        self.assertEqual(self.req("GET", first, host="evil.example:80")[0], 403)   # not to a non-owner
        for bad in ("../journal.jsonl", "x.webm", got[0][1]["id"] + "/", "", "20261001-120000.txt"):
            self.assertEqual(self.req("GET", "/api/voice/failed?id=" + quote(bad))[0], 404, bad)
        with open(os.path.join(self.state, "20261001-120000.webm"), "wb") as f:   # next to the folder — not in it
            f.write(b"x")
        self.assertEqual(self.req("GET", "/api/voice/failed?id=" + quote("../20261001-120000.webm"))[0], 404)

    def test_failed_voice_says_where_it_went_and_what_was_heard(self):
        """The failure reply serves a decision from a phone (owner request 2026-10-26): where the owner was speaking
        and what that conversation is about (the addressee's gist in one line), and what the engine managed to
        transcribe before the failure. Unknown addressee — no context field; no partial text — no partial field."""
        import base64
        self.seed()
        self.j.append("session.gist", "a", {"text": "Fixes serving of voice notes", "channel": "session", "sender": "a"})
        self.settle()
        d = tempfile.mkdtemp(prefix="web-voice-")
        ffmpeg = exe.script(os.path.join(d, "ffmpeg"), "import sys, shutil\na = sys.argv\nshutil.copy(a[a.index('-i') + 1], a[-1])\n")
        cut = exe.script(os.path.join(d, "asr"), "import sys, json\nprint(json.dumps({'partial': 'buy bread and'}))\n"
                                                 "sys.exit('looping transcription')\n")
        self.web.config = lambda: {"voice": {"cmd": f"{cut} {{wav}}", "ffmpeg": ffmpeg}}
        rec = base64.b64encode(b"rec").decode()
        code, out = self.req("POST", "/api/voice", {"data": rec, "mime": "audio/webm", "session": "a"})
        body = json.loads(out)
        self.assertEqual(code, 502)
        self.assertEqual(body["partial"], "buy bread and")
        self.assertEqual(body["context"], self.web.t("web.srv.voice_context", session="a", topic="Fixes serving of voice notes"))
        self.assertEqual(set(body), {"error", "id", "file", "reason", "context", "partial"})
        # no addressee and no partial text — only error, id, file
        self.web.config = lambda: {"voice": {"cmd": "/no/asr {wav}", "ffmpeg": ffmpeg}}
        for extra in ({}, {"session": "no-such"}, {"session": ["a"]}):
            code, out = self.req("POST", "/api/voice", {"data": rec, "mime": "audio/webm", **extra})
            self.assertEqual((code, set(json.loads(out))), (502, {"error", "id", "file", "reason"}), extra)

    def test_failed_voice_says_why_in_a_few_words_without_paths(self):
        """Owner request 2026-10-26: from a phone "could not make it out" is not enough — a short reason is needed
        (the reason field): ffmpeg not found, recognition did not respond, etc. Only the hub's words for the reason
        code: the program path, the state folder and the raw command output go to the hub log, not into the reply."""
        import base64
        self.seed()
        d = tempfile.mkdtemp(prefix="web-voice-")
        ffmpeg = exe.script(os.path.join(d, "ffmpeg"), "import sys, shutil\na = sys.argv\nshutil.copy(a[a.index('-i') + 1], a[-1])\n")
        bad = exe.script(os.path.join(d, "asr"), "import sys\nprint('SECRET-OUTPUT /home/x/key', file=sys.stderr)\nsys.exit(3)\n")
        rec = base64.b64encode(b"rec").decode()
        for cfg, why in (({"cmd": "/no/asr {wav}", "ffmpeg": "/no/ffmpeg"}, "ffmpeg_start"),
                         ({"cmd": "/no/asr {wav}", "ffmpeg": ffmpeg}, "engine_start"),
                         ({"cmd": f"{bad} {{wav}}", "ffmpeg": ffmpeg}, "engine_failed")):
            self.web.config = lambda cfg=cfg: {"voice": cfg}
            code, out = self.req("POST", "/api/voice", {"data": rec, "mime": "audio/webm"})
            body = json.loads(out)
            self.assertEqual((code, body["reason"]), (502, self.web.t("web.srv.voice_why_" + why)), why)
            for secret in (b"SECRET-OUTPUT", b"/home/x", b"/no/", d.encode(), self.state.encode()):
                self.assertNotIn(secret, out, why)
        from unittest import mock
        from modules.voice import transcribe as voice
        with mock.patch.object(voice, "transcribe", side_effect=voice.VoiceError("odd: /home/x/y")):   # the reason code is unknown
            code, out = self.req("POST", "/api/voice", {"data": rec, "mime": "audio/webm"})
        self.assertEqual((code, json.loads(out)["reason"]), (502, self.web.t("web.srv.voice_why_other")))
        self.assertNotIn(b"/home/x", out)

    def test_failed_voice_recordings_are_pruned_by_age_and_size(self):
        """All failed recordings are kept, but not forever: those older than a week and those beyond the size cap
        (oldest first) are deleted at the next failure."""
        import base64
        from unittest import mock
        self.seed()
        self.web.config = lambda: {"voice": {"cmd": "/no/asr {wav}", "ffmpeg": "/no/ffmpeg"}}
        kept = os.path.join(self.state, "voice-failed")
        os.makedirs(kept)
        now = time.time()
        for name, age in (("20261001-120000.webm", 8 * 86400), ("20261018-120000.webm", 6 * 86400),
                          ("20261019-120000-abcdef.webm", 5 * 86400)):
            with open(os.path.join(kept, name), "wb") as f:
                f.write(b"x" * 10)
            os.utime(os.path.join(kept, name), (now - age, now - age))
        code, out = self.req("POST", "/api/voice", {"data": base64.b64encode(b"new").decode(), "mime": "audio/webm"})
        self.assertEqual(code, 502)
        new = json.loads(out)["id"]
        self.assertEqual(sorted(os.listdir(kept)), sorted(["20261018-120000.webm", "20261019-120000-abcdef.webm", new]))
        with mock.patch.object(type(self.web), "VOICE_FAILED_BYTES", 15):   # the size cap: the oldest go first
            code, out = self.req("POST", "/api/voice", {"data": base64.b64encode(b"next").decode(), "mime": "audio/webm"})
        self.assertEqual(sorted(os.listdir(kept)), sorted([new, json.loads(out)["id"]]))

    def test_a_retried_failed_recording_is_kept_once(self):
        """"Retry transcription" and a client retrying a 502 send the same recording again: one copy and the same id —
        otherwise retries fill the size cap and push out other failed recordings. A different recording gets its own
        copy."""
        import base64
        self.seed()
        self.web.config = lambda: {"voice": {"cmd": "/no/asr {wav}", "ffmpeg": "/no/ffmpeg"}}
        kept = os.path.join(self.state, "voice-failed")
        ids = []
        for raw in (b"same", b"same", b"same", b"other"):
            code, out = self.req("POST", "/api/voice", {"data": base64.b64encode(raw).decode(), "mime": "audio/webm"})
            self.assertEqual(code, 502)
            ids.append(json.loads(out)["id"])
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(ids[0], ids[2])
        self.assertNotEqual(ids[0], ids[3])
        self.assertEqual(sorted(os.listdir(kept)), sorted({ids[0], ids[3]}))
        # an unfinished copy from a parallel request (.part) does not count toward the size and goes only by age
        from unittest import mock
        old = time.time() - 8 * 86400
        for n, when in ((".fresh.part", None), (".old.part", old)):
            with open(os.path.join(kept, n), "wb") as f:
                f.write(b"x" * 100)
            if when:
                os.utime(os.path.join(kept, n), (when, when))
        with mock.patch.object(type(self.web), "VOICE_FAILED_BYTES", 20):
            code, out = self.req("POST", "/api/voice", {"data": base64.b64encode(b"same").decode(), "mime": "audio/webm"})
        self.assertEqual(json.loads(out)["id"], ids[0])
        self.assertEqual(sorted(os.listdir(kept)), sorted({ids[0], ids[3], ".fresh.part"}))

    def test_voice_without_speech_is_422_not_a_failure(self):
        """No speech in the recording is a result, not a failure: a client took a 502 for a lost connection and sent
        the same recording hundreds of times. Now 422 with a reason; the recording is not kept and does not push out
        the last failed one."""
        import base64
        self.seed()
        d = tempfile.mkdtemp(prefix="web-voice-")
        ffmpeg = exe.script(os.path.join(d, "ffmpeg"), "import sys, shutil\na = sys.argv\nshutil.copy(a[a.index('-i') + 1], a[-1])\n")
        asr = exe.script(os.path.join(d, "asr"), "import json\nprint(json.dumps({'text': '  '}))\n")
        kept = os.path.join(self.state, "voice-failed")
        os.makedirs(kept)
        with open(os.path.join(kept, "20261001-120000.webm"), "wb") as f:
            f.write(b"real")
        self.web.config = lambda: {"voice": {"cmd": f"{asr} {{wav}}", "ffmpeg": ffmpeg}}
        code, out = self.req("POST", "/api/voice", {"data": base64.b64encode(b"rec").decode(), "mime": "audio/webm"})
        self.assertEqual(code, 422)
        self.assertEqual(json.loads(out), {"error": self.web.t("web.srv.no_speech")})
        self.assertEqual(os.listdir(kept), ["20261001-120000.webm"])
        # a real engine failure is still 502
        self.web.config = lambda: {"voice": {"cmd": "/no/asr {wav}", "ffmpeg": ffmpeg}}
        self.assertEqual(self.req("POST", "/api/voice", {"data": base64.b64encode(b"rec").decode(), "mime": "audio/webm"})[0], 502)

    def test_voice_too_long_says_so_with_the_limit_in_the_language_of_the_page(self):
        """A recording above the limit is a 413 that names the limit and what to do, not "empty or too long" and not
        "not JSON" (the body was cut at twice the limit): the page shows this text as is, in its own language."""
        import base64
        from core import i18n
        from modules.web import modproxy
        from modules.web.server import MAX_FILE
        self.seed()
        for lang in ("es", "ru"):
            self.web.config = lambda lang=lang: {"voice": {"cmd": "true", "ffmpeg": "true"}, "language": lang}
            size = modproxy.size_text(MAX_FILE, lang)
            want = i18n.tr(lang, "web.srv.recording_too_long", size=size)
            self.assertIn(size, want)                                     # the limit in words
            self.assertNotIn("recording_too_long", want)                  # the key exists in the catalog
            for what, n in (("just above the limit", MAX_FILE + 1), ("a body the reader used to cut", MAX_FILE * 8 // 5)):
                code, out = self.req("POST", "/api/voice", {"data": base64.b64encode(b"\0" * n).decode(), "mime": "audio/webm"})
                self.assertEqual((code, json.loads(out)["error"]), (413, want), (lang, what))
        # not the English fallback: the page language is es / ru
        self.assertNotEqual(i18n.tr("es", "web.srv.recording_too_long", size="20 MB"), i18n.tr("en", "web.srv.recording_too_long", size="20 MB"))
        self.assertNotEqual(i18n.tr("ru", "web.srv.recording_too_long", size="20 MB"), i18n.tr("en", "web.srv.recording_too_long", size="20 MB"))
        # an empty recording is its own answer, not "too long"
        code, out = self.req("POST", "/api/voice", {"data": ""})
        self.assertEqual((code, json.loads(out)["error"]), (400, i18n.tr("ru", "web.srv.recording_empty")))
        # a length that str.isdigit takes and int does not ("²"): an answer, not a dropped connection (release review)
        for length in (chr(0xb2), "9" * 5000):   # and more digits than int() reads
            self.assertEqual(self.req("POST", "/api/voice", {}, headers={"Content-Length": length})[0], 400)

    def test_file_thumbnail_of_a_transparent_picture_is_a_png(self):
        """/api/file?w=<px> of a picture with an alpha channel (a window screenshot with its shadow) is a png named
        .png, with its own tag (its -w<px> tail stays: clients read it): a jpeg copy a browser kept from before (white
        where the picture was transparent) is not confirmed by a 304. Every answer says X-Hub-Thumb: the width of
        the copy, 0 for the original — a 304 too."""
        import base64
        import subprocess
        from unittest import mock
        from urllib.parse import quote
        from modules.files import server as fsrv
        alpha = ["yes"]

        def fake_sips(cmd, **_):
            if cmd[1] == "-g":
                return subprocess.CompletedProcess(cmd, 0, f"x\n  pixelWidth: 400\n  pixelHeight: 200\n  hasAlpha: {alpha[0]}\n", "")
            with open(cmd[-1], "wb") as f:
                f.write(b"\x89PNG small" if "png" in cmd else b"\xff\xd8\xff small")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        self.seed()
        code, meta = self.req("POST", "/api/upload", {"name": "shot.png", "mime": "image/png",
                                                      "data": base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 4000).decode()})
        self.assertEqual(code, 200)
        url = "/api/file?path=" + quote(json.loads(meta)["path"]) + "&w=120"

        def get(headers=None, url=url):
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            c.request("GET", url, headers={"Host": f"127.0.0.1:{self.port}", "X-Hub": "1", **(headers or {})})
            r = c.getresponse()
            body = r.read()
            c.close()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, body
        with mock.patch.object(fsrv.shutil, "which", return_value="/x/sips"), \
                mock.patch.object(fsrv.subprocess, "run", side_effect=fake_sips):
            code, head, body = get()
            self.assertEqual((code, head["content-type"], body), (200, "image/png", b"\x89PNG small"))
            self.assertIn('filename="shot.png"', head["content-disposition"])
            self.assertEqual(head["x-hub-thumb"], "120")
            self.assertTrue(head["etag"].endswith('-w120"'), head["etag"])
            code, again, _ = get({"If-None-Match": head["etag"]})
            self.assertEqual((code, again["x-hub-thumb"]), (304, "120"))
            self.assertEqual(get({"If-None-Match": head["etag"].replace("-png", "")})[0], 200)   # a jpeg's tag, before
            code, head, body = get(url=url.split("&w=")[0])   # the original: no copy
            self.assertEqual((code, head["content-type"], head["x-hub-thumb"], len(body)), (200, "image/png", "0", 4008))

    def test_file_thumbnail_and_etag(self):
        """/api/file?w=<px> (feature file.thumb): a raster picture scaled by width as a jpeg named .jpg, with a tag of its
        source; every /api/file answer has an ETag and a repeat with If-None-Match is a 304; never enlarged; a file
        whose content is not a picture is not given to any tool, whatever its name."""
        import base64
        import subprocess
        from unittest import mock
        from urllib.parse import quote
        from modules.files import server as fsrv
        calls = []

        def fake_sips(cmd, **_):   # reads the dimensions, writes a small jpeg
            calls.append(cmd[1])
            if cmd[1] == "-g":
                return subprocess.CompletedProcess(cmd, 0, "x\n  pixelWidth: 400\n  pixelHeight: 200\n", "")
            with open(cmd[-1], "wb") as f:
                f.write(b"\xff\xd8\xff small")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 4000
        self.seed()

        def upload(name, data):
            code, meta = self.req("POST", "/api/upload", {"name": name, "mime": "image/png",
                                                          "data": base64.b64encode(data).decode()})
            self.assertEqual(code, 200)
            return "/api/file?path=" + quote(json.loads(meta)["path"])

        def get(path, headers=None):
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            c.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}", "X-Hub": "1", **(headers or {})})
            r = c.getresponse()
            body = r.read()
            c.close()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, body

        url, bad = upload("wide.png", png), upload("script.png", b"%!PS-Adobe-3.0\n" + b"0" * 4000)
        self.assertIn("file.thumb", self.snap()["features"])
        code, head, whole = get(url)
        self.assertEqual((code, whole), (200, png))
        self.assertEqual(get(url, {"If-None-Match": head["etag"]})[0::2], (304, b""))
        # the owner's files are kept only by the one who asked, revalidated each time (a download too)
        self.assertEqual(head["cache-control"], "private, no-cache")
        with mock.patch.object(fsrv.shutil, "which", return_value="/x/sips"), \
                mock.patch.object(fsrv.subprocess, "run", side_effect=fake_sips):
            code, small_head, small = get(url + "&w=100")   # asked 100: the nearest step up, 120
            self.assertEqual((code, small_head["content-type"], small), (200, "image/jpeg", b"\xff\xd8\xff small"))
            self.assertIn('filename="wide.jpg"', small_head["content-disposition"])
            self.assertNotEqual(small_head["etag"], head["etag"])
            # a copy made again (a week later, a newer sips) may differ in bytes: its tag is weak, so a piece of it is
            # never stitched onto a piece of the earlier copy
            self.assertTrue(small_head["etag"].startswith('W/"'))
            for asked in ({"Range": "bytes=0-3", "If-Range": small_head["etag"]}, {"Range": "bytes=4-"}):
                self.assertEqual(get(url + "&w=100", asked)[0::2], (200, b"\xff\xd8\xff small"))
            # If-None-Match compares weakly (RFC 9110): the same tag without W/ is the same picture
            self.assertEqual(get(url + "&w=100", {"If-None-Match": small_head["etag"][2:]})[0], 304)
            # the tag is of the source: a repeat (the copy's own time moves on every view) is still a 304
            self.assertEqual(get(url + "&w=100", {"If-None-Match": small_head["etag"]})[0::2], (304, b""))
            self.assertEqual(oct(os.stat(os.path.join(self.state, "cache", "files-jpeg")).st_mode & 0o077) if os.name == "posix" else "0o0", "0o0")
            self.assertEqual(get(url + "&w=400")[2], png)   # not wider than asked: the original, never enlarged
            self.assertEqual(get(url + "&w=abc")[2], png)
            tried = len(calls)
            self.assertEqual(get(url + "&w=400")[2], png)   # the refusal is remembered: sips is not run again
            self.assertEqual(len(calls), tried)
            n = len(calls)
            self.assertEqual(get(bad + "&w=100")[2][:6], b"%!PS-A")   # by content not a picture: no tool at all
            self.assertEqual(len(calls), n)

    def test_upload_file_and_send_with_attachment(self):
        import base64
        self.seed()
        png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode()
        code, meta = self.req("POST", "/api/upload", {"name": "../../écran 1.png", "mime": "image/png", "data": png})
        self.assertEqual(code, 200)
        meta = json.loads(meta)
        self.assertTrue(meta["path"].startswith(os.path.join(self.state, "attachments")))
        self.assertEqual(os.path.basename(meta["path"]), "écran 1.png")  # without escaping the folder
        from urllib.parse import quote
        self.assertEqual(self.req("GET", "/api/file?path=" + quote(meta["path"]))[0], 200)
        self.assertEqual(self.req("GET", "/api/file?path=/etc/hosts")[0], 404)
        forged = {"path": "/etc/hosts", "name": "hosts", "mime": "text/plain", "size": 1}
        # a foreign path is rejected with a reason, not silently dropped (the client would think the file went);
        # nothing in the mailbox
        code, out = self.req("POST", "/api/send", {"session": "a", "text": "", "attachments": [meta, forged]})
        self.assertEqual(code, 400)
        self.assertIn("hosts", json.loads(out)["error"])
        self.assertEqual(self.spooled(), [])
        for bad in ("/etc/hosts", {"name": "x"}):
            self.assertEqual(self.req("POST", "/api/send", {"session": "a", "text": "x", "attachments": [bad]})[0], 400)
        self.assertEqual(self.req("POST", "/api/send", {"session": "a", "text": "x", "attachments": "x"})[0], 400)
        self.assertEqual(self.spooled(), [])
        code, _ = self.req("POST", "/api/send", {"session": "a", "text": "", "attachments": [meta]})
        self.assertEqual(code, 202)
        (ev,) = self.spooled()
        self.assertEqual([f["path"] for f in ev["data"]["attachments"]], [meta["path"]])
        self.assertEqual(ev["data"]["text"], "(attachments)")

    def test_image_a_session_worked_with_is_served_by_the_page(self):
        # 2026-10-11, owner: an image in the thread is broken — the session's screenshot lies in a temporary folder
        # and "Files" (:8788) does not serve it to remote clients over tailnet. The page serves images whose paths
        # appear in session steps and replies itself — and only those
        self.seed()
        d = tempfile.mkdtemp(prefix="shots-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        read, named, other, text = (os.path.join(d, n) for n in ("read.png", "named.jpg", "other.png", "notes.txt"))
        for p in (read, named, other, text):
            with open(p, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        from urllib.parse import quote
        self.assertEqual(self.req("GET", "/api/file?path=" + quote(read))[0], 404)
        self.j.append("turn.tool", "a", {"id": "t1", "name": "Read", "input": {"file_path": read}})
        self.j.append("turn.tool", "a", {"id": "t2", "name": "Read", "input": {"file_path": text}})
        self.j.append("turn.tool", "a", {"id": "t3", "name": "shot", "input": {}})
        self.j.append("turn.tool_result", "a", {"id": "t3", "text": f"Screenshot saved to {named}", "error": False})
        self.settle()
        for p in (read, named):
            self.assertEqual(self.req("GET", "/api/file?path=" + quote(p))[1][:4], b"\x89PNG", p)
        self.assertEqual(self.req("GET", "/api/file?path=" + quote(other))[0], 404)   # a neighbor — the session did not name it
        self.assertEqual(self.req("GET", "/api/file?path=" + quote(text))[0], 404)    # not an image

    @unittest.skipUnless(os.name == "nt", "a drive letter — Windows")
    def test_image_named_by_a_file_url_with_a_drive_is_served_as_the_page_asks_for_it(self):
        # PR #180, review: file:///D:/x.png — the index has "D:/x.png", the page asks for "/D:/x.png" (a URL path)
        self.seed()
        d = tempfile.mkdtemp(prefix="shots-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        png = os.path.join(d, "w.png")
        with open(png, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        from urllib.parse import quote
        url = "file:///" + png.replace(os.sep, "/")
        self.j.append("session.said", "a", {"text": f"screenshot {url}"})
        self.settle()
        for asked in ("/" + png.replace(os.sep, "/"), png.replace(os.sep, "/"), png):
            self.assertEqual(self.req("GET", "/api/file?path=" + quote(asked))[1][:4], b"\x89PNG", asked)

    def test_a_picture_far_back_in_the_feed_and_only_a_picture(self):
        # PR #116, follow-up: an image that fell out of the feed window (400 lines) is served too; svg is not (it
        # may contain a script)
        self.seed()
        d = tempfile.mkdtemp(prefix="shots-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        old, svg = os.path.join(d, "old.png"), os.path.join(d, "icon.svg")
        for p in (old, svg):
            with open(p, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        # portable: only the text of a journal event; the paths are opened by the product, not by this URL
        self.j.append("session.said", "a", {"text": f"screenshot: file://{old}, icon: {svg}"})
        for n in range(450):
            self.j.append("session.said", "a", {"text": f"line {n}"})
        self.settle()
        from urllib.parse import quote
        self.assertEqual(self.req("GET", "/api/file?path=" + quote(old))[0], 200)
        self.assertEqual(self.req("GET", "/api/file?path=" + quote(svg))[0], 404)

    def test_what_a_session_showed_before_the_journal_rotated_is_still_served(self):
        # 2026-10-17: a file sent by a session (hub send-file) outside attachments/ and an image named in a step,
        # from before a journal rotation — /api/file answered 404 (the shown list was kept only from the current
        # journal; on a live hub 237 of 734 history files opened). What the core rejected (core.rejected) and what
        # was never shown still is not served
        from core import reducer
        from urllib.parse import quote
        self.seed()
        d = tempfile.mkdtemp(prefix="sent-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        sent, named, refused, other = (os.path.join(d, n) for n in ("report.pdf", "shot.png", "refused.pdf", "other.png"))
        for p in (sent, named, refused, other):
            with open(p, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        self.j.append("owner.file", None, {"path": sent, "caption": "", "channel": "session", "owner": False, "sender": "a"})
        self.j.append("turn.tool", "a", {"id": "t1", "name": "Read", "input": {"file_path": named}})
        bad = self.j.append("owner.file", None, {"path": refused, "caption": "", "channel": "session", "owner": False,
                                                 "sender": "a"})
        self.j.append("core.rejected", None, {"of": bad["seq"], "reason": "test"})
        for n in range(30):
            self.j.append("session.said", "a", {"text": f"line {n}"})
        self.settle()
        for rnd in range(2):   # two rotations in a row: what was shown before the first survives the second too
            self.web.shown.refresh()   # the page reads the rest of the journal itself (shown.follow, once a second)
            journal.rotate(self.state, reducer.replay(journal.read(self.j.path)), keep=3, per_session=3)
            self.settle()
            self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
            self.j.append("session.said", "a", {"text": f"after rotation {rnd}"})
            self.settle()
        self.assertFalse([e for e in journal.read(self.j.path) if e["type"] in ("owner.file", "turn.tool")])
        self.assertEqual(self.head("/api/file?path=" + quote(sent))[0], 200)
        self.assertEqual(self.req("GET", "/api/file?path=" + quote(named))[1][:4], b"\x89PNG")
        for p in (refused, other):
            self.assertEqual(self.head("/api/file?path=" + quote(p))[0], 404, p)

    def test_a_file_a_session_sent_is_a_download_and_never_runs_or_goes_as_an_attachment(self):
        # a client opens what a session sent (hub send-file) of any type — PDF, text, archive, sound. Review
        # 2026-10-13: html and svg from the page's address are a script with its rights (it would call the hub API
        # as the owner), so everything except images and video is download-only: attachment, nosniff, a CSP sandbox;
        # and such a path does not pass as an attachment in /api/send — it is not an owner upload
        self.seed()
        d = tempfile.mkdtemp(prefix="sent-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        names = ("shot.png", "logo.svg", "page.html", "notes.md", "report.pdf", "data.tar.gz", "voice.ogg", "noext")
        paths = {n: os.path.join(d, n) for n in names}
        for p in paths.values():
            with open(p, "wb") as f:
                f.write(b"<html><script>fetch('/api/send',{method:'POST'})</script><svg onload=alert(1)>")
            self.j.append("owner.file", None, {"path": p, "caption": "", "channel": "session", "owner": False,
                                               "sender": "a"})
        named = os.path.join(d, "named.html")   # only named in the text, not sent — not served
        with open(named, "w") as f:
            f.write("<script></script>")
        self.j.append("session.said", "a", {"text": f"report: {named}"})
        self.j.append("owner.file", None, {"path": d, "caption": "", "channel": "session", "owner": False,
                                           "sender": "a"})   # a folder is not a file
        self.settle()
        from urllib.parse import quote
        got = {}
        for n, p in paths.items():
            code, head = self.head("/api/file?path=" + quote(p))
            self.assertEqual(code, 200, n)
            self.assertEqual(head["X-Content-Type-Options"], "nosniff", n)
            self.assertIn("sandbox", head["Content-Security-Policy"], n)
            got[n] = (head["Content-Disposition"] or "").split(";")[0]
        self.assertEqual(got, {"shot.png": "inline", "logo.svg": "attachment", "page.html": "attachment",
                               "notes.md": "attachment", "report.pdf": "attachment", "data.tar.gz": "attachment",
                               "voice.ogg": "inline", "noext": "attachment"})   # sound, video, raster — as attachments
        for p in (named, d):
            self.assertEqual(self.head("/api/file?path=" + quote(p))[0], 404, p)
        sent = {"path": paths["shot.png"], "name": "shot.png", "mime": "image/png", "size": 40}
        self.assertEqual(self.req("POST", "/api/send", {"session": "a", "text": "x", "attachments": [sent]})[0], 400)
        self.assertEqual([e for e in self.spooled() if e["type"] == "message.received"], [])   # not the owner's upload

    def head(self, path, headers=None, method="GET", body=None):
        """The status code and response headers as is (without X-Hub unless asked)."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, body, {"Host": f"127.0.0.1:{self.port}", **(headers or {})})
        r = c.getresponse()
        r.read()
        c.close()
        return r.status, r.headers

    def test_attachments_are_downloads_unless_media(self):
        # an attachment is never a page at the hub's address: html, svg and the rest are download-only, sandboxed,
        # with no type sniffing; raster images, sound and video the browser shows itself
        from urllib.parse import quote
        own = os.path.join(self.state, "attachments")
        os.makedirs(own, exist_ok=True)

        def got(name):
            path = os.path.join(own, name)
            with open(path, "wb") as f:
                f.write(b"<svg onload=alert(1)>")
            code, head = self.head("/api/file?path=" + quote(path))
            self.assertEqual(code, 200, name)
            self.assertIn("sandbox", head["Content-Security-Policy"], name)
            self.assertEqual(head["X-Content-Type-Options"], "nosniff", name)
            return (head["Content-Disposition"] or "inline").split(";")[0]
        for name in ("bill.html", "icon.svg", "x.xhtml", "notes.txt", "x.js", "x.pdf", "noext"):
            self.assertEqual(got(name), "attachment", name)
        for name in ("photo.png", "photo.jpg", "a.webp", "a.gif", "clip.mp4", "voice.ogg", "a.mp3"):
            self.assertEqual(got(name), "inline", name)

    def test_theme_files_never_run(self):
        # a theme icon (svg) and art as an open document — without scripts and without type sniffing
        from unittest import mock
        conf = tempfile.mkdtemp(prefix="conf-")
        self.addCleanup(platforms.current.rmtree, conf)
        own = os.path.join(conf, "themes")
        os.makedirs(own)
        with open(os.path.join(own, "t.json"), "w") as f:
            json.dump({"icons": {"bot": "bot.svg"}, "art": {"image": "art.png"}}, f)
        with open(os.path.join(own, "bot.svg"), "w") as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
        with open(os.path.join(own, "art.png"), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")
        with mock.patch.dict(os.environ, {"HUB_CONFIG": os.path.join(conf, "hub.json")}):
            for path in ("/api/themes/file?id=own:t.json&f=bot.svg", "/api/themes/art?id=own:t.json"):
                code, head = self.head(path)
                self.assertEqual(code, 200, path)
                self.assertIn("sandbox", head["Content-Security-Policy"], path)
                self.assertEqual(head["X-Content-Type-Options"], "nosniff", path)

    def test_cross_site_requests_are_refused_but_links_open(self):
        # a foreign site cannot poke the hub page even with a request whose reply it does not read (<img>, fetch
        # no-cors, a form); following a link (from a chat, a tracker) opens the page as before
        no_cors = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"}
        go = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}
        self.assertEqual(self.head("/api/snapshot", no_cors)[0], 403)
        self.assertEqual(self.head("/api/snapshot", {**no_cors, "Sec-Fetch-Mode": "cors"})[0], 403)
        self.assertEqual(self.head("/mod/x/view/", no_cors)[0], 403)
        self.assertEqual(self.head("/api/send", {**go, "X-Hub": "1", "Content-Type": "application/json"}, "POST",
                                   b'{"session": "a", "text": "x"}')[0], 403)   # a form is also a navigation, but not a GET
        self.assertEqual(self.spooled(), [])
        self.assertEqual(self.head("/", go)[0], 200)
        for site in ("same-origin", "same-site", "none"):
            self.assertEqual(self.head("/api/snapshot", {"Sec-Fetch-Site": site, "Sec-Fetch-Mode": "cors"})[0], 200)
        self.assertEqual(self.head("/api/snapshot")[0], 200)   # a client without browser headers (an app, curl)

    def test_page_version_follows_plugin_code_not_its_data(self):
        # 2026-10-14, owner: why does the page keep reloading? — the page version was computed over all files of a
        # plugin folder, while its process rewrote its own data (out/*.json) every minute: tabs reloaded in a loop
        d = tempfile.mkdtemp(prefix="ext-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        files = {"main.js": "x", "hub-plugin.json": "{}", "out/lines.json": "{}", ".git/index": "1", "notes.txt": "a",
                 ".cache/build.js": "x"}
        for n, body in files.items():
            os.makedirs(os.path.dirname(os.path.join(d, n)), exist_ok=True)
            with open(os.path.join(d, n), "w") as f:
                f.write(body)
        self.web.extensions = lambda: [{"dir": d, "name": "x"}]

        def ver():
            self.web._ver_at = 0
            return self.web.version()
        v = ver()
        for n in ("out/lines.json", ".git/index", "notes.txt", ".cache/build.js"):
            with open(os.path.join(d, n), "w") as f:
                f.write("changed, longer")
            self.assertEqual(ver(), v, n)
        for n in ("main.js", "hub-plugin.json"):
            with open(os.path.join(d, n), "w") as f:
                f.write("changed, longer " + n)
            self.assertNotEqual(ver(), v, n)
            v = ver()

    def test_page_version_ignores_a_reinstall_of_the_same_code(self):
        # a plugin or the core reinstalled with the same code (a market update, a checkout) gets new file times: open
        # tabs reloaded and lost their place for nothing. The version follows the content, not the time
        d = tempfile.mkdtemp(prefix="ext-")
        self.addCleanup(platforms.current.rmtree, d)
        for n in ("main.js", "style.css", "hub-plugin.json"):
            with open(os.path.join(d, n), "w") as f:
                f.write("same " + n)
        self.web.extensions = lambda: [{"dir": d, "name": "x"}]

        def ver():
            self.web._ver_at = 0
            return self.web.version()
        v = ver()
        later = time.time() + 3600
        for n in ("main.js", "style.css", "hub-plugin.json"):
            with open(os.path.join(d, n), "w") as f:
                f.write("same " + n)
            os.utime(os.path.join(d, n), (later, later))
        self.assertEqual(ver(), v)
        with open(os.path.join(d, "main.js"), "w") as f:
            f.write("diff main.js")   # same size, other content
        # a new time, as a real write gives: on Windows st_ctime is the creation time, so size+times+inode of a file
        # rewritten with a forced equal mtime do not change there and its cached sum is reused (as with main's size+mtime)
        os.utime(os.path.join(d, "main.js"), (later + 60, later + 60))
        self.assertNotEqual(ver(), v)

    def test_page_version_follows_the_string_catalogs(self):
        # 2026-10-17: the page strings (i18n/*.json) changed on a server restart while the page code did not; open
        # tabs reload as they do for new code, otherwise they keep old strings until a manual reload
        from unittest import mock
        from core import i18n
        d = tempfile.mkdtemp(prefix="i18n-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        for n in ("en.json", "ru.json", "README.md", "pending.json", "sync.json"):
            with open(os.path.join(d, n), "w") as f:
                f.write("{}")
        self.web.extensions = lambda: []

        def ver():
            self.web._ver_at = 0
            with mock.patch.object(i18n, "ROOT", d):
                return self.web.version()
        v = ver()
        for n in ("README.md", "pending.json", "sync.json"):   # catalog service files are not page strings
            with open(os.path.join(d, n), "w") as f:
                f.write("changed, longer")
            self.assertEqual(ver(), v, n)
        with open(os.path.join(d, "ru.json"), "w") as f:
            f.write('{"web.x": "changed"}')
        self.assertNotEqual(ver(), v)

    def test_a_video_sent_to_the_owner_plays_from_the_page(self):
        # 2026-10-14: a hub send-file video reached Telegram but the page gave 404 "no such attachment". A sent
        # video the page serves with Range (seeking, Safari); what is merely named in text or a step is still served
        # only if it is an image
        self.seed()
        d = tempfile.mkdtemp(prefix="sent-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        paths = {n: os.path.join(d, n) for n in ("demo.mp4", "clip.webm", "take.MOV", "said.mp4")}
        for p in paths.values():
            with open(p, "wb") as f:
                f.write(b"\x00\x00\x00\x18ftypmp42" + b"0" * 100)
        for n in ("demo.mp4", "clip.webm", "take.MOV"):
            self.j.append("owner.file", None, {"path": paths[n], "caption": "", "channel": "session", "owner": False,
                                               "sender": "a"})
        self.j.append("session.said", "a", {"text": f"recording: {paths['said.mp4']}"})
        self.settle()
        from urllib.parse import quote
        got = {n: self.req("GET", "/api/file?path=" + quote(p))[0] for n, p in paths.items()}
        self.assertEqual(got, {"demo.mp4": 200, "clip.webm": 200, "take.MOV": 200, "said.mp4": 404})
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/file?path=" + quote(paths["demo.mp4"]),
                  headers={"Host": f"127.0.0.1:{self.port}", "Range": "bytes=4-11"})
        r = c.getresponse()
        body = r.read()
        c.close()
        self.assertEqual((r.status, body, r.getheader("Content-Type")), (206, b"ftypmp42", "video/mp4"))
        self.assertEqual(r.getheader("Content-Range"), "bytes 4-11/112")

    def test_over_tailnet_a_shown_picture_outside_projects_stays_a_link(self):
        # review 2026-10-13: over tailnet "Files" cannot open a screenshot from /tmp — /api/paths gave {}, the path
        # became text, and the thumbnail vanished on the next redraw
        self.seed()
        d = tempfile.mkdtemp(prefix="shots-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        shot, other = os.path.join(d, "page.png"), os.path.join(d, "other.png")
        for p in (shot, other):
            open(p, "wb").close()
        self.j.append("session.said", "a", {"text": f"screenshot: {shot}"})
        self.settle()
        got = self.web.resolve_paths([{"path": shot}, {"path": other}], local=False)
        from urllib.parse import unquote
        self.assertEqual(got[1], {})
        link = got[0]["file"]   # the path as named or the real one (on a Mac /var is a link to /private/var)
        self.assertTrue(link.startswith("/api/file?path="), link)
        self.assertEqual(os.path.realpath(unquote(link.split("=", 1)[1])), os.path.realpath(shot))
        self.assertEqual(self.req("GET", link)[0], 200)

    def test_an_absolute_path_on_the_page_port_leads_to_files_when_files_shows_it(self):
        # a link to a file typed as an address on the page's port (127.0.0.1:<page>/home/x/plan.md) answered 404; it
        # now goes to "Files" (its /open) when "Files" would show the file to this client, otherwise it is a 404 as before
        d = os.path.realpath(tempfile.mkdtemp(prefix="abs-"))
        self.addCleanup(platforms.current.rmtree, d)
        plan = os.path.join(d, "plan.md")
        with open(plan, "w", encoding="utf-8") as f:
            f.write("# plan\n")
        from urllib.parse import quote, unquote, urlsplit
        from modules.web.server import files_port
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", quote(plan), headers={"Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual(r.status, 302)
        where = urlsplit(r.getheader("Location"))
        self.assertEqual((where.scheme, where.hostname, where.port, where.path), ("", "127.0.0.1", files_port(self.state), "/open"))   # the page's scheme
        self.assertEqual(unquote(where.query), "path=" + plan)
        self.assertEqual(self.req("GET", quote(os.path.join(d, "missing.md")))[0], 404)   # nothing there
        self.assertEqual(self.req("GET", "/api/" + "nothing")[0], 404)
        # from the tailnet "Files" shows project folders only: this file is in none, so no redirect
        self.assertIsNone(self.web.files_target(plan, local=False))
        self.web.config = lambda: {"projects": {"docs": {"path": d}}}
        self.assertEqual(self.web.files_target(plan, local=False), plan)

    def test_file_sent_by_a_session_is_served_by_the_page(self):
        # 2026-10-08, owner: hub send-file images in the feed are broken over tailnet — "Files" (:8788) serves
        # remote clients only project folders, while the screenshot lies in ~/.claude/artifacts. The page itself
        # serves exactly the paths that were sent
        self.seed()
        d = tempfile.mkdtemp(prefix="sent-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        sent, other = os.path.join(d, "shot.png"), os.path.join(d, "other.png")
        for p in (sent, other):
            with open(p, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        from urllib.parse import quote
        self.assertEqual(self.req("GET", "/api/file?path=" + quote(sent))[0], 404)
        self.j.append("owner.file", None, {"path": sent, "caption": "here", "channel": "session", "owner": False,
                                           "sender": "a"})
        self.settle()
        code, body = self.req("GET", "/api/file?path=" + quote(sent))
        self.assertEqual((code, body[:4]), (200, b"\x89PNG"))
        self.assertEqual(self.req("GET", "/api/file?path=" + quote(other))[0], 404)  # a neighboring file was not sent

    def test_an_upload_named_dot_dot_stays_a_file(self):
        # ".." passed the name filter: the file was opened as a folder above the attachment and the connection dropped
        import base64
        data = base64.b64encode(b"x").decode()
        for name in ("..", ".", "/.."):
            code, meta = self.req("POST", "/api/upload", {"name": name, "mime": "text/plain", "data": data})
            self.assertEqual(code, 200, name)
            meta = json.loads(meta)
            self.assertTrue(os.path.isfile(meta["path"]), meta)
            self.assertEqual(os.path.dirname(os.path.dirname(meta["path"])), os.path.join(self.state, "attachments"))

    def test_a_post_body_that_is_not_an_object_gets_an_error(self):
        # a list or a number in JSON reached body.get(): the handler crashed and the connection dropped with no reply
        for body in ([1, 2], 7, "text"):
            for path in ("/api/send", "/api/paths", "/api/upload"):
                code, out = self.req("POST", path, body)
                self.assertEqual(code, 400, (path, body))
                self.assertIn("error", json.loads(out))

    def test_unreadable_numbers_in_the_address_get_an_answer(self):
        # int()/float() on the address were unprotected: a malformed link dropped the connection instead of replying
        for path in ("/api/flow/history?hours=abc", "/api/flow/history?hours=nan", "/api/flow/history?hours=-inf",
                     "/api/flow?since=x", "/api/feed?plugin=p&since=x", "/api/history?session=a&before=x&n=y",
                     "/api/timeline?session=a&since=1e999"):
            code, out = self.req("GET", path)
            self.assertEqual(code, 200, path)
            json.loads(out)

    def test_pasted_image_marker(self):
        """Owner request 2026-09-30: image pasting should use a marker like [Image 1], as claude code does — the page
        puts [Image #N] into the text, the attachment carries the same label, and the session sees which image is
        where."""
        import base64
        from core.reducer import attachments_note
        self.seed()
        png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode()
        meta = json.loads(self.req("POST", "/api/upload", {"name": "image.png", "mime": "image/png", "data": png})[1])
        code, _ = self.req("POST", "/api/send", {"session": "a", "text": "here [Image #1] button",
                                                 "attachments": [{**meta, "label": "Image #1"}, {**meta, "label": "<b>"}]})
        self.assertEqual(code, 202)
        (ev,) = self.spooled()
        self.assertEqual([f.get("label") for f in ev["data"]["attachments"]], ["Image #1", None])  # a foreign label — none
        note = attachments_note(ev["data"]["attachments"])
        self.assertIn(f"- [Image #1] {meta['path']} (image.png,", note)

    def test_answer_question(self):
        self.seed()
        q = {"questions": [{"question": "Which?", "options": [{"label": "A"}, {"label": "B"}]}]}
        self.j.append("turn.started", "a", {"marker": None})
        self.j.append("permission.requested", "a", {"request_id": "r1", "tool": "AskUserQuestion", "input": q})
        self.settle()
        self.assertEqual(self.req("POST", "/api/answer", {"session": "a", "request_id": "r1", "answers": {"Another?": "B"}})[0], 400)
        self.assertEqual(self.req("POST", "/api/answer", {"session": "a", "request_id": "r1", "answers": {"Which?": "B"}})[0], 202)
        (ev,) = self.spooled()
        self.assertEqual(ev["data"]["answers"], {"Which?": "B"})
        self.assertEqual(ev["data"]["behavior"], "allow")

    def test_send_wakes_only_when_asked(self):
        # your message in its field wakes an executor that has handed over (owner decision 2026-10-04); an ordinary
        # one has no mark
        self.seed()
        self.assertEqual(self.req("POST", "/api/send", {"session": "a", "text": "x", "wake": True})[0], 202)
        self.assertEqual(self.req("POST", "/api/send", {"session": "a", "text": "y"})[0], 202)
        self.assertEqual([e["data"].get("wake") for e in self.spooled()], [True, None])

    def test_close_question_without_answer(self):
        # owner decision 2026-10-04: Esc and the cross close a question card — the question is cleared in the core
        self.seed()
        q = {"questions": [{"question": "Which?", "options": [{"label": "A"}, {"label": "B"}]}]}
        self.j.append("turn.started", "a", {"marker": None})
        self.j.append("permission.requested", "a", {"request_id": "r1", "tool": "AskUserQuestion", "input": q})
        self.settle()
        self.assertEqual(self.req("POST", "/api/question/dismiss", {"session": "a", "request_id": "r0"})[0], 409)
        self.assertEqual(self.req("POST", "/api/question/dismiss", {"session": "a", "request_id": "r1"})[0], 202)
        (ev,) = self.spooled()
        self.assertEqual((ev["type"], ev["entity"], ev["data"]["request_id"]), ("question.dismissed", "a", "r1"))

    def test_guards_foreign_host_and_missing_header(self):
        self.seed()
        self.assertEqual(self.req("GET", "/", host="evil.example:80")[0], 403)
        self.assertEqual(self.req("POST", "/api/send", {"session": "a", "text": "x"}, headers={"X-Hub": ""})[0], 403)
        self.assertEqual(self.spooled(), [])

    def test_stream_pushes_new_snapshot(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/stream", headers={"Host": f"localhost:{self.port}"})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        first = r.fp.readline()  # event: snapshot
        r.fp.readline()
        r.fp.readline()
        self.assertEqual(first.strip(), b"event: snapshot")
        self.seed()
        deadline = time.time() + 5
        got = None
        while time.time() < deadline and got is None:
            line = r.fp.readline()
            if line.startswith(b"data: "):
                snap = json.loads(line[6:])
                if snap["sessions"]:
                    got = snap
        c.close()
        self.assertEqual(got["sessions"][0]["name"], "a")

    def test_schedule_view_remove_retarget_and_clock(self):
        self.seed()
        self.j.append("schedule.set", "x", {"session": "a", "every": "1h", "prompt": "check", "channel": "cli",
                                             "owner": True})
        self.settle()
        snap = json.loads(self.req("GET", "/api/snapshot")[1])
        job = snap["schedule"][0]
        self.assertEqual((job["id"], job["session"], job["label"], job["by"]), ("x", "a", "running", ":owner"))
        self.assertTrue(job["when"].startswith("every hour until "))
        self.assertGreater(job["next"], time.time())
        self.assertEqual(snap["clock"]["state"], "down")  # the clock is not running — the page will say so
        self.assertEqual(self.req("POST", "/api/schedule/remove", {"id": "x"})[0], 202)
        self.assertEqual(self.req("POST", "/api/schedule/retarget", {"id": "x", "session": "no-such"})[0], 400)
        self.assertEqual(self.req("POST", "/api/schedule/retarget", {"id": "x", "session": "a"})[0], 202)
        self.assertEqual(self.req("POST", "/api/schedule/remove", {"id": "y"})[0], 409)
        got = [(e["type"], e["entity"], e["data"].get("session"), e["data"]["channel"]) for e in self.spooled()]
        self.assertEqual(got, [("schedule.remove_requested", "x", None, "web"), ("schedule.retarget", "x", "a", "web")])
        for path in ("/static/js/schedule.js", "/static/css/schedule.css"):
            self.assertEqual(self.req("GET", path)[0], 200, path)

    def test_an_update_with_new_rights_asks_the_owner_on_the_page(self):
        # audit finding: "Update" on the page installed a new process command without asking — now 409 with a list
        # of the new rights, and "Allow and update" is the same request with accept: true
        from modules.market import market
        self.seed()
        wider = [["process", '["sh","-c","x"]']]
        with mock.patch.object(market.Market, "update", side_effect=market.NeedsConsent("asks for more", wider)) as up:
            code, body = self.req("POST", "/api/plugins/update", {"name": "alice:owl"})
        self.assertEqual(code, 409)
        self.assertIn("sh -c x", json.loads(body)["wider"][0])
        up.assert_called_once_with("alice:owl", accept=False)
        self.assertFalse([e for e in self.spooled() if e["type"] == "plugins.changed"])
        with mock.patch.object(market.Market, "update", return_value="updated") as up:
            self.assertEqual(self.req("POST", "/api/plugins/update", {"name": "alice:owl", "accept": True})[0], 200)
        up.assert_called_once_with("alice:owl", accept=True)
        with mock.patch.object(market.Market, "update", return_value="updated") as up:
            self.req("POST", "/api/plugins/update", {"name": "alice:owl", "accept": "yes"})   # only true
        up.assert_called_once_with("alice:owl", accept=False)
        self.assertEqual(self.web.market().via, "web")
        js = self.req("GET", "/static/js/plugins.js")[1].decode()
        self.assertIn("web.plugins.consent_allow", js)

    def test_browsers_in_snapshot(self):
        self.j.append("session.requested", "a", {"session_id": "s", "cwd": "/w", "tools": ["browser:vm"]})
        self.j.append("browser.status", "vm", {"kind": "local", "url": "http://127.0.0.1:9230", "up": True,
                                               "version": "HeadlessChrome/153", "risk": []})
        self.settle()
        snap = json.loads(self.req("GET", "/api/snapshot")[1])
        self.assertEqual([(b["name"], b["up"], b["users"]) for b in snap["browsers"]], [("vm", True, ["a"])])
        self.assertEqual(self.req("GET", "/static/js/browsers.js")[0], 200)


class TokenCompatTest(unittest.TestCase):
    """Names and values of the previous version's tokens (what themes and plugins rely on) are preserved; new ones go
    under new names. The reference is tokens.css before the redesign: the numbers below are copied from it."""
    ROOT = os.path.join(os.path.dirname(__file__), "..", "modules", "web", "static", "css")
    OLD_SCALE = {
        "--n-0": "oklch(1 0 0)", "--n-50": "oklch(0.985 0.003 255)", "--n-100": "oklch(0.967 0.005 255)",
        "--n-150": "oklch(0.945 0.006 255)", "--n-200": "oklch(0.92 0.008 255)", "--n-300": "oklch(0.87 0.01 255)",
        "--n-400": "oklch(0.71 0.014 255)", "--n-500": "oklch(0.556 0.016 255)", "--n-600": "oklch(0.44 0.016 255)",
        "--n-700": "oklch(0.37 0.014 255)", "--n-800": "oklch(0.27 0.012 255)", "--n-850": "oklch(0.235 0.011 255)",
        "--n-900": "oklch(0.205 0.01 255)", "--n-950": "oklch(0.16 0.008 255)"}
    OLD_GEOMETRY = {
        "--space-1": "4px", "--space-2": "8px", "--space-3": "12px", "--space-4": "16px", "--space-6": "24px",
        "--space-8": "32px", "--radius": "8px", "--radius-sm": "calc(var(--radius) - 4px)",
        "--radius-lg": "calc(var(--radius) + 4px)"}
    OLD_TEXT = {"--text-xs": 12, "--text-sm": 14, "--text-base": 15, "--text-lg": 18}
    OLD_NAMES = ("--blue-500 --blue-400 --red-500 --red-400 --amber-500 --amber-400 --green-500 --green-400 --violet-500 "
                 "--violet-300 --background --foreground --card --muted --muted-2 --muted-foreground --accent --border "
                 "--input --primary --primary-foreground --destructive --warning --success --ring --code --font-ui "
                 "--font-code --font --font-mono --shadow-2 --outline-image").split()

    def setUp(self):
        with open(os.path.join(self.ROOT, "tokens.css"), encoding="utf-8") as f:
            self.css = f.read()
        root = re.search(r"\n:root \{(.*?)\n\}", self.css, re.S).group(1)
        self.root = {m.group(1): re.sub(r"\s+", " ", m.group(2)).strip()
                     for m in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+);", root)}

    def test_old_names_kept(self):
        for name in self.OLD_NAMES:
            self.assertIn(name, self.root)

    def test_old_neutral_scale_has_old_values(self):
        for name, value in self.OLD_SCALE.items():
            self.assertEqual(self.root.get(name), value, name)

    def test_old_geometry_has_old_values(self):
        for name, value in self.OLD_GEOMETRY.items():
            self.assertEqual(self.root.get(name), value, name)

    def test_old_text_sizes_equal_at_scale_one(self):
        for name, px in self.OLD_TEXT.items():
            self.assertEqual(self.root.get(name), f"calc({px}px * var(--ui-scale, 1))", name)

    def test_new_values_under_new_names(self):
        for name, value in (("--space-5", "24px"), ("--space-7", "32px"), ("--space-9", "48px"),
                            ("--radius-xs", "6px"), ("--radius-xl", "16px"), ("--radius-pill", "999px")):
            self.assertEqual(self.root.get(name), value, name)
        self.assertIn("--zinc-0", self.root)
        self.assertIn("--zinc-9", self.root)
        self.assertNotRegex(self.css, r"--n-[1-9]\b")   # the current scale used to be called --n-1…--n-9: avoid confusion with the old one

    def test_every_used_token_is_defined(self):
        root = os.path.join(os.path.dirname(__file__), "..", "modules", "web")
        known = set(self.root) | set(re.findall(r"(--[\w-]+)\s*:", self.css))
        for d, _, names in os.walk(root):
            for n in names:
                if n.endswith(".css") and n != "tokens.css":
                    with open(os.path.join(d, n), encoding="utf-8") as f:
                        text = f.read()
                    own = set(re.findall(r"(--[\w-]+)\s*:", text))
                    for used in re.findall(r"var\((--[\w-]+)", text):
                        if used.startswith(("--n-", "--space-", "--radius", "--text-", "--zinc-")):
                            self.assertIn(used, known | own, f"{n}: {used}")


class PageSourceInvariantsTest(unittest.TestCase):
    """Invariants of the page sources that cannot be checked in a browser and do not depend on formatting: token
    names and meaning, no libraries, no literal font sizes or layout animation, the hub.input contract. Layout and
    behavior (the bar, the queue, the input field, menus, text size) are checked in tests/test_web_e2e.py."""
    ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "modules", "web")

    def read(self, *parts):
        with open(os.path.join(self.ROOT, *parts), encoding="utf-8") as f:
            return f.read()

    def css_files(self):
        for base in (os.path.join(self.ROOT, "static", "css"), os.path.join(self.ROOT, "bundled")):
            for d, _, names in os.walk(base):
                for n in names:
                    if n.endswith(".css"):
                        yield os.path.join(d, n)

    def tokens(self):
        css = self.read("static", "css", "tokens.css")
        out = {}
        for block in re.findall(r"\{([^{}]*)\}", css):
            for m in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+);", block):
                out.setdefault(m.group(1), re.sub(r"\s+", " ", m.group(2)).strip())
        return out

    def test_tokens_the_page_relies_on_exist(self):
        have = self.tokens()
        for name in ("--shell", "--on-solid", "--action", "--who", "--who-soft", "--front-soft", "--z-menu", "--ease-out",
                     "--dur-fast", "--dur-hover", "--dur-dialog", "--dur-pane", "--ui-scale"):
            self.assertIn(name, have)
        self.assertEqual(have["--action"], "var(--foreground)")   # the action button follows the theme colors, not the neutral scale

    def test_state_colours_keep_one_sign_one_meaning(self):
        have = self.tokens()
        for name, value in (("wait", "--destructive"), ("work", "--primary"), ("free", "--success"), ("rest", "--muted-foreground")):
            self.assertEqual(have.get(f"--st-{name}"), f"var({value})", name)   # red is only for "waiting for you"

    def test_participant_hues_skip_red(self):
        hues = [int(h) for h in re.findall(r"--who-h-\d+:\s*(\d+)", self.read("static", "css", "tokens.css"))]
        self.assertEqual(len(hues), 12)
        self.assertTrue(all(40 <= h <= 340 for h in hues), hues)    # 345…35 is the red sector
        self.assertEqual(len(set(hues)), 12)

    def test_state_pills_mix_their_colour_with_the_foreground(self):
        # contrast of state labels: the state color is mixed with the text color so it reads in both themes
        base = self.read("static", "css", "base.css")
        for state, token in (("busy", "st-work"), ("idle", "st-free"), ("dialog", "st-wait"), ("failed", "st-wait")):
            rule = re.search(r'\.pill\[data-s="%s"\][^{]*\{([^}]*)\}' % state, base)
            self.assertTrue(rule, state)
            self.assertIn(f"var(--{token})", rule.group(1))
            self.assertIn("var(--foreground)", rule.group(1))

    def test_spinner_turns_without_rewriting_the_page(self):
        # a working session's spinner text was rewritten every 100 ms: ~600 layouts of the whole page a minute (a slow page
        # in the browser). Now it is a CSS strip of the same frames; no timer touches .spin, the frames match SPIN
        for n in sorted(n for n in os.listdir(os.path.join(self.ROOT, "static", "js")) if n.endswith(".js")):
            self.assertNotRegex(self.read("static", "js", n), r'querySelectorAll\("\.spin"\)', n)
        base = self.read("static", "css", "base.css")
        strip = re.search(r'\.spin::after \{[^}]*content: "([^"]*)"', base)
        self.assertTrue(strip)
        frames = "".join(chr(int(h, 16)) for h in re.findall(r"\\([0-9A-F]{4})", strip.group(1)))
        spin = re.search(r'export const SPIN = "([^"]*)"', self.read("static", "js", "dom.js")).group(1)
        self.assertEqual(frames, spin)
        self.assertIn("steps(%d)" % len(spin), base)

    def test_no_libraries_and_no_import_map(self):
        self.assertNotIn("importmap", self.read("static", "index.html"))
        self.assertEqual(sorted(n for n in os.listdir(os.path.join(self.ROOT, "vendor")) if n.endswith((".mjs", ".txt"))), [])
        for n in sorted(n for n in os.listdir(os.path.join(self.ROOT, "static", "js")) if n.endswith(".js")):
            self.assertNotRegex(self.read("static", "js", n), r'from "(preact|htm)', n)

    def test_overview_screen_is_gone(self):
        # owner decision 2026-10-06: there is no "Overview" screen, an empty address opens the hub feed
        for parts in (("static", "js", "overview.js"), ("static", "css", "overview.css")):
            self.assertFalse(os.path.exists(os.path.join(self.ROOT, *parts)), parts[-1])
        self.assertNotIn("overview", self.read("static", "index.html"))

    def test_hub_input_contract(self):
        # plugins' hub.input (API 1.7): a shared field component, {node, focus, set}; behavior is covered in e2e
        # (inputBox)
        box, ext = self.read("static", "js", "inputbox.js"), self.read("static", "js", "ext.js")
        self.assertIn("export function inputBox", box)
        self.assertIn("buildComposer", box)
        for member in ("node", "focus", "set"):
            self.assertRegex(box, r"\b%s\b" % member)
        self.assertIn("inputBox", ext)
        self.assertRegex(ext, r"\binput\s*:")
        self.assertTrue(os.path.exists(os.path.join(self.ROOT, "static", "js", "composer.js")))

    def test_no_literal_font_sizes_outside_tokens(self):
        # "Text size" is one multiplier: a font is a --text-* token, or (Clean, drawn in fixed pixels) a pixel size times
        # --c-rel, its ratio to the default step; the line height of a font follows the font. Clean used to be skipped here
        # (its folder is not css/): its 14/20 and 12/16 never grew — nothing of the default look changed with the setting
        bad = []
        clean = os.path.join(self.ROOT, "static", "clean", "clean.css")
        for path in [*self.css_files(), clean]:
            if path.endswith("tokens.css"):
                continue
            with open(path, encoding="utf-8") as f:
                text = f.read()
            pattern = r"(?:font(?:-size)?|(?<![-\w])line-height):[^;}]*" if path == clean else r"font(?:-size)?:[^;}]*"
            for m in re.finditer(pattern, text):
                decl = m.group(0)
                if path == clean and re.fullmatch(r"font-size: 17px", decl) and ".av.emoji" in text[max(0, m.start() - 60):m.start()]:
                    continue   # a session's emoji inside a 32 px plate: the plate does not grow, so neither does the glyph
                # --c-fs…: Clean's own tokens; --row: the height of a finger-sized button, its text is centered in it
                if re.search(r"\d(px|rem|pt)\b", decl) and not re.search(r"--ui-scale|--c-rel|--c-fs|var\(--row,", decl):
                    bad.append(f"{os.path.basename(path)}: {decl}")
        self.assertEqual(bad, [])

    def test_the_default_text_size_is_the_ratio_one_of_clean(self):
        # Clean's pixel sizes are multiplied by --ui-scale / --ui-base: at the default step the ratio is exactly 1 (the design's
        # pixels), so the default of the setting is --ui-base itself, and the page's default step is that same number
        tokens, steps = self.read("static", "css", "tokens.css"), self.read("static", "js", "textscale.js")
        base = re.search(r"--ui-base: ([\d.]+);", tokens).group(1)
        self.assertIn("--ui-scale: var(--ui-base);", tokens)
        self.assertEqual(re.search(r'const DEFAULT_SCALE = "([\d.]+)";', steps).group(1), base)
        self.assertIn("--c-rel: calc(var(--ui-scale) / var(--ui-base));", self.read("static", "clean", "clean.css"))

    def test_no_literal_font_sizes_in_js(self):
        for d, _, names in os.walk(self.ROOT):
            for n in names:
                path = os.path.join(d, n)
                if n.endswith(".js") and os.path.getsize(path) < 200_000:   # third-party bundled libraries do not count
                    with open(path, encoding="utf-8") as f:
                        self.assertNotRegex(f.read(), r"fontSize\s*=\s*[\"'`]\d", n)

    def test_no_layout_animation_and_no_ease_in(self):
        layout = r"\b(width|height|margin|padding|top|left|right|bottom|inset)\b"
        for path in self.css_files():
            name, css = os.path.basename(path), open(path, encoding="utf-8").read()
            for decl in re.findall(r"transition(?:-property)?\s*:[^;}]*", css):
                self.assertNotRegex(decl, layout, f"{name}: {decl}")
            for body in re.findall(r"@keyframes [\w-]+ \{((?:[^{}]*\{[^}]*\})*)\s*\}", css):
                self.assertNotRegex(body, layout, f"{name}: keyframes")
            self.assertNotRegex(css, r"(?<![-\w])ease-in(?![-\w])", name)


class NoForeignNamesTest(unittest.TestCase):
    """The shared repository has no foreign brands, internal documents or personal names: comments explain themselves."""
    def test_page_tests_and_changes_are_neutral(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        files = [os.path.join(root, "CHANGES.md")]
        files += [os.path.join(root, "tests", n) for n in ("test_web.py", "test_web_e2e.py", "test_files.py")]
        for base, _, names in os.walk(os.path.join(root, "modules", "web", "static")):
            files += [os.path.join(base, n) for n in names if n.endswith((".js", ".css", ".html", ".txt")) and n != "OFL.txt"]
        words = ["A" + "UI", "Cyber" + "punk", "Super " + "Mario", "Press " + "Start", "Exo " + "2"]
        # Russian words, searched in the text spelt in Latin by the language data: a Russian letter becomes a capital
        # (its transliteration), the text's own Latin is lower case, so the two never meet
        latin = lang.data("translit", "latin")
        russian = ["DIZAJN-JAZYK", "PULT", "NAVYKA motion", "NAVYK motion"]
        for path in files:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            spelt = "".join(latin[c].upper() if c in latin else c for c in text.lower())
            for w in words:
                self.assertNotIn(w, text, os.path.basename(path))
            for w in russian:
                self.assertNotIn(w, spelt, os.path.basename(path))



class AnswerRetryTest(unittest.TestCase):
    """A repeated reply to a card (the client got no receipt and retried from the queue, a second client, a page
    restart in between) is one core fact: the same reply gets 200 "already accepted" with the accepted fact's
    number, a different one gets 409 answered_other, not "no such card", after which the client drew "not sent"
    although the reply was accepted."""

    setUp, tearDown, req, spooled, settle, snap, core_decides, seed = (
        WebTest.setUp, WebTest.tearDown, WebTest.req, WebTest.spooled, WebTest.settle, WebTest.snap,
        WebTest.core_decides, WebTest.seed)   # the page scaffold without its tests
    Q = {"questions": [{"question": "Which?", "options": [{"label": "A"}, {"label": "B"}]}]}

    def ask(self, rid="r1", tool="AskUserQuestion"):
        self.seed()
        self.j.append("turn.started", "a", {"marker": None})
        self.j.append("permission.requested", "a", {"request_id": rid, "tool": tool,
                                                    "input": self.Q if tool == "AskUserQuestion" else {"command": "ls"}})
        self.settle()
        self.core_decides()

    def post(self, path, body):
        code, out = self.req("POST", path, {"session": "a", **body})
        return code, json.loads(out)

    def facts(self, kind="permission.answered"):
        evs = journal.read(self.j.path)
        rejected = {e["data"]["of"] for e in evs if e["type"] == "core.rejected"}
        return [e for e in evs if e["type"] == kind and e["seq"] not in rejected]

    def test_lost_ack_retry_is_one_fact_with_the_same_seq(self):
        self.ask()
        code, out = self.post("/api/answer", {"request_id": "r1", "answers": {"Which?": "B"}, "client_id": "c-1"})
        self.assertEqual(code, 202)
        (fact,) = self.facts()
        self.assertEqual((out, fact["data"]["client_id"]), ({"ok": True, "seq": fact["seq"]}, "c-1"))
        self.settle()
        a = next(s for s in self.snap()["sessions"] if s["name"] == "a")
        self.assertEqual([(r["request_id"], r["seq"], r.get("client_id")) for r in a["answered"]],
                         [("r1", fact["seq"], "c-1")])   # applied: the same seq in the snapshot
        for body in ({"answers": {"Which?": "B"}, "client_id": "c-1"},   # a retry from the client's queue
                     {"answers": {"Which?": " B "}},                     # the same reply without client_id (a second client)
                     {"answers": {"Which?": "A"}, "client_id": "c-1"}):  # the same client_id is the same request
            self.assertEqual(self.post("/api/answer", {"request_id": "r1", **body}),
                             (200, {"ok": True, "already": True, "seq": fact["seq"]}))
        code, out = self.post("/api/answer", {"request_id": "r1", "answers": {"Which?": "A"}})
        self.assertEqual((code, out["reason"], out["seq"]), (409, "answered_other", fact["seq"]))
        self.assertNotEqual(out["error"], json.loads(self.req("POST", "/api/answer", {
            "session": "a", "request_id": "r9", "answers": {"Which?": "A"}})[1])["error"])   # "with a different reply" ≠ "none"
        self.assertEqual(len(self.facts()), 1)

    def test_two_clients_race_before_the_page_sees_the_answer(self):
        # a second reply arrived while the page still sees the card as open: the core rejects the second fact, the
        # page checks the journal and answers on the merits, not with the core's rejection reason
        self.ask()
        first = self.post("/api/answer", {"request_id": "r1", "answers": {"Which?": "B"}})
        same = self.post("/api/answer", {"request_id": "r1", "answers": {"Which?": "B"}})
        self.assertEqual(same, (200, {"ok": True, "already": True, "seq": first[1]["seq"]}))
        self.web.tail.offset = 0   # "doesn't see" again: reread the journal from the start — the card is open
        self.web.tail.reader = __import__("core.views", fromlist=["Reader"]).Reader()
        other = self.post("/api/answer", {"request_id": "r1", "answers": {"Which?": "A"}})
        self.assertEqual((other[0], other[1]["reason"]), (409, "answered_other"))
        self.assertEqual(len(self.facts()), 1)

    def test_retry_after_page_restart(self):
        self.ask(tool="Bash")
        code, out = self.post("/api/permission", {"request_id": "r1", "behavior": "allow", "client_id": "c-2"})
        self.assertEqual(code, 202)
        self.core_stop()
        self.core_stop = None
        self.web.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.web = Web(self.state)   # the page anew: all knowledge comes from the core journal
        self.web.decide_wait = 0
        self.server = self.web.serve(port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.settle()
        self.assertEqual(self.post("/api/permission", {"request_id": "r1", "behavior": "allow", "client_id": "c-2"}),
                         (200, {"ok": True, "already": True, "seq": out["seq"]}))
        code, other = self.post("/api/permission", {"request_id": "r1", "behavior": "deny"})
        self.assertEqual((code, other["reason"]), (409, "answered_other"))
        self.assertEqual(self.spooled(), [])
        self.assertEqual(len(self.facts()), 1)

    def test_dismissed_question_retry_and_answer_after_it(self):
        self.seed()
        self.j.append("turn.started", "a", {"marker": None})
        self.j.append("permission.requested", "a", {"request_id": "r1", "tool": "AskUserQuestion", "input": self.Q,
                                                    "interactive": True})   # a question by form — card qN (asks)
        self.settle()
        self.core_decides()
        code, out = self.post("/api/question/dismiss", {"request_id": "r1", "client_id": "c-3"})
        self.assertEqual(code, 202, out)
        (fact,) = self.facts("question.dismissed")
        self.assertEqual(fact["data"]["client_id"], "c-3")
        self.settle()
        self.assertEqual(self.post("/api/question/dismiss", {"request_id": "r1"}),
                         (200, {"ok": True, "already": True, "seq": out["seq"]}))
        code, other = self.post("/api/answer", {"request_id": "r1", "answers": {"Which?": "B"}})
        self.assertEqual((code, other["reason"]), (409, "answered_other"))

    def test_an_answer_to_a_card_that_was_just_withdrawn_is_not_already_accepted(self):
        # a session (or the hub) withdraws the card; the closed card is now in answered[] with how "withdrawn". The owner's
        # answer, his x, or the retry of a reply sent before it must not be told "already accepted" by that record: the card
        # is gone, and nothing of his was applied
        self.seed()
        self.j.append("turn.started", "a", {"marker": None})
        self.j.append("permission.requested", "a", {"request_id": "r1", "tool": "AskUserQuestion", "input": self.Q,
                                                    "interactive": True})
        self.settle()
        self.core_decides()
        card = next(s for s in self.snap()["sessions"] if s["name"] == "a")["pending"][0]["id"]
        self.j.append("question.withdrawn", "a", {"id": card, "why": "settled", "sender": "a", "channel": "session",
                                                  "owner": False})
        self.settle()
        a = next(s for s in self.snap()["sessions"] if s["name"] == "a")
        self.assertEqual([(r["request_id"], r["how"], r["by"], r["why"]) for r in a["answered"]],
                         [("r1", "withdrawn", "a", "settled")])
        gone = self.post("/api/answer", {"request_id": "r9", "answers": {"Which?": "B"}})
        for path, body in (("/api/answer", {"answers": {"Which?": "B"}, "client_id": "c-5"}),
                           ("/api/question/dismiss", {"client_id": "c-5"}),
                           ("/api/question/dismiss", {}),
                           ("/api/permission", {"behavior": "deny"})):
            self.assertEqual(self.post(path, {"request_id": "r1", **body}), gone, path)
        self.assertEqual(self.facts() + self.facts("question.dismissed"), [])

    def test_a_queued_answer_asked_again_is_one_fact_and_then_brings_the_refusal(self):
        # the core did not record the reply within decide_wait — 202 queued; the page asks for the outcome with the
        # same request and the same client_id: while the fact is in the mailbox — queued again without a second fact,
        # if the core rejected it — 409 with its reason (the question stayed open)
        self.seed()
        self.j.append("turn.started", "a", {"marker": None})
        self.j.append("permission.requested", "a", {"request_id": "r1", "tool": "AskUserQuestion", "input": self.Q})
        self.settle()
        body = {"request_id": "r1", "answers": {"Which?": "B"}, "client_id": "c-4"}
        for _ in range(2):
            code, out = self.post("/api/answer", body)
            self.assertEqual((code, out.get("queued")), (202, True))
        box = os.path.join(self.state, "spool")
        ((path, item),) = spool.take(box)                          # one fact in the mailbox
        ev = self.j.append(item["type"], item["entity"], item["data"], src="web")   # hubd: recorded it and rejected it
        self.j.append("core.rejected", "a", {"of": ev["seq"], "type": item["type"], "reason": "Session is busy"})
        os.unlink(path)
        self.settle()
        code, out = self.post("/api/answer", body)
        self.assertEqual((code, out.get("error"), out.get("reason")), (409, "Session is busy", "refused"))
        self.assertEqual(self.spooled(), [])                        # and the rejection did not put the reply in again
        self.assertEqual(len(self.facts()), 0)

    def test_a_busy_journal_reader_brings_503_and_no_second_answer(self):
        # the journal reader is busy (rereading from the start): whether the core recorded this reply or rejected it
        # cannot be known. Putting it in again would make a second fact with the same client_id: the first would be
        # rejected, the second accepted. The answer is 503, ask later
        self.seed()
        self.j.append("turn.started", "a", {"marker": None})
        self.j.append("permission.requested", "a", {"request_id": "r1", "tool": "AskUserQuestion", "input": self.Q})
        self.settle()
        self.web.READ_WAIT = 0.05
        self.assertTrue(self.web.tail.reading.acquire(timeout=5))
        try:
            code, _ = self.post("/api/answer", {"request_id": "r1", "answers": {"Which?": "B"}, "client_id": "c-5"})
        finally:
            self.web.tail.reading.release()
        self.assertEqual(code, 503)
        self.assertEqual(self.spooled(), [])

    def test_an_answer_still_in_the_spool_after_a_page_restart_is_not_put_twice(self):
        # the page was restarted (hub restart web), the reply is still in the mailbox: a retry with the same
        # client_id gives queued by the mailbox, not a second fact (only the previous page remembered the mailbox
        # file name)
        self.seed()
        self.j.append("turn.started", "a", {"marker": None})
        self.j.append("permission.requested", "a", {"request_id": "r1", "tool": "AskUserQuestion", "input": self.Q})
        self.settle()
        body = {"request_id": "r1", "answers": {"Which?": "B"}, "client_id": "c-6"}
        self.assertEqual(self.post("/api/answer", body)[1].get("queued"), True)
        self.web.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.web = Web(self.state)
        self.web.decide_wait = 0
        self.server = self.web.serve(port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.settle()
        code, out = self.post("/api/answer", body)
        self.assertEqual((code, out.get("queued")), (202, True))
        self.assertEqual([e["data"]["client_id"] for e in self.spooled()], ["c-6"])

    def test_client_id_shape(self):
        self.ask()
        self.assertEqual(self.post("/api/answer", {"request_id": "r1", "answers": {"Which?": "B"}, "client_id": 5})[0], 400)
        self.assertEqual(self.post("/api/answer", {"request_id": "r1", "answers": {"Which?": "B"},
                                                   "client_id": "x" * 129})[0], 400)
        self.assertEqual(self.facts(), [])


@cut.needs("profile")
class AccountsUse(unittest.TestCase):
    """POST /api/accounts/use {account, force} — the clients' contract (2026-10-22): a busy account without force
    gets 409 with a reason."""
    setUp, tearDown, req, spooled, settle, snap, seed = (
        WebTest.setUp, WebTest.tearDown, WebTest.req, WebTest.spooled, WebTest.settle, WebTest.snap, WebTest.seed)

    def use(self, off=False, **body):
        pool = {"accounts": [{"name": "main", "token": "tok-m-0123456789"}, {"name": "work", "token": "tok-a-0123456789"}],
                "switch_at": 0.95, **({"off": True} if off else {})}
        with mock.patch("modules.accounts.accounts.load", return_value=pool):
            code, out = self.req("POST", "/api/accounts/use", body)
        return code, json.loads(out)

    def test_busy_is_refused_unless_forced_and_the_feature_is_announced(self):
        self.seed()
        self.j.append("account.switched", None, {"to": "main", "channel": "core"})
        self.j.append("account.limits", None, {"account": "work", "status": "allowed", "type": "seven_day", "ts": time.time(),
                                                "windows": {"seven_day": {"used": 0.97, "resets": time.time() + 3600}}})
        self.settle()
        self.assertIn("accounts.use", self.snap()["features"])
        code, out = self.use(account="work")
        self.assertEqual(code, 409)
        self.assertIn("97%", out["error"])
        self.assertGreater(out["until"], time.time())
        self.assertIs(out["forceable"], True)                       # the client knows force will help
        self.assertIn("97%", out["why"])                            # below 100%: the old words in `why`
        self.assertEqual(out["code"], "busy")                       # a client tells the refusal by code, not by its text
        self.assertEqual(self.use(account="work", force="false")[0], 409)   # force is only true, not a string
        self.assertEqual(self.spooled(), [])                        # "accepted" is not written
        code, out = self.use(account="work", force=True)
        self.assertEqual((code, out["ok"], out["active"]), (200, True, "work"))
        (fact,) = self.spooled()
        self.assertEqual((fact["type"], fact["data"]["to"], fact["data"]["force"]), ("account.switched", "work", True))
        self.assertEqual(self.use(account="main")[0], 200)          # a free one — without force
        self.assertEqual(self.use(account="nope")[0], 404)
        self.assertEqual(self.use(off=True, account="work"), (409, {"error": mock.ANY, "code": "paused"}))   # paused: by code
        self.j.append("account.limits", None, {"account": "main", "status": "rejected", "type": "seven_day",
                                                "ts": time.time(), "windows": {"seven_day": {"used": 1, "resets": time.time() + 3600}}})
        self.settle()
        code, out = self.use(account="main", force=True)
        self.assertEqual((code, out["forceable"]), (409, False))   # a real API rejection: force bypasses the threshold, not this

    def test_a_sign_in_module_that_does_not_answer(self):
        # session login belongs to the module (core/profiles.py, modules.use.profile), and it is silent: there is no
        # "Accounts" tab, the switch gives 503 without a fact; once it answers — the same contract
        from core import profiles
        from tests.test_profiles import LISTED, Mods
        self.seed()
        self.settle()
        mods = Mods()
        self.web.profiles = profiles.Profiles(self.state, mods=mods, config=lambda: {"modules": {"use": {"profile": "acc"}}})
        self.assertIsNone(self.snap()["accounts"])
        code, out = self.req("POST", "/api/accounts/use", {"account": "b"})
        self.assertEqual(code, 503)
        self.assertEqual(self.spooled(), [])
        mods.answers.update({"profile.list": lambda p: LISTED,
                             "profile.use": lambda p: {"ok": True} if p["force"] else
                             {"error": "busy", "why": "limit week 97%", "until": 5.0, "forceable": True}})
        self.assertEqual(self.snap()["accounts"]["pool"][0], {"name": "a", "email": "a@example.com", "probe": True})
        code, out = self.req("POST", "/api/accounts/use", {"account": "b"})
        self.assertEqual((code, json.loads(out)["forceable"], json.loads(out)["until"]), (409, True, 5.0))
        code, out = self.req("POST", "/api/accounts/use", {"account": "b", "force": True})
        self.assertEqual((code, json.loads(out)["active"]), (200, "b"))
        self.assertEqual([f["data"]["to"] for f in self.spooled()], ["b"])


@cut.needs("profile")
class AccountsProbe(unittest.TestCase):
    """POST /api/accounts/probe {account?} — "Refresh" in Settings -> Accounts: a fresh limits reading without a switch,
    the limits go to the core as account.limits with the account and its token generation."""
    setUp, tearDown, req, spooled, settle, snap, seed, core_decides = (
        WebTest.setUp, WebTest.tearDown, WebTest.req, WebTest.spooled, WebTest.settle, WebTest.snap, WebTest.seed,
        WebTest.core_decides)
    POOL = {"accounts": [{"name": "main", "token": "tok-m-0123456789"}, {"name": "work", "token": "tok-a-0123456789"},
                         {"name": "corp", "command": ["get-token"]}, {"name": "login", "login": True}], "switch_at": 0.95}
    LIM = {"status": "allowed", "type": "five_hour", "overage": False,
           "windows": {"five_hour": {"used": 0.25, "resets": 4000000000}}}

    def probe(self, measure, **body):
        with mock.patch("modules.accounts.accounts.load", return_value=self.POOL), \
                mock.patch("modules.gateway.gateway.measure", measure):
            code, out = self.req("POST", "/api/accounts/probe", body)
        return code, json.loads(out)

    def readings(self):
        return [e["data"] for e in journal.read(self.j.path) if e["type"] == "account.limits"]

    def test_a_reading_goes_to_the_core_and_the_pool_says_who_can_be_read(self):
        self.seed()
        self.core_decides()
        self.j.append("account.token", None, {"name": "main", "fp": "a" * 16, "channel": "core"})
        self.j.append("account.token", None, {"name": "main", "fp": "b" * 16, "channel": "core"})   # a new generation
        self.settle()
        with mock.patch("modules.accounts.accounts.load", return_value=self.POOL):
            pool = self.snap()["accounts"]["pool"]
        self.assertEqual({p["name"]: p.get("probe") for p in pool}, {"main": True, "work": True, "corp": None, "login": None})
        tokens = []

        def measure(token, **kw):
            tokens.append(token)
            return 200, self.LIM
        code, out = self.probe(measure, account="main")
        self.assertEqual((code, out), (200, {"results": [{"name": "main", "ok": True}]}))
        self.assertEqual(tokens, ["tok-m-0123456789"])
        (fact,) = self.readings()
        gen = self.web.tail.reader.state["account"]["refreshed"]["main"]
        self.assertEqual((fact["account"], fact["gen"], fact["windows"]), ("main", gen, self.LIM["windows"]))
        self.assertNotIn("tok-m-0123456789", json.dumps(fact))
        # another field is a 400, not "every account" (each reading spends a limit)
        for body in ({"name": "main"}, {"account": "main", "x": 1}, {"account": 5}):
            self.assertEqual(self.probe(measure, **body)[0], 400, body)
        code, out = self.probe(measure)                                 # all: own tokens only
        self.assertEqual(code, 200)
        self.assertEqual([r["name"] for r in out["results"]], ["main", "work"])
        self.assertEqual(sorted(tokens[1:]), ["tok-a-0123456789", "tok-m-0123456789"])
        self.assertEqual([f["account"] for f in self.readings()][-2:], ["main", "work"])
        before = len(self.readings())
        self.assertEqual(self.probe(measure, account="nope")[0], 404)
        code, out = self.probe(measure, account="corp")                 # a command token is not read
        self.assertEqual((code, out["results"][0]["ok"]), (200, False))
        self.assertEqual(len(tokens), 3)

        def broken(token, **kw):
            raise ConnectionRefusedError("x")
        code, out = self.probe(broken, account="work")
        self.assertEqual(code, 200)
        self.assertEqual(out["results"][0]["ok"], False)
        self.assertIn("ConnectionRefusedError", out["results"][0]["error"])
        self.assertEqual(len(self.readings()), before)                   # nothing was written for them

    def test_a_reading_the_core_has_not_recorded_is_not_a_success(self):
        self.seed()
        self.settle()   # no hubd (decide_wait 0): the fact stays in the mailbox
        code, out = self.probe(lambda token, **kw: (200, self.LIM), account="main")
        self.assertEqual(code, 200)
        self.assertEqual(out["results"], [{"name": "main", "ok": False, "error": self.web.t("web.srv.account_probe_queued")}])
        self.assertEqual([f["type"] for f in self.spooled()], ["account.limits"])

    def test_one_reading_at_a_time(self):
        self.seed()
        self.settle()
        self.web.probe_lock.acquire()
        try:
            code, out = self.probe(lambda token, **kw: self.fail("measured"), account="main")
        finally:
            self.web.probe_lock.release()
        self.assertEqual(code, 409)
        self.assertEqual(self.spooled(), [])

    def test_a_sign_in_module_measures_its_own_pool(self):
        # the login module leads session login: its pool is its own — the core asks its cli point for the reading
        # (accounts probe --json), the module writes account.limits itself with a req, the core waits for it once
        from core import profiles
        from tests.test_profiles import LISTED, Mods
        self.seed()
        self.core_decides()
        mods, asked = Mods(), []

        def cli(p):
            asked.append((p["args"], p["caller"]))
            rows = []
            for n in p["args"][2:] or ["a", "b"]:
                if n not in ("a", "b", "box", "org"):
                    rows.append({"name": n, "code": "unknown"})
                elif n in ("box", "org"):
                    rows.append({"name": n, "code": "login" if n == "box" else "command"})
                else:
                    req = "r" + n * 11
                    spool.put(os.path.join(self.state, "spool"), "account.limits", None,
                              {**self.LIM, "account": n, "req": req, "channel": "plugin", "owner": False,
                               "sender": "mod:acc"}, src="module")
                    rows.append({"name": n, "code": "ok", "limits": self.LIM, "req": req})
            return {"out": json.dumps({"results": rows}), "code": 0}
        mods.answers.update({"profile.list": lambda p: LISTED, "cli": cli})
        self.web.profiles = profiles.Profiles(self.state, mods=mods, config=lambda: {"modules": {"use": {"profile": "acc"}}},
                                              providers=lambda: {"profile": {"module": "acc", "state": "module"}})
        self.assertEqual({p["name"]: p.get("probe") for p in self.snap()["accounts"]["pool"]},
                         {"a": True, "b": True, "box": None, "org": None})
        code, out = self.probe(lambda token, **kw: self.fail("the core measured"), account="a")
        self.assertEqual((code, out), (200, {"results": [{"name": "a", "ok": True}]}))
        self.assertEqual(asked, [(["probe", "--json", "a"], {"owner": True})])
        self.assertEqual([(f["account"], f["req"]) for f in self.readings()], [("a", "r" + "a" * 11)])
        code, out = self.probe(lambda token, **kw: self.fail("the core measured"))   # all: the module's choice
        self.assertEqual((code, [r["ok"] for r in out["results"]]), (200, [True, True]))
        self.assertEqual(asked[-1][0], ["probe", "--json"])
        code, out = self.probe(lambda token, **kw: self.fail("the core measured"), account="org")
        self.assertEqual((code, out["results"][0]["ok"]), (200, False))
        self.assertEqual(self.probe(lambda token, **kw: self.fail("measured"), account="nope")[0], 404)
        # a module before the measurement (the accounts module 1.0.7 prints its help) — "update the module", not a failure
        mods.answers["cli"] = lambda p: {"out": "hub accounts …\n", "code": 2}
        code, out = self.probe(lambda token, **kw: self.fail("measured"), account="a")
        self.assertEqual(code, 409)
        self.assertIn("hub update", out["error"])
        mods.answers["cli"] = lambda p: {"out": json.dumps({"error": "not allowed"}), "code": 1}   # its own refusal
        self.assertEqual(self.probe(lambda token, **kw: self.fail("measured"), account="a"),
                         (409, {"error": self.web.t("web.srv.account_probe_module", module="acc", why="not allowed")}))
        mods.answers.pop("cli")   # silent: not ready, timed out
        code, out = self.probe(lambda token, **kw: self.fail("measured"), account="a")
        self.assertEqual(code, 409)
        self.assertIn("hub doctor", out["error"])
        self.assertEqual(len(self.readings()), 3)


if __name__ == "__main__":
    unittest.main()
