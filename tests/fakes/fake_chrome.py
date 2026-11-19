#!/usr/bin/env python3
"""Fake Chrome for supervisor tests: listens on --remote-debugging-port, answers /json/version, writes its argv.
Tabs: it opens the positional url like Chrome does; /json/list, /json/new?<url>, /json/close/<id> as in the real one."""
import http.server
import json
import os
import socketserver
import sys
import urllib.parse

port = int(next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--remote-debugging-port=")))
profile = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--user-data-dir="))
os.makedirs(profile, exist_ok=True)
with open(os.path.join(profile, "argv.json"), "w") as f:
    json.dump(sys.argv[1:], f)


tabs = [{"id": "T0", "type": "page", "url": next((a for a in sys.argv[1:] if "://" in a or a.startswith("about:")), "about:blank")}]


class H(http.server.BaseHTTPRequestHandler):
    def do_PUT(self):
        self.do_GET()

    def do_GET(self):
        body = json.dumps({"Browser": "FakeChrome/1.0", "webSocketDebuggerUrl": f"ws://127.0.0.1/devtools/browser/{os.getpid()}"}).encode()
        code = 200 if self.path == "/json/version" else 404
        if self.path == "/json/list":
            code, body = 200, json.dumps(tabs).encode()
        elif self.path.startswith("/json/new?"):
            tabs.append({"id": f"T{len(tabs) + 100}", "type": "page", "url": urllib.parse.unquote(self.path.split("?", 1)[1])})
            code, body = 200, json.dumps(tabs[-1]).encode()
        elif self.path.startswith("/json/close/"):
            tabs[:] = [t for t in tabs if t["id"] != self.path.rsplit("/", 1)[1]]
            code, body = 200, b"Target is closing"
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class Server(http.server.HTTPServer):
    def server_bind(self):   # no socket.getfqdn — as in core/httpd: a slow resolver does not hold up startup
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


Server(("127.0.0.1", port), H).serve_forever()
