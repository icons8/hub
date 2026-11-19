"""A page plugin's stylesheet (the `css` field of its record) is in the page's <head> from the first byte: a plugin that
changes a token (--column, the width of the feed) no longer shows one width and then the other, which happened while its
CSS came only when the page had loaded its JS and the plugin called hub.addStyle. The stylesheet is also served and vetted
by the server, and the page without the new server (the old one for a few minutes after an update) still gets it."""
import tests  # noqa: F401
import json
import os
import re
import unittest
from unittest import mock

from tests.test_web_e2e import PageInChrome

FIRST = "window.__first = { column: null }; requestAnimationFrame(() => { window.__first.column = getComputedStyle(document.documentElement).getPropertyValue('--column').trim(); });"


class PluginCss(PageInChrome):
    def plugin(self, css, name="narrow", call_add_style=True):
        path = os.path.join(self.state, "plug-" + name)
        os.makedirs(os.path.join(path, "web"), exist_ok=True)
        with open(os.path.join(path, "web", "narrow.css"), "w") as f:
            f.write(":root { --column: 333px; }\n")
        with open(os.path.join(path, "main.js"), "w") as f:
            f.write("export default function register(hub) {\n"
                    + ("  hub.addStyle('web/narrow.css');\n" if call_add_style else "")
                    + "  hub.addSlot('topbar', { id: 'x', render(root) { root.textContent = 'x'; } });\n}\n")
        cfg = os.path.join(self.state, "hub.json")
        with open(cfg, "w") as f:
            json.dump({"extensions": {name: {"path": path, "web": "main.js", **({"css": css} if css is not None else {})}}}, f)
        env = mock.patch.dict(os.environ, {"HUB_CONFIG": cfg})
        env.start()
        self.addCleanup(env.stop)

    def load(self):
        return self.page({"go": f"http://127.0.0.1:{self.port}/", "init": FIRST},
                         {"until": "!!document.querySelector('link[data-plugin=narrow]') && !!document.querySelector('[data-plugin=narrow]:not(link)')", "ms": 10000},
                         {"eval": "JSON.stringify({ first: window.__first, now: getComputedStyle(document.documentElement).getPropertyValue('--column').trim(), "
                                  "links: document.querySelectorAll('link[data-plugin=narrow]').length, "
                                  "inHead: [...document.head.children].some((n) => n.tagName === 'LINK' && n.dataset.plugin === 'narrow') })"})

    def test_the_first_frame_already_has_the_plugin_token(self):
        self.plugin(["web/narrow.css"])
        got = json.loads(self.load()[-1]["eval"])
        self.assertEqual(got["first"]["column"], "333px", got)   # on main: 800px at the first frame, 333px later
        self.assertEqual(got["now"], "333px")
        self.assertEqual(got["links"], 1, got)   # hub.addStyle of the same file adds nothing

    def test_a_plugin_that_only_declares_the_css_gets_it(self):
        self.plugin(["web/narrow.css"], call_add_style=False)
        got = json.loads(self.load()[-1]["eval"])
        self.assertEqual((got["first"]["column"], got["links"]), ("333px", 1), got)

    def test_a_page_served_without_the_links_gets_the_sheets_from_the_snapshot_before_the_plugin_code_runs(self):
        # a plugin that appears while the page is open (an install, an update) and a page served by a server from before
        # the field are the same case for the page: the snapshot names the files, the <head> does not have them
        self.plugin(["web/narrow.css"], call_add_style=False)
        patch = mock.patch("modules.web.server.Web.ext_style_links", lambda web: "")
        patch.start()
        self.addCleanup(patch.stop)
        got = json.loads(self.load()[-1]["eval"])
        self.assertEqual((got["links"], got["now"]), (1, "333px"), got)

    def test_the_page_names_only_vetted_files_and_serves_them(self):
        self.plugin(["web/narrow.css", "../x.css", "/etc/a.css", "web/a b.css", "web/n.js", "web/narrow.css", 7, "web/ok-2.css"])
        code, body = self.req("GET", "/")
        links = re.findall(rb'<link rel="stylesheet" href="(/ext/[^"]+)" data-plugin="([^"]+)"', body)
        self.assertEqual(links, [(b"/ext/narrow/web/narrow.css", b"narrow"), (b"/ext/narrow/web/ok-2.css", b"narrow")])
        self.assertEqual(self.req("GET", "/ext/narrow/web/narrow.css")[0], 200)
        self.assertLess(body.index(links[0][0]), body.index(b"</head>"))
        self.assertGreater(body.index(links[0][0]), body.rindex(b'<link rel="stylesheet" href="/static', 0, body.index(links[0][0])))   # after the core's own

    def test_the_tag_follows_the_links_and_no_field_is_the_page_as_before(self):
        self.plugin(None)
        code, body = self.req("GET", "/")
        self.assertNotIn(b'data-plugin="narrow"', body)
        with_none = self.etag()
        self.plugin(["web/narrow.css"])
        self.assertNotEqual(self.etag(), with_none)

    def etag(self):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/", headers={"Host": f"127.0.0.1:{self.port}", "Accept-Language": "en"})
        r = c.getresponse()
        r.read()
        c.close()
        return r.getheader("ETag")


def load_tests(loader, standard, pattern):   # only its own tests, not the ones inherited from the page
    suite = unittest.TestSuite(PluginCss(n) for n in PluginCss.__dict__ if n.startswith("test_"))
    suite.addTests(PluginCssRecord(n) for n in PluginCssRecord.__dict__ if n.startswith("test_"))
    return suite


class PluginCssRecord(unittest.TestCase):
    """The manifest side: the catalog accepts the field, the install writes it into the record, the page filters it."""

    def man(self, **more):
        return {"name": "narrow", "web": "main.js", "description": "d", "why": "w", "icon": "i.png", **more}

    def test_the_catalog_takes_the_vetted_part_of_the_list_and_never_refuses_the_plugin_for_it(self):
        from modules.market import market
        self.assertEqual(market.valid(self.man(css=["web/a.css", "b.css"]))["css"], ["web/a.css", "b.css"])
        self.assertEqual(market.valid(self.man())["css"], [])
        for bad, kept in ((["../a.css"], []), (["/etc/a.css"], []), (["a.js"], []), (["web//a.css"], []), (["./a.css"], []),
                          ("a.css", []), ([], []), ([7, "ok.css"], ["ok.css"]), (["a b.css", "b.css"], ["b.css"]),
                          ([f"{n}.css" for n in range(9)], [f"{n}.css" for n in range(8)]), (["a" * 201 + ".css"], [])):
            got = market.valid(self.man(css=bad))
            self.assertIsNotNone(got, bad)   # a cosmetic field cannot make a plugin uninstallable or un-updatable
            self.assertEqual(got["css"], kept, bad)
        # without a page module there is nothing to apply it to: a plugin of themes alone keeps its themes, no css
        self.assertEqual(market.valid({"name": "narrow", "themes": ["t.json"], "css": ["a.css"]})["css"], [])

    def test_an_install_and_an_update_manage_the_field(self):
        from core import mpinstall
        from modules.market import market
        self.assertIn("css", market.FROM_MANIFEST)
        self.assertIn("css", mpinstall.FROM_MANIFEST)
        self.assertIn("css", market.MANAGED)
        self.assertIn("css", mpinstall.MANAGED)

    def test_the_catalog_check_names_the_field(self):
        import tempfile
        from core import marketplace
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "hub-plugin.json"), "w") as f:
                json.dump(self.man(css=["../a.css"]), f)
            entry = {"type": "plugin", "source": {"path": ""}}
            errors, warn = marketplace._check_folder_entry(d, entry)
            self.assertEqual(errors, [])   # not refused
            self.assertTrue(warn and "css" in warn[0], warn)   # but the author is told the page drops it
            with open(os.path.join(d, "hub-plugin.json"), "w") as f:
                json.dump(self.man(css=["a.css"]), f)
            self.assertEqual(marketplace._check_folder_entry(d, entry), ([], []))
            with open(os.path.join(d, "hub-plugin.json"), "w") as f:
                json.dump({"name": "narrow", "themes": ["t.json"], "css": ["a.css"]}, f)
            errors, warn = marketplace._check_folder_entry(d, entry)
            self.assertTrue(not errors and warn and "page module" in warn[0], (errors, warn))   # the true reason

    def test_the_page_keeps_only_vetted_files(self):
        from core import pluginstyle
        self.assertEqual(pluginstyle.css_files(["a.css", "w/b-1.css", "a.css", "../c.css", "/d.css", "e.js", "f//g.css", ".h.css/x", 3, None]),
                         ["a.css", "w/b-1.css"])
        self.assertEqual(pluginstyle.css_files(["x%d.css" % n for n in range(20)]), ["x%d.css" % n for n in range(8)])
        self.assertEqual((pluginstyle.css_files("a.css"), pluginstyle.css_files(None), pluginstyle.css_files({"a": 1})), ([], [], []))
