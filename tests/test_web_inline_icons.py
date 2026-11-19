"""The core page's first icons resolve from its HTML, without a second sprite request."""
import tests  # noqa: F401
import http.client
import os
import re
import shutil
import tempfile
from unittest import mock

from core import i18n
from modules.web import server
from tests.test_web import WebTest
from tests.test_web_e2e import PageInChrome


class InlineIcons(WebTest):
    def page(self, etag=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/", headers={"Host": f"127.0.0.1:{self.port}", "Accept-Language": "en",
                                      **({"If-None-Match": etag} if etag else {})})
        r = c.getresponse()
        result = r.status, r.read().decode(), r.getheader("ETag")
        c.close()
        return result

    def test_initial_shell_icons_resolve_inside_the_same_html_response(self):
        status, page, _ = self.page()
        self.assertEqual(status, 200)
        self.assertIn('id="hub-icon-sprite"', page)
        self.assertLess(page.index('id="hub-icon-sprite"'), page.index('id="app"'))
        symbols = set(re.findall(r'<symbol id="([^"]+)"', page))
        uses = re.findall(r'<use href="([^"]+)"', page)
        self.assertGreaterEqual(len(uses), 3)
        self.assertTrue(all(href.startswith("#") and href[1:] in symbols for href in uses), uses)
        self.assertNotIn("<!-- hub-icon-sprite -->", page)
        self.assertEqual(self.req("GET", "/static/icons.svg")[0], 200)

    def test_html_cache_validator_changes_when_the_bundled_sprite_changes(self):
        static = tempfile.mkdtemp(prefix="inline-icons-")
        self.addCleanup(shutil.rmtree, static)
        for name in ("index.html", "icons.svg"):
            shutil.copyfile(os.path.join(server.STATIC, name), os.path.join(static, name))
        with mock.patch.object(server, "STATIC", static):
            _, page, before = self.page()
            self.assertEqual(self.page(before)[0], 304)
            path = os.path.join(static, "icons.svg")
            with open(path, encoding="utf-8") as f:
                sprite = f.read()
            with open(path, "w", encoding="utf-8") as f:
                f.write(sprite.replace("</svg>", '<symbol id="i-regression" viewBox="0 0 24 24"/></svg>'))
            status, changed, after = self.page(before)
            self.assertEqual(status, 200)
            self.assertNotEqual(after, before)
            self.assertIn('id="i-regression"', changed)

    def test_a_missing_sprite_does_not_break_the_page(self):
        static = tempfile.mkdtemp(prefix="inline-icons-")
        self.addCleanup(shutil.rmtree, static)
        shutil.copyfile(os.path.join(server.STATIC, "index.html"), os.path.join(static, "index.html"))
        with mock.patch.object(server, "STATIC", static):
            status, page, _ = self.page()
        self.assertEqual(status, 200)
        self.assertNotIn("<!-- hub-icon-sprite -->", page)
        uses = re.findall(r'<use href="([^"]+)"', page)
        self.assertTrue(uses and all(href.startswith("/static/icons.svg#") for href in uses), uses)

    def test_index_file_alone_points_its_icons_to_the_sprite_file(self):
        # a server started before the update serves the new index.html from disk without the inlined set: file
        # icons must still be found, via a link to /static/icons.svg rather than the set inside the page
        with open(os.path.join(server.STATIC, "index.html"), encoding="utf-8") as f:
            page = f.read()
        with open(os.path.join(server.STATIC, "icons.svg"), encoding="utf-8") as f:
            symbols = set(re.findall(r'<symbol id="([^"]+)"', f.read()))
        uses = re.findall(r'<use href="([^"]+)"', page)
        self.assertGreaterEqual(len(uses), 3)
        for href in uses:
            self.assertTrue(href.startswith("/static/icons.svg#"), href)
            self.assertIn(href.split("#", 1)[1], symbols, href)


class ColdIcons(PageInChrome):
    def test_cold_shell_and_dynamic_icons_render_without_the_external_sprite(self):
        requested = []
        handler = self.server.RequestHandlerClass
        original = handler.send_file

        def unavailable(request, path, *args, **kwargs):
            if path.endswith("icons.svg"):
                requested.append(path)
                return request.send(503, "sprite unavailable")
            return original(request, path, *args, **kwargs)

        self.seed()
        with mock.patch.object(handler, "send_file", unavailable):
            for width in (390, 1280):
                shots = os.environ.get("HUB_INLINE_ICON_SHOTS") or self.state
                os.makedirs(shots, exist_ok=True)
                self.page({"shot": os.path.join(self.state, "viewport.png"), "width": width},
                          {"go": f"http://127.0.0.1:{self.port}/"},
                          {"until": "document.getElementById('hub-icon-sprite') && document.querySelector('.search use').getBBox().width > 0"})
                got = self.page(
                    {"eval": "(() => { window.iconCheck = null; import('/static/js/dom.js').then(({icon, kit}) => { const n = icon('send'); n.id = 'cold-dynamic-icon'; document.body.append(n); kit.icons['test-theme'] = '/theme.png'; const themed = icon('test-theme'); const sprite = document.getElementById('hub-icon-sprite'); sprite.id = 'temporarily-detached'; const fallback = icon('send').querySelector('use').getAttribute('href'); sprite.id = 'hub-icon-sprite'; window.iconCheck = { fallback, href: n.querySelector('use').getAttribute('href'), theme: themed.classList.contains('kit-img') && themed.style.backgroundImage.includes('/theme.png') }; }); return true; })()"},
                    {"until": "window.iconCheck && document.getElementById('cold-dynamic-icon').querySelector('use').getBBox().width > 0"},
                    {"eval": "({ ...window.iconCheck, shell: document.querySelector('.search use').getAttribute('href'), width: document.querySelector('.search use').getBBox().width })"},
                    {"shot": os.path.join(shots, f"cold-icons-{width}.png"), "width": width})
                self.assertTrue(got[1]["until"])
                self.assertEqual(got[2]["eval"]["href"], "#i-send")
                self.assertEqual(got[2]["eval"]["shell"], "#i-search")
                self.assertEqual(got[2]["eval"]["fallback"], "/static/icons.svg#i-send")
                self.assertTrue(got[2]["eval"]["theme"])
                self.assertGreater(got[2]["eval"]["width"], 0)
            self.assertEqual(requested, [])

    def test_shell_icons_show_when_the_server_does_not_inline_the_sprite(self):
        # a server from before the set was inlined (old process after the files were updated): index.html as is
        def old_send_page(request, path, lang):
            with open(path, encoding="utf-8") as f:
                return request.send(200, i18n.render_html(f.read(), lang), "text/html; charset=utf-8", "no-cache")

        self.seed()
        with mock.patch.object(self.server.RequestHandlerClass, "send_page", old_send_page):
            got = self.page({"go": f"http://127.0.0.1:{self.port}/"},
                            {"until": "!!document.querySelector('#navmenu use')"},
                            {"eval": "(document.getElementById('nav-extra').hidden = false, true)"},
                            {"until": "document.querySelector('#navmenu use').getBBox().width > 0"
                                      " && document.querySelector('.search use').getBBox().width > 0"
                                      " && document.querySelector('#conn use').getBBox().width > 0"},
                            {"eval": "({ sprite: !!document.getElementById('hub-icon-sprite'),"
                                     " menu: document.querySelector('#navmenu use').getAttribute('href') })"})
        self.assertTrue(got[3]["until"], got)
        self.assertFalse(got[4]["eval"]["sprite"])
        self.assertEqual(got[4]["eval"]["menu"], "/static/icons.svg#i-menu")


def load_tests(loader, standard, pattern):
    import unittest
    return unittest.TestSuite(cls(n) for cls in (InlineIcons, ColdIcons) for n in cls.__dict__ if n.startswith("test_"))
