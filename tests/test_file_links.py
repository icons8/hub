"""File links in session texts — only for what actually opens (owner report 2026-10-11: links often gave no access to the
file, and a bare domain without http was highlighted as a file). The page asks the server where a path leads: a file on
disk in the «Files» folders is a link; a domain is a link to a site; anything else is plain text."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import http.client
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest

from modules.files import paths
from modules.web.server import Web


def touch(*parts):
    path = os.path.join(*parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("x\n")
    return path


class Resolve(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="links-"))
        self.a, self.b = os.path.join(self.dir, "a"), os.path.join(self.dir, "b")
        touch(self.a, "docs", "plan.md")
        touch(self.a, "src", "api", "pack.ts")
        touch(self.b, "lib", "only-here.ts")
        touch(self.b, "docs", "plan.md")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_from_the_session_folder(self):
        self.assertEqual(paths.resolve("docs/plan.md", self.a), os.path.join(self.a, "docs", "plan.md"))
        self.assertEqual(paths.resolve("pack.ts", self.a), os.path.join(self.a, "src", "api", "pack.ts"))

    def test_a_file_the_session_read_only_while_it_is_there(self):
        # 164 links pointed at a file once read in a deleted worktree, yet relative to the session folder it exists (analysis 2026-10-11)
        gone = os.path.join(self.dir, "wt-old", "docs", "plan.md")
        self.assertEqual(paths.resolve("docs/plan.md", self.a, hint=gone), os.path.join(self.a, "docs", "plan.md"))
        other = touch(self.dir, "wt-new", "docs", "plan.md")
        self.assertEqual(paths.resolve("docs/plan.md", self.a, hint=other), other)

    def test_a_file_of_another_project_only_when_it_is_the_only_one(self):
        self.assertEqual(paths.resolve("lib/only-here.ts", self.a, projects=[self.a, self.b]),
                         os.path.join(self.b, "lib", "only-here.ts"))
        self.assertIsNone(paths.resolve("lib/only-here.ts", self.a))
        touch(self.dir, "c", "lib", "only-here.ts")
        self.assertIsNone(paths.resolve("lib/only-here.ts", self.a,
                                        projects=[self.a, self.b, os.path.join(self.dir, "c")]))   # two — we do not guess

    def test_nothing_for_what_is_not_there(self):
        self.assertIsNone(paths.resolve("/tmp/no-such-file-here.png", self.a))
        self.assertIsNone(paths.resolve("icons8/icons8-icon", self.a, projects=[self.a, self.b]))
        self.assertIsNone(paths.resolve("claims.roles", self.a))
        self.assertIsNone(paths.resolve("docs/plan.md", None))

    def test_domains_are_web_addresses(self):
        self.assertEqual(paths.web_address("hello.com"), "https://hello.com")
        self.assertEqual(paths.web_address("example.de/icons/set/x--style-color"),
                         "https://example.de/icons/set/x--style-color")
        self.assertEqual(paths.web_address("gitlab.example.com/team/repo"), "https://gitlab.example.com/team/repo")
        self.assertEqual(paths.web_address("example8.de"), "https://example8.de")
        self.assertEqual(paths.web_address("www.example.ca"), "https://www.example.ca")
        for not_web in ("README.md", "hub.json", "v2.60", "os.path", "claims.roles", "run.sh", "main.py",
                        "/icons/set/x", "team/repo", "a.ts", "notes.txt", "translations.de", "session.info", "os.link",
                        "oauthClients.info"):
            self.assertIsNone(paths.web_address(not_web), not_web)


class Api(unittest.TestCase):
    def setUp(self):
        self.state = os.path.realpath(tempfile.mkdtemp(prefix="hubweb-"))
        self.proj = os.path.join(self.state, "work", "proj")
        self.plan = touch(self.proj, "docs", "plan.md")
        subprocess.run(["git", "init", "-q", self.proj], check=True)
        self.outside = touch(self.state, "elsewhere", "notes.md")
        self.web = Web(self.state)
        self.web.config = lambda: {"projects": {"proj": {"path": self.proj}}}
        self.server = self.web.serve(port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.web.stop.set()
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.state, ignore_errors=True)

    def post(self, body, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", "/api/paths", json.dumps(body), {"Host": f"127.0.0.1:{self.port}", "X-Hub": "1",
                                                          "Content-Type": "application/json", **(headers or {})})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, json.loads(data)

    def test_only_what_opens_becomes_a_link(self):
        code, out = self.post({"items": [
            {"path": "docs/plan.md", "cwd": self.proj},
            {"path": "plan.md", "cwd": self.proj, "hint": os.path.join(self.state, "gone", "docs", "plan.md")},
            {"path": "/tmp/no-such-file-here.png"},
            {"path": "hello.com"},
            {"path": self.outside},
            {"path": "docs/plan.md"},   # no session folder — nothing to search from
        ]})
        self.assertEqual(code, 200)
        got = out["items"]
        self.assertEqual(got[0], {"open": "#/proj/docs/plan.md"})
        self.assertEqual(got[1], {"open": "#/proj/docs/plan.md"})
        self.assertEqual(got[2], {})
        self.assertEqual(got[3], {"web": "https://hello.com"})
        drive, rest = os.path.splitdrive(self.outside)
        if os.path.normcase(drive) == os.path.normcase(os.path.splitdrive(os.path.abspath(os.sep))[0]):
            # from the machine itself — the whole disk (Windows: the «Mac» root is the drive of the hub's current folder; the tests' temp may be on another)
            self.assertEqual(got[4], {"open": "#/Mac" + rest.replace(os.sep, "/")})
        self.assertEqual(got[5], {})

    def test_only_from_the_page(self):
        code, _ = self.post({"items": []}, headers={"X-Hub": ""})
        self.assertEqual(code, 403)
        code, out = self.post({"items": [{"path": "x.md"}] * 600})
        self.assertEqual((code, len(out["items"])), (200, 500))   # no more than 500 at a time


if __name__ == "__main__":
    unittest.main()
