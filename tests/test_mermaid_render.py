"""A mermaid diagram → PNG for Telegram (modules/telegram/mermaid.py) in a real headless Chrome: it draws, and a broken
diagram gives None, not a hang (2026-10-06: Chrome returned the result in a second but did not exit — the render hung
until the timeout). The diagram text comes from a session's answer and is not trusted: it runs no script on the render
page, the page reads no local files and makes no network requests."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import pathlib
import re
import shutil
import tempfile
import time
import unittest

from modules.telegram import mermaid
from modules.testchrome import chrome

CHROME = chrome.find()


@unittest.skipUnless(CHROME, chrome.INSTALL)
class Render(unittest.TestCase):
    def test_a_diagram_becomes_a_png_quickly(self):
        t = time.time()
        png = mermaid.render("graph TD\n  A[Request] --> B{Has access?}", chrome=CHROME, timeout=30)
        self.assertIsNotNone(png)
        self.addCleanup(shutil.rmtree, os.path.dirname(png), True)
        with open(png, "rb") as f:
            self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")
        self.assertLess(time.time() - t, 25)

    def test_a_broken_diagram_is_none_not_a_hang(self):
        t = time.time()
        self.assertIsNone(mermaid.render("graph TD\n  A -->", chrome=CHROME, timeout=15))
        self.assertLess(time.time() - t, 20)

    def test_a_closing_script_tag_in_the_code_runs_nothing(self):
        # an injected script would title the page "7x7" — and a 7×7 screenshot of nothing would go out as the diagram
        code = "graph TD\nA</script><script>document.title='7x7'</script><script>"
        png = mermaid.render(code, chrome=CHROME, timeout=20)
        if png:
            self.addCleanup(shutil.rmtree, os.path.dirname(png), True)
        self.assertIsNone(png)

    def test_the_render_page_cannot_read_local_files(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        canary = os.path.join(d, "canary.txt")
        with open(canary, "w") as f:
            f.write("canary-secret")
        page = os.path.join(d, "probe.html")
        with open(page, "w") as f:
            f.write("<!doctype html><title>start</title><script>try { const x = new XMLHttpRequest();"
                    f" x.open('GET', {json.dumps(pathlib.Path(canary).as_uri())}, false); x.send();"
                    " document.title = 'read:' + x.responseText } catch (e) { document.title = 'blocked' }</script>")
        dom = os.path.join(d, "dom.html")
        done = lambda: os.path.exists(dom) and "</title>" in open(dom).read()  # noqa: E731
        self.assertTrue(mermaid._chrome(CHROME, [f"--user-data-dir={d}/p", "--dump-dom", pathlib.Path(page).as_uri()],
                                        20, done, out=dom))
        self.assertNotIn("canary-secret", open(dom).read())


class Page(unittest.TestCase):
    def test_the_code_cannot_close_the_script(self):
        code = "graph TD\nA</script><!--<script>&amp; "
        html = mermaid.page(code, "n0nce")
        self.assertEqual(html.count("</script>"), mermaid.PAGE.count("</script>"))
        self.assertNotIn("<!--", html)
        literal = re.search(r'mermaid\.render\("m", (".*?")\)', html, re.S).group(1)
        self.assertEqual(json.loads(literal), code)

    def test_the_page_allows_no_network_and_only_its_own_scripts(self):
        html = mermaid.page("graph TD\nA", "n0nce")
        csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', html).group(1)
        self.assertIn("default-src 'none'", csp)
        script = next(d.strip() for d in csp.split(";") if d.strip().startswith("script-src"))
        self.assertEqual(script, "script-src 'nonce-n0nce'")
        self.assertEqual(len(re.findall(r'<script nonce="n0nce"', html)), 2)

    def test_the_diagram_chrome_gets_no_file_access_flag(self):
        # diagram rendering has no file access; hub shot/video screenshots (modules/proof) ask for it themselves, as before
        self.assertNotIn("--allow-file-access-from-files", mermaid.args("chrome", []))


if __name__ == "__main__":
    unittest.main()
