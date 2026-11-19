"""A module's HTML view in a real browser (Module API, spec §2.2): the hub page embeds /mod/<name>/view/… in an
`<iframe>`, and the response carries a CSP sandbox: the view's script runs but cannot read the hub API or touch the
page and its storage; the same holds if the embedder forgot the sandbox attribute and if the view is opened in a
separate tab."""
import tests  # noqa: F401 — keep this machine's settings out
import json
import os
import unittest

from tests import test_modapi_http, test_web_e2e

BLOCKED = {"origin": "null", "fetch": "blocked", "parent": "blocked", "storage": "blocked", "cookie": "blocked"}


@unittest.skipUnless(test_web_e2e.READY, "needs node and Chrome for the tests")
class ViewInChrome(test_web_e2e.PageInChrome):
    caps, http = ("http",), None
    module = test_modapi_http.ModHttp.module
    logged = test_modapi_http.ModHttp.logged
    stop_modules = test_modapi_http.ModHttp.stop_modules

    def setUp(self):
        super().setUp()
        self.log_path = os.path.join(self.state, "module.log")
        self.procs = []
        self.addCleanup(self.stop_modules)
        self.module()

    def embed(self, sandbox):
        attr = "f.setAttribute('sandbox', 'allow-scripts');" if sandbox else ""
        return ("window.__got = null; addEventListener('message', (e) => { window.__got = e.data.probe; });"
                f"const f = document.createElement('iframe'); {attr} f.src = '/mod/fake/view/page';"
                "document.body.append(f); 1")

    def test_view_script_runs_but_reads_nothing_of_the_hub(self):
        for sandbox in (True, False):
            got = self.page({"go": f"http://127.0.0.1:{self.port}/"}, {"until": "!!document.querySelector('main')"},
                            {"eval": self.embed(sandbox)}, {"until": "!!window.__got", "ms": 8000},
                            {"eval": "JSON.stringify(window.__got)"})
            # the response arrived: the view's CSP let it into the page's embedded window (frame-ancestors 'self') and the script works
            self.assertTrue(got[3]["until"], f"the view did not answer (sandbox={sandbox}): {got}")
            self.assertEqual(json.loads(got[4]["eval"]), BLOCKED, f"sandbox={sandbox}")
        # only loads of the view itself reached the module process
        self.assertEqual({r["path"] for r in self.logged("request") if not r["path"].startswith("/hook/")},
                         {"/mod/fake/view/page"})

    def test_view_opened_as_its_own_tab_is_sandboxed_too(self):
        got = self.page({"go": f"http://127.0.0.1:{self.port}/mod/fake/view/page"},
                        {"until": "!!window.__probe", "ms": 8000}, {"eval": "JSON.stringify(window.__probe)"})
        self.assertTrue(got[1]["until"], got)
        probe = json.loads(got[2]["eval"])
        self.assertEqual({k: probe[k] for k in ("origin", "fetch", "storage")},
                         {"origin": "null", "fetch": "blocked", "storage": "blocked"})

    def test_a_click_in_the_view_opens_a_new_window(self):
        # a link with target="_blank" in a page a module serves (a trip plan, a report) must open: the sandbox allows
        # popups and lets the opened window out of the sandbox, the view itself stays an opaque origin without network
        got = self.page({"go": f"http://127.0.0.1:{self.port}/mod/fake/view/popup"},
                        {"until": "!!document.getElementById('out')", "ms": 8000},
                        {"drag": "#out", "to": "#out"},
                        {"until": "!!window.__pop", "ms": 4000}, {"eval": "window.__pop + ' ' + String(self.origin)"})
        self.assertTrue(got[1]["until"], got)
        self.assertEqual(got[4]["eval"], "opened null", got)


class ViewCallsItsOwnHandles(ViewInChrome):
    """Module API 2.3: a view calls the handles of ITS OWN module through the page (modview.js, hubmod.VIEW_JS); the
    page does not pass on another module, the hub API or an unknown method; a document the frame navigated to on its
    own gets no port."""

    def frame(self, src):
        # the frame comes after the page is up (main.js has run): plugins embed views into an already live page
        return ("window.__got = {}; addEventListener('message', (e) => { Object.assign(window.__got, e.data); });"
                "import('/static/js/main.js').then(() => { const f = document.createElement('iframe');"
                f"f.setAttribute('sandbox', 'allow-scripts'); f.src = '{src}'; document.body.append(f); }}); 1")

    def test_own_handle_yes_other_module_and_hub_no(self):
        self.module(name="other")
        got = self.page({"go": f"http://127.0.0.1:{self.port}/"}, {"until": "!!document.querySelector('main')"},
                        {"eval": self.frame("/mod/fake/view/bridge")}, {"until": "!!window.__got.bridge", "ms": 8000},
                        {"eval": "JSON.stringify(window.__got.bridge)"})
        self.assertTrue(got[3]["until"], got)
        out = json.loads(got[4]["eval"])
        code, _, body = out["own"].partition(" ")
        echo = json.loads(body)
        self.assertEqual((code, echo["method"], echo["path"], echo["size"]), ("200", "POST", "/api/mod/fake/echo?x=1",
                                                                              len("γειά".encode())))
        self.assertEqual(echo["headers"].get("x-hub-view"), "1")
        self.assertNotIn("x-hub-owner", echo["headers"])   # a call from a view is not the owner's word
        self.assertTrue(out["json"].startswith("200 "), out)
        for k in ("hub", "encoded", "other", "absolute"):
            self.assertIn("only the handlers of module fake", out[k], k)
        self.assertIn("TRACE", out["method"])
        paths = [r["path"] for r in self.logged("request") if not r["path"].startswith("/hook/")]
        self.assertEqual(sorted(paths), ["/api/mod/fake/echo", "/api/mod/fake/echo?x=1", "/mod/fake/view/bridge"])

    def test_a_document_the_frame_went_to_gets_no_port(self):
        got = self.page({"go": f"http://127.0.0.1:{self.port}/"}, {"until": "!!document.querySelector('main')"},
                        {"eval": self.frame("/mod/fake/view/bridge?go=late")},
                        {"until": "!!window.__got.late", "ms": 10000}, {"eval": "window.__got.late"})
        self.assertTrue(got[3]["until"], got)
        self.assertEqual(got[4]["eval"], "no port")

    def test_the_view_moves_to_its_own_page_through_the_page_and_keeps_the_port(self):
        got = self.page({"go": f"http://127.0.0.1:{self.port}/"}, {"until": "!!document.querySelector('main')"},
                        {"eval": self.frame("/mod/fake/view/hop")}, {"until": "!!window.__got.bridge", "ms": 8000},
                        {"eval": "window.__got.bridge.own.slice(0, 3) + ' ' + document.querySelector('iframe').src"})
        self.assertTrue(got[3]["until"], got)
        self.assertEqual(got[4]["eval"], f"200 http://127.0.0.1:{self.port}/mod/fake/view/bridge")


def load_tests(loader, standard, pattern):
    """Own tests only: the class inherits the page tests, which already run in test_web and test_web_e2e."""
    suite = unittest.TestSuite()
    suite.addTests(ViewInChrome(n) for n in ViewInChrome.__dict__ if n.startswith("test"))
    suite.addTests(ViewCallsItsOwnHandles(n) for n in ViewCallsItsOwnHandles.__dict__ if n.startswith("test"))
    return suite


if __name__ == "__main__":
    unittest.main()
