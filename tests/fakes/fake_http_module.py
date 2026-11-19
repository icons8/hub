"""A fake module process with handles (Module API, handles /api/mod/<name>/… and views /mod/<name>/view/…): what the page
passes to the module and how it relays the module's answer. Also answers /hook/hello — the module stand (modstand) may start it.

Run: `python3 fake_http_module.py <module folder>` (cwd — the folder with module.json). Environment: HUB_MODULE_PORT,
HUB_MODULE_SECRET, HUB_STATE_DIR, HUB_MODULE (set by the core), FAKE_MODULE_LOG — JSONL of what arrived and what happened.

Handles (the last word of the path after /api/mod/<name>/ or /mod/<name>/view/):
  echo   — read the body in chunks: {method, path, headers, size, sha256}
  first  — like echo, but mark the first body chunk in the log at once ({"event": "first", "size"}): the body is streamed
  sink   — read the body and answer {size} (no hash — for load measurements)
  sse    — text/event-stream: ?n= events every ?gap= s (n=0 — endless); client gone — {"event": "gone"}
  hang   — stream headers and one event, then silence
  big    — ?n= bytes of response with Content-Length
  cut    — Content-Length 1000, send 10 bytes and close
  nohead — accept and stay silent
  garbage — not HTTP
  inject — a response header with a bare line feed inside (an attempt to slip the client a header of its own)
  cookies — a response that tries to clear or plant the owner's sign-in cookies, next to its own
  digitlen — a response whose Content-Length is "²" (a digit to str.isdigit, not a number to int); digitstatus — the same in the status code
  owner  — with X-Hub-Owner: 1 put the owner's word into the mailbox (message.received from channel mod:<name>); {"owner": bool}
  page   — an HTML view with a script that tries the hub API and tells the parent what came out
  bridge — an HTML view with hubmod.VIEW_JS: calls its own handle, the hub API and another module through the page; ?go=late —
           then moves on to the late view
  late   — an HTML view opened by navigation inside the frame: waits for the port 1.5 s and reports whether it was given
  hop    — an HTML view that asks the page to go to bridge (hub.go): there the port is available again
  popup  — an HTML view with a button that opens a new window on click (a link with target="_blank" is the same sandbox
           flag): window.__pop — opened | blocked
"""
import hashlib
import json
import os
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from core import hubmod, spool  # noqa: E402

PAGE = b"""<!doctype html><meta charset="utf-8"><body>view<script>
(async () => {
  const out = {origin: String(self.origin)};
  try { const r = await fetch('/api/snapshot', {headers: {'X-Hub': '1'}}); out.fetch = 'status ' + r.status; }
  catch (e) { out.fetch = 'blocked'; }
  try { out.parent = String(parent.document.title); } catch (e) { out.parent = 'blocked'; }
  try { out.storage = String(localStorage.length); } catch (e) { out.storage = 'blocked'; }
  try { out.cookie = String(document.cookie); } catch (e) { out.cookie = 'blocked'; }
  window.__probe = out;
  parent.postMessage({probe: out}, '*');
})();
</script>"""


BRIDGE = """<!doctype html><meta charset="utf-8"><body>bridge""" + hubmod.VIEW_JS + """<script>
(async () => {
  const out = {};
  const t = async (k, p, o) => {
    try { const r = await hub.call(p, o); out[k] = r.status + " " + r.body; }
    catch (e) { out[k] = "refused: " + e.message; }
  };
  await t("own", "echo?x=1", {method: "POST", body: "γειά", type: "text/plain"});
  await t("json", "echo", {method: "GET"});
  await t("hub", "../../snapshot");
  await t("encoded", "%2e%2e/%2e%2e/snapshot");
  await t("other", "../other/echo");
  await t("absolute", new URL("/api/snapshot", document.referrer).href);   // the full address of the hub page
  await t("method", "echo", {method: "TRACE"});
  parent.postMessage({bridge: out}, "*");
  if (__GO__) location.href = "late";
})();
</script>"""
LATE = """<!doctype html><meta charset="utf-8"><body>late""" + hubmod.VIEW_JS + """<script>
setTimeout(() => parent.postMessage({late: "no port"}, "*"), 1500);
hub.call("echo").then(() => parent.postMessage({late: "got port"}, "*"));
</script>"""

POPUP = b"""<!doctype html><meta charset="utf-8"><body style="margin:0">
<button id="out" style="width:240px;height:80px">out</button><script>
document.getElementById("out").addEventListener("click", () => {
  let w = null;
  try { w = window.open("about:blank", "_blank"); } catch (e) { w = null; }
  window.__pop = w ? "opened" : "blocked";
  parent.postMessage({pop: window.__pop}, "*");
});
</script>"""

HOP = """<!doctype html><meta charset="utf-8"><body>hop""" + hubmod.VIEW_JS + """<script>hub.go("bridge");</script>"""


def log(entry):
    path = os.environ.get("FAKE_MODULE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def manifest():
    with open("module.json", encoding="utf-8") as f:
        return json.load(f)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def answer(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self, mark_first=False):
        left, h, size = int(self.headers.get("Content-Length") or 0), hashlib.sha256(), 0
        while left > 0:
            chunk = self.rfile.read1(min(left, 1 << 16)) if hasattr(self.rfile, "read1") else self.rfile.read(min(left, 1 << 16))
            if not chunk:
                break
            if mark_first and not size:
                log({"event": "first", "size": len(chunk)})
            h.update(chunk)
            size += len(chunk)
            left -= len(chunk)
        return size, h.hexdigest()

    def handle_any(self):
        ok = self.headers.get("X-Hub-Secret") == os.environ.get("HUB_MODULE_SECRET")
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        what = url.path.rstrip("/").rsplit("/", 1)[-1]
        log({"event": "request", "method": self.command, "path": self.path, "secret_ok": ok,
             "headers": {k.lower(): v for k, v in self.headers.items()}})
        if not ok:
            return self.answer(403, b'{"error": "secret"}')
        if url.path == "/hook/hello":
            self.read_body()
            man = manifest()
            return self.answer(200, json.dumps({"name": man["name"], "version": man["version"],
                                                "points": []}).encode())
        if what == "sink":
            left, size = int(self.headers.get("Content-Length") or 0), 0
            while left > 0:
                chunk = self.rfile.read1(min(left, 1 << 16))
                if not chunk:
                    break
                size += len(chunk)
                left -= len(chunk)
            return self.answer(200, json.dumps({"size": size}).encode())
        if what in ("echo", "first"):
            size, digest = self.read_body(mark_first=what == "first")
            return self.answer(200, json.dumps({"method": self.command, "path": self.path, "size": size,
                                                "sha256": digest,
                                                "headers": {k.lower(): v for k, v in self.headers.items()}}).encode())
        if what == "owner":
            owner = self.headers.get("X-Hub-Owner") == "1"
            if owner:
                name = os.environ["HUB_MODULE"]
                spool.put(os.path.join(os.environ["HUB_STATE_DIR"], "spool"), "message.received", None,
                          {"to": q.get("to") or "a", "text": q.get("text") or "hello",
                           "channel": f"mod:{name}", "owner": True}, src="module")
            return self.answer(200, json.dumps({"owner": owner}).encode())
        if what in ("sse", "hang"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            n, gap = int(q.get("n", "3")), float(q.get("gap", "0.05"))
            try:
                i = 0
                while n == 0 or i < n:
                    self.wfile.write(f"event: tick\ndata: {i}\n\n".encode())
                    self.wfile.flush()
                    i += 1
                    if what == "hang":
                        time.sleep(3600)
                    time.sleep(gap)
            except OSError:
                log({"event": "gone", "after": i})
            return
        if what == "big":
            n = int(q.get("n", "0"))
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(n))
            self.end_headers()
            block = b"x" * (1 << 16)
            try:
                while n > 0:
                    self.wfile.write(block[:n])
                    n -= len(block)
            except OSError:
                log({"event": "gone"})
            return
        if what == "cut":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b"0123456789")
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)
            self.close_connection = True
            return
        if what == "nohead":
            time.sleep(3600)
        if what == "drop":   # close the connection without answering a single byte
            self.connection.shutdown(socket.SHUT_RDWR)
            self.close_connection = True
            return
        if what == "garbage":
            self.wfile.write(b"nonsense\r\n\r\n")
            self.close_connection = True
            return
        if what == "inject":
            self.wfile.write(b"HTTP/1.0 200 OK\r\nX-A: a\nSet-Cookie: x=1\r\nContent-Length: 2\r\n\r\nok")
            self.close_connection = True
            return
        if what == "cookies":
            self.wfile.write(b"HTTP/1.0 200 OK\r\nSet-Cookie: hub_main=; Path=/; Max-Age=0\r\n"
                             b"Set-Cookie: CF_Authorization=planted; Path=/\r\nSet-Cookie: mod_seen=2; Path=/\r\n"
                             b"Set-Cookie: Hub_layout=wide; Path=/\r\nClear-Site-Data: \"cookies\"\r\n"
                             b"Set-Cookie: =hub_main=planted; Path=/\r\nSet-Cookie: CF_Session=; Max-Age=0\r\n"
                             b"Set-Cookie: cf_filter=open\r\nWWW-Authenticate: Basic realm=\"hub\"\r\n"
                             b"Content-Length: 2\r\n\r\nok")
            self.close_connection = True
            return
        if what == "digitlen":
            self.wfile.write(b"HTTP/1.0 200 OK\r\nContent-Length: \xb2\r\n\r\nok")
            self.close_connection = True
            return
        if what == "digitstatus":
            self.wfile.write(b"HTTP/1.0 2\xb20 OK\r\nContent-Length: 2\r\n\r\nok")
            self.close_connection = True
            return
        if what == "page":
            return self.answer(200, PAGE, "text/html; charset=utf-8")
        if what == "bridge":
            page = BRIDGE.replace("__GO__", "true" if q.get("go") == "late" else "false")
            return self.answer(200, page.encode(), "text/html; charset=utf-8")
        if what == "popup":
            return self.answer(200, POPUP, "text/html; charset=utf-8")
        if what == "hop":
            return self.answer(200, HOP.encode(), "text/html; charset=utf-8")
        if what == "late":
            return self.answer(200, LATE.encode(), "text/html; charset=utf-8")
        return self.answer(404, b'{"error": "no such route"}')

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_any


def main():
    if len(sys.argv) > 1:
        os.chdir(sys.argv[1])
    srv = ThreadingHTTPServer(("127.0.0.1", int(os.environ["HUB_MODULE_PORT"])), Handler)
    srv.daemon_threads = True
    log({"event": "started", "pid": os.getpid()})
    srv.serve_forever()


if __name__ == "__main__":
    main()
