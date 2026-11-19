"""The page opens without revalidating its files: index.html points them at /static/v/<version>/…, and a file there is
cached for good only when the bytes sent are the bytes of that version. Before, every open revalidated ~70 files
(304 each): over a phone's ~100 ms round trip that was most of the first screen."""
import tests  # noqa: F401
import hashlib
import http.client
import json
import os
import re
import shutil
import tempfile
from unittest import mock

import platforms

from modules.web import server
from tests.test_web import WebTest
from tests.test_web_e2e import PageInChrome

IMMUTABLE = "immutable"


class PageCache(WebTest):
    def get(self, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}", **(headers or {})})
        r = c.getresponse()
        out = r.status, r.read(), {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return out

    def copy_static(self):
        static = tempfile.mkdtemp(prefix="page-cache-")
        self.addCleanup(platforms.current.rmtree, static)
        shutil.copytree(server.STATIC, static, dirs_exist_ok=True)
        patch = mock.patch.object(server, "STATIC", static)
        patch.start()
        self.addCleanup(patch.stop)
        return static

    def versioned(self):
        """The version the page points its files at, and the page."""
        code, page, _ = self.get("/")
        self.assertEqual(code, 200)
        page = page.decode()
        m = re.search(r'<script type="importmap">(.*?)</script>', page, re.S)
        self.assertTrue(m, "no import map")
        got = json.loads(m.group(1))["imports"]["/static/js/"]
        self.assertRegex(got, r"^/static/v/[0-9a-f]{12}/js/$")
        return got.split("/")[3], page

    def test_the_page_points_every_file_at_its_version_and_maps_absolute_imports(self):
        ver, page = self.versioned()
        links = re.findall(r'<link rel="stylesheet" href="([^"]+)"', page)
        self.assertTrue(links and all(h.startswith((f"/static/v/{ver}/css/", f"/static/v/{ver}/clean/")) for h in links), links)
        self.assertNotIn('="/static/js/', page)
        self.assertNotIn('="/static/css/', page)
        m = re.search(r'<script type="importmap">(.*?)</script>', page, re.S)
        self.assertEqual(json.loads(m.group(1)), {"imports": {"/static/js/": f"/static/v/{ver}/js/"}})
        # the entry goes through the map: a browser without import maps loads all modules by the plain addresses
        # (as before), never one module under two addresses — a plugin importing /static/js/dom.js shares its state
        self.assertIn('<script type="module">import "/static/js/main.js";</script>', page)
        self.assertEqual(page.count('type="module"'), 1)
        # an empty cache: the entry starts loading at once, not after the vendor scripts (the scanner does not read
        # an inline import)
        self.assertIn(f'<link rel="modulepreload" href="/static/v/{ver}/js/main.js">', page)
        self.assertLess(page.index('type="importmap"'), page.index('rel="modulepreload"'))
        plain = re.findall(r'<script src="([^"]+)"></script>', page)
        # first the chosen look, in <head> before the first paint (js/first-paint.js, not a module), then the vendor ones
        self.assertEqual(plain[0], f"/static/v/{ver}/js/first-paint.js", plain)
        self.assertLess(page.index("first-paint.js"), page.index("<body"))
        vendor = plain[1:]
        self.assertEqual(len(vendor), 3, vendor)
        self.assertTrue(all(re.fullmatch(r"/vendor/v/[0-9a-f]{12}/\w+\.min\.js", v) for v in vendor), vendor)
        # the home screen icon keeps its plain address: a new address on every update would update the installed app
        self.assertIn('<link rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png">', page)
        # the map comes before anything that loads a module
        self.assertLess(page.index('type="importmap"'), page.index('type="module"'))
        self.assertLess(page.index('type="importmap"'), page.index('<link rel="stylesheet"'))

    def test_a_file_of_the_current_version_is_cached_for_good(self):
        ver, _ = self.versioned()
        code, body, h = self.get(f"/static/v/{ver}/js/main.js")
        self.assertEqual(code, 200)
        with open(os.path.join(server.STATIC, "js", "main.js"), "rb") as f:
            self.assertEqual(body, f.read())
        self.assertIn(IMMUTABLE, h["cache-control"])
        self.assertIn("max-age=31536000", h["cache-control"])
        self.assertTrue(h["content-type"].startswith("text/javascript"))
        # a font behind a relative url(../fonts/…) of a versioned stylesheet is under the same version
        fonts = sorted(f for f in os.listdir(os.path.join(server.STATIC, "fonts")) if f.endswith(".woff2"))
        code, _, h = self.get(f"/static/v/{ver}/fonts/{fonts[0]}")
        self.assertEqual((code, IMMUTABLE in h["cache-control"]), (200, True))
        # a revalidation (a hard reload) of the same bytes: 304, still for good
        code, body, h2 = self.get(f"/static/v/{ver}/js/main.js", {"If-None-Match": self.get(f"/static/v/{ver}/js/main.js")[2]["etag"]})
        self.assertEqual((code, body), (304, b""))
        self.assertIn(IMMUTABLE, h2["cache-control"])

    def test_the_old_addresses_stay_revalidated(self):
        for path in ("/static/js/main.js", "/vendor/marked.min.js"):
            code, _, h = self.get(path)
            self.assertEqual(code, 200)
            self.assertEqual(h["cache-control"], "no-cache")

    def test_vendor_scripts_of_the_current_version_are_cached_for_good(self):
        _, page = self.versioned()
        src = re.search(r'<script src="(/vendor/v/[0-9a-f]{12}/marked\.min\.js)"', page).group(1)
        code, body, h = self.get(src)
        self.assertEqual(code, 200)
        self.assertIn(IMMUTABLE, h["cache-control"])
        with open(os.path.join(server.VENDOR_DIR, "marked.min.js"), "rb") as f:
            self.assertEqual(body, f.read())
        ver = src.split("/")[3]
        for path in (f"/vendor/v/{ver}/nope.min.js", f"/vendor/v/{ver}/../server.py", "/vendor/v/zz/marked.min.js"):
            self.assertEqual(self.get(path)[0], 404, path)
        self.assertEqual(self.get(src + "?x=1")[0], 200)

    def test_an_entry_of_another_shape_leaves_the_page_as_it_was(self):
        # rather than the entry under a versioned src and plugins importing the plain address: two copies of a
        # module in a browser without import maps
        static = self.copy_static()
        path = os.path.join(static, "index.html")
        with open(path, encoding="utf-8") as f:
            page = f.read()
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(page.replace('<script type="module" src="/static/js/main.js">', '<script type="module" defer src="/static/js/main.js">'))
        code, got, _ = self.get("/")
        self.assertEqual(code, 200)
        self.assertNotIn(b"/static/v/", got)
        self.assertNotIn(b"importmap", got)
        self.assertIn(b'src="/static/js/main.js"', got)

    def test_another_version_gets_the_disk_file_without_long_caching(self):
        ver, _ = self.versioned()
        other = "0" * 12 if ver != "0" * 12 else "1" * 12
        code, body, h = self.get(f"/static/v/{other}/js/main.js")
        self.assertEqual(code, 200)
        self.assertEqual(h["cache-control"], "no-cache")
        with open(os.path.join(server.STATIC, "js", "main.js"), "rb") as f:
            self.assertEqual(body, f.read())

    def test_a_file_changed_after_the_page_was_served_is_not_cached_for_good(self):
        # an update lands between the page and its modules: the old version's address gets the new bytes, but only
        # for this load — otherwise the old version's address keeps the new bytes for a year (a rollback to it would
        # run a mix)
        static = self.copy_static()
        ver, _ = self.versioned()
        path = os.path.join(static, "js", "main.js")
        with open(path, "ab") as f:
            f.write(b"\n// changed\n")
        code, body, h = self.get(f"/static/v/{ver}/js/main.js")
        self.assertEqual(code, 200)
        self.assertTrue(body.endswith(b"// changed\n"))
        self.assertEqual(h["cache-control"], "no-cache")
        # the next page points at the new version, and that one is cached for good
        new, _ = self.versioned()
        self.assertNotEqual(new, ver)
        code, body, h = self.get(f"/static/v/{new}/js/main.js")
        self.assertEqual((code, IMMUTABLE in h["cache-control"]), (200, True))
        self.assertTrue(body.endswith(b"// changed\n"))

    def test_a_file_changed_within_the_walk_cache_is_not_cached_for_good(self):
        # the walk is cached for a moment: a file replaced inside that moment must not go out for good under the
        # version the walk still names
        static = self.copy_static()
        ver, _ = self.versioned()
        with mock.patch("time.time", return_value=__import__("time").time()):
            path = os.path.join(static, "js", "state.js")
            with open(path, "ab") as f:
                f.write(b"\n// changed\n")
            code, body, h = self.get(f"/static/v/{ver}/js/state.js")
        self.assertEqual(code, 200)
        self.assertEqual(h["cache-control"], "no-cache")

    def test_a_walk_that_began_earlier_does_not_overwrite_a_newer_one(self):
        # walk A has read the files before an update; the update lands and walk B (a page open) finishes first with
        # the new version; A finishing later must not put the old version back (the new files would go out
        # revalidated for the next seconds)
        from modules.web import pagecache
        static = self.copy_static()
        old, _ = pagecache.walk(static)
        real_walk, newer = os.walk, []

        def slow(top, *a, **k):
            yield from real_walk(top, *a, **k)
            if not newer:   # A has read every file: now the update and B
                newer.append(None)
                with open(os.path.join(static, "js", "feed.js"), "ab") as f:
                    f.write(b"\n// changed\n")
                newer[0] = pagecache.walk(static)[0]

        with mock.patch.object(pagecache.os, "walk", slow):
            self.assertEqual(pagecache.walk(static)[0], old)
        self.assertNotEqual(newer[0], old)
        self.assertEqual(pagecache.walk(static, pagecache.FRESH)[0], newer[0])

    def test_the_page_tag_follows_the_version(self):
        # index.html itself does not change when a module does: its tag must, or a 304 would keep the old version's
        # addresses (and their year-long copies) in the browser
        static = self.copy_static()
        _, _, h = self.get("/")
        tag = h["etag"]
        self.assertEqual(self.get("/", {"If-None-Match": tag})[0], 304)
        with open(os.path.join(static, "js", "feed.js"), "ab") as f:
            f.write(b"\n// changed\n")
        code, _, h = self.get("/", {"If-None-Match": tag})
        self.assertEqual(code, 200)
        self.assertNotEqual(h["etag"], tag)

    def test_versioned_addresses_stay_inside_static(self):
        ver, _ = self.versioned()
        for path in (f"/static/v/{ver}/../server.py", f"/static/v/{ver}/%2e%2e/server.py", f"/static/v/{ver}/",
                     f"/static/v/{ver}/js/nope.js", "/static/v/zz/js/main.js"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_the_version_is_the_content_of_static(self):
        static = self.copy_static()
        ver, _ = self.versioned()
        main = os.path.join(static, "js", "main.js")
        st = os.stat(main)
        os.utime(main, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))   # portable: sets the time itself; the version is the content
        self.assertEqual(self.versioned()[0], ver)
        with open(os.path.join(static, "js", ".DS_Store"), "wb") as f:   # Finder, editors: not a served file
            f.write(b"x")
        self.assertEqual(self.versioned()[0], ver)
        with open(os.path.join(static, "css", "base.css"), "ab") as f:
            f.write(b"\n")
        with mock.patch("time.time", return_value=__import__("time").time() + 10):
            self.assertNotEqual(self.versioned()[0], ver)

    def test_index_file_alone_keeps_plain_addresses(self):
        # a page server started before the update serves the new index.html as it is: no versions, no map — the files
        # must load from the plain addresses
        with open(os.path.join(server.STATIC, "index.html"), encoding="utf-8") as f:
            page = f.read()
        self.assertNotIn("/static/v/", page)
        self.assertNotIn("importmap", page)
        self.assertIn('<script type="module" src="/static/js/main.js">', page)

    def test_sha_of_served_bytes_is_the_tag(self):
        ver, _ = self.versioned()
        _, body, h = self.get(f"/static/v/{ver}/js/api.js")
        self.assertEqual(h["etag"], 'W/"%s"' % hashlib.sha1(body).hexdigest()[:20])


class PageCacheInChrome(PageInChrome):
    def test_a_second_open_asks_the_server_for_no_page_file_and_plain_imports_share_the_modules(self):
        asked = []
        handler = self.server.RequestHandlerClass
        get = handler.do_GET

        def note(request):
            asked.append(request.path)
            return get(request)

        self.seed()
        url = f"http://127.0.0.1:{self.port}/"
        # a module imported by its plain address (plugins, tests) is the page's own: the state it reads is filled
        same = ("(async () => { const m = await import('/static/js/state.js'); window.__same = {"
                " snap: !!(m.S.snap && m.S.snap.sessions), plain: performance.getEntriesByType('resource')"
                ".filter((e) => e.name.includes('/static/js/')).length,"
                " versioned: performance.getEntriesByType('resource').filter((e) => /\\/static\\/v\\/[0-9a-f]{12}\\/js\\//.test(e.name)).length }; })(), 1")
        with mock.patch.object(handler, "do_GET", note):
            first = self.page({"go": url}, {"until": "!!document.querySelector('#list li')"},
                              {"eval": same}, {"until": "!!window.__same"}, {"eval": "window.__same"})
            self.assertTrue(first[1]["until"] and first[3]["until"], first)
            self.assertEqual(first[4]["eval"]["snap"], True)
            self.assertEqual(first[4]["eval"]["plain"], 0)
            self.assertGreater(first[4]["eval"]["versioned"], 40)
            self.assertTrue(any(p.startswith("/static/v/") for p in asked), asked)
            self.assertTrue(any(p.startswith("/vendor/v/") for p in asked), asked)
            del asked[:]
            again = self.page({"go": "about:blank"}, {"go": url}, {"until": "!!document.querySelector('#list li')"})
            self.assertTrue(again[2]["until"], again)
        # what the first screen waits for; icons (manifest, the home screen) keep plain, revalidated addresses
        self.assertEqual([p for p in asked if p.startswith(("/static/js/", "/static/css/", "/static/fonts/", "/static/v/",
                                                             "/vendor/"))], [])


def load_tests(loader, standard, pattern):
    import unittest
    return unittest.TestSuite(cls(n) for cls in (PageCache, PageCacheInChrome) for n in cls.__dict__ if n.startswith("test_"))
