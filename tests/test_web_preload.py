"""Early hints in the page head: the module graph and the page strings are requested together, not one level at a time."""
import tests  # noqa: F401
import http.client
import json
import os
import re
import tempfile
from unittest import mock

import platforms

from modules.web import pagecache, preload, server
from tests.test_web import WebTest
from tests.test_web_e2e import PageInChrome

ENTRY = '<html><head></head><body><script type="module" src="/static/js/main.js"></script></body></html>'


def tree(files):
    root = tempfile.mkdtemp(prefix="preload-")
    for name, text in files.items():
        path = os.path.join(root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return root


class Graph(WebTest):
    def test_static_imports_are_followed_in_breadth_first_order_and_dynamic_ones_are_not(self):
        root = tree({
            "js/main.js": 'import { a } from "./a.js";\nimport "./side.js";\nexport * from "./b.js";\nimport("./lazy.js");',
            "js/a.js": 'import {\n  x,\n  y,\n} from "./deep/c.js";\nimport { m } from "./main.js";',
            "js/b.js": 'const s = "from ./not-an-import.js";',
            "js/side.js": "", "js/deep/c.js": 'import { z } from "../d.js";', "js/d.js": "", "js/lazy.js": "",
        })
        self.addCleanup(platforms.current.rmtree, root)
        self.assertEqual(preload.module_urls(root, ENTRY),
                         ["/static/js/main.js", "/static/js/a.js", "/static/js/side.js", "/static/js/b.js",
                          "/static/js/deep/c.js", "/static/js/d.js"])

    def test_the_entry_is_one_module_however_its_path_is_spelled(self):
        # the entry path is compared with the normalised paths of imports: on Windows "static\js/main.js" against
        # "static\js\main.js", here the same mismatch through "./" in the script address
        root = tree({"js/main.js": 'import "./a.js";', "js/a.js": 'import "./main.js";'})
        self.addCleanup(platforms.current.rmtree, root)
        entry = ENTRY.replace("/static/js/main.js", "/static/js/./main.js")
        self.assertEqual(preload.module_urls(root, entry), ["/static/js/main.js", "/static/js/a.js"])

    def test_a_missing_file_or_an_import_outside_the_static_folder_is_left_out(self):
        root = tree({"js/main.js": 'import "./gone.js";\nimport "../../outside.js";\nimport "./ok.js";', "js/ok.js": ""})
        self.addCleanup(platforms.current.rmtree, root)
        self.assertEqual(preload.module_urls(root, ENTRY), ["/static/js/main.js", "/static/js/ok.js"])
        self.assertEqual(preload.head_hints(tempfile.gettempdir() + "/no-such-static", ENTRY), "")

    def test_a_changed_file_is_read_again(self):
        root = tree({"js/main.js": 'import "./a.js";', "js/a.js": "", "js/b.js": ""})
        self.addCleanup(platforms.current.rmtree, root)
        self.assertEqual(preload.module_urls(root, ENTRY)[1:], ["/static/js/a.js"])
        with open(os.path.join(root, "js", "main.js"), "w", encoding="utf-8") as f:
            f.write('import "./a.js";\nimport "./b.js";')
        self.assertEqual(preload.module_urls(root, ENTRY)[1:], ["/static/js/a.js", "/static/js/b.js"])

    def test_the_real_graph_has_every_module_that_another_one_imports_statically(self):
        # the check does not use the parser under test: every static import written anywhere in js/ must be announced
        # (a dynamic `import("./x.js")` is not one)
        js = os.path.join(server.STATIC, "js")
        wanted = {"/static/js/main.js"}
        for name in os.listdir(js):
            if name.endswith(".js"):
                with open(os.path.join(js, name), encoding="utf-8") as f:
                    wanted |= {"/static/js/" + m for m in re.findall(r'(?:^|\n)\s*(?:import|export)(?!\s*\()[^;\n]*?"\./([\w.-]+\.js)"', f.read())}
                    f.seek(0)
                    wanted |= {"/static/js/" + m for m in re.findall(r'\n\s*\} from "\./([\w.-]+\.js)"', f.read())}
        with open(os.path.join(server.STATIC, "index.html"), encoding="utf-8") as f:
            got = set(preload.module_urls(server.STATIC, f.read()))
        self.assertLessEqual(wanted - got, set(), "an imported module is not announced")
        self.assertTrue(all(os.path.isfile(os.path.join(server.STATIC, u[len("/static/"):])) for u in got))


class Page(WebTest):
    def page(self, etag=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/", headers={"Host": f"127.0.0.1:{self.port}", "Accept-Language": "en",
                                      **({"If-None-Match": etag} if etag else {})})
        r = c.getresponse()
        result = r.status, r.read().decode(), r.getheader("ETag")
        c.close()
        return result

    def test_the_page_announces_its_strings_and_modules_inside_head_at_the_versioned_addresses(self):
        status, page, _ = self.page()
        head = page[:page.index("</head>")]
        ver = pagecache.walk(server.STATIC)[0]
        self.assertEqual(status, 200)
        self.assertEqual(head.count('<link rel="preload" href="/i18n.json" as="fetch" crossorigin>'), 1)
        hinted = re.findall(r'<link rel="modulepreload" href="([^"]+)">', head)
        self.assertIn(f"/static/v/{ver}/js/main.js", hinted)
        self.assertEqual(len(hinted), len(set(hinted)))   # the entry is announced once, not by two mechanisms
        self.assertGreater(len(hinted), 30)
        # the page's modules and the Clean theme's (clean/clean.js, imported by main.js)
        self.assertTrue(all(u.startswith((f"/static/v/{ver}/js/", f"/static/v/{ver}/clean/")) for u in hinted), hinted)
        # the import map comes first: a hint is resolved through it
        self.assertLess(head.index('type="importmap"'), head.index('rel="modulepreload"'))
        for url in hinted:
            self.assertEqual(self.req("GET", url)[0], 200, url)

    def test_the_page_tag_follows_the_files_so_the_announced_set_follows_them_too(self):
        root = tree({"index.html": ENTRY, "js/main.js": "", "js/new.js": ""})
        self.addCleanup(platforms.current.rmtree, root)
        with mock.patch.object(server, "STATIC", root):
            _, page, before = self.page()
            self.assertEqual(self.page(before)[0], 304)
            self.assertRegex(page, r'href="/static/v/[0-9a-f]{12}/js/main\.js"')
            self.assertNotIn("new.js", page)
            with open(os.path.join(root, "js", "main.js"), "w", encoding="utf-8") as f:
                f.write('import "./new.js";')
            status, changed, after = self.page(before)
            self.assertEqual(status, 200)   # the index.html file itself did not change: only the module did
            self.assertNotEqual(after, before)
            self.assertRegex(changed, r'href="/static/v/[0-9a-f]{12}/js/new\.js"')

    def test_a_page_with_no_version_keeps_the_plain_addresses_and_still_gets_the_hints(self):
        root = tree({"js/main.js": 'import "./a.js";', "js/a.js": ""})
        self.addCleanup(platforms.current.rmtree, root)
        hints = lambda prefix: preload.head_hints(root, ENTRY, prefix)   # noqa: E731
        plain = pagecache.page(ENTRY, None, None, hints)
        self.assertIn('<link rel="modulepreload" href="/static/js/a.js">\n</head>', plain)
        self.assertIn('<script type="module" src="/static/js/main.js">', plain)   # the entry is as it was
        self.assertNotIn("importmap", plain)
        versioned = pagecache.page(ENTRY, "abcdef012345", None, hints)
        self.assertEqual(versioned.count('rel="modulepreload"'), 2)
        self.assertIn('<link rel="modulepreload" href="/static/v/abcdef012345/js/a.js">', versioned)
        # no module found: the entry alone is announced, as without the hints
        self.assertEqual(pagecache.page(ENTRY, "abcdef012345", None, lambda prefix: "").count('rel="modulepreload"'), 1)


class Requests(PageInChrome):
    def test_modules_and_strings_are_requested_before_the_entry_script_has_arrived(self):
        # causality, not stopwatch: without the hints a module is requested only after its parent has run, i.e. after
        # the parent's response; with them every request is made while the page head is parsed
        self.seed()
        with open(os.path.join(server.STATIC, "index.html"), encoding="utf-8") as f:
            graph = [u.replace("\\", "/") for u in preload.module_urls(server.STATIC, f.read())]   # js/ and clean/
        names = json.dumps(graph)
        # the modules come under /static/v/<version>/ (pagecache.py): compare by the address without that prefix
        plain = "new URL(e.name).pathname.replace(/^\\/static\\/v\\/[0-9a-f]+\\//, '/static/')"
        got = self.page({"go": f"http://127.0.0.1:{self.port}/"},
                        {"until": f"(() => {{ const n = performance.getEntriesByType('resource').map(e => {plain});"
                                  f" return {names}.every(u => n.includes(u)) && n.includes('/i18n.json'); }})()"},
                        {"eval": f"(() => {{ const r = performance.getEntriesByType('resource'), at = u => r.filter(e => {plain} === u);"
                                 f" const main = at('/static/js/main.js')[0];"
                                 f" return {{ latest: Math.max(...{names}.map(u => at(u)[0].fetchStart)), mainEnd: main.responseEnd,"
                                 f" strings: at('/i18n.json').map(e => e.fetchStart), strings_n: at('/i18n.json').length,"
                                 f" modules_n: {names}.map(u => at(u).length).filter(n => n !== 1).length }}; }})()"})
        r = got[2]["eval"]
        self.assertTrue(got[1]["until"], got)
        self.assertLess(r["latest"], r["mainEnd"], r)       # the whole graph was asked for before main.js came back
        self.assertLess(r["strings"][0], r["mainEnd"], r)   # and so were the page strings
        self.assertEqual(r["strings_n"], 1, r)              # the preloaded strings are used, not fetched a second time
        self.assertEqual(r["modules_n"], 0, r)


def load_tests(loader, standard, pattern):
    import unittest
    return unittest.TestSuite(cls(n) for cls in (Graph, Page, Requests) for n in cls.__dict__ if n.startswith("test_"))
