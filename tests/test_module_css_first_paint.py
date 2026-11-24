"""A core module's page plugin comes as early as a page plugin from the settings: its stylesheets (the `css` field of
module.json, Module API 2.11) are in the page's <head> from the first byte, and every plugin's main file is asked for at once (the plugins
still run one after another, in the snapshot's order). Before, a module's CSS came only through hub.addStyle once its code had run, and the
page imported the plugins one after another: the twentieth plugin's slot (the sidebar tiles) was drawn only after the
nineteen before it had loaded and run, first as bare links, then pushing the sidebar down."""
import tests  # noqa: F401
import http.client
import json
import os
import re
import unittest
from unittest import mock

from core import modrun, modules
from tests.test_web_e2e import PageInChrome

FIRST = "window.__first = { column: null }; requestAnimationFrame(() => { window.__first.column = getComputedStyle(document.documentElement).getPropertyValue('--column').trim(); });"


def module(state, css=("web/style.css",), name="todo", api="^2.11", call_add_style=True):
    """A loaded module with a page plugin in its web/ folder (no process: only the want file the page reads)."""
    path = os.path.join(state, "mods", name)
    os.makedirs(os.path.join(path, "web"), exist_ok=True)
    with open(os.path.join(path, "module.json"), "w") as f:
        json.dump({"name": name, "version": "1.0.0", "api": api, "web": "web/main.js",
                   **({"css": list(css)} if css is not None else {})}, f)
    with open(os.path.join(path, "web", "main.js"), "w") as f:
        f.write("export default function register(hub) {\n"
                + ("  hub.addStyle('style.css');\n" if call_add_style else "")
                + "  hub.addSlot('sidebar.tiles', { id: 'tiles', render(root) { root.textContent = 'tiles'; } });\n}\n")
    for rel in ("web/style.css", "web/ok-2.css", "main.css"):
        with open(os.path.join(path, rel), "w") as f:
            f.write(":root { --column: 333px; }\n")
    os.makedirs(modrun.run_dir(state), exist_ok=True)
    with open(os.path.join(modrun.run_dir(state), f"{name}.want.json"), "w") as f:
        json.dump({"name": name, "cmd": ["python3", "main.py"], "cwd": path, "version": "1.0.0", "order": 0, "key": f"k-{name}-{len(css or ())}"}, f)
    return path


def get(port, path="/"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path, headers={"Host": f"127.0.0.1:{port}", "Accept-Language": "en"})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r, body


def etag(port):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/", headers={"Host": f"127.0.0.1:{port}", "Accept-Language": "en"})
    r = c.getresponse()
    r.read()
    c.close()
    return r.getheader("ETag")


class ModuleCssRecord(unittest.TestCase):
    """The manifest takes the field, the page's <head> and the snapshot name the vetted part of it."""

    def setUp(self):
        from tests.test_web import WebTest
        self.h = WebTest("test_page_and_snapshot")
        self.h.setUp()
        self.addCleanup(self.h.tearDown)

    def test_the_manifest_takes_the_field_from_2_11(self):
        base = {"name": "x", "version": "1.0.0", "web": "web/main.js"}
        self.assertEqual(modules.validate({**base, "api": "^2.11", "css": ["web/a.css"]}), [])
        # a cosmetic field does not refuse a module for a bad entry: the page drops the entry
        self.assertEqual(modules.validate({**base, "api": "^2.11", "css": ["../a.css", 7]}), [])
        self.assertTrue(modules.validate({**base, "api": "^2.1", "css": ["web/a.css"]}))   # api would lie about the field

    def test_the_head_and_the_snapshot_name_the_vetted_module_sheets(self):
        module(self.h.state, ["web/style.css", "../x.css", "/etc/a.css", "web/a b.css", "web/main.js", "web/style.css",
                              "main.css", 7, "web/ok-2.css"])
        code, body = self.h.req("GET", "/")
        links = re.findall(rb'<link rel="stylesheet" href="(/mod/[^"]+)" data-plugin="([^"]+)"', body)
        # main.css lies outside the plugin's folder: the page could not serve it at /mod/todo/web/
        self.assertEqual(links, [(b"/mod/todo/web/style.css", b"mod:todo"), (b"/mod/todo/web/ok-2.css", b"mod:todo")])
        self.assertLess(body.index(links[0][0]), body.index(b"</head>"))
        self.assertEqual(self.h.req("GET", "/mod/todo/web/style.css")[0], 200)
        self.h.seed()
        snap = json.loads(self.h.req("GET", "/api/snapshot")[1])
        self.assertIn({"name": "mod:todo", "main": "main.js", "base": "/mod/todo/web/", "css": ["style.css", "ok-2.css"]},
                      snap["extensions"])

    def test_no_field_is_the_page_as_before_and_the_tag_follows_the_sheets(self):
        module(self.h.state, None)
        code, body = self.h.req("GET", "/")
        self.assertNotIn(b'data-plugin="mod:todo"', body)
        self.h.seed()
        snap = json.loads(self.h.req("GET", "/api/snapshot")[1])
        self.assertIn({"name": "mod:todo", "main": "main.js", "base": "/mod/todo/web/"}, snap["extensions"])
        before = etag(self.h.port)
        module(self.h.state, ["web/style.css"])
        self.assertNotEqual(etag(self.h.port), before)

    def test_a_page_without_the_marker_still_gets_the_sheets_before_head_closes(self):
        # the page files from before the marker under this server (an update in the middle, a rollback of the files):
        # the links go before </head>, as before — not nowhere
        module(self.h.state, ["web/style.css"])
        from modules.web import pagecache
        orig = pagecache.page
        strip = lambda html, *a, **k: orig(html.replace("<!-- plugin-styles -->\n", ""), *a, **k)  # noqa: E731
        with mock.patch("modules.web.server.pagecache.page", strip):
            r, body = get(self.h.port)
        self.assertEqual(r.status, 200)
        self.assertNotIn(b"plugin-styles", body)
        link = body.index(b'<link rel="stylesheet" href="/mod/todo/web/style.css" data-plugin="mod:todo">')
        self.assertLess(link, body.index(b"</head>"))


class ModulePluginsEarly(PageInChrome):
    def test_the_first_frame_already_has_the_module_sheet(self):
        module(self.state)
        got = self.page({"go": f"http://127.0.0.1:{self.port}/", "init": FIRST},
                        {"until": "document.body.innerText.includes('tiles')", "ms": 10000},
                        {"eval": "JSON.stringify({ first: window.__first, "
                                 "links: document.querySelectorAll('link[href=\"/mod/todo/web/style.css\"]').length })"})
        self.assertTrue(got[-2]["until"], got)
        got = json.loads(got[-1]["eval"])
        self.assertEqual(got["first"]["column"], "333px", got)   # before: the standard width at the first frame
        self.assertEqual(got["links"], 1, got)   # hub.addStyle of the listed file adds nothing

    def test_a_sheet_the_plugin_turned_off_stays_off_from_the_first_frame(self):
        # a plugin with a look of its own that the person can turn off keeps its sheet in the css field and switches it
        # through hub.addStyle(file, { on }); the next load must not draw that look for the time its code takes to run, nor
        # keep it off once it is back on
        path = module(self.state)
        with open(os.path.join(path, "web", "main.js"), "w") as f:
            f.write("export default function register(hub) {\n"
                    "  hub.addStyle('style.css', { on: localStorage.getItem('t.on') === '1' });\n"
                    "  hub.addSlot('sidebar.tiles', { id: 'tiles', render(root) { root.textContent = 'tiles'; } });\n}\n")
        load = ({"go": f"http://127.0.0.1:{self.port}/", "init": FIRST},
                {"until": "document.body.innerText.includes('tiles')", "ms": 10000},
                {"eval": "JSON.stringify(window.__first)"})
        firsts = []
        for turn_on in (False, False, True, False, False):
            got = self.page(*load, *([{"eval": "localStorage.setItem('t.on', '1')"}] if turn_on else []))
            self.assertTrue(got[1]["until"], got)
            firsts.append(json.loads(got[2]["eval"])["column"])
        # the very first load cannot know; the second keeps it off (before: 333px until the code ran); turned back on after
        # the third, the fourth still starts off and its code turns it on, so the fifth draws it from the start
        self.assertEqual([c == "333px" for c in firsts], [True, False, False, False, True], firsts)

    SETTLED = ("JSON.stringify({ column: getComputedStyle(document.documentElement).getPropertyValue('--column').trim(), "
               "record: localStorage.getItem('hub.sheetMedia') })")

    def test_a_plugin_that_speaks_only_when_its_look_is_off_gets_the_sheet_back_once_it_is_on(self):
        # the code turns the sheet off when the look is off and says nothing when it is on: the kept "off" must not outlive
        # what the code sets — once the look is on, the sheet is on by the end of register() and the record is gone
        path = module(self.state)
        with open(os.path.join(path, "web", "main.js"), "w") as f:
            f.write("export default function register(hub) {\n"
                    "  if (localStorage.getItem('t.on') !== '1') hub.addStyle('style.css', { on: false });\n"
                    "  hub.addSlot('sidebar.tiles', { id: 'tiles', render(root) { root.textContent = 'tiles'; } });\n}\n")
        load = ({"go": f"http://127.0.0.1:{self.port}/", "init": FIRST},
                {"until": "document.body.innerText.includes('tiles')", "ms": 10000},
                {"eval": "JSON.stringify(window.__first)"}, {"eval": self.SETTLED})
        firsts, settled = [], []
        for turn_on in (False, False, True, False, False):
            got = self.page(*load, *([{"eval": "localStorage.setItem('t.on', '1')"}] if turn_on else []))
            self.assertTrue(got[1]["until"], got)
            firsts.append(json.loads(got[2]["eval"])["column"])
            settled.append(json.loads(got[3]["eval"]))
        # off, off; turned on after the third load: the fourth still starts off, but ends on with no record; the fifth is on
        self.assertEqual([c == "333px" for c in firsts], [True, False, False, False, True], firsts)
        self.assertEqual(settled[3], {"column": "333px", "record": None}, settled)
        self.assertEqual(settled[1]["column"], "800px", settled)
        self.assertEqual(json.loads(settled[1]["record"]), {"/mod/todo/web/style.css": "not all"}, settled)

    def test_a_version_that_dropped_the_switch_has_its_sheet_on(self):
        # the plugin's next version does not touch the sheet: the record of the old one must not keep it off for good
        path = module(self.state)
        with open(os.path.join(path, "web", "main.js"), "w") as f:
            f.write("export default function register(hub) {\n  hub.addStyle('style.css', { on: false });\n"
                    "  hub.addSlot('sidebar.tiles', { id: 'tiles', render(root) { root.textContent = 'tiles'; } });\n}\n")
        load = ({"go": f"http://127.0.0.1:{self.port}/", "init": FIRST},
                {"until": "document.body.innerText.includes('tiles')", "ms": 10000},
                {"eval": "JSON.stringify(window.__first)"}, {"eval": self.SETTLED})
        got = self.page(*load)
        self.assertTrue(got[1]["until"], got)
        got = self.page(*load)
        self.assertEqual(json.loads(got[2]["eval"])["column"], "800px", got)   # the kept "off" from the first frame
        with open(os.path.join(path, "web", "main.js"), "w") as f:   # v2: no switch
            f.write("export default function register(hub) {\n"
                    "  hub.addSlot('sidebar.tiles', { id: 'tiles', render(root) { root.textContent = 'tiles'; } });\n}\n")
        got = self.page(*load)
        self.assertTrue(got[1]["until"], got)
        self.assertEqual(json.loads(got[3]["eval"]), {"column": "333px", "record": None}, got)   # on again once registered
        got = self.page(*load)
        self.assertEqual(json.loads(got[2]["eval"])["column"], "333px", got)   # and from the first frame of the next load

    def test_media_the_code_set_on_the_link_itself_is_its_own(self):
        # a plugin that also works on a core before the switch sets media on the <link> by hand: the page must not take
        # that off after register() (the sheet is not in its record) and must not keep it for the next load either
        path = module(self.state)
        with open(os.path.join(path, "web", "main.js"), "w") as f:
            f.write("export default function register(hub) {\n  hub.addStyle('style.css');\n"
                    "  document.querySelector('link[href$=\"/mod/todo/web/style.css\"]').media = 'not all';\n"
                    "  hub.addSlot('sidebar.tiles', { id: 'tiles', render(root) { root.textContent = 'tiles'; } });\n}\n")
        load = ({"go": f"http://127.0.0.1:{self.port}/", "init": FIRST},
                {"until": "document.body.innerText.includes('tiles')", "ms": 10000},
                {"eval": "JSON.stringify(window.__first)"}, {"eval": self.SETTLED})
        got = self.page(*load)
        self.assertTrue(got[1]["until"], got)
        self.assertEqual(json.loads(got[3]["eval"]), {"column": "800px", "record": None}, got)   # off by its own hand, no record
        got = self.page(*load)
        self.assertEqual(json.loads(got[2]["eval"])["column"], "333px", got)   # the next load starts as listed
        self.assertEqual(json.loads(got[3]["eval"]), {"column": "800px", "record": None}, got)
        # the record of a previous version ({ on: false }) and a version that puts its own media over it by hand: the page
        # lets the record go but keeps the code's media — on this load and the next (nothing kept, the code sets it again)
        with open(os.path.join(path, "web", "main.js"), "w") as f:
            f.write("export default function register(hub) {\n  hub.addStyle('style.css');\n"
                    "  document.querySelector('link[href$=\"/mod/todo/web/style.css\"]').media = 'print';\n"
                    "  hub.addSlot('sidebar.tiles', { id: 'tiles', render(root) { root.textContent = 'tiles'; } });\n}\n")
        seed = "localStorage.setItem('hub.sheetMedia', JSON.stringify({'/mod/todo/web/style.css': 'not all'}));"
        media = "document.querySelector('link[href$=\"/mod/todo/web/style.css\"]').getAttribute('media')"
        got = self.page({**load[0], "init": seed + FIRST}, *load[1:], {"eval": media})
        self.assertEqual(json.loads(got[2]["eval"])["column"], "800px", got)   # off from the first frame, by the record
        self.assertEqual((json.loads(got[3]["eval"]), got[4]["eval"]), ({"column": "800px", "record": None}, "print"), got)
        got = self.page(*load, {"eval": media})
        self.assertEqual((json.loads(got[3]["eval"]), got[4]["eval"]), ({"column": "800px", "record": None}, "print"), got)

    def test_an_async_register_that_fails_is_a_failed_load(self):
        # an async register() that throws after an await: on main an unhandled rejection in the console and nothing in
        # "Settings" → "Plugins"; here the plugin's failure is recorded like a sync throw, and the record stays as it was
        path = module(self.state)
        with open(os.path.join(path, "web", "main.js"), "w") as f:
            f.write("export default async function register(hub) {\n"
                    "  hub.addSlot('sidebar.tiles', { id: 'tiles', render(root) { root.textContent = 'tiles'; } });\n"
                    "  await new Promise((r) => setTimeout(r, 100));\n  throw new Error('boom');\n}\n")
        unheard = "window.__unheard = 0; addEventListener('unhandledrejection', () => { window.__unheard += 1; });"
        probe = ("import('/static/js/ext.js').then((m) => { window.__p = (m.problems.get('mod:todo') || []).slice(); })")
        got = self.page({"go": f"http://127.0.0.1:{self.port}/", "init": unheard + FIRST},
                        {"until": "document.body.innerText.includes('tiles')", "ms": 10000}, {"sleep": 400},
                        {"eval": probe}, {"until": "!!window.__p", "ms": 5000},
                        {"eval": "JSON.stringify({ p: window.__p, unheard: window.__unheard })"})
        self.assertTrue(got[1]["until"], got)
        seen = json.loads(got[5]["eval"])
        self.assertEqual(seen["unheard"], 0, seen)
        self.assertEqual(len(seen["p"]), 1, seen)
        self.assertIn("boom", seen["p"][0], seen)

    def test_an_async_register_that_asks_after_an_await_is_heard(self):
        # the sheets settle once register() is done: an ask after an await counts, and on the next load the kept "off" does
        # not blink on while the await lasts
        path = module(self.state)
        with open(os.path.join(path, "web", "main.js"), "w") as f:
            f.write("export default async function register(hub) {\n  window.__inside = true;\n"
                    "  await new Promise((r) => setTimeout(r, 1500));\n  hub.addStyle('style.css', { on: false });\n"
                    "  window.__done = true;\n}\n")
        col = "getComputedStyle(document.documentElement).getPropertyValue('--column').trim()"
        load = ({"go": f"http://127.0.0.1:{self.port}/", "init": FIRST},
                {"until": "window.__inside === true", "ms": 10000}, {"eval": col},
                {"until": "window.__done === true", "ms": 10000},
                {"eval": "JSON.stringify(window.__first)"}, {"eval": self.SETTLED})
        got = self.page(*load)
        self.assertTrue(got[1]["until"] and got[3]["until"], got)
        settled = json.loads(got[5]["eval"])
        record = settled["record"]
        self.assertEqual((settled["column"], json.loads(record) if record else record), ("800px", {"/mod/todo/web/style.css": "not all"}), got)
        got = self.page(*load)
        self.assertEqual(json.loads(got[4]["eval"])["column"], "800px", got)   # off from the first frame
        self.assertEqual(got[2]["eval"], "800px", got)                          # and still off while register() awaits
        self.assertEqual(json.loads(got[5]["eval"])["column"], "800px", got)

    def test_a_plugin_absent_for_a_load_keeps_its_record(self):
        # "todo" is switched off for one load (its module stopped) while "bee" sets its own sheet: the record must still
        # have "todo", or its look flashes on the first frame when it is back
        for name in ("todo", "bee"):
            path = module(self.state, name=name)
            with open(os.path.join(path, "web", "main.js"), "w") as f:
                f.write("export default function register(hub) {\n  hub.addStyle('style.css', { on: false });\n"
                        f"  hub.addSlot('sidebar.tiles', {{ id: '{name}', render(root) {{ root.textContent = '{name}'; }} }});\n}}\n")
        load = ({"go": f"http://127.0.0.1:{self.port}/", "init": FIRST},
                {"until": "document.body.innerText.includes('bee')", "ms": 10000},
                {"eval": "JSON.stringify(window.__first)"}, {"eval": self.SETTLED})
        got = self.page(*load)
        self.assertTrue(got[1]["until"], got)
        want = os.path.join(modrun.run_dir(self.state), "todo.want.json")
        with open(want) as f:
            kept = f.read()
        os.remove(want)   # todo is gone for this load
        got = self.page(*load)
        self.assertTrue(got[1]["until"], got)
        record = json.loads(got[3]["eval"])["record"]
        self.assertEqual(json.loads(record) if record else record,
                         {"/mod/todo/web/style.css": "not all", "/mod/bee/web/style.css": "not all"}, got)
        with open(want, "w") as f:
            f.write(kept)
        got = self.page(*load)
        self.assertEqual(json.loads(got[2]["eval"])["column"], "800px", got)   # todo's sheet off from the first frame

    def test_a_slow_plugin_does_not_hold_back_the_ones_after_it_and_the_order_stays(self):
        # "a" finishes loading only once the page has asked for "b": asked for one after another, "b" never is and neither
        # registers; "broken" between them fails on import and costs only itself
        code = {
            "a": "await new Promise((r) => { const t = setInterval(() => { if (performance.getEntriesByType('resource')"
                 ".some((x) => x.name.endsWith('/ext/b/main.js'))) { clearInterval(t); r(); } }, 20); });\n",
            "broken": "throw new Error('broken on import');\n",
            "b": "",
        }
        exts = {}
        for name, head in code.items():
            path = os.path.join(self.state, "plug-" + name)
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, "main.js"), "w") as f:
                f.write(head + f"export default function register() {{ (window.__reg ||= []).push('{name}'); }}\n")
            exts[name] = {"path": path, "web": "main.js"}
        cfg = os.path.join(self.state, "hub.json")
        with open(cfg, "w") as f:
            json.dump({"extensions": exts}, f)
        env = mock.patch.dict(os.environ, {"HUB_CONFIG": cfg})
        env.start()
        self.addCleanup(env.stop)
        got = self.page({"go": f"http://127.0.0.1:{self.port}/"},
                        {"until": "(window.__reg || []).length >= 2", "ms": 8000},
                        {"eval": "JSON.stringify(window.__reg || [])"})
        self.assertTrue(got[-2]["until"], got)
        self.assertEqual(json.loads(got[-1]["eval"]), ["a", "b"])


def load_tests(loader, standard, pattern):   # only its own tests, not the ones inherited from the page
    suite = unittest.TestSuite(ModuleCssRecord(n) for n in ModuleCssRecord.__dict__ if n.startswith("test_"))
    suite.addTests(ModulePluginsEarly(n) for n in ModulePluginsEarly.__dict__ if n.startswith("test_"))
    return suite
