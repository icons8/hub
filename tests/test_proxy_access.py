"""A proxy or tunnel to localhost is not this computer (modules/tailnet/access.py, proxied).

Before: a request with peer 127.0.0.1 and Host localhost was "the person at the keyboard", so a tunnel that rewrites
Host (cloudflared with httpHostHeader=localhost, tailscale serve, a reverse proxy) gave the internet the whole page and,
in "Files", the whole disk. Now a request carrying a header proxies add, or sent by a browser page at another address
(its Origin or Referer), is refused with a reason. The tailnet path (peer is a tailnet address) is unchanged: see
tests/test_tailnet.py."""
import tests  # noqa: F401 — keep the machine's settings out (~/.config/hub-core), see tests/__init__.py
import email.message
import http.client
import http.server
import json
import time
import ast
import os
import plistlib
import shutil
import tempfile
import threading
import unittest

from modules.files.server import Files
from modules.tailnet import access
from modules.update import update
from modules.web.server import Web
from platforms import posix

PUBLIC = "https://hub.example.test"   # the address a tunnel serves the page under
MARKERS = ({"X-Forwarded-For": "203.0.113.7"}, {"x-forwarded-proto": "https"}, {"X-Forwarded-Host": "hub.example.test"},
           {"Forwarded": "for=203.0.113.7;proto=https"}, {"Via": "1.1 proxy"}, {"X-Real-IP": "203.0.113.7"},
           {"True-Client-IP": "203.0.113.7"}, {"CDN-Loop": "cloudflare"}, {"Cf-Connecting-Ip": "203.0.113.7"},
           {"CF-RAY": "8a1b2c3d4e5f6a7b-WAW"}, {"Tailscale-User-Login": "someone@example.test"})


class Rule(unittest.TestCase):
    def test_markers_make_a_local_request_not_local(self):
        for extra in MARKERS:
            self.assertFalse(access.local("localhost:8787", "127.0.0.1", extra), extra)
            self.assertFalse(access.allowed("/nonexistent", "127.0.0.1:8787", "::1", headers=extra), extra)
            self.assertEqual(access.proxied(extra), "proxy", extra)

    def test_a_plain_local_request_stays_local(self):
        for headers in ({}, {"User-Agent": "curl/8.5.0", "Accept": "*/*"},
                        {"Origin": "http://localhost:8787", "Sec-Fetch-Site": "same-origin"},
                        {"Referer": "http://127.0.0.1:8787/#/s/maps", "Sec-Fetch-Site": "same-origin"},
                        {"Referer": "http://localhost:8787/", "Sec-Fetch-Site": "same-site"},   # "Files" framed by the page
                        {"Origin": "http://[::1]:8787"}, {"Origin": "null"},
                        {"Origin": "chrome-extension://abcdefgh"}, {"Origin": "vscode-webview://x"}):   # not a web page
            self.assertTrue(access.local("127.0.0.1:8788", "127.0.0.1", headers), headers)
            self.assertIsNone(access.proxied(headers), headers)

    def test_a_page_at_another_address_is_not_local(self):
        for headers in ({"Origin": PUBLIC}, {"Referer": PUBLIC + "/#/s/maps"},
                        {"Origin": PUBLIC, "Sec-Fetch-Site": "same-origin"},
                        {"Referer": PUBLIC + "/", "Sec-Fetch-Site": "same-site"}):
            self.assertFalse(access.local("localhost:8787", "127.0.0.1", headers), headers)
            self.assertEqual(access.proxied(headers), "origin", headers)
        self.assertEqual(access.proxied({"Origin": "hub.example.test"}), "origin")   # no scheme: not a browser's, judged

    def test_a_link_from_another_site_is_still_followed(self):
        # a link in a chat or tracker on this machine: the browser marks it cross-site (cross_site lets a navigation in)
        link = {"Referer": "https://chat.example.test/", "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}
        self.assertTrue(access.local("localhost:8787", "127.0.0.1", link))
        self.assertTrue(access.local("localhost:8787", "127.0.0.1", {"Sec-Fetch-Site": "none", "Origin": PUBLIC}))

    def test_any_case_and_a_repeated_header(self):
        msg = email.message.Message()   # what BaseHTTPRequestHandler.headers is
        msg["Host"] = "localhost:8787"
        msg["X-FORWARDED-FOR"] = "203.0.113.7"
        msg["x-forwarded-for"] = "203.0.113.8"
        self.assertFalse(access.local("localhost:8787", "127.0.0.1", msg))

    def test_without_headers_as_before(self):
        # the page server of an earlier release, still in memory during an update, calls with two/three arguments
        self.assertTrue(access.local("localhost:8787", "127.0.0.1"))
        self.assertTrue(access.allowed("/nonexistent", "localhost:8787", "127.0.0.1"))

    def test_the_tailnet_path_does_not_look_at_markers(self):
        d = tempfile.mkdtemp(prefix="proxyacc-")
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, "tailnet.json"), "w") as f:
            json.dump({"ok": True, "ts": time.time(),
                       "owner": "owner@x", "ip": "100.9.9.9", "hosts": ["100.9.9.9:8787"]}, f)
        whois = lambda _, p: {"100.1.1.1": "owner@x"}.get(p)
        for headers in ({}, {"Origin": "http://100.9.9.9:8787"}, {"X-Forwarded-For": "203.0.113.7"}):
            self.assertTrue(access.allowed(d, "100.9.9.9:8787", "100.1.1.1", whois=whois, headers=headers), headers)
        self.assertFalse(access.via_proxy("100.1.1.1", {"X-Forwarded-For": "203.0.113.7"}))

    def test_every_caller_in_the_tree_passes_the_headers(self):
        # without headers the check is skipped (an earlier release's server in memory): a new caller must not forget them
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        calls = []
        for top in ("modules", "core"):
            for folder, _, names in os.walk(os.path.join(root, top)):
                for n in names:
                    if n.endswith(".py"):
                        with open(os.path.join(folder, n), encoding="utf-8") as f:
                            tree = ast.parse(f.read())
                        calls += [(n, c.lineno, any(k.arg == "headers" for k in c.keywords)) for c in ast.walk(tree)
                                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                                  and c.func.attr in ("allowed", "local") and isinstance(c.func.value, ast.Name)
                                  and c.func.value.id == "access"]
        self.assertGreaterEqual(len(calls), 4)
        self.assertEqual([c for c in calls if not c[2]], [])

    def test_the_service_names_both_ports(self):
        home = tempfile.mkdtemp(prefix="proxysvc-")
        self.addCleanup(shutil.rmtree, home, True)
        state = os.path.join(home, "state")
        d = os.path.join(home, ".config", "systemd", "user")
        os.makedirs(d)
        with open(os.path.join(d, "hub-two.service"), "w") as f:
            f.write(f"[Service]\nExecStart=/py /repo/bin/hub-up\nEnvironment=HUB_STATE_DIR={state}\n"
                    "Environment=HUB_WEB_PORT=8797\nEnvironment=HUB_FILES_PORT=8798\n")
        u = posix.service_unit(state, home=home, platform="linux")
        self.assertEqual((u["port"], u["files_port"]), (8797, 8798))
        d = os.path.join(home, "Library", "LaunchAgents")
        os.makedirs(d)
        with open(os.path.join(d, "com.hub-two.plist"), "wb") as f:
            plistlib.dump({"Label": "com.hub-two", "ProgramArguments": ["/py", "/repo/bin/hub-up"],
                           "EnvironmentVariables": {"HUB_STATE_DIR": state, "HUB_WEB_PORT": "8797",
                                                    "HUB_FILES_PORT": "8799"}}, f)
        u = posix.service_unit(state, home=home, platform="darwin")
        self.assertEqual((u["port"], u["files_port"]), (8797, 8799))

    def test_an_update_restarts_files_with_the_page(self):
        # an old "Files" process would keep the old rule until a full restart
        self.assertTrue({"web", "files"} <= set(update.WEB_NOW["parts"]))


def ask(port, method, path, headers, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, json.dumps(body) if body is not None else None, headers)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


class PageServer(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="proxyweb-")
        self.addCleanup(shutil.rmtree, self.state, True)
        self.web = Web(self.state)
        self.web.decide_wait = 0
        self.server = self.web.serve(port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.web.stop.set)

    def req(self, method, path, extra=None, body=None):
        h = {"Host": f"localhost:{self.port}", "X-Hub": "1", "Content-Type": "application/json", **(extra or {})}
        return ask(self.port, method, path, h, body)

    def test_a_tunnel_gets_403_with_the_reason(self):
        for extra in MARKERS:
            code, body = self.req("GET", "/api/snapshot", extra)
            self.assertEqual(code, 403, extra)
            self.assertIn("Tailscale", json.loads(body)["error"], extra)
        code, body = self.req("POST", "/api/paths", {"X-Forwarded-For": "203.0.113.7"}, {"items": []})
        self.assertEqual(code, 403)
        self.assertIn("Tailscale", json.loads(body)["error"])

    def test_a_browser_behind_a_proxy_that_adds_no_markers(self):
        # nginx proxy_pass by default: Host localhost:<port> and nothing else; the page's own requests name its address
        code, _ = self.req("GET", "/api/snapshot", {"Referer": PUBLIC + "/", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(code, 403)
        code, _ = self.req("POST", "/api/send", {"Origin": PUBLIC, "Sec-Fetch-Site": "same-origin"},
                           {"session": "a", "text": "hi"})
        self.assertEqual(code, 403)

    def test_this_computer_is_let_in(self):
        self.assertEqual(self.req("GET", "/api/snapshot")[0], 200)   # curl
        own = {"Origin": f"http://localhost:{self.port}", "Referer": f"http://localhost:{self.port}/",
               "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"}
        self.assertEqual(self.req("GET", "/api/snapshot", own)[0], 200)   # the page's own fetch
        self.assertEqual(self.req("POST", "/api/paths", own, {"items": []})[0], 200)
        link = {"Referer": "https://chat.example.test/", "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}
        self.assertEqual(self.req("GET", "/", link)[0], 200)   # a link from a chat opens the page

    def test_another_host_is_refused_as_before(self):
        code, body = self.req("GET", "/api/snapshot", {"Host": "evil.example:80"})
        self.assertEqual(code, 403)
        self.assertNotIn("Tailscale", json.loads(body)["error"])   # DNS rebinding, not a proxy


class FilesServer(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="proxyfiles-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.files = Files(config=lambda: {"projects": {}}, state_dir=self.dir)
        self.server = self.files.serve(port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def req(self, method, path, extra=None, body=None):
        h = {"Host": f"127.0.0.1:{self.port}", "X-Hub": "1", "Content-Type": "application/json", **(extra or {})}
        return ask(self.port, method, path, h, body)

    def test_a_tunnel_does_not_get_the_disk(self):
        for extra in ({"X-Forwarded-For": "203.0.113.7"}, {"Cf-Connecting-Ip": "203.0.113.7"},
                      {"Referer": PUBLIC + "/", "Sec-Fetch-Site": "same-origin"}):
            code, body = self.req("GET", "/api/roots", extra)
            self.assertEqual(code, 403, extra)
            self.assertIn("Tailscale", json.loads(body)["error"], extra)
        code, body = self.req("POST", "/api/save", {"Origin": PUBLIC}, {})
        self.assertEqual(code, 403)
        self.assertIn("Tailscale", json.loads(body)["error"])

    def test_this_computer_and_the_page_frame(self):
        self.assertEqual(self.req("GET", "/api/roots")[0], 200)
        framed = {"Referer": "http://127.0.0.1:8787/", "Sec-Fetch-Site": "same-site", "Sec-Fetch-Dest": "iframe"}
        self.assertEqual(self.req("GET", "/", framed)[0], 200)   # the hub page embeds "Files" in its panel
        own = {"Origin": f"http://127.0.0.1:{self.port}", "Referer": f"http://127.0.0.1:{self.port}/",
               "Sec-Fetch-Site": "same-origin"}
        self.assertEqual(self.req("GET", "/api/roots", own)[0], 200)


class Doctor(unittest.TestCase):
    """hub doctor asks the running page whether it already refuses a proxied request (a process started before the
    update keeps the old rule)."""

    def serve(self, handler):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def test_probe(self):
        state = tempfile.mkdtemp(prefix="proxydoc-")
        self.addCleanup(shutil.rmtree, state, True)
        web = Web(state)
        srv = web.serve(port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.addCleanup(web.stop.set)
        self.assertIs(access.refuses_proxied(srv.server_address[1]), True)

        class Old(http.server.BaseHTTPRequestHandler):   # lets everyone in: a server on the old rule
            def do_GET(self):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_):
                pass
        self.assertIs(access.refuses_proxied(self.serve(Old)), False)
        self.assertIsNone(access.refuses_proxied(1))   # nothing listens

    def test_rows(self):
        from unittest import mock
        from core import modcli
        with mock.patch.object(access, "refuses_proxied", side_effect=lambda p: {1: True, 2: False}.get(p)), \
                mock.patch.object(access, "proxy_programs", side_effect=lambda p: ["cloudflared"] if p == 2 else []):
            rows = modcli.proxy_rows({"web": 1, "files": 2, "x": 3})
        self.assertEqual([(lvl, what) for lvl, what, _ in rows],
                         [("ok", "web · proxy"), ("warn", "files · proxy"), ("warn", "files · proxy")])
        self.assertIn("hub restart files", rows[1][2])
        self.assertIn("cloudflared", rows[2][2])
        self.assertEqual(modcli.proxy_rows({}), [])

    def test_proxy_programs_by_name(self):
        ss = ('ESTAB 0 0 127.0.0.1:51514 127.0.0.1:8787 users:(("cloudflared",pid=4242,fd=9))\n'
              'ESTAB 0 0 127.0.0.1:51515 127.0.0.1:8787 users:(("python3",pid=4343,fd=3))\n')
        self.assertEqual(access.proxy_programs_in(ss), ["cloudflared"])
        lsof = "p4242\nccloudflared\nf9\np4343\ncpython3\np99\ncnginx\n"
        self.assertEqual(access.proxy_programs_in(lsof), ["cloudflared", "nginx"])
        self.assertEqual(access.proxy_programs_in('users:(("sshd",pid=1,fd=3))'), [])   # names as listed, not prefixes


if __name__ == "__main__":
    unittest.main()
