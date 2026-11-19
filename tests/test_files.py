"""The Files module (owner decision 2026-09-30: a session writes docs/decisions.md and the owner needs a way to open it):
project folders from the settings, viewing, editing with protection against overwriting someone else's changes, an
/open?path= link to any file inside those folders. Nothing outside the project folders."""
import tests  # noqa: F401 — do not see the machine's settings
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import quote, unquote

import platforms
from core import journal
from modules.files import paths
from modules.files import server as files_server
from modules.files.server import Files
from tests import winsafe


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="files-")
        self.proj = os.path.join(self.dir, "proj")
        os.makedirs(os.path.join(self.proj, "docs"))
        os.makedirs(os.path.join(self.proj, ".git"))
        with open(os.path.join(self.proj, "docs", "decisions.md"), "w", newline="") as f:  # no \r\n on Windows
            f.write("# Decisions\n")
        with open(os.path.join(self.dir, "secret.txt"), "w") as f:
            f.write("forbidden")
        # Windows without the right to symlinks (WinError 1314): no leak.txt — "does not let outside" is a 404 anyway
        self.links = winsafe.try_symlink(os.path.join(self.dir, "secret.txt"), os.path.join(self.proj, "leak.txt"))
        self.cfg = {"projects": {"proj": {"path": self.proj}}}
        json.dump({"ok": True, "ts": time.time(), "owner": "me@x", "hosts": ["100.1.2.3:8788"]},
                  open(os.path.join(self.dir, "tailnet.json"), "w"))  # as the tailnet module writes it
        self.files = Files(config=lambda: self.cfg, state_dir=self.dir)
        self.server = self.files.serve(port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def req(self, method, path, body=None, host=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": host or f"127.0.0.1:{self.port}", "X-Hub": "1", "Content-Type": "application/json",
             **(headers or {})}
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, (json.loads(data) if r.getheader("Content-Type", "").startswith("application/json") else data)

    def test_sessions_without_a_process_are_asleep_for_the_comment_picker(self):
        # a parked executor and one that handed in its result have their own core states since 2026-10-26 (parked,
        # retired), before that asleep with a flag: in the comment's "to whom" list they go by the page snapshot's
        # words (comments.js checks state === "asleep")
        st = {"sessions": {n: {"name": n, "state": s, "cwd": "/w"} for n, s in
                           (("p", "parked"), ("r", "retired"), ("z", "asleep"), ("i", "idle"))}}
        with mock.patch.object(self.files, "state", return_value=st):
            got = {s["name"]: s["state"] for s in self.files.sessions()}
        self.assertEqual(got, {"p": "idle", "r": "asleep", "z": "asleep", "i": "idle"})

    def test_the_hub_link_follows_the_hubs_port(self):
        # a second hub has its own page port (HUB_WEB_PORT): the "Hub" button in Files led to the first hub's 8787
        with mock.patch.dict(os.environ, {"HUB_WEB_PORT": "9797"}):
            _, page = self.req("GET", "/")
        self.assertRegex(page.decode(), r'<html [^>]*data-hub-port="9797"')
        with mock.patch.dict(os.environ, {"HUB_WEB_PORT": '1"><script>'}):
            _, page = self.req("GET", "/index.html")
        self.assertRegex(page.decode(), r'<html [^>]*data-hub-port="8787"')
        self.assertNotIn("<script>\"", page.decode())
        with open(os.path.join(files_server.STATIC, "files.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertNotIn(":8787", js)
        self.assertIn("dataset.hubPort", js)

    def test_page_fonts_come_from_the_hub_static(self):
        # redesign (2026-10-05): Files takes the hub page's styles from /web/, the Onest fonts too, no internet
        code, css = self.req("GET", "/web/css/tokens.css")
        self.assertEqual(code, 200)
        fonts = re.findall(rb"url\(\.\./fonts/([\w-]+\.woff2)\)", css)
        self.assertTrue(fonts)
        for name in fonts:
            code, data = self.req("GET", "/web/fonts/" + name.decode())
            self.assertEqual((code, data[:4]), (200, b"wOF2"), name)
        code, page = self.req("GET", "/")
        self.assertNotIn(b"fonts.googleapis", page)

    def test_the_tab_icons_of_the_page_are_served(self):
        # the page links the hub's tab icons by size (png) under /web/: a type missing from the server's table is a 404 and no icon
        code, page = self.req("GET", "/")
        icons = re.findall(rb'<link rel="icon"[^>]*href="(/[^"]+)"', page)
        self.assertEqual(len(icons), 3)
        for link in icons:
            code, data = self.req("GET", link.decode())
            self.assertEqual((code, data[:4]), (200, b"\x89PNG"), link)

    def test_page_design_from_hub_tokens(self):
        """The design matches the hub page: its tokens/base/feed/code from /web/, colors only through tokens (light,
        dark and plugin themes without their own code), the font via the --text-* scale; an md document uses the
        interface font at the --measure width."""
        code, page = self.req("GET", "/")
        links = re.findall(rb'href="(/[^"]+\.css)"', page)
        self.assertIn(b"/web/css/base.css", links)
        for link in links:
            self.assertEqual(self.req("GET", link.decode())[0], 200, link)
        _, css = self.req("GET", "/static/files.css")
        css = re.sub(rb"/\*.*?\*/", b"", css, flags=re.S).decode()
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b", css), [])  # own hex colors would break the themes
        self.assertEqual(re.findall(r"font(?:-size)?:[^;]*\d+(?:\.\d+)?px", css), [])
        self.assertRegex(css, r"\.md\.doc\s*\{[^}]*var\(--font\)")
        self.assertRegex(css, r"\.md\.doc\s*\{[^}]*--measure")
        self.assertIn("var(--shell)", css + page.decode() + self.req("GET", "/web/css/base.css")[1].decode())

    def test_page_shows_html_in_sandboxed_frame(self):
        """On the page html is shown in an iframe from /render with a sandbox without allow-same-origin; view
        switches: html — "Page | Code", markdown — "Render | Source"."""
        code, js = self.req("GET", "/static/files.js")
        self.assertEqual(code, 200)
        js = js.decode()
        self.assertIn('"iframe"', js)
        self.assertIn("/render/", js)
        self.assertIn('"allow-scripts allow-popups allow-forms"', js)
        self.assertNotIn("allow-same-origin", js)
        # labels come from the catalog (i18n/)
        from core import i18n
        for key, label in (("files.view_page", "Page"), ("files.view_code", "Code"), ("files.view_render", "Rendered"),
                           ("files.view_source", "Source")):
            self.assertIn(f't("{key}")', js, key)
            self.assertEqual(i18n.t(key), label)

    def test_roots_list_read(self):
        code, roots = self.req("GET", "/api/roots")
        self.assertEqual((code, [r["name"] for r in roots]), (200, ["Mac", "proj"]))  # from the Mac — the whole disk too
        code, ls = self.req("GET", "/api/list?root=proj&path=")
        self.assertEqual([e["name"] for e in ls["entries"]], ["docs"] + (["leak.txt"] if self.links else []))  # .git is hidden
        code, f = self.req("GET", "/api/read?root=proj&path=docs/decisions.md")
        self.assertEqual((code, f["text"], f["path"]),
                         (200, "# Decisions\n", os.path.realpath(os.path.join(self.proj, "docs", "decisions.md"))))

    def test_hidden_names_ignore_case(self):
        # a macOS disk ignores case: a request for ".GIT" opens .git (realpath keeps the request's spelling there), and
        # a folder may be named so on disk. Two parents — on such a disk ".GIT" and ".Git" side by side are one folder
        for parent, name in (("a", ".GIT"), ("b", ".Git"), ("c", "Node_Modules")):
            os.makedirs(os.path.join(self.proj, parent, name))
            with open(os.path.join(self.proj, parent, name, "config"), "w") as f:
                f.write("[remote]\n")
            code, ls = self.req("GET", f"/api/list?root=proj&path={parent}")
            self.assertEqual((code, ls["entries"]), (200, []), name)
            for path in (f"{parent}/{name}/config", f"{parent}/{name}"):
                self.assertEqual(self.req("GET", f"/api/read?root=proj&path={quote(path)}")[0], 404, path)
                self.assertEqual(self.req("GET", f"/api/list?root=proj&path={quote(path)}")[0], 404, path)
                self.assertEqual(self.req("GET", f"/raw?root=proj&path={quote(path)}")[0], 404, path)
                self.assertEqual(self.req("GET", f"/render/proj/{quote(path)}")[0], 404, path)

    def test_another_spelling_of_git_that_the_disk_opens_is_hidden(self):
        # an old HFS+ disk ignores zero-width characters: ".g<ZWNJ>it" opens the real .git, and realpath keeps the
        # request's spelling. There is no such disk here: os.stat answers for that spelling as the disk would (two real
        # folders on Linux, the second one stands for the first)
        alias = ".g" + chr(0x200C) + "it"
        for name, words in ((".git", "[remote] real\n"), (alias, "[remote] other\n")):
            os.makedirs(os.path.join(self.proj, "a", name))
            with open(os.path.join(self.proj, "a", name, "config"), "w") as f:
                f.write(words)
        real_stat = os.stat
        disk = lambda p, *a, **k: real_stat(os.fspath(p).replace(alias, ".git"), *a, **k)  # noqa: E731
        path = f"a/{alias}/config"
        self.assertEqual(self.req("GET", f"/api/read?root=proj&path={quote(path)}")[0], 200, "another folder: opens")
        with mock.patch("os.stat", disk):
            for url in (f"/api/read?root=proj&path={quote(path)}", f"/api/list?root=proj&path={quote('a/' + alias)}",
                        f"/raw?root=proj&path={quote(path)}", f"/render/proj/{quote(path)}"):
                self.assertEqual(self.req("GET", url)[0], 404, url)
            self.assertEqual(self.req("GET", "/api/read?root=proj&path=docs/decisions.md")[0], 200)

    def test_a_git_folder_kept_elsewhere_by_a_link_is_hidden_by_the_name_asked(self):
        # .git may be a link to the repository's folder kept under another name inside the same root: realpath has no
        # ".git" in it, the name asked has
        os.makedirs(os.path.join(self.proj, "store", "repo"))
        with open(os.path.join(self.proj, "store", "repo", "config"), "w") as f:
            f.write("[remote]\n")
        os.makedirs(os.path.join(self.proj, "b"))
        if not winsafe.try_symlink(os.path.join(self.proj, "store", "repo"), os.path.join(self.proj, "b", ".git")):
            self.skipTest("no right to make links")
        # ".GIT" and "\\" count on macOS and Windows; on Linux such a path does not exist anyway
        for path in ("b/.git/config", "b/.GIT/config", "b\\.git\\config"):
            self.assertEqual(self.req("GET", f"/api/read?root=proj&path={quote(path)}")[0], 404, path)
        # a full path (an image from a session step on the hub page) is turned into a root and a path through realpath
        full = os.path.join(self.proj, "b", ".git", "config")
        self.assertEqual(self.req("GET", f"/raw?path={quote(full)}")[0], 404, full)
        self.assertEqual(self.req("GET", f"/raw?path={quote(os.path.join(self.proj, 'docs', 'decisions.md'))}")[0], 200)
        self.assertEqual(self.req("GET", "/api/read?root=proj&path=store/repo/config")[0], 200, "by its own name: opens")
        self.assertEqual(self.req("GET", f"/api/read?root=proj&path={quote('b/.git/../../docs/decisions.md')}")[0], 200)

    def test_nothing_outside_projects(self):
        for path in ("../secret.txt", "leak.txt", "/etc/hosts"):
            code, _ = self.req("GET", f"/api/read?root=proj&path={quote(path)}")
            self.assertEqual(code, 404, path)
        self.assertEqual(self.req("GET", "/api/read?root=nope&path=x")[0], 404)

    def same_drive(self, path):
        """The "Mac" root is "/", on Windows it is the drive of the current folder: a temp folder on another drive is not in it."""
        if os.path.splitdrive(path)[0].lower() != os.path.splitdrive(os.path.abspath(os.sep))[0].lower():
            self.skipTest("the «Mac» root is the disk of the current folder, and the temporary folder is on another disk")

    def test_mac_sees_everything(self):
        """Owner decision 2026-09-30: a request from 127.0.0.1 needs no project access check. From the Mac — the
        "Mac" root (/): any file; a project file still opens in its own project. From the tailnet — projects only."""
        secret = os.path.realpath(os.path.join(self.dir, "secret.txt"))
        self.same_drive(secret)
        inner = os.path.relpath(secret, os.path.abspath(os.sep)).replace(os.sep, "/")   # Windows: without "C:\\"
        self.assertEqual([r["name"] for r in self.req("GET", "/api/roots")[1]], ["Mac", "proj"])
        self.assertEqual(self.open_("path=" + quote(secret)), (302, "/#/Mac/" + quote(inner)))
        self.assertEqual(self.open_("path=" + quote(os.path.join(self.proj, "docs", "decisions.md"))),
                         (302, "/#/proj/docs/decisions.md"))
        code, f = self.req("GET", "/api/read?root=Mac&path=" + quote(inner))
        self.assertEqual((code, f["text"]), (200, "forbidden"))
        # the same request from the tailnet (owner by whois) — there is no "Mac" root
        remote = Files(config=lambda: self.cfg, state_dir=self.dir)
        self.assertEqual(sorted(remote.roots(local=False)), ["proj"])
        self.assertIsNone(remote.resolve("Mac", inner, local=False))
        self.assertIsNone(remote.locate(secret, local=False))

    def test_raw_by_absolute_path(self):
        """Owner request 2026-09-30 (a `Read(boards.png)` screenshot): an image a session has read is visible in its
        step — /raw by the full path, with the same rights as /open (from the Mac — any file, from the tailnet —
        projects only)."""
        img = os.path.join(self.proj, "docs", "shot.png")
        with open(img, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\nfake")
        code, body = self.req("GET", "/raw?path=" + quote(img))
        self.assertEqual((code, body[:4]), (200, b"\x89PNG"))
        secret = os.path.join(self.dir, "secret.txt")
        self.same_drive(secret)
        self.assertEqual(self.req("GET", "/raw?path=" + quote(secret))[0], 200)  # from the Mac
        self.assertEqual(self.req("GET", "/raw?path=" + quote(os.path.join(self.dir, "nope.png")))[0], 404)

    def media(self, name, size=1000):
        path = os.path.join(self.proj, "docs", name)
        with open(path, "wb") as f:
            f.write(bytes(i % 251 for i in range(size)))
        return path

    def test_raw_serves_ranges_for_video(self):
        """A video plays and seeks (owner report 2026-10-08: a video does not show when opened): the browser asks for
        chunks with `Range: bytes=…` — a 206 answer with Content-Range; without it Chrome cannot seek and Safari does
        not play."""
        self.media("clip.mp4")
        data = bytes(i % 251 for i in range(1000))
        url = "/raw?root=proj&path=docs/clip.mp4"
        code, h, body = self.raw("GET", url)
        self.assertEqual((code, h.get("accept-ranges"), h["content-type"], body), (200, "bytes", "video/mp4", data))
        for rng, want in (("bytes=10-19", (10, 19)), ("bytes=990-", (990, 999)), ("bytes=-5", (995, 999)),
                          ("bytes=900-5000", (900, 999))):
            code, h, body = self.raw("GET", url, {"Range": rng})
            self.assertEqual((code, h.get("content-range"), body, h["content-length"]),
                             (206, f"bytes {want[0]}-{want[1]}/1000", data[want[0]:want[1] + 1], str(want[1] - want[0] + 1)), rng)
        code, h, _ = self.raw("GET", url, {"Range": "bytes=2000-"})
        self.assertEqual((code, h.get("content-range")), (416, "bytes */1000"))
        code, h, body = self.raw("GET", url, {"Range": "items=1-2"})  # an unintelligible Range — the whole file
        self.assertEqual((code, len(body)), (200, 1000))

    def test_raw_opened_directly_plays_media(self):
        """A video opened by a /raw link in a tab is shown by the browser's own player — its document lives under the
        response's CSP: `default-src 'none'` without media-src did not let the player load the clip itself (10-08)."""
        self.media("clip.webm")
        code, h, _ = self.raw("GET", "/raw?root=proj&path=docs/clip.webm")
        csp = h.get("content-security-policy", "")
        self.assertIn("media-src 'self'", csp)
        self.assertIn("img-src 'self'", csp)
        self.assertNotIn("script-src", csp)  # still no scripts in the served file
        self.assertEqual(h.get("x-content-type-options"), "nosniff")

    def test_raw_files_are_sandboxed(self):
        """/raw is not a live page: html or svg opened by a link run in a sandbox and cannot submit forms; the browser
        will not let a third-party site read the file (CORP). pdf has no sandbox: Chrome's viewer does not open in
        one."""
        with open(os.path.join(self.proj, "r.html"), "w") as f:
            f.write("<script>alert(1)</script>")
        with open(os.path.join(self.proj, "r.pdf"), "wb") as f:
            f.write(b"%PDF-1.4\n")
        _, h, _ = self.raw("GET", "/raw?root=proj&path=r.html")
        self.assertIn("sandbox", h["content-security-policy"])
        self.assertIn("form-action 'none'", h["content-security-policy"])
        self.assertEqual(h["cross-origin-resource-policy"], "same-site")
        _, h, _ = self.raw("GET", "/raw?root=proj&path=r.pdf")
        self.assertNotIn("sandbox", h["content-security-policy"])
        self.assertIn("form-action 'none'", h["content-security-policy"])
        self.assertEqual(h["cross-origin-resource-policy"], "same-site")

    def test_raw_download(self):
        """"Download" on every file: ?download=1 gives Content-Disposition attachment with the file name, including
        spaces and non-Latin letters (RFC 5987); without it — inline, but with the same name so that "Save as" does not offer
        "raw"."""
        self.media("το βίντεό μου.mp4", 10)
        code, h, body = self.raw("GET", "/raw?root=proj&path=" + quote("docs/το βίντεό μου.mp4") + "&download=1")
        self.assertEqual((code, len(body)), (200, 10))
        cd = h.get("content-disposition", "")
        self.assertTrue(cd.startswith("attachment;"), cd)
        self.assertIn("filename*=UTF-8''" + quote("το βίντεό μου.mp4"), cd)
        cd.encode("latin-1")  # the header is ASCII only
        code, h, _ = self.raw("GET", "/raw?root=proj&path=" + quote("docs/το βίντεό μου.mp4"))
        self.assertTrue(h.get("content-disposition", "").startswith("inline;"), h.get("content-disposition"))
        full = quote(os.path.join(self.proj, "docs", "το βίντεό μου.mp4"))   # by the full path (a link from the feed) too
        code, h, _ = self.raw("GET", "/raw?path=" + full + "&download=1")
        self.assertTrue(h.get("content-disposition", "").startswith("attachment;"), h.get("content-disposition"))
        code, h, _ = self.raw("GET", "/raw?root=proj&path=docs/decisions.md")  # text in a tab is UTF-8
        self.assertEqual(h["content-type"], "text/markdown; charset=utf-8")

    def test_raw_big_file_is_streamed(self):
        """A large file is served in chunks, not wholly into memory: the answer is given even if it is larger than MAX_RENDER."""
        from modules.files import server
        self.media("big.mov", 300_000)
        with mock.patch.object(server, "CHUNK", 4096):
            code, h, body = self.raw("GET", "/raw?root=proj&path=docs/big.mov")
        self.assertEqual((code, len(body), h["content-type"]), (200, 300_000, "video/quicktime"))

    @unittest.skipUnless(shutil.which("sips"), "needs sips (macOS)")
    def test_heic_shown_as_jpeg(self):
        """Chrome does not draw iPhone photos (HEIC) — ?as=jpeg gives a jpeg copy (sips on the Mac)."""
        import struct
        import zlib

        def chunk(t, d):
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
        png = os.path.join(self.dir, "p.png")
        with open(png, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress(b"\x00" + b"\xff\x00\x00" * 2 + b"\x00" + b"\x00\xff\x00" * 2))
                    + chunk(b"IEND", b""))
        heic = os.path.join(self.proj, "docs", "φωτο.heic")
        subprocess.run(["sips", "-s", "format", "heic", png, "--out", heic], capture_output=True, check=True)
        code, h, body = self.raw("GET", "/raw?root=proj&path=" + quote("docs/φωτο.heic") + "&as=jpeg")
        self.assertEqual((code, h["content-type"], body[:2]), (200, "image/jpeg", b"\xff\xd8"))
        self.assertIn(quote("φωτο.jpg"), h["content-disposition"])

    def test_heic_copies_are_not_kept_under_a_fixed_name_in_the_shared_temp_folder(self):
        # the jpeg copy lived in <temp>/hub-files-jpeg/<hash of path and time>.jpg: another user of the machine could
        # make that folder first and put any picture under the name the hub would serve for the owner's photo. The
        # shared temp folder is the test's own here: nothing is written to the machine's
        from modules.files import server
        heic = os.path.join(self.proj, "docs", "photo.heic")
        with open(heic, "wb") as f:
            f.write(b"heic")
        st = os.stat(heic)
        name = hashlib.sha1(f"{heic}|{st.st_mtime}|{st.st_size}".encode()).hexdigest()[:16] + ".jpg"   # portable: the key the server computes
        shared = os.path.join(self.dir, "shared-tmp")
        planted = os.path.join(shared, "hub-files-jpeg")
        os.makedirs(planted)
        with open(os.path.join(planted, name), "wb") as f:
            f.write(b"planted")
        state = os.path.join(self.dir, "state")

        def sips(cmd, **_):   # the fake sips writes its --out
            with open(cmd[-1], "wb") as f:
                f.write(b"\xff\xd8 converted")
            return subprocess.CompletedProcess(cmd, 0)
        files = server.Files(config=lambda: self.cfg, state_dir=state)
        with mock.patch.object(server.shutil, "which", return_value="/x/sips"), \
                mock.patch.object(server.subprocess, "run", side_effect=sips), \
                mock.patch.object(server.tempfile, "gettempdir", return_value=shared):
            out = files.jpeg(heic)
            with open(out, "rb") as f:
                self.assertEqual(f.read(), b"\xff\xd8 converted")
            self.assertFalse(out.startswith(shared))
            if os.name == "posix":
                self.assertEqual(os.stat(os.path.dirname(out)).st_mode & 0o077, 0)
            # the cache does not grow without end: a copy of over a week ago goes when a new one is made
            old = os.path.join(os.path.dirname(out), "0123456789abcdef.jpg")
            with open(old, "wb") as f:
                f.write(b"old")
            os.utime(old, (time.time() - 8 * 86400, time.time() - 8 * 86400))   # portable: set by the test itself
            os.utime(out, (time.time() - 8 * 86400, time.time() - 8 * 86400))   # portable: set by the test itself
            with open(heic, "ab") as f:   # the photo changed: a new copy
                f.write(b" edited")
            files.jpeg(heic)
            self.assertFalse(os.path.exists(old))
            self.assertFalse(os.path.exists(out))
            # a failed sips (code 1, a half-written file) leaves no copy behind to be served for a week
            before = sorted(os.listdir(os.path.dirname(out)))
            with open(heic, "ab") as f:
                f.write(b" again")
            server.subprocess.run.side_effect = lambda cmd, **_: (sips(cmd), subprocess.CompletedProcess(cmd, 1))[1]
            self.assertIsNone(files.jpeg(heic))
            self.assertEqual(sorted(os.listdir(os.path.dirname(out))), before)

    def test_thumbnail_limits(self):
        """thumbnail(): the pixel limit is read from the header before anything is decoded, a picture is never enlarged,
        a refusal is remembered once per source (not per width), the cache folder is private, and what is not a picture
        by content is refused."""
        from unittest import mock
        from modules.files import server
        png = os.path.join(self.dir, "big.png")
        with open(png, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 5000)
        ps = os.path.join(self.dir, "ps.png")
        with open(ps, "wb") as f:
            f.write(b"%!PS-Adobe-3.0 EPSF-3.0\n" + b"0" * 5000)
        text = os.path.join(self.dir, "t.bmp")
        with open(text, "wb") as f:
            f.write(b"BM is the start of a text file, not of a picture " * 100)
        state, size, calls = os.path.join(self.dir, "state"), ["400", "200"], []

        def sips(cmd, **_):
            calls.append(cmd[1])
            if cmd[1] == "-g":
                return subprocess.CompletedProcess(cmd, 0, f"pixelWidth: {size[0]}\npixelHeight: {size[1]}\n", "")
            with open(cmd[-1], "wb") as f:
                f.write(b"\xff\xd8 small")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with mock.patch.object(server.shutil, "which", return_value="/x/sips"), \
                mock.patch.object(server.subprocess, "run", side_effect=sips):
            self.assertIsNone(server.thumbnail(state, ps, 120))   # a name says png, the content says PostScript
            self.assertIsNone(server.thumbnail(state, text, 120))   # "BM" alone is not a bmp
            self.assertEqual(calls, [])
            out, st = server.thumbnail(state, png, 120)
            self.assertEqual(open(out, "rb").read(), b"\xff\xd8 small")
            self.assertEqual(st.st_size, os.stat(png).st_size)
            if os.name == "posix":
                self.assertEqual(os.stat(os.path.dirname(out)).st_mode & 0o077, 0)
            # not wider than asked: no enlarging, and 400 px is remembered for EVERY width that is not smaller
            n = len(calls)
            for w in (480, 960, 1920, 2048):
                self.assertIsNone(server.thumbnail(state, png, w))
            self.assertEqual(len(calls), n)   # known from the first look: no tool run
            size[:] = ["12000", "12000"]          # 144 Mpx: refused by the header, no resample
            big = os.path.join(self.dir, "huge.png")
            with open(big, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + b"1" * 5000)
            n = len(calls)
            for w in (120, 240, 480):
                self.assertIsNone(server.thumbnail(state, big, w))
            self.assertEqual(calls[n:], ["-g"])   # looked at once; no resample, no look at other widths
        self.assertEqual([server.thumb_width(x) for x in ("480", "5", "130", "99999", "-1", "0", "x", None)],
                         [480, 120, 240, 2048, None, None, None, None])

    def test_thumbnail_races(self):
        """A picture rewritten while it is scaled gives no copy (an old picture under the new tag); at most two
        scalers run at once; identical requests share one scaler."""
        from unittest import mock
        from modules.files import server
        state, lock, now, top, resamples = os.path.join(self.dir, "state"), threading.Lock(), [0], [0], [0]
        pics = []
        for i in range(8):
            p = os.path.join(self.dir, f"p{i}.png")
            with open(p, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + bytes([i]) * 5000)
            pics.append(p)
        rewrite = []

        def sips(cmd, **_):
            if cmd[1] == "-g":
                return subprocess.CompletedProcess(cmd, 0, "pixelWidth: 400\npixelHeight: 200\n", "")
            with lock:
                now[0] += 1
                top[0] = max(top[0], now[0])
                resamples[0] += 1
            time.sleep(0.05)
            if rewrite:
                with open(cmd[-3], "ab") as f:   # the picture is replaced under sips
                    f.write(b"new")
            with open(cmd[-1], "wb") as f:
                f.write(b"\xff\xd8 small")
            with lock:
                now[0] -= 1
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with mock.patch.object(server.shutil, "which", return_value="/x/sips"), \
                mock.patch.object(server.subprocess, "run", side_effect=sips):
            results = []
            threads = [threading.Thread(target=lambda p=p: results.append(server.thumbnail(state, p, 120))) for p in pics]
            threads += [threading.Thread(target=lambda: results.append(server.thumbnail(state, pics[0], 120))) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertTrue(all(results))
            self.assertLessEqual(top[0], server.THUMB_SLOTS)
            self.assertEqual(resamples[0], 8)   # four more requests for the first picture made no scaler of their own
            rewrite.append(1)
            self.assertIsNone(server.thumbnail(state, pics[1], 240))   # rewritten mid-scale: no copy of the old picture
            self.assertEqual([n for n in os.listdir(os.path.join(state, "cache", "files-jpeg")) if n.endswith(".tmp")], [])

    def picture(self, name, fill, size=5000):
        p = os.path.join(self.dir, name)
        with open(p, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + bytes([fill]) * size)
        return p

    def age_marks(self, state):
        from modules.files import server
        folder = os.path.join(state, "cache", "files-jpeg")
        old = time.time() - server.THUMB_RETRY - 1
        for n in os.listdir(folder):
            if n.endswith(".none"):
                os.utime(os.path.join(folder, n), (old, old))

    def waiting_on_a_key(self, n):
        from modules.files import server
        end = time.monotonic() + 5
        while not any(e[1] == n for e in server._thumb_locks.values()) and time.monotonic() < end:
            time.sleep(0.01)

    def test_thumbnail_a_request_that_waited_for_a_rewritten_picture_runs_no_scaler(self):
        """A picture rewritten while it is scaled: the identical request that waited for that scaler holds the old
        picture's stat, so it gives up at once instead of running sips again only to fail the same check."""
        from modules.files import server
        state, pic = os.path.join(self.dir, "state"), self.picture("r.png", 4)
        rewrite, started, runs = threading.Event(), threading.Semaphore(0), []

        def sips(cmd, **_):
            runs.append(cmd[1])
            if cmd[1] == "-g":
                return subprocess.CompletedProcess(cmd, 0, "pixelWidth: 400\npixelHeight: 200\n", "")
            started.release()
            rewrite.wait(10)
            with open(pic, "ab") as f:
                f.write(b"new")   # the picture is replaced under sips
            with open(cmd[-1], "wb") as f:
                f.write(b"\xff\xd8 small")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        res = []
        with mock.patch.object(server.shutil, "which", return_value="/x/sips"), \
                mock.patch.object(server.subprocess, "run", side_effect=sips), \
                mock.patch.object(server, "THUMB_WAIT", 10):
            first = threading.Thread(target=lambda: res.append(server.thumbnail(state, pic, 120)))
            first.start()
            self.assertTrue(started.acquire(timeout=5))
            second = threading.Thread(target=lambda: res.append(server.thumbnail(state, pic, 120)))
            second.start()
            self.waiting_on_a_key(2)   # the second waits for the first
            rewrite.set()
            first.join(10)
            second.join(10)
        self.assertEqual(res, [None, None])
        self.assertEqual(runs, ["-g", "--resampleWidth"])

    def test_thumbnail_waits_for_a_scaler_only_briefly(self):
        """A request waits for a busy scaler at most THUMB_WAIT and then gets None (the page serves the original): a
        waiting request holds one of the page's connection slots, and a feed of pictures used to fill them all."""
        from modules.files import server
        state, pics = os.path.join(self.dir, "state"), [self.picture(f"w{i}.png", i) for i in range(3)]
        gate, started = threading.Event(), threading.Semaphore(0)

        def sips(cmd, **_):
            if cmd[1] == "-g":
                return subprocess.CompletedProcess(cmd, 0, "pixelWidth: 400\npixelHeight: 200\n", "")
            started.release()
            gate.wait(10)
            with open(cmd[-1], "wb") as f:
                f.write(b"\xff\xd8 small")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        made, late = {}, {}
        with mock.patch.object(server.shutil, "which", return_value="/x/sips"), \
                mock.patch.object(server.subprocess, "run", side_effect=sips), \
                mock.patch.object(server, "THUMB_WAIT", 0.2):
            busy = [threading.Thread(target=lambda p=p: made.setdefault(p, server.thumbnail(state, p, 120))) for p in pics[:2]]
            for t in busy:
                t.start()
            self.assertTrue(started.acquire(timeout=5) and started.acquire(timeout=5))   # both scalers are busy
            waiting = [threading.Thread(target=lambda: late.setdefault("other", server.thumbnail(state, pics[2], 120))),
                       threading.Thread(target=lambda: late.setdefault("same", server.thumbnail(state, pics[0], 120)))]
            for t in waiting:
                t.start()
            for t in waiting:
                t.join(5)
            gave_up = dict(late)
            gate.set()
            for t in busy + waiting:
                t.join(10)
        self.assertEqual(gave_up, {"other": None, "same": None})
        self.assertTrue(made[pics[0]] and made[pics[1]])

    def test_thumbnail_refusal_of_one_width_is_not_one_of_the_picture(self):
        """A jpeg not smaller than its source, or a failed scale, refuse that width only: a well-compressed screenshot
        asked wide first still gets its narrow thumbnail, and a refused width is not scaled again."""
        from modules.files import server
        state, pic, runs = os.path.join(self.dir, "state"), self.picture("shot.png", 7, size=30000), []

        def sips(cmd, **_):
            if cmd[1] == "-g":
                return subprocess.CompletedProcess(cmd, 0, "pixelWidth: 4000\npixelHeight: 3000\n", "")
            runs.append(int(cmd[2]))
            if cmd[2] == "960":
                return subprocess.CompletedProcess(cmd, 1, "", "")   # sips fails at this width
            with open(cmd[-1], "wb") as f:
                f.write(b"\xff\xd8" + b"j" * (int(cmd[2]) * 20))   # 1920 px: 38 KB, bigger than the 30 KB source
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with mock.patch.object(server.shutil, "which", return_value="/x/sips"), \
                mock.patch.object(server.subprocess, "run", side_effect=sips):
            self.assertIsNone(server.thumbnail(state, pic, 1920))
            self.assertIsNone(server.thumbnail(state, pic, 960))
            self.assertTrue(server.thumbnail(state, pic, 240))
            self.assertIsNone(server.thumbnail(state, pic, 1920))
            self.assertIsNone(server.thumbnail(state, pic, 960))
            self.assertEqual(runs, [1920, 960, 240])
            self.age_marks(state)   # a failed scale may pass later: tried again after THUMB_RETRY; "not smaller" may not
            self.assertIsNone(server.thumbnail(state, pic, 960))
            self.assertIsNone(server.thumbnail(state, pic, 1920))
        self.assertEqual(runs, [1920, 960, 240, 960])

    def test_thumbnail_a_scaler_out_of_time_is_not_run_again(self):
        """A scaler that runs out of time is a refusal of that picture, at any width: run again on every view it held
        a scaler slot for up to 30 s each time."""
        from modules.files import server
        state, runs = os.path.join(self.dir, "state"), []
        slow_scale, slow_look = self.picture("s.png", 1), self.picture("l.png", 2)

        def sips(cmd, **kw):
            runs.append((cmd[-3] if cmd[1] != "-g" else cmd[-1], cmd[1]))
            if cmd[1] == "-g" and cmd[-1] == slow_look:
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            if cmd[1] == "-g":
                return subprocess.CompletedProcess(cmd, 0, "pixelWidth: 4000\npixelHeight: 3000\n", "")
            if cmd[2] == "960":
                with open(cmd[-1], "wb") as f:
                    f.write(b"\xff\xd8 small")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        fast_after = slow_scale
        with mock.patch.object(server.shutil, "which", return_value="/x/sips"), \
                mock.patch.object(server.subprocess, "run", side_effect=sips):
            for w in (480, 480, 120):
                self.assertIsNone(server.thumbnail(state, slow_scale, w))
                self.assertIsNone(server.thumbnail(state, slow_look, w))
            self.assertEqual(runs, [(slow_scale, "-g"), (slow_scale, "--resampleWidth"), (slow_look, "-g"),
                                    (slow_scale, "-g"), (slow_scale, "--resampleWidth")])   # 480 once, then 120 once
            del runs[3:]
            self.age_marks(state)   # a busy machine is no fact about the picture: tried again after THUMB_RETRY
            self.assertIsNone(server.thumbnail(state, slow_look, 120))
            self.assertEqual(runs[3:], [(slow_look, "-g")])
            # a scale out of time refuses that width only: another width of the same picture is still made
            self.assertTrue(server.thumbnail(state, fast_after, 960))

    @unittest.skipUnless(shutil.which("sips"), "needs sips (macOS)")
    def test_thumbnail_of_a_transparent_picture_keeps_its_transparency(self):
        """A picture with an alpha channel (a window screenshot with its shadow, a logo) is scaled to a png: a jpeg has
        no transparency, and sips painted the transparent area white — a white box on a dark page. An opaque picture
        still gives a jpeg."""
        import random
        import struct
        import zlib
        from modules.files import server

        def chunk(t, d):
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))

        def png(path, alpha):   # 240 x 40 of noise (a copy must be smaller than its source; noise does not compress)
            noise = random.Random(7).randbytes(240 * 40 * 3)
            rows = b"".join(b"\x00" + b"".join(noise[i:i + 3] + (b"\x00" if alpha else b"") for i in range(y * 720, y * 720 + 720, 3))
                            for y in range(40))   # rgba with every alpha 0 (clear), or rgb
            with open(path, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 240, 40, 8, 6 if alpha else 2, 0, 0, 0))
                        + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
        clear, solid = os.path.join(self.dir, "clear.png"), os.path.join(self.dir, "solid.png")
        png(clear, True)
        png(solid, False)
        state = os.path.join(self.dir, "state")
        out, _ = server.thumbnail(state, clear, 120)
        with open(out, "rb") as f:
            data = f.read()
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"), out)
        self.assertTrue(out.endswith(".png"))
        width, height, depth, kind, _, _, interlace = struct.unpack(">IIBBBBB", data[16:29])
        self.assertEqual((width, depth, kind, interlace), (120, 8, 6, 0))   # rgba, 8 bit, not interlaced
        idat, i = b"", 8
        while i < len(data):
            n, kind = struct.unpack(">I", data[i:i + 4])[0], data[i + 4:i + 8]
            idat += data[i + 8:i + 8 + n] if kind == b"IDAT" else b""
            i += 12 + n
        # the first pixel of the first row is stored as it is under every png filter (nothing left of it, nothing above)
        self.assertEqual(zlib.decompress(idat)[4], 0)   # the corner stays transparent
        out, _ = server.thumbnail(state, solid, 120)
        with open(out, "rb") as f:
            self.assertEqual(f.read(2), b"\xff\xd8")   # jpeg
        self.assertTrue(out.endswith(".jpg"))

    def test_thumbnail_format_follows_the_alpha_channel(self):
        """The scaler is told png for a picture with an alpha channel and jpeg for one without; the copy is named by
        its format; a jpeg copy made for a transparent picture before the format followed the alpha channel (white
        where the picture was transparent) is not served again."""
        from modules.files import server
        pic = os.path.join(self.dir, "shot.png")
        with open(pic, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 5000)
        state, alpha, calls = os.path.join(self.dir, "state"), ["yes"], []

        def sips(cmd, **_):
            calls.append(cmd)
            if cmd[1] == "-g":
                return subprocess.CompletedProcess(cmd, 0, f"pixelWidth: 400\npixelHeight: 200\nhasAlpha: {alpha[0]}\n", "")
            with open(cmd[-1], "wb") as f:
                f.write(b"scaled as " + cmd[cmd.index("format") + 1].encode())
            return subprocess.CompletedProcess(cmd, 0, "", "")
        st = os.stat(pic)
        folder = os.path.join(state, "cache", "files-jpeg")
        os.makedirs(folder, mode=0o700)
        old = hashlib.sha1(f"{pic}|{st.st_mtime_ns}|{st.st_size}|{st.st_ino}".encode()).hexdigest()[:16]   # portable: the key the server computed before, of this one stat
        with open(os.path.join(folder, f"{old}-120.jpg"), "wb") as f:
            f.write(b"white where transparent")
        with mock.patch.object(server.shutil, "which", return_value="/x/sips"), \
                mock.patch.object(server.subprocess, "run", side_effect=sips):
            out, _ = server.thumbnail(state, pic, 120)
            self.assertTrue(out.endswith(".png"), out)
            with open(out, "rb") as f:
                self.assertEqual(f.read(), b"scaled as png")
            self.assertEqual(calls[0][1:7], ["-g", "pixelWidth", "-g", "pixelHeight", "-g", "hasAlpha"])
            self.assertEqual(server.thumbnail(state, pic, 120)[0], out)   # found in the cache: no second run
            self.assertEqual(len(calls), 2)
            alpha[0] = "no"
            other = os.path.join(self.dir, "photo.png")
            shutil.copy(pic, other)
            out, _ = server.thumbnail(state, other, 120)
            self.assertTrue(out.endswith(".jpg"), out)
            with open(out, "rb") as f:
                self.assertEqual(f.read(), b"scaled as jpeg")

    def test_thumbnail_a_mark_it_cannot_read_is_no_refusal(self):
        """Refusal marks are written whole (a temporary file, then a rename: requests for other widths read them
        without a lock), and one that cannot be read is taken as no mark, not as a refusal."""
        from modules.files import server
        state, pic = os.path.join(self.dir, "state"), self.picture("m.png", 3)

        def sips(cmd, **_):
            if cmd[1] == "-g":
                return subprocess.CompletedProcess(cmd, 0, "pixelWidth: 400\npixelHeight: 200\n", "")
            with open(cmd[-1], "wb") as f:
                f.write(b"\xff\xd8 small")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with mock.patch.object(server.shutil, "which", return_value="/x/sips"), \
                mock.patch.object(server.subprocess, "run", side_effect=sips):
            self.assertIsNone(server.thumbnail(state, pic, 480))   # 400 px: not wider, remembered
            folder = os.path.join(state, "cache", "files-jpeg")
            marks = [n for n in os.listdir(folder) if n.endswith(".none")]
            self.assertEqual(len(marks), 1)
            with open(os.path.join(folder, marks[0]), "w"):
                pass   # caught half-written
            self.assertTrue(server.thumbnail(state, pic, 120))

    def test_thumbnail_identical_requests_share_a_scaler_after_a_failure(self):
        """The key's lock leaves the table only with its last waiter: when the first scaler fails, the request that
        waited for it runs the next one, and a newcomer waits for that one instead of starting a second next to it."""
        from modules.files import server
        state, pic = os.path.join(self.dir, "state"), self.picture("k.png", 9)
        first_fails, others, started = threading.Event(), threading.Event(), threading.Semaphore(0)
        lock, now, top, calls = threading.Lock(), [0], [0], [0]

        def sips(cmd, **_):
            if cmd[1] == "-g":
                return subprocess.CompletedProcess(cmd, 0, "pixelWidth: 400\npixelHeight: 200\n", "")
            with lock:
                calls[0] += 1
                first = calls[0] == 1
                now[0] += 1
                top[0] = max(top[0], now[0])
            started.release()
            try:
                if first:
                    first_fails.wait(10)
                    raise OSError("sips did not start")   # no copy and no mark
                others.wait(10)
                with open(cmd[-1], "wb") as f:
                    f.write(b"\xff\xd8 small")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            finally:
                with lock:
                    now[0] -= 1
        res = []

        def ask():
            res.append(server.thumbnail(state, pic, 120))
        with mock.patch.object(server.shutil, "which", return_value="/x/sips"), \
                mock.patch.object(server.subprocess, "run", side_effect=sips), \
                mock.patch.object(server, "THUMB_WAIT", 10):
            a = threading.Thread(target=ask)
            a.start()
            self.assertTrue(started.acquire(timeout=5))   # the first request scales
            b = threading.Thread(target=ask)
            b.start()
            self.waiting_on_a_key(2)   # the second waits for the first
            first_fails.set()
            self.assertTrue(started.acquire(timeout=5))   # the second scales after the first failed
            c = threading.Thread(target=ask)
            c.start()
            started.acquire(timeout=0.25)   # a third scaler, if the newcomer started one
            others.set()
            for t in (a, b, c):
                t.join(10)
        self.assertEqual(top[0], 1)
        self.assertEqual(calls[0], 2)
        self.assertEqual(sorted(map(bool, res)), [False, True, True])

    def test_read_tells_what_the_file_is(self):
        """The viewer picks the view by type: image, video, audio, pdf — not "Not shown: not text"."""
        for name, kind in (("a.mp4", "video"), ("a.mov", "video"), ("a.webm", "video"), ("a.mp3", "audio"),
                           ("a.png", "image"), ("a.webp", "image"), ("a.heic", "image"), ("a.pdf", "pdf"),
                           ("a.bin", "binary")):
            self.media(name, 300)  # bytes past 0x7f are not UTF-8
            code, f = self.req("GET", "/api/read?root=proj&path=docs/" + name)
            self.assertEqual((code, f.get("kind")), (200, kind), name)
        code, f = self.req("GET", "/api/read?root=proj&path=docs/decisions.md")
        self.assertEqual(f.get("kind"), "text")

    def test_list_gives_kinds(self):
        """A folder listing carries the file kind: an icon and neighbouring files for "next / previous"."""
        self.media("a.mp4", 5)
        code, ls = self.req("GET", "/api/list?root=proj&path=docs")
        kinds = {e["name"]: e.get("kind") for e in ls["entries"]}
        self.assertEqual((kinds["a.mp4"], kinds["decisions.md"]), ("video", "file"))

    def test_write_with_conflict_check(self):
        _, f = self.req("GET", "/api/read?root=proj&path=docs/decisions.md")
        code, out = self.req("POST", "/api/write", {"root": "proj", "path": "docs/decisions.md", "text": "new\n",
                                                     "sha": f["sha"]})
        self.assertEqual(code, 200)
        self.assertEqual(open(os.path.join(self.proj, "docs", "decisions.md")).read(), "new\n")
        code, out = self.req("POST", "/api/write", {"root": "proj", "path": "docs/decisions.md", "text": "old",
                                                     "sha": f["sha"]})  # the file has already changed — do not overwrite
        self.assertEqual(code, 409)
        self.assertEqual(self.req("POST", "/api/write", {"root": "proj", "path": "docs/decisions.md", "text": "x",
                                                         "sha": out.get("sha", "")}, headers={"X-Hub": ""})[0], 403)

    def test_reveal_shows_the_file_in_its_folder_only_on_this_machine(self):
        # owner request 2026-10-07: let the folder holding a file be viewed — Finder with the file selected
        shown = []
        self.files.reveal = shown.append
        self.files.desktop = "finder"
        code, _ = self.req("POST", "/api/reveal", {"root": "proj", "path": "docs/decisions.md"})
        self.assertEqual(code, 200)
        self.assertEqual(shown, [os.path.realpath(os.path.join(self.proj, "docs", "decisions.md"))])
        self.assertEqual(self.req("POST", "/api/reveal", {"root": "proj", "path": "nope.md"})[0], 404)
        self.assertEqual(self.req("POST", "/api/reveal", {"root": "proj", "path": "leak.txt"})[0], 404)   # outside — no
        self.assertEqual(self.req("POST", "/api/reveal", {"root": "proj", "path": "docs/decisions.md"},
                                  host="100.1.2.3:8788")[0], 403)                 # from a phone — the Finder is not theirs
        self.assertEqual(self.req("POST", "/api/reveal", {"root": "proj", "path": "docs/decisions.md"},
                                  headers={"X-Hub": ""})[0], 403)
        self.assertEqual(len(shown), 1)

    def test_reveal_is_refused_where_the_hub_machine_has_no_desktop(self):
        # owner 2026-11-10: the hub on a server has no screen — nothing to open a window on; refused up front
        shown = []
        self.files.reveal = shown.append
        self.files.desktop = None
        code, out = self.req("POST", "/api/reveal", {"root": "proj", "path": "docs/decisions.md"})
        self.assertEqual(code, 409)
        self.assertIn("error", out)
        self.assertEqual(shown, [])

    def test_the_page_carries_the_file_manager_only_for_a_request_from_this_machine_with_a_desktop(self):
        # owner 2026-11-10: the button is shown only when the hub machine has a desktop and the page is opened on it
        def tag(host=None):
            return re.search(r'<html [^>]*data-reveal="([^"]*)"', self.req("GET", "/", host=host)[1].decode())
        self.files.desktop = None
        self.assertIsNone(tag())
        self.files.desktop = "dolphin"
        self.assertEqual(tag().group(1), "dolphin")
        with mock.patch.object(files_server.access, "local", return_value=False), \
                mock.patch.object(files_server.access, "allowed", return_value=True):   # the owner from a phone
            self.assertIsNone(tag())   # the window would open on another computer
        # the browser's cached copy follows the answer: the same page file, another tag
        def etag():
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            c.request("GET", "/", headers={"Host": f"127.0.0.1:{self.port}"})
            r = c.getresponse()
            r.read()
            c.close()
            return r.getheader("ETag")
        with_desktop = etag()
        self.files.desktop = None
        self.assertNotEqual(etag(), with_desktop)

    def test_file_manager_name_per_os(self):
        # the hub machine knows whether it has a desktop and what its file manager is called (platforms/*.file_manager)
        from platforms import posix, windows
        self.assertEqual(posix.file_manager({}, "darwin"), "finder")
        self.assertEqual(windows.file_manager(), "explorer")
        shown = {"xdg-open": "/usr/bin/xdg-open", "xdg-mime": "/usr/bin/xdg-mime"}
        which = shown.get

        def run(default):
            calls = []

            def fake(cmd, **kw):
                calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, default + "\n", "")
            return fake, calls
        # a service of a server: no DISPLAY and no WAYLAND_DISPLAY anywhere — no desktop, and xdg-mime is not asked
        fake, calls = run("org.gnome.Nautilus.desktop")
        self.assertIsNone(posix.file_manager({}, "linux", which, fake))
        self.assertNotIn(["xdg-mime", "query", "default", "inode/directory"], calls)
        # a desktop session without xdg-open — nothing to open the folder with
        self.assertIsNone(posix.file_manager({"DISPLAY": ":0"}, "linux", {"xdg-mime": "x"}.get, fake))
        for default, want in (("org.gnome.Nautilus.desktop", "files"), ("nautilus.desktop", "files"),
                              ("org.kde.dolphin.desktop", "dolphin"), ("thunar.desktop", "thunar"),
                              ("nemo.desktop", "nemo"), ("pcmanfm-qt.desktop", "pcmanfm"), ("caja.desktop", "caja"),
                              ("io.elementary.files.desktop", "files"), ("somenewfm.desktop", "folder"), ("", "folder")):
            fake, calls = run(default)
            for env in ({"DISPLAY": ":0"}, {"WAYLAND_DISPLAY": "wayland-0"}):
                self.assertEqual(posix.file_manager(env, "linux", which, fake), want, (default, env))
            self.assertEqual(calls[0], ["xdg-mime", "query", "default", "inode/directory"])
        # xdg-mime is missing or fails: the desktop is there, the name is general
        self.assertEqual(posix.file_manager({"DISPLAY": ":0"}, "linux", {"xdg-open": "x"}.get, fake), "folder")

        def boom(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 5)
        self.assertEqual(posix.file_manager({"DISPLAY": ":0"}, "linux", which, boom), "folder")

    def test_a_service_without_a_display_still_finds_the_users_desktop(self):
        # a colleague's hub is a systemd --user service: its own environment has no DISPLAY although the screen is there;
        # the second sign is the user manager's environment or a graphical login session (x11 / wayland)
        from platforms import posix
        which = {"xdg-open": "/usr/bin/xdg-open", "xdg-mime": "/usr/bin/xdg-mime", "systemctl": "/bin/systemctl",
                 "loginctl": "/bin/loginctl"}.get

        def sys_run(manager="", sessions=None, calls=None):
            sessions = sessions or {}

            def fake(cmd, **kw):
                if calls is not None:
                    calls.append(cmd)
                out = ""
                if cmd[:3] == ["systemctl", "--user", "show-environment"]:
                    out = manager
                elif cmd[:2] == ["loginctl", "list-sessions"]:
                    out = "".join(f"{sid} {uid} user seat0 -\n" for sid, (uid, _) in sessions.items())
                elif cmd[:2] == ["loginctl", "show-session"]:
                    out = sessions.get(cmd[2], (0, ""))[1] + "\n"
                elif cmd[:2] == ["xdg-mime", "query"]:
                    out = "org.kde.dolphin.desktop\n"
                return subprocess.CompletedProcess(cmd, 0, out, "")
            return fake
        # the user's id as Linux has it: a fixed one, so that the test also runs where os has no getuid (Windows)
        me = 1000
        uid = mock.patch.object(posix.os, "getuid", lambda: me, create=True)
        uid.start()
        self.addCleanup(uid.stop)
        # the manager has the display: yes, whichever way it is written
        for text in ("PATH=/bin\nDISPLAY=:0\n", "WAYLAND_DISPLAY=wayland-0\nLANG=C\n"):
            self.assertEqual(posix.file_manager({}, "linux", which, sys_run(manager=text)), "dolphin", text)
        # neither in the environment nor in the user manager: the user's own graphical login session says it
        calls = []
        fake = sys_run(manager="PATH=/bin\n", sessions={"3": (me, "wayland"), "4": (me + 1, "x11")}, calls=calls)
        self.assertEqual(posix.file_manager({}, "linux", which, fake), "dolphin")
        self.assertEqual(posix.file_manager({"XDG_SESSION_ID": "3"}, "linux", which, fake), "dolphin")
        self.assertIn(["loginctl", "show-session", "3", "-p", "Type", "--value"], calls)
        # a text session (tty, ssh) is no desktop; another user's graphical session is not this user's
        for sessions, env in (({"5": (me, "tty")}, {}), ({"6": (me, "unspecified")}, {"XDG_SESSION_ID": "6"}),
                              ({"7": (me + 1, "x11")}, {})):
            self.assertIsNone(posix.file_manager(env, "linux", which, sys_run(manager="", sessions=sessions)), sessions)
        # still no xdg-open: nothing to open the folder with; and the tools failing is "no desktop", not a crash
        self.assertIsNone(posix.file_manager({}, "linux", {"systemctl": "x"}.get, sys_run(manager="DISPLAY=:0\n")))

        def boom(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 2)
        self.assertIsNone(posix.file_manager({}, "linux", which, boom))
        # the display in its own environment needs none of that: the tools are not asked
        calls = []
        posix.file_manager({"DISPLAY": ":0"}, "linux", which, sys_run(calls=calls))
        self.assertEqual([c[0] for c in calls], ["xdg-mime"])

    def test_reveal_command_per_os(self):
        from platforms import posix, windows
        self.assertEqual(posix.reveal_command("/p/a.md", "darwin"), ["open", "-R", "/p/a.md"])
        self.assertEqual(posix.reveal_command("/p/a.md", "linux"), ["xdg-open", "/p"])
        self.assertEqual(windows.reveal_command(r"C:\p\a.md"), r'explorer /select,"C:\p\a.md"')

    def test_reveal_command_windows_quotes_only_the_path(self):
        """Explorer parses /select,"path" — quotes around the path alone; subprocess.list2cmdline quotes the whole
        argument "/select,path" — Explorer then opens "Documents". We check the command line that goes to
        CreateProcess: on Windows Popen joins a list through list2cmdline and passes a string as is."""
        import subprocess
        from platforms import windows

        def cmdline(line):
            return subprocess.list2cmdline(line) if isinstance(line, list) else line
        path = r"C:\Users\me\My Documents\notes v2.md"
        self.assertEqual(cmdline(windows.reveal_command(path)), 'explorer /select,"' + path + '"')
        self.assertNotIn('"/select', cmdline(windows.reveal_command(path)))

    def test_open_by_absolute_path(self):
        target = os.path.join(self.proj, "docs", "decisions.md")
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/open?path=" + quote(target), headers={"Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        r.read()
        self.assertEqual((r.status, r.getheader("Location")), (302, "/#/proj/docs/decisions.md"))
        c.close()
        self.assertIsNone(self.files.locate(os.path.join(self.dir, "secret.txt")))  # from the tailnet — nothing outside the projects

    def test_open_a_deleted_sent_file_by_its_copy(self):
        # 10-19: hub send-file puts a copy into attachments; the feed opens a document by its source (comments to it),
        # a deleted one by its copy from alt. Only attachments are in alt: otherwise a link would open any file
        from modules.web import attach
        kept = attach.new_path(self.dir, "report.md")
        with open(kept, "w") as f:
            f.write("# Report")
        base = os.path.abspath(os.sep)
        if os.path.splitdrive(kept)[0] != os.path.splitdrive(base)[0]:
            # Windows: the "Mac" root is one drive (the process's), while attachments (temp) are on another — their folder needs its own root
            self.cfg["files"] = {"roots": {"kept": self.dir}}
            base = self.dir
        gone = quote(os.path.join(self.dir, "tmp", "report.md"))
        code, loc = self.open_(f"path={gone}&alt={quote(kept)}")
        self.assertEqual(code, 302)
        self.assertTrue(unquote(loc).endswith(os.path.relpath(kept, base).replace(os.sep, "/")), loc)
        self.assertEqual(self.open_(f"path={gone}&alt={quote(os.path.join(self.dir, 'secret.txt'))}")[0], 404)
        target = os.path.join(self.proj, "docs", "decisions.md")   # the source exists — it, not the copy
        self.assertEqual(self.open_(f"path={quote(target)}&alt={quote(kept)}"), (302, "/#/proj/docs/decisions.md"))

    def test_open_embedded_keeps_the_flag(self):
        # the embedded window on the hub page (a right panel, a plugin): only the file, no folder listing
        target = quote(os.path.join(self.proj, "docs", "decisions.md"))
        self.assertEqual(self.open_(f"path={target}&embed=1"), (302, "/#/proj/docs/decisions.md?embed=1"))
        self.assertEqual(self.open_(f"path={target}&embed=yes"), (302, "/#/proj/docs/decisions.md"))

    def test_open_embedded_for_the_hub_header(self):
        # chrome=hub: the hub page draws the window header itself (md-panel); only together with embed=1 and only "hub"
        target = quote(os.path.join(self.proj, "docs", "decisions.md"))
        self.assertEqual(self.open_(f"path={target}&embed=1&chrome=hub"),
                         (302, "/#/proj/docs/decisions.md?embed=1&chrome=hub"))
        self.assertEqual(self.open_(f"path={target}&chrome=hub"), (302, "/#/proj/docs/decisions.md"))
        self.assertEqual(self.open_(f"path={target}&embed=1&chrome=x"), (302, "/#/proj/docs/decisions.md?embed=1"))
        # and the hub page's theme, so the window looks like the page around it; only an id, nothing else in the address
        self.assertEqual(self.open_(f"path={target}&embed=1&chrome=hub&theme=clean%3Aclean-dark.json"),
                         (302, "/#/proj/docs/decisions.md?embed=1&chrome=hub&theme=clean%3Aclean-dark.json"))
        self.assertEqual(self.open_(f"path={target}&embed=1&theme=" + quote("x&embed=0#/etc")),
                         (302, "/#/proj/docs/decisions.md?embed=1"))

    def open_(self, query):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/open?" + query, headers={"Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        r.read()
        c.close()
        return r.status, r.getheader("Location")

    def test_open_at_line(self):
        """Owner request 2026-09-30 (`message-history.ts:89`): a click also jumps to the line — the line number reaches
        the Files page (#/project/path?line=N), which scrolls to the line and highlights it."""
        target = os.path.join(self.proj, "docs", "decisions.md")
        self.assertEqual(self.open_("path=" + quote(target) + "&line=89"), (302, "/#/proj/docs/decisions.md?line=89"))
        self.assertEqual(self.open_("path=" + quote(target) + "&line=x"), (302, "/#/proj/docs/decisions.md"))

    def test_open_path_as_the_session_wrote_it(self):
        """Owner report 2026-09-30: a link …/proj-wt2/proj-wt2/modules/files/server.py — the session wrote a path
        relative to a neighbouring folder and the page glued it to its own. The path is looked up from the session's
        folder, then from its parents; an already doubled one — without the repeat."""
        docs = os.path.join(self.proj, "docs")
        ok = (302, "/#/proj/docs/decisions.md")
        self.assertEqual(self.open_("cwd=" + quote(self.proj) + "&path=docs/decisions.md"), ok)
        self.assertEqual(self.open_("cwd=" + quote(self.proj) + "&path=proj/docs/decisions.md"), ok)  # from the neighbours
        # from the repo root: a folder from the address that no session has is not taken — only the session's own folder
        self.assertEqual(self.open_("cwd=" + quote(docs) + "&path=docs/decisions.md")[0], 404)
        journal.Journal(os.path.join(self.dir, "journal.jsonl")).append("session.requested", "d",
                                                                         {"session_id": "s", "cwd": docs})
        self.assertEqual(self.open_("session=d&path=docs/decisions.md"), (302, ok[1] + "?session=d"))
        self.assertEqual(self.open_("path=" + quote(os.path.join(self.proj, "proj", "docs", "decisions.md"))), ok)
        # `brief.md` for a file docs/brief.md — looked up by name inside the session folder; ambiguous — the shortest
        self.assertEqual(self.open_("cwd=" + quote(self.proj) + "&path=decisions.md"), ok)
        os.makedirs(os.path.join(self.proj, "old", "docs"))
        open(os.path.join(self.proj, "old", "docs", "decisions.md"), "w").close()
        self.assertEqual(self.open_("cwd=" + quote(self.proj) + "&path=decisions.md"), ok)
        # hub.json comes from the hub's settings folder, even if the repo has an example with the same name
        conf = os.path.join(self.dir, "conf")
        os.makedirs(conf)
        open(os.path.join(conf, "hub.json"), "w").close()
        open(os.path.join(docs, "hub.json"), "w").close()
        self.assertEqual(paths.find("hub.json", self.proj, extra=[conf]), os.path.join(conf, "hub.json"))
        self.assertIsNone(self.files.locate(paths.find("secret.txt", self.proj)))  # from the tailnet outside — no
        self.assertEqual(self.open_("cwd=" + quote(self.proj) + "&path=nope.txt")[0], 404)

    def test_open_takes_the_folder_from_the_session_not_the_address(self):
        """/open looks the path up from the session folder by its name, not from the `cwd` in the address: any site can
        send the link (and `<img src>`), and in the folder it names git would run a foreign repo's settings. A folder
        from the address counts only if it already is someone's session or project folder (links made before the
        session name was in them)."""
        evil = tempfile.mkdtemp(prefix="evil-")
        self.addCleanup(shutil.rmtree, evil, True)
        with open(os.path.join(evil, "doc.md"), "w") as f:
            f.write("x")
        with open(os.path.join(self.proj, "doc.md"), "w") as f:
            f.write("y")
        j = journal.Journal(os.path.join(self.dir, "journal.jsonl"))
        j.append("session.requested", "w", {"session_id": "s", "cwd": self.proj})
        j.append("session.started", "w", {"session_id": "s"})
        self.assertEqual(self.open_("path=doc.md&cwd=" + quote(evil))[0], 404)   # only in the address — not taken
        self.assertEqual(self.open_("path=doc.md&session=w&cwd=" + quote(evil)), (302, "/#/proj/doc.md?session=w"))
        self.assertEqual(self.open_("path=doc.md&cwd=" + quote(self.proj))[0], 302)   # a known session's folder
        self.assertEqual(self.open_("path=doc.md&session=nobody&cwd=" + quote(evil))[0], 404)

    def test_reading_the_journal_does_not_copy_the_state_per_event(self):
        """10-18: Files folded the journal with a state copy on every event — on a big hub ~25 ms per event, the first
        open after a restart and after a journal rotation waited tens of seconds. The outcome of events is recorded in
        the journal (core.rejected on the next line): the known ones are in place, as with other readers
        (reducer.fold)."""
        from core import reducer
        j = journal.Journal(os.path.join(self.dir, "journal.jsonl"))
        j.append("session.requested", "w", {"session_id": "s", "cwd": self.proj})
        for i in range(30):
            j.append("session.requested", f"n{i}", {"session_id": f"s{i}", "cwd": self.proj})
        j.append("session.started", "w", {"session_id": "s"})
        real, copies = reducer.reduce, []
        with mock.patch.object(reducer, "reduce", lambda st, ev, known=None: (
                copies.append(1) if known is None else None) or real(st, ev, known)):
            st = self.files.state()
        self.assertEqual((st["sessions"]["w"]["cwd"], len(st["sessions"])), (self.proj, 31))
        self.assertLessEqual(len(copies), 1)   # only the last one: its outcome is not recorded yet
        before = self.files.state()
        j.append("session.requested", "late", {"session_id": "s9", "cwd": self.proj})
        self.assertIn("late", self.files.state()["sessions"])
        self.assertNotIn("late", before["sessions"])   # the earlier state of another request does not change under it

    def test_first_read_starts_from_the_state_file_not_from_the_last_snapshot(self):
        """11-10: a periodic snapshot waits until the journal grows by the weight of the state file (daemon.snapshot_due),
        so the last snapshot lags by tens of thousands of events, and the first open of "Files" after a restart folded them
        all (34 s on a 30 MB state). The commands start from the state file and its tail (journal.load_state): so does
        the module, and the result is the same state."""
        from core import reducer
        path = os.path.join(self.dir, "journal.jsonl")
        j = journal.Journal(path)
        j.append("session.requested", "w", {"session_id": "s", "cwd": self.proj})
        for i in range(40):
            j.append("session.requested", f"n{i}", {"session_id": f"s{i}", "cwd": self.proj})
        journal.save_state(self.dir, reducer.replay(journal.read(path)))
        j.append("session.requested", "late", {"session_id": "sl", "cwd": self.proj})
        j.append("session.started", "w", {"session_id": "s"})
        real, folded = reducer.reduce, []
        with mock.patch.object(reducer, "reduce", lambda st, ev, known=None: folded.append(ev["seq"]) or real(st, ev, known)):
            st = Files(config=lambda: self.cfg, state_dir=self.dir).state()
        self.assertEqual(len(folded), 2)   # the two events after the state file, not the 43 of the journal
        want = reducer.replay(journal.read(path))
        self.assertEqual((st["seq"], sorted(st["sessions"]), st["sessions"]["w"]["state"]),
                         (want["seq"], sorted(want["sessions"]), want["sessions"]["w"]["state"]))

    def test_first_read_ignores_a_state_file_of_another_journal(self):
        """A state file that does not continue this journal (the journal was started anew, a file from another hub) is
        not trusted: the journal is folded whole, as before."""
        from core import reducer
        path = os.path.join(self.dir, "journal.jsonl")
        j = journal.Journal(path)
        j.append("session.requested", "w", {"session_id": "s", "cwd": self.proj}, ts=1700000000.5)
        journal.save_state(self.dir, reducer.replay(journal.read(path)))
        os.unlink(path)   # a new journal: it starts from the same number 1
        j = journal.Journal(path)
        # the first line of the same length as the old one (11-18: a line boundary at the saved offset, with the same
        # first number — the old state file was taken for this journal's, one run in thirty by the length of the time)
        j.append("session.requested", "x", {"session_id": "s", "cwd": self.proj}, ts=1700000001.5)
        j.append("session.requested", "b", {"session_id": "sb", "cwd": self.proj})
        with open(path, "rb") as f:
            self.assertEqual(journal.read_state_file(self.dir)["offset"], len(f.readline()))   # the trap is set: a line boundary
        st = Files(config=lambda: self.cfg, state_dir=self.dir).state()
        self.assertEqual(sorted(st["sessions"]), ["b", "x"])

    def test_first_read_does_not_trust_a_state_file_ahead_of_the_journal_it_reads(self):
        """A state file whose journal offset lies past the end of what is readable (the journal was cut back at hubd's start,
        a copy of the folder) is not trusted; nor is a state file older than the journal's last snapshot, and a line
        without a number does not break the read."""
        from core import reducer
        path = os.path.join(self.dir, "journal.jsonl")
        j = journal.Journal(path)
        for i in range(6):
            j.append("session.requested", f"n{i}", {"session_id": f"s{i}", "cwd": self.proj})
        journal.save_state(self.dir, reducer.replay(journal.read(path)))
        with open(path, "rb") as f:
            lines = f.read().split(b"\n")
        with open(path, "wb") as f:   # the journal cut back: two events less than the state file has folded
            f.write(b"\n".join(lines[:4]) + b"\n")
        self.assertEqual(sorted(Files(config=lambda: self.cfg, state_dir=self.dir).state()["sessions"]), ["n0", "n1", "n2", "n3"])
        j = journal.Journal(path)   # the next event takes the next number, and a snapshot after the state file
        j.append("session.requested", "n4", {"session_id": "s4", "cwd": self.proj})
        j.append("core.snapshot", None, {"why": "periodic", "state": reducer.replay(journal.read(path))})
        with open(path, "ab") as f:
            f.write(b'{"type": "odd"}\n')
        j.append("session.requested", "n5", {"session_id": "s5", "cwd": self.proj})
        real, folded = reducer.reduce, []
        with mock.patch.object(reducer, "reduce", lambda st, ev, known=None: folded.append(ev.get("seq")) or real(st, ev, known)):
            st = Files(config=lambda: self.cfg, state_dir=self.dir).state()
        self.assertEqual(sorted(st["sessions"]), [f"n{i}" for i in range(6)])
        self.assertLessEqual(len(folded), 3)   # after the snapshot, not the whole journal

    def test_a_rotation_seen_by_a_thread_poll_does_not_leave_an_empty_state(self):
        """The thread poll (track) meets a journal started anew first: the state it leaves is the new journal's, not the
        empty one, also for the requests that take the state of a moment ago."""
        from core import reducer
        path = os.path.join(self.dir, "journal.jsonl")
        j = journal.Journal(path)
        j.append("session.requested", "old", {"session_id": "s", "cwd": self.proj})
        self.files.state()
        os.replace(path, path + ".archived")
        j = journal.Journal(path)
        j.append("session.requested", "new", {"session_id": "s2", "cwd": self.proj})
        self.files.track()
        self.assertEqual(sorted(self.files.state(stale_ok=True)["sessions"]), ["new"])

    def test_a_spec_made_a_moment_ago_has_its_strip(self):
        """spec_of takes the state of a moment ago, and a spec the state has not seen yet is looked for in the current one."""
        from core import specmode
        j = journal.Journal(os.path.join(self.dir, "journal.jsonl"))
        j.append("session.requested", "w", {"session_id": "s", "cwd": self.proj})
        self.files.state()
        path = os.path.join(self.dir, specmode.DIR, "d1", "spec.md")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("# spec\n")
        self.assertIsNone(self.files.spec_of(os.path.realpath(path)))   # no task has it
        with mock.patch.object(self.files, "_spec_tasks", side_effect=[[], []]) as tasks:
            self.assertIsNone(self.files.spec_of(os.path.realpath(path)))
            self.assertEqual(tasks.call_count, 2)   # the stale state first, then the current one

    def test_a_session_made_a_moment_ago_opens_its_links(self):
        """/open takes the session's folder from the state of a moment ago and, when the name is not there, from the current one."""
        j = journal.Journal(os.path.join(self.dir, "journal.jsonl"))
        j.append("session.requested", "w", {"session_id": "s", "cwd": self.proj})
        self.files.state()
        j.append("session.requested", "fresh", {"session_id": "s2", "cwd": self.dir})
        self.assertEqual(self.files.session_cwd("fresh"), self.dir)
        self.assertIsNone(self.files.session_cwd("nobody"))

    def test_requests_that_only_read_do_not_fold_the_journal_again_within_a_moment(self):
        """11-10: every request read the journal on and copied the whole state (0.3 s on a 30 MB state) while a busy hub
        wrote events all the time; the page's one open makes four such requests in a row under one lock. Reads that only
        look (the language, the session list, a spec's strip) take the state of a moment ago; an action and a comment
        thread read the journal on as before."""
        from core import reducer
        j = journal.Journal(os.path.join(self.dir, "journal.jsonl"))
        j.append("session.requested", "w", {"session_id": "s", "cwd": self.proj})
        self.files.state()
        folds = []
        real = reducer.fold
        with mock.patch.object(reducer, "fold", lambda st, evs: folds.append(len(evs)) or real(st, evs)):
            j.append("session.requested", "late", {"session_id": "s9", "cwd": self.proj})
            self.assertNotIn("late", self.files.state(stale_ok=True)["sessions"])   # a moment ago: not folded
            self.assertEqual(folds, [])
            self.assertIn("late", self.files.state()["sessions"])   # an action wants the current one
            self.assertEqual(folds, [1])
            with mock.patch.object(files_server, "STATE_TTL", 0):
                j.append("session.requested", "later", {"session_id": "s8", "cwd": self.proj})
                self.assertIn("later", self.files.state(stale_ok=True)["sessions"])   # the moment has passed

    def test_a_thread_poll_reads_the_journal_without_folding_it(self):
        """11-10: the comments panel asks for the threads every 4 s while a file is open; each ask folded the journal into
        the state (a copy of the whole of it). Session replies come from the events themselves: the poll reads them and
        leaves the folding to the next request that needs the state, which then sees everything the poll read."""
        from core import reducer
        j = journal.Journal(os.path.join(self.dir, "journal.jsonl"))
        j.append("session.requested", "w", {"session_id": "s", "cwd": self.proj})
        self.files.state()
        folds = []
        real = reducer.fold
        with mock.patch.object(reducer, "fold", lambda st, evs: folds.append(len(evs)) or real(st, evs)):
            j.append("session.requested", "late", {"session_id": "s9", "cwd": self.proj})
            self.files.track()
            j.append("session.requested", "later", {"session_id": "s8", "cwd": self.proj})
            self.files.track()
            self.assertEqual(folds, [])
            self.assertEqual(sorted(self.files.state()["sessions"]), ["late", "later", "w"])
            self.assertEqual(folds, [2])   # both events, once, and none twice
            self.files.track()
            self.assertEqual(sorted(self.files.state()["sessions"]), ["late", "later", "w"])
            self.assertEqual(folds, [2])

    def test_git_in_a_found_repo_never_runs_its_fsmonitor(self):
        """git in the found folder runs without the repo's core.fsmonitor (it is a command git would execute) and
        without a bare repo found by walking up the folders."""
        repo = os.path.realpath(tempfile.mkdtemp(prefix="fsmon-"))   # macOS: /var → /private/var, git gives the real one
        canary = os.path.join(tempfile.mkdtemp(prefix="canary-"), "ran")
        self.addCleanup(shutil.rmtree, repo, True)
        self.addCleanup(shutil.rmtree, os.path.dirname(canary), True)
        git = lambda *a: subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True)  # noqa: E731
        git("init", "-q")
        os.makedirs(os.path.join(repo, "docs"))
        with open(os.path.join(repo, "docs", "brief.md"), "w") as f:
            f.write("x")
        git("add", ".")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i")
        hook = f'touch "{canary}"; false' if os.name != "nt" else f'cmd /c type nul > "{canary}"'
        git("config", "core.fsmonitor", hook)
        self.assertEqual(paths.find("brief.md", repo), os.path.join(repo, "docs", "brief.md"))
        paths.worktrees(repo)
        self.files.worktrees(repo)
        self.assertFalse(os.path.exists(canary))

    def test_write_does_not_follow_a_planted_temp_link(self):
        """Writing goes through a new temp file (O_EXCL): a link planted next to it with the former temp file name
        does not divert the write into someone else's file."""
        target = os.path.join(self.dir, "outside.txt")
        with open(target, "w") as f:
            f.write("keep")
        notes = os.path.join(self.proj, "notes.md")
        with open(notes, "w") as f:
            f.write("old")
        winsafe.symlink(self, target, notes + ".hub-files.tmp")
        sha = self.req("GET", "/api/read?root=proj&path=notes.md")[1]["sha"]
        code, _ = self.req("POST", "/api/write", {"root": "proj", "path": "notes.md", "text": "new", "sha": sha})
        self.assertEqual(code, 200)
        with open(target) as f:
            self.assertEqual(f.read(), "keep")
        with open(notes) as f:
            self.assertEqual(f.read(), "new")
        self.assertFalse(os.path.islink(notes))
        self.assertEqual([n for n in os.listdir(self.proj) if n.endswith(".tmp") and not os.path.islink(
            os.path.join(self.proj, n))], [])   # no temp file left behind

    def test_cross_site_requests_are_refused_but_links_open(self):
        """`<img src=…/open?…>` from a foreign site — 403 before the path lookup; following a link — as before. A
        /render report in a sandbox (opaque origin: the browser sends its css and images as cross-site) still gets
        its own files."""
        target = quote(os.path.join(self.proj, "docs", "decisions.md"))
        no_cors = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"}
        go = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}
        self.assertEqual(self.raw("GET", "/open?path=" + target, no_cors)[0], 403)
        self.assertEqual(self.raw("GET", "/api/roots", {**no_cors, "Sec-Fetch-Mode": "cors"})[0], 403)
        self.assertEqual(self.raw("GET", "/raw?root=proj&path=docs/decisions.md", no_cors)[0], 403)
        self.assertEqual(self.raw("GET", "/open?path=" + target, go)[0], 302)
        self.assertEqual(self.raw("GET", "/render/proj/docs/decisions.md", no_cors)[0], 200)
        code, _ = self.req("POST", "/api/write", {"root": "proj", "path": "docs/decisions.md", "text": "x", "sha": ""},
                           headers=go)
        self.assertEqual(code, 403)   # a form from a foreign site — a navigation, but not a GET
        self.assertEqual(self.raw("GET", "/api/roots", {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "cors"})[0], 200)

    def test_worktree_of_project(self):
        """Owner report 2026-09-30: "the file does not belong to the project ~/…/proj-wt2" keeps happening — a session
        in a project's git worktree writes paths from its own copy; a project's worktree is also its folder."""
        git = lambda *a: subprocess.run(["git", "-C", self.proj, *a], check=True, capture_output=True)  # noqa: E731
        shutil.rmtree(os.path.join(self.proj, ".git"))
        git("init", "-q")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
        wt = os.path.join(self.dir, "proj-wt2")
        git("worktree", "add", "-q", "-b", "dev", wt)
        with open(os.path.join(wt, "notes.md"), "w", newline="") as f:
            f.write("from worktree\n")
        code, roots = self.req("GET", "/api/roots")
        self.assertEqual([r["name"] for r in roots], ["Mac", "proj", "proj@proj-wt2"])
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/open?path=" + quote(os.path.join(wt, "notes.md")), headers={"Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual((r.status, r.getheader("Location")), (302, "/#/proj%40proj-wt2/notes.md"))
        self.assertEqual(self.req("GET", "/api/read?root=proj%40proj-wt2&path=notes.md")[1]["text"], "from worktree\n")

    def test_relative_path_written_from_a_worktree(self):
        """Owner report 2026-09-30: a plan `.harness/plans/active/…md` from a session did not open — the session is
        recorded at the repo root but worked in its git worktree (.claude/worktrees/<branch>): the file exists only
        there."""
        git = lambda *a: subprocess.run(["git", "-C", self.proj, *a], check=True, capture_output=True)  # noqa: E731
        shutil.rmtree(os.path.join(self.proj, ".git"))
        git("init", "-q")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
        wt = os.path.join(self.proj, ".claude", "worktrees", "feat+X-1")
        git("worktree", "add", "-q", "-b", "feat", wt)
        plan = os.path.join(wt, ".harness", "plans", "active", "x-plan.md")
        os.makedirs(os.path.dirname(plan))
        open(plan, "w").close()
        self.assertEqual(os.path.realpath(paths.find(".harness/plans/active/x-plan.md", self.proj)),
                         os.path.realpath(plan))

    def raw(self, method, path, headers=None):
        """The whole response: code, headers, body — for header checks (CSP, CORS)."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, headers={"Host": f"127.0.0.1:{self.port}", **(headers or {})})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, data

    def test_page_files_are_cached_and_packed(self):
        """10-18: Files' styles, scripts and libraries (mermaid — 3.5 MB) were sent again on every file open, without
        compression: from the tailnet and through a tunnel — hundreds of KB and megabytes on every click on a link.
        As with the hub page: the browser keeps the file and asks (no-cache) — unchanged gives a 304 without a body;
        text is gzip if the browser accepts it."""
        import gzip
        for path in ("/vendor/marked.min.js", "/static/files.js", "/web/css/base.css"):
            code, h, body = self.raw("GET", path, {"Accept-Encoding": "gzip"})
            self.assertEqual((code, h.get("content-encoding"), h.get("cache-control")), (200, "gzip", "no-cache"), path)
            self.assertIn("accept-encoding", h.get("vary", "").lower())
            plain = self.raw("GET", path)
            self.assertEqual(gzip.decompress(body), plain[2])
            self.assertNotIn("content-encoding", plain[1])
            code, h2, body = self.raw("GET", path, {"If-None-Match": h["etag"]})
            self.assertEqual((code, body, h2.get("etag")), (304, b"", h["etag"]), path)
            self.assertIn("frame-ancestors", h2.get("content-security-policy", ""))
        self.assertEqual(self.raw("GET", "/static/nope.js")[0], 404)

    def test_render_html_in_sandbox(self):
        """A session's HTML report is a page, not text: /render/<root>/<path> serves the file as is, neighbouring css
        and images through relative links. CSP sandbox without allow-same-origin: the document has an opaque origin,
        its scripts read neither the Files API nor its localStorage; only Files itself may embed it."""
        docs = os.path.join(self.proj, "docs")
        with open(os.path.join(docs, "report.html"), "w") as f:
            f.write('<link rel="stylesheet" href="style.css"><h1>Report</h1><script>fetch("/api/roots")</script>')
        with open(os.path.join(docs, "style.css"), "w") as f:
            f.write("h1 { color: red }")
        code, h, body = self.raw("GET", "/render/proj/docs/report.html")
        self.assertEqual(code, 200)
        self.assertTrue(h["content-type"].startswith("text/html"), h["content-type"])
        self.assertIn(b"<h1>", body)
        csp = h.get("content-security-policy", "")
        self.assertTrue(csp.startswith("sandbox allow-scripts allow-popups"), csp)
        self.assertNotIn("allow-same-origin", csp)
        self.assertIn("frame-ancestors 'self'", csp)
        self.assertEqual((h.get("x-content-type-options"), h.get("cache-control")), ("nosniff", "no-store"))
        code, h, body = self.raw("GET", "/render/proj/docs/style.css")  # a relative link from report.html
        self.assertEqual((code, body), (200, b"h1 { color: red }"))
        self.assertTrue(h["content-type"].startswith("text/css"), h["content-type"])
        self.assertIn("sandbox", h.get("content-security-policy", ""))
        # names with spaces and non-Latin letters — the segments are url-encoded
        with open(os.path.join(docs, "η αναφορά μου.html"), "w") as f:
            f.write("<p>yes</p>")
        self.assertEqual(self.raw("GET", "/render/proj/docs/" + quote("η αναφορά μου.html"))[0], 200)
        self.assertEqual(self.raw("GET", "/render/" + quote("proj") + "/docs%2Freport.html")[0], 200)

    def test_only_the_hub_page_may_frame_files(self):
        """Only the hub page itself (a file in its panel) may embed Files — not a foreign site that would slip it
        under its own click: frame-ancestors is 'self' and the hub page's addresses (localhost with its port and its
        tailnet addresses). /render keeps its own sandbox and is framed by the same addresses: a question card on the hub
        page shows an option's html report as a live preview."""
        json.dump({"ok": True, "ts": time.time(), "owner": "me@x",
                   "hosts": ["100.1.2.3:8798", "100.1.2.3:8797", "mac.tail.ts.net:8797", "bad host;script-src *:8797"]},
                  open(os.path.join(self.dir, "tailnet.json"), "w"))
        with open(os.path.join(self.proj, "docs", "report.html"), "w") as f:
            f.write("<h1>Report</h1>")

        def policies(path):
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            c.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}"})
            r = c.getresponse()
            r.read()
            c.close()
            return r.msg.get_all("Content-Security-Policy") or []

        page = ("frame-ancestors 'self' http://localhost:8797 http://127.0.0.1:8797"
                " http://100.1.2.3:8797 http://mac.tail.ts.net:8797")
        with mock.patch.dict(os.environ, {"HUB_WEB_PORT": "8797"}):
            for path in ("/", "/static/files.js", "/api/roots", "/open?path=nope", "/nope"):
                self.assertEqual(policies(path), [page], path)
            raw = policies("/raw?root=proj&path=docs%2Fdecisions.md")   # the file's own CSP + the same sources
            self.assertEqual(raw, [files_server.RAW_CSP + "; sandbox; " + page])
            self.assertEqual(policies("/render/proj/docs/report.html"),
                             [files_server.RENDER_CSP + page.removeprefix("frame-ancestors 'self'")])
        with mock.patch.dict(os.environ, {"HUB_WEB_PORT": ""}):    # the default page port; tailnet is not connected
            json.dump({"ok": False}, open(os.path.join(self.dir, "tailnet.json"), "w"))
            self.assertEqual(policies("/"), ["frame-ancestors 'self' http://localhost:8787 http://127.0.0.1:8787"])

    def test_render_links_open_in_new_tab(self):
        """A link in an html report opens in a new tab, not inside the Files window (owner report 2026-10-08: all
        links open inside the file viewer): a script is appended to the html that sets target=_blank on outward links
        (#… anchors stay in place), the sandbox lets the tab out without a sandbox (a foreign site works as usual,
        /render is again under its own CSP)."""
        with open(os.path.join(self.proj, "docs", "links.html"), "w") as f:
            f.write('<a href="https://example.com/">outward</a><a href="#part">to the section</a>')
        code, h, body = self.raw("GET", "/render/proj/docs/links.html")
        self.assertEqual(code, 200)
        self.assertIn("allow-popups-to-escape-sandbox", h.get("content-security-policy", ""))
        self.assertNotIn("allow-same-origin", h.get("content-security-policy", ""))
        self.assertIn(b"files-links", body)
        self.assertIn(b'_blank', body)
        self.assertIn(b'"noopener"', body)  # an opened site cannot replace the report through window.opener
        self.assertTrue(body.startswith(b'<a href="https://example.com/">'), body[:60])  # the document itself is as it was
        with open(os.path.join(self.proj, "docs", "style.css"), "w") as f:
            f.write("a{}")
        self.assertNotIn(b"files-links", self.raw("GET", "/render/proj/docs/style.css")[2])  # css is left alone

    def test_render_nothing_outside_projects(self):
        for path in ("/render/proj/../secret.txt", "/render/proj/docs/..%2F..%2Fsecret.txt", "/render/proj/leak.txt",
                     "/render/proj/.git", "/render/nope/x.html", "/render/proj/docs/nope.html", "/render/proj/docs",
                     "/render/proj", "/render/"):
            self.assertEqual(self.raw("GET", path)[0], 404, path)
        # from the tailnet there is no "Mac" root — and /render does not serve through it
        self.assertIsNone(Files(config=lambda: self.cfg, state_dir=self.dir).resolve("Mac", "etc/hosts", local=False))

    def test_no_cors_for_framed_scripts(self):
        """A script from rendered html (opaque origin) sends fetch to the Files API: a POST with X-Hub needs a CORS
        preflight, and the server does not approve it — it gives no Access-Control-Allow-* anywhere; without X-Hub —
        403."""
        self.assertEqual(self.req("POST", "/api/write", {"root": "proj", "path": "docs/decisions.md", "text": "x",
                                                         "sha": ""}, headers={"X-Hub": ""})[0], 403)
        pre = {"Origin": "null", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-hub"}
        for method, path in (("OPTIONS", "/api/write"), ("OPTIONS", "/api/comment"), ("GET", "/api/roots"),
                             ("GET", "/render/proj/docs/decisions.md"), ("GET", "/"), ("GET", "/api/themes")):
            code, h, _ = self.raw(method, path, pre)
            self.assertFalse([k for k in h if k.startswith("access-control-")], (method, path, h))
            if method == "OPTIONS":
                self.assertGreaterEqual(code, 400, path)

    def test_themes_like_the_hub(self):
        """The hub's theme is kept in localStorage of its page (port 8787), Files is another origin and does not see
        it: the theme list is the same as the page's (own files and plugin themes), by the same modules/web code; the
        theme's art and files are served here too."""
        self.assertEqual(self.req("GET", "/api/themes")[1]["themes"], [])  # no themes — no choice on the page
        cfgdir = os.path.join(self.dir, "cfg")
        own = os.path.join(cfgdir, "themes")
        plugin = os.path.join(self.dir, "plugin")
        os.makedirs(own)
        os.makedirs(plugin)
        with open(os.path.join(own, "night.json"), "w") as f:
            json.dump({"name": "Night", "dark": True, "art": {"image": "sky.png"}, "font": "f.woff2",
                       "colors": {"background": "#101010", "foreground": "#eeeeee", "primary": "#88aaff"}}, f)
        with open(os.path.join(own, "sky.png"), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\nfake")
        with open(os.path.join(own, "f.woff2"), "wb") as f:
            f.write(b"wOF2fake")
        with open(os.path.join(plugin, "sand.json"), "w") as f:
            json.dump({"name": "Sand", "colors": {"background": "#fbf6ec", "foreground": "#3b3226", "primary": "#b4572e"}}, f)
        self.cfg["extensions"] = {"me:sand": {"path": plugin, "themes": ["sand.json"]}}
        with mock.patch.dict(os.environ, {"HUB_CONFIG": os.path.join(cfgdir, "hub.json")}):
            code, got = self.req("GET", "/api/themes")
            self.assertEqual(code, 200)
            by = {t["id"]: t for t in got["themes"]}
            self.assertEqual(sorted(by), ["me:sand:sand.json", "own:night.json"])
            self.assertEqual(by["own:night.json"]["theme"]["name"], "Night")
            art = by["own:night.json"]["theme"]["art"]["url"]
            self.assertTrue(art.startswith("/api/themes/art?id="), art)
            code, body = self.req("GET", art)
            self.assertEqual((code, body[:4]), (200, b"\x89PNG"))
            font = by["own:night.json"]["theme"]["font_url"]
            self.assertEqual(self.req("GET", font)[1][:4], b"wOF2")
            self.assertEqual(self.req("GET", "/api/themes/file?id=own:night.json&f=../tailnet.json")[0], 404)
            self.assertEqual(self.req("GET", "/api/themes/art?id=nobody")[0], 404)

    def test_page_theme_picker(self):
        """On the page: the theme from the module's localStorage — before the first paint (the same {id, theme} format
        as the hub's), ?theme=<id> selects and remembers it, the header has the core's themes (Clean, Classic) and the
        others; Clean's colors and font come from the hub page's palette.css before the module's own styles."""
        _, page = self.req("GET", "/")
        page = page.decode()
        boot = page.index("theme.js")
        self.assertLess(boot, page.index("/static/files.css"))  # before styles and markup: no flicker
        code, js = self.req("GET", "/static/theme.js")
        self.assertEqual(code, 200)
        js = js.decode()
        for needle in ("localStorage", "/api/themes", 't("web.themes.classic")', "theme", "setProperty"):
            self.assertIn(needle, js, needle)
        self.assertLess(page.index("/web/clean/palette.css"), page.index("/static/files.css"))
        code, css = self.req("GET", "/web/clean/palette.css")
        self.assertEqual(code, 200)
        self.assertIn(b":root.th-clean", css)

    def test_built_in_themes_are_the_same_as_on_the_hub_page(self):
        """The core's themes (Clean by default, Classic) and the move of the Clean plugin's choice: one table in both
        pages — "Files" is another origin with its own choice, the same ids mean the same theme."""
        import re
        with open(os.path.join(os.path.dirname(__file__), "..", "modules", "web", "static", "js", "themes.js")) as f:
            page = f.read()
        code, js = self.req("GET", "/static/theme.js")
        js = js.decode()
        for name in ("DEFAULT", "BUILTIN", "MOVED"):
            got = [re.search(rf"const {name} = (.*?);", src).group(1) for src in (page, js)]
            self.assertEqual(got[0], got[1], name)

    def test_theme_colors_are_the_same_as_on_the_hub_page(self):
        """A theme color the hub page knows (code-bg…) is not rejected here: the lists of keys of both pages match."""
        import re
        keys = lambda src: re.search(r"const KEYS = \[(.*?)\];", src, re.S).group(1).replace("\n", " ").split()
        with open(os.path.join(os.path.dirname(__file__), "..", "modules", "web", "static", "js", "themes.js")) as f:
            page = f.read()
        code, js = self.req("GET", "/static/theme.js")
        self.assertEqual(keys(js.decode()), keys(page))
        self.assertIn('"code-bg"', js.decode())

    def test_theme_picker_draws_its_own_arrow_with_room(self):
        """10-08: the system arrow of the theme picker stuck to the right border — its own arrow and room for it."""
        code, css = self.req("GET", "/static/files.css")
        rule = css.decode().split(".theme-pick {", 1)[1].split("}", 1)[0]
        self.assertIn("appearance: none", rule)
        self.assertIn("padding-inline: var(--space-2) 22px", rule)   # the room at the line end, the arrow side
        hover = css.decode().split(".theme-pick:hover {", 1)[1].split("}", 1)[0]
        self.assertNotIn("background:", hover)   # the shorthand erased the arrow on hover (10-08)

    def test_foreign_host(self):
        """Who is let in from the tailnet is in tests/test_tailnet.py (AccessTest); here — that the check is in place."""
        self.assertEqual(self.req("GET", "/api/roots", host="evil.example")[0], 403)
        self.assertEqual(self.req("GET", "/api/roots", host="100.1.2.3:8788")[0], 403)  # from the Mac, but a Host from the tailnet


if __name__ == "__main__":
    unittest.main()
