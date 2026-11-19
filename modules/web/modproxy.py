"""Module endpoints on the page (Module API, spec §2.2): the page forwards `/api/mod/<name>/…` and the HTML views
`/mod/<name>/view/…` to the module's process (core/modrun.py) and relays its response to the client. The module gets
the same entry as the page, while the module does not occupy core paths: a new core endpoint does not break a module,
and a module cannot replace a core endpoint.

- Access is as for the page (localhost or the owner from the tailnet). `X-Hub: 1` is required on all methods of
  endpoints: a foreign site in the owner's browser would send such a request only after a CORS check, which the page
  does not pass.
- The request goes to the module as is (method, path with /api/mod/<name>/, client headers without service ones and
  without the owner's sign-in to the page — SIGN_IN: the hub's cookie, Authorization, Cloudflare Access's) with
  the `X-Hub-Secret` of its launch and `X-Hub-Owner: 1` — the request passed the owner check: only on it does the
  module put the owner's word into the mailbox (channel `mod:<name>`). `X-Hub-*` headers from the client never reach
  the module — they cannot be forged.
- The body and the response stream in chunks (CHUNK): the page holds neither in memory. The body is accepted only
  with Content-Length, no larger than the module's max_body; at once there are at most BODIES requests with a body per
  module, STREAMS streams (text/event-stream) per module and TOTAL connections to all modules: beyond that — 429 and
  503, the page's ordinary endpoints keep working.
- Stream: if the module sends no byte for IDLE seconds, or max_stream expires, the connection is closed; if
  the client leaves, the connection to the module is closed too. A module failure before the response headers is a 502
  and module.error (after FAILS failures the module is suspended — 503); after that the client sees the response end.
  If the process does not accept connections (crashed, hub-up restarts it) — 503 with Retry-After; the journal gets
  one entry per launch (core/modrun.py, CRASH): a single crash does not pause the module, however often a client
  polled the endpoint.
- An HTML view is GET only, without X-Hub (it is opened in an `<iframe>`) and without X-Hub-Owner; the response is
  sandboxed by VIEW_CSP: an opaque origin, the view's script cannot read the hub API or fetch from the network, even
  if the embedder forgot `sandbox`. A link with target="_blank" (or window.open) opens a new tab outside the sandbox —
  as for files (RENDER_CSP): a view could already leave for any address by navigating itself. A view calls its own module's endpoints through the page (static/js/modview.js): that
  request carries `X-Hub-View: 1` — the module receives `X-Hub-View: 1` instead of `X-Hub-Owner: 1` (a client can
  only reduce its own rights this way).
- A module's own paths (Module API 2.4, core/modclaims.py): a path /api/… that the core does not have and that the
  module named in http.paths (endpoint) or http.views (HTML view) is handled the same way as /api/mod/<name>/… and
  /mod/<name>/view/…; the module receives it as /api/mod/<name><path> or /mod/<name>/view<path>, the query as is.
  Whose path it is was decided by hubd (the want file: claims); the page never hands a core path to a module,
  whatever lies in want.
Without modules (neither a want file nor a module entry in the journal) paths are not recognized — the page responds
as if this file did not exist.
"""
import json
import os
import re
import select
import socket
import threading
import time

from core import i18n, modclaims, modrun, modules
from modules.tailnet import access

IDLE = 60.0                         # how long to wait for a byte in either direction, seconds
RECHECK = 60.0                      # an open response asks again who may read it (a remote token expired), seconds
MAX_BODY = 20 << 20                 # default request body — like a page attachment; up to HTTP_MAX_BODY in the manifest
MAX_STREAM = 600                    # how long to hold one response, seconds; up to HTTP_MAX_STREAM in the manifest
BODIES, STREAMS, TOTAL = 4, 8, 32   # at once: requests with a body per module, streams per module, connections to all
CHUNK = 64 << 10
HEAD_MAX = 64 << 10                 # the module's response headers
DRAIN = 32 << 20                    # on refusal: read the body to no more than this (in chunks) so the client sees the response
VIEW_CSP = ("sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src data: blob:; media-src data: blob:; font-src data:; frame-ancestors 'self'")
_NAME = modules.NAME.pattern
API = re.compile(rf"/api/mod/({_NAME})(?:[/?]|$)")
VIEW = re.compile(rf"/mod/({_NAME})/view(?:[/?]|$)")
# not forwarded to the module (each side has its own connection) and not returned to the client
HOP = {"connection", "keep-alive", "proxy-connection", "proxy-authenticate", "proxy-authorization", "te", "trailer",
       "upgrade", "expect", "host", "content-length", "transfer-encoding"}
WITH_BODY = ("POST", "PUT", "PATCH")
# the owner's sign-in to the page is the hub's, never a module's: a module that got it could open the page as the owner.
# Headers: X-Hub-* (forged, or the local token of `hub secret`), Authorization (Basic auth of a proxy in front of the
# page), Cloudflare Access's token and a service token's id and secret (Cf-Access-*). Cookies: the page key's and
# password's of every hub of the host (hub_<…>, core/instance.cookie_name) and Cloudflare Access's (CF_Authorization,
# CF_AppSession, CF_Binding…: all CF_<…>). Nor may a module's answer set or clear them, or ask the browser for a
# password under the hub's address (Set-Cookie with such a name or none, Clear-Site-Data, WWW-Authenticate).
SIGN_IN = ("authorization",)
SIGN_IN_PREFIXES = ("x-hub-", "cf-access-")
SIGN_IN_COOKIES = ("hub_", "CF_")
ANSWER_DROPPED = ("server", "date", "clear-site-data", "www-authenticate")


def sign_in_cookie(name):
    """Is the cookie `name` the owner's sign-in (the hub's or Cloudflare Access's), never a module's? A nameless one
    counts too: a browser sends `=hub_main=…` back as `hub_main=…`."""
    name = name.strip()
    return not name or name.startswith(SIGN_IN_COOKIES)


def limits(man):
    """The module's (max_body, max_stream) from the manifest's http field."""
    http = (man or {}).get("http") or {}
    return int(http.get("max_body") or MAX_BODY), int(http.get("max_stream") or MAX_STREAM)


class Proxy:
    """records() — state["modules"] of the page's journal reader."""

    def __init__(self, state_dir, records):
        self.state_dir = state_dir
        self.records = records
        self.mods = modrun.Modules(state_dir, records=records)
        self.lock = threading.Lock()
        self.total, self.bodies, self.streams = 0, {}, {}
        self.manifests = {}   # folder -> (mtime, manifest)

    def route(self, path):
        """(name, "api" | "view", path for the module, wants()) — a module path the hub knows about; otherwise None."""
        for kind, rx in (("api", API), ("view", VIEW)):
            m = rx.match(path or "")
            if m:
                name, wanted = m.group(1), modrun.wants(self.state_dir)
                if name in wanted or name in (self.records() or {}):
                    return name, kind, path, wanted
                return None
        own, sep, query = (path or "").partition("?")
        if not own.startswith("/api/") or modclaims.core_path(own):
            return None   # core paths belong to the core, do not read module files
        wanted = modrun.wants(self.state_dir)
        for name, w in sorted(wanted.items()):
            claims = w.get("claims") if isinstance(w.get("claims"), dict) else {}
            if own in (claims.get("paths") or []):
                return name, "api", f"/api/mod/{name}{own}{sep}{query}", wanted
            if own in (claims.get("views") or []):
                return name, "view", f"/mod/{name}/view{own}{sep}{query}", wanted
        return None

    def manifest(self, want):
        path = os.path.join(want.get("cwd") or "", "module.json")
        try:
            mtime = os.stat(path).st_mtime_ns
            if path not in self.manifests or self.manifests[path][0] != mtime:
                with open(path, encoding="utf-8-sig") as f:
                    self.manifests[path] = (mtime, json.load(f))
            return self.manifests[path][1]
        except (OSError, ValueError):
            return {}

    # --- request -----------------------------------------------------------------------------------------------------

    def serve(self, h, method):
        """Answer request h (the page's Handler) if it is a module path. False — not a module path, the page answers."""
        hit = self.route(h.path)
        if not hit:
            return False
        name, kind, target, wanted = hit
        if not h.local_host():   # through a proxy: says so (access, not a method of h: a page server of an earlier release)
            from core import remote
            if remote.needs_signin(self.state_dir, h.headers.get("Host"), h.client_address[0], h.headers):
                return refuse(h, 401, t("web.pass.err_signin", h))   # only the page's password is missing
            if access.via_proxy(h.client_address[0], h.headers):
                by = remote.refused_by(self.state_dir, h.headers.get("Host"), h.client_address[0], h.headers)
                return refuse(h, 403, t("web.srv.err_cloudflare" if by == "cloudflare" else "web.srv.err_proxied", h))
            return refuse(h, 403, t("web.srv.err_localhost", h))
        if kind == "api" and h.headers.get("X-Hub") != "1":
            return refuse(h, 403, t("web.srv.err_not_from_page", h))
        if kind == "view" and method != "GET":
            return refuse(h, 405, t("web.modproxy.view_get_only", h), extra={"Allow": "GET"})
        want = wanted.get(name)
        if not want:
            return refuse(h, 503, t("web.modproxy.not_running", h, name=name))
        if "http" not in (want.get("capabilities") or []):
            return refuse(h, 404, t("web.modproxy.no_http", h, name=name))
        run = self.mods.ready(name, wanted)
        if not run:
            why = self.mods.status(name)
            if why == "ok":
                return refuse(h, 503, t("web.modproxy.starting", h, name=name), extra={"Retry-After": "2"})
            return refuse(h, 503, t("web.modproxy.not_ready", h, name=name, why=why))
        max_body, max_stream = limits(self.manifest(want))
        length = content_length(h, method in WITH_BODY)
        if length is None:
            return True
        if length > max_body:
            drain(h, length)
            return refuse(h, 413, t("web.modproxy.body_too_big", h, size=size_text(max_body, _lang(h)), name=name),
                          close=True)
        with self.lock:
            if self.total >= TOTAL:
                full = (503, t("web.modproxy.too_many", h, max=TOTAL))
            elif length and self.bodies.get(name, 0) >= BODIES:
                full = (429, t("web.modproxy.busy_bodies", h, name=name, count=BODIES))
            else:
                full = None
                self.total += 1
                if length:
                    self.bodies[name] = self.bodies.get(name, 0) + 1
        if full:
            drain(h, length)
            return refuse(h, *full, close=True, extra={"Retry-After": "1"})
        try:
            self.pass_through(h, name, kind, method, run, length, max_stream, target)
        finally:
            with self.lock:
                self.total -= 1
                if length:
                    self.bodies[name] -= 1
            try:   # from here the connection is as the server's (core/httpd.py conn_timeout)
                h.connection.settimeout(getattr(h.server, "conn_timeout", None))
            except OSError:
                pass
        return True

    def fail(self, h, name, run, kind, error, sent=True):
        """A module failure before the response headers: module.error and a 502 with the reason; if the process did not
        accept the request (sent=False: crashed and restarting) — 503 with Retry-After, one journal entry per launch.
        If the request already went out — only 502: the module may have executed it, so we do not advise the client to
        repeat it. `error` is a text, or (catalog key, params): the journal fact gets it in the process language, the
        page's refusal in the language of the request."""
        key, params = error if isinstance(error, tuple) else (None, {})
        self.mods.fail(name, "http", kind, i18n.t(key, **params) if key else error, run.get("launch"))
        if not sent:
            return refuse(h, 503, t("web.modproxy.restarting", h, name=name), close=True,
                          extra={"Retry-After": "2"})
        return refuse(h, 502, t("web.modproxy.failed", h, name=name, error=t(key, h, **params) if key else error),
                      close=True)

    def pass_through(self, h, name, kind, method, run, length, max_stream, target):
        try:
            m = socket.create_connection(("127.0.0.1", int(run["port"])), timeout=IDLE)
        except OSError as e:
            drain(h, length)
            return self.fail(h, name, run, "connect", ("web.modproxy.err_connect", {"error": type(e).__name__}),
                             sent=False)
        with m:
            m.settimeout(IDLE)
            h.connection.settimeout(IDLE)
            try:
                m.sendall(self.head(h, method, kind, run, length, target))
            except OSError as e:
                drain(h, length)
                return self.fail(h, name, run, "closed", ("web.modproxy.err_request", {"error": type(e).__name__}),
                                 sent=False)
            # the body in chunks: read from the client, passed to the module
            left = length
            for chunk in body(h, length):
                try:
                    m.sendall(chunk)
                except socket.timeout:
                    return self.fail(h, name, run, "timeout", ("web.modproxy.err_body", {"seconds": f"{IDLE:g}"}))
                except OSError:
                    h.close_connection = True   # the module closed its input — next comes its response, if any
                    break
                left -= len(chunk)
            else:
                if left:   # the client left or went silent for IDLE: nobody to answer
                    return None
            deadline = time.monotonic() + max_stream
            got = _read_head(m, deadline)
            if isinstance(got, tuple) and len(got) == 2:
                return self.fail(h, name, run, *got)
            code, headers, rest = got
            ctype = next((v for k, v in headers if k.lower() == "content-type"), "")
            stream = ctype.lower().startswith("text/event-stream")
            if stream:
                with self.lock:
                    if self.streams.get(name, 0) >= STREAMS:
                        stream = None
                    else:
                        self.streams[name] = self.streams.get(name, 0) + 1
                if stream is None:
                    return refuse(h, 429, t("web.modproxy.busy_streams", h, name=name, count=STREAMS), close=True)
            try:
                self.answer(h, m, kind, code, headers, rest, deadline)
            finally:
                if stream:
                    with self.lock:
                        self.streams[name] -= 1
        return None

    def head(self, h, method, kind, run, length, target):
        lines = [f"{method} {target} HTTP/1.0", f"Host: 127.0.0.1:{run['port']}"]
        for k, v in h.headers.items():
            low = k.lower()
            if low in HOP or low in SIGN_IN or low.startswith(SIGN_IN_PREFIXES) or "\r" in v or "\n" in v:
                continue
            if low == "cookie":   # the module's own cookies go on
                v = "; ".join(p for p in (p.strip() for p in v.split(";")) if p and not sign_in_cookie(p.split("=", 1)[0]))
                if not v:
                    continue
            lines.append(f"{k}: {v}")
        lines.append(f"X-Hub-Secret: {run['secret']}")
        if kind == "api" and h.headers.get("X-Hub-View") == "1":
            lines.append("X-Hub-View: 1")   # a request from a module's HTML view through the page (modview.js): not the owner's word
        elif kind == "api":
            lines.append("X-Hub-Owner: 1")
        if length or method in WITH_BODY:
            lines.append(f"Content-Length: {length}")
        lines.append("Connection: close")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace")

    def answer(self, h, m, kind, code, headers, rest, deadline):
        """The module's headers to the client (without service ones) and the body in chunks until the end, the deadline
        or either side leaving."""
        size = None
        for k, v in headers:
            if k.lower() == "content-length" and number(v) and size is None:
                size = int(v.strip())
        if any(k.lower() == "transfer-encoding" for k, _ in headers):
            size = None
        h.send_response(code)
        for k, v in headers:
            low = k.lower()
            if low in HOP - {"transfer-encoding"} or low in ANSWER_DROPPED:
                continue
            if low == "set-cookie" and sign_in_cookie(v.split(";", 1)[0].split("=", 1)[0]):
                continue
            h.send_header(k, v)
        if size is not None:
            h.send_header("Content-Length", str(size))
        else:
            h.send_header("Connection", "close")   # the end of the response is the connection closing
        if kind == "view":
            h.send_header("Content-Security-Policy", VIEW_CSP)
            h.send_header("X-Content-Type-Options", "nosniff")
        h.end_headers()
        left = size
        client, watch = h.connection, True
        # asked again while the answer streams: a remote token expires, the page key is turned on or changed
        checked = moved = time.monotonic()
        try:
            if rest:
                rest = rest if left is None else rest[:left]
                h.wfile.write(rest)
                left = None if left is None else left - len(rest)
            while left is None or left > 0:
                wait = deadline - time.monotonic()
                if wait <= 0:
                    break
                now = time.monotonic()
                if now - checked >= RECHECK:
                    if not h.local_host():
                        left = left or 1
                        break
                    checked = now
                budget = min(IDLE - (now - moved), wait, RECHECK - (now - checked))
                ready, _, _ = select.select([m, client] if watch else [m], [], [], max(0.0, budget))
                if not ready:
                    if time.monotonic() - moved >= IDLE:
                        break   # no byte for IDLE seconds
                    continue    # woke up for the re-check
                if client in ready:
                    if not client.recv(1, socket.MSG_PEEK):
                        break   # the client left
                    watch = False   # the client sent its next request — we will notice a departure on write
                if m not in ready:
                    continue
                chunk = m.recv(CHUNK if left is None else min(CHUNK, left))
                if not chunk:
                    break
                moved = time.monotonic()
                h.wfile.write(chunk)
                if left is not None:
                    left -= len(chunk)
        except (OSError, ValueError):
            left = left or 1
        if left:
            h.close_connection = True   # the response was cut off: the client must see that it is incomplete


def _read_head(m, deadline):
    """The module's response headers: (code, [(name, value)], start of the body) or (kind of failure, (catalog key,
    params) of the text - for fail())."""
    data = b""
    while b"\r\n\r\n" not in data:
        wait = min(IDLE, deadline - time.monotonic())
        if wait <= 0:
            return "timeout", ("web.modproxy.err_deadline", {})
        m.settimeout(wait)
        try:
            chunk = m.recv(CHUNK)
        except socket.timeout:
            return "timeout", ("web.modproxy.err_silent", {"seconds": f"{wait:g}"})
        except OSError as e:
            return "closed", ("web.modproxy.err_dropped", {"error": type(e).__name__})
        if not chunk:
            if not data:
                return "closed", ("web.modproxy.err_closed", {})
            return "schema", ("web.modproxy.err_not_http", {})
        data += chunk
        if len(data) > HEAD_MAX and b"\r\n\r\n" not in data[:HEAD_MAX + 4]:
            return "schema", ("web.modproxy.err_head_big", {"kb": HEAD_MAX >> 10})
    top, _, rest = data.partition(b"\r\n\r\n")
    lines = top.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/") or not (number(parts[1]) and 200 <= int(parts[1]) < 600):
        return "schema", ("web.modproxy.err_not_http", {})
    headers = []
    for line in lines[1:]:
        k, sep, v = line.partition(":")
        if not sep or not k.strip() or k != k.strip() or "\n" in line or "\r" in line:
            return "schema", ("web.modproxy.err_bad_header", {})
        headers.append((k, v.strip()))
    m.settimeout(IDLE)
    return int(parts[1]), headers, rest


def number(v):
    """v is a whole number int() reads: ASCII digits only ("²" is a digit to str.isdigit and not a number to int()) and
    fewer than int()'s digit limit."""
    v = v.strip()
    return v.isascii() and v.isdigit() and len(v) < 20


def content_length(h, need):
    """The body length from Content-Length. If a body is needed (need) but there is no length, or it is chunked, or the
    length is not a number — a 411 / 400 refusal that closes the connection, and None."""
    length = h.headers.get("Content-Length")
    if h.headers.get("Transfer-Encoding") or (length is None and need):
        refuse(h, 411, t("web.modproxy.need_length", h), close=True)
        return None
    if length is not None and not number(length):
        refuse(h, 400, t("web.modproxy.length_number", h), close=True)
        return None
    return int(length or 0)


def body(h, length):
    """The client's body in CHUNK-sized pieces, no more than length in total: one chunk in memory. If the client leaves
    or goes silent for the connection timeout — it ends before length (the caller sees the shortfall) and sets
    h.close_connection."""
    left = length
    while left > 0:
        try:
            chunk = h.rfile.read1(min(CHUNK, left))
        except OSError:
            chunk = b""
        if not chunk:
            h.close_connection = True
            return
        left -= len(chunk)
        yield chunk


def drain(h, length, cap=DRAIN):
    """Read off a small body we answer with a refusal: otherwise a client still sending it will not see the response
    (cap — the largest body worth reading off, DRAIN by default)."""
    if not 0 < length <= cap:
        return
    try:
        h.connection.settimeout(min(IDLE, 5))
        left = length
        while left > 0:
            chunk = h.rfile.read1(min(CHUNK, left))
            if not chunk:
                break
            left -= len(chunk)
    except OSError:
        pass


def size_text(n, lang=None):
    """A size in words in language lang (without it, the process language, core/i18n.py current): whole GB, MB or bytes."""
    lang = lang or i18n.current()
    if n >= 1 << 30 and not n % (1 << 30):
        return i18n.tr(lang, "web.modproxy.size_gb", n=n >> 30)
    if n >= 1 << 20:
        return i18n.tr(lang, "web.modproxy.size_mb", n=n >> 20)
    return i18n.tr(lang, "web.modproxy.size_bytes", count=n)


def _lang(h):
    """The language of request h (the page's Handler: lang()); if none, the process language (core/i18n.py current)."""
    ask = getattr(h, "lang", None)
    return ask() if callable(ask) else i18n.current()


def t(key, h, **params):
    """The refusal text in the language of request h."""
    return i18n.tr(_lang(h), key, **params)


def refuse(h, code, text, close=False, extra=None, fields=None):
    """A page refusal as JSON {"error": text}; a 401 is the page's password (core/pagepass.py): {"code": "signin"} too;
    fields — more members of that object (a number the client needs)."""
    body = json.dumps({"error": text, **({"code": "signin"} if code == 401 else {}), **(fields or {})}, ensure_ascii=False).encode()
    try:
        h.send_response(code)
        h.send_header("Content-Type", "application/json; charset=utf-8")
        h.send_header("Content-Length", str(len(body)))
        h.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            h.send_header(k, v)
        if close:
            h.send_header("Connection", "close")
        h.end_headers()
        h.wfile.write(body)
    except OSError:
        h.close_connection = True
    return True
