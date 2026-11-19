"""Tests reach their own servers on 127.0.0.1 directly under any machine proxy (tests/__init__.py). The update self-check
runs on everyone's machine with its environment: with HTTP(S)_PROXY and no 127.0.0.1 in NO_PROXY the git of the update
tests went to the test server through the proxy («Failed to connect … via 127.0.0.1») — a red self-check behind a
company proxy (2026-11-03, tests.test_update_net). urllib is covered by core.net; git and curl read only the environment."""
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest

import platforms

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def dead():
    """A proxy nobody listens on (a port just freed): a request through it fails at once, not hangs."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return "http://127.0.0.1:%d" % s.getsockname()[1]


PROBE = textwrap.dedent("""
    import http.server, json, os, subprocess, sys, threading
    sys.path.insert(0, sys.argv[1])
    machine = {k: os.environ.get(k) for k in ("NO_PROXY", "no_proxy")}   # on Windows one variable of either case
    import tests  # noqa: F401 — as in any run
    seen = []
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/hub.git" % srv.server_address[1]
    r = subprocess.run(["git", "ls-remote", url], capture_output=True, text=True,
                       env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    srv.shutdown()
    print(json.dumps({"seen": seen, "git": r.stderr.strip(), "machine": machine,
                      "env": {k: os.environ.get(k) for k in ("NO_PROXY", "no_proxy")}}))
""")


class MachineProxy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, self.tmp)

    def probe(self, **env):
        clean = {k: v for k, v in os.environ.items() if k.lower() not in (
            "http_proxy", "https_proxy", "all_proxy", "no_proxy")}
        clean.update(env)
        r = subprocess.run([sys.executable, "-c", PROBE, ROOT], env=clean, cwd=self.tmp,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        import json
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_git_reaches_a_local_server_past_the_machine_proxy(self):
        proxy = dead()
        out = self.probe(HTTP_PROXY=proxy, HTTPS_PROXY=proxy, http_proxy=proxy, https_proxy=proxy, ALL_PROXY=proxy,
                         all_proxy=proxy)
        self.assertTrue(out["seen"], out["git"])

    def test_the_machines_own_lists_of_both_cases_stay(self):
        proxy = dead()
        out = self.probe(http_proxy=proxy, https_proxy=proxy, NO_PROXY="corp.example", no_proxy="corp.example,lab.example")
        self.assertTrue(out["seen"], out["git"])
        had = {x for v in out["machine"].values() for x in (v or "").split(",") if x}
        self.assertTrue(had, out)
        for name in ("NO_PROXY", "no_proxy"):   # urllib and curl take the lowercase one, other programs the uppercase
            got = out["env"][name].split(",")
            self.assertTrue(had <= set(got), out)
            self.assertEqual(len(got), len(set(got)), out)

    def test_without_a_proxy_nothing_is_added(self):
        out = self.probe()
        self.assertTrue(out["seen"], out["git"])
        self.assertEqual(out["env"], {"NO_PROXY": None, "no_proxy": None})


if __name__ == "__main__":
    unittest.main()
