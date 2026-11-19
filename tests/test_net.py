"""HTTPS through the owner's proxy (core/net.py): the proxy login works as in curl, a TLS failure or a refused CONNECT
leaves no open sockets, an IPv6 target goes in brackets, a server that closes without a TLS goodbye means end of read,
not an error."""
import tests  # noqa: F401 — hide the machine's settings
import base64
import http.client
import http.server
import os
import re
import socket
import ssl
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from unittest import mock

import platforms
from core import net


def proxy(url):
    return urllib.parse.urlsplit(url)


class PlainProxy:
    """Local proxy: reads one CONNECT and answers with the given status line."""

    def __init__(self, status):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.status, self.request, self.conn = status, b"", None
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.conn, _ = self.srv.accept()
        while not self.request.endswith(b"\r\n\r\n"):
            chunk = self.conn.recv(1)
            if not chunk:
                return
            self.request += chunk
        self.conn.sendall(self.status + b"\r\n\r\n")

    def close(self):
        self.thread.join(5)
        for s in (self.conn, self.srv):
            if s:
                s.close()


class FakeTunnel:
    """TLS to a proxy that itself listens on TLS: records what it was sent, replies with the given bytes."""

    def __init__(self, reply):
        self.reply, self.sent, self.closed = reply, b"", False

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        chunk, self.reply = self.reply[:n], self.reply[n:]
        return chunk

    def settimeout(self, t):
        pass

    def close(self):
        self.closed = True


class Proxy(unittest.TestCase):
    def test_the_login_is_percent_decoded(self):
        auth = net.proxy_auth(proxy("https://us%40er:p%3Ass%40@proxy.example:443"))
        self.assertEqual(base64.b64decode(auth.split()[1]), b"us@er:p:ss@")

    def test_an_ipv6_target_is_named_in_brackets(self):
        # CONNECT ::1:443 is not an address: an IPv6 target goes as [::1]:443
        fake = PlainProxy(b"HTTP/1.1 403 Forbidden")
        self.addCleanup(fake.close)
        with self.assertRaises(OSError):
            net.connect(proxy(f"http://127.0.0.1:{fake.port}"), "2001:db8::1", 443, 5)
        fake.thread.join(5)
        self.assertTrue(fake.request.startswith(b"CONNECT [2001:db8::1]:443 HTTP/1.1\r\nHost: [2001:db8::1]:443\r\n"),
                        fake.request)

    def test_a_refused_connect_closes_the_socket(self):
        fake = PlainProxy(b"HTTP/1.1 407 Proxy Authentication Required")
        self.addCleanup(fake.close)
        made, real = [], socket.create_connection

        def create(*a, **kw):
            made.append(real(*a, **kw))
            return made[-1]
        self.addCleanup(lambda: [s.close() for s in made])
        with mock.patch.object(net.socket, "create_connection", create), self.assertRaises(OSError):
            net.connect(proxy(f"http://127.0.0.1:{fake.port}"), "example.com", 443, 5)
        self.assertEqual([s.fileno() for s in made], [-1])

    def test_a_failed_tls_to_the_proxy_closes_the_raw_socket(self):
        # TLS to the proxy failed: the raw socket used to stay open
        raw, ctx = mock.Mock(), mock.Mock()
        ctx.wrap_socket.side_effect = ssl.SSLError("handshake failed")
        with mock.patch.object(net.socket, "create_connection", return_value=raw), self.assertRaises(ssl.SSLError):
            net.connect(proxy("https://proxy.example:443"), "api.example", 443, 5, ctx)
        raw.close.assert_called()

    def test_a_refused_connect_through_a_tls_proxy_closes_the_tunnel(self):
        tunnel, ctx = FakeTunnel(b"HTTP/1.1 403 Forbidden\r\n\r\n"), mock.Mock()
        ctx.wrap_socket.return_value = tunnel
        with mock.patch.object(net.socket, "create_connection", return_value=mock.Mock()), \
                self.assertRaises(OSError):
            net.tls_via(proxy("https://proxy.example:443"), "::1", 443, 5, ctx)
        self.assertTrue(tunnel.closed)
        self.assertTrue(tunnel.sent.startswith(b"CONNECT [::1]:443 HTTP/1.1\r\nHost: [::1]:443\r\n"), tunnel.sent)

    def test_a_failed_tls_to_the_target_closes_the_tunnel(self):
        # CONNECT went through but TLS to the target did not: nobody needs the tunnel to the proxy any more
        tunnel, ctx = FakeTunnel(b"HTTP/1.1 200 OK\r\n\r\n"), mock.Mock()
        ctx.wrap_socket.side_effect = ssl.SSLError("bad certificate")
        with mock.patch.object(net.socket, "create_connection", return_value=tunnel), \
                self.assertRaises(ssl.SSLError):
            net.tls_via(proxy("http://proxy.example:8080"), "api.example", 443, 5, ctx)
        self.assertTrue(tunnel.closed)

    def test_a_server_closing_without_a_tls_goodbye_ends_the_read(self):
        # a plain ssl socket reads such an end as b"" (suppress_ragged_eofs); the inner TLS layer does too
        inner = object.__new__(net.InnerTLS)
        inner._obj = mock.Mock()
        inner._obj.read.side_effect = ssl.SSLEOFError(8, "EOF occurred in violation of protocol")
        self.assertEqual(inner.recv(10), b"")


class TlsInTls(unittest.TestCase):
    """A real TLS proxy and a real TLS target on localhost: http.client reads through net.tls_via like in the gateway
    and the bot."""
    BODY = b"x" * (1 << 20)

    @classmethod
    def setUpClass(cls):
        if not shutil.which("openssl"):
            raise unittest.SkipTest("no openssl: cannot make a certificate for TLS")
        cls.dir = tempfile.mkdtemp(prefix="net-tls-")
        cls.addClassCleanup(platforms.current.rmtree, cls.dir)
        cls.cert, key = os.path.join(cls.dir, "c.pem"), os.path.join(cls.dir, "k.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1", "-keyout", key, "-out", cls.cert],
                       check=True, capture_output=True)
        cls.ctx_server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.ctx_server.load_cert_chain(cls.cert, key)

    def serve(self, handle):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        self.addCleanup(srv.close)

        def loop():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                threading.Thread(target=handle, args=(conn,), daemon=True).start()
        threading.Thread(target=loop, daemon=True).start()
        return srv.getsockname()[1]

    def start(self):
        def target(conn):   # one response with Connection: close and a body larger than any buffer, then closes
            with conn, self.ctx_server.wrap_socket(conn, server_side=True) as tls:
                seen = b""
                while not seen.endswith(b"\r\n\r\n"):
                    seen += tls.recv(1)
                tls.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: %d\r\n\r\n" % len(self.BODY)
                            + self.BODY)
        target_port = self.serve(target)

        def pipe(src, dst):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        def proxy_side(conn):   # TLS up to the client, CONNECT inside, then the target's bytes as they are
            with conn, self.ctx_server.wrap_socket(conn, server_side=True) as tls:
                seen = b""
                while not seen.endswith(b"\r\n\r\n"):
                    seen += tls.recv(1)
                with socket.create_connection(("127.0.0.1", target_port)) as up:
                    tls.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    back = threading.Thread(target=pipe, args=(up, tls), daemon=True)
                    back.start()
                    pipe(tls, up)
                    back.join(10)
        return self.serve(proxy_side), target_port

    def test_a_response_with_connection_close_is_read_to_the_end(self):
        # http.client closes the connection on "Connection: close" before it reads the body; a real socket stays open
        # while the response file is alive, and the inner TLS layer used to close the tunnel at once (truncated body)
        proxy_port, _ = self.start()
        ctx = ssl.create_default_context(cafile=self.cert)
        conn = http.client.HTTPSConnection("localhost", 443, timeout=10, context=ctx)
        conn.sock = net.tls_via(proxy(f"https://127.0.0.1:{proxy_port}"), "localhost", 443, 10, ctx)
        conn.request("GET", "/")
        resp = conn.getresponse()
        self.assertEqual(resp.read(), self.BODY)
        self.assertEqual(resp.status, 200)
        conn.close()

    def test_the_tunnel_closes_with_the_last_reference(self):
        inner = object.__new__(net.InnerTLS)
        inner._tunnel = FakeTunnel(b"")
        inner._io_refs, inner._closed = 0, False
        reader = inner.makefile("rb")
        inner.close()
        self.assertFalse(inner._tunnel.closed, "the response file is still alive")
        reader.close()
        self.assertTrue(inner._tunnel.closed)
        inner.close()   # closing twice is harmless


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# the machine's proxy, which does not exist (nobody listens on port 9), and NO_PROXY without the hub's own addresses
DEAD_PROXY = {"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9",
              "http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9", "all_proxy": "http://127.0.0.1:9",
              "NO_PROXY": "example.org", "no_proxy": "example.org"}


def hub_modules():
    """Core modules that make HTTP calls through urllib or http.client's TLS: each is a hub process or part of one."""
    out = []
    for top in ("core", "modules"):
        for dirpath, _, files in os.walk(os.path.join(ROOT, top)):
            for f in files:
                path = os.path.join(dirpath, f)
                if not f.endswith(".py"):
                    continue
                with open(path, encoding="utf-8") as src:
                    text = src.read()
                if any(w in text for w in ("urllib.request", "HTTPSConnection", "create_default_context")):
                    out.append(os.path.relpath(path, ROOT)[:-3].replace(os.sep, "."))
    return sorted(out)


class Ok(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


class LocalDirect(unittest.TestCase):
    """The hub reaches itself (127.0.0.1, localhost, ::1) directly under any machine proxy, even without them in
    NO_PROXY; other addresses go through the proxy as before; the process environment (inherited by sessions) is
    unchanged."""

    def test_own_addresses_bypass_the_proxy_others_do_not(self):
        with mock.patch.dict(os.environ, DEAD_PROXY):
            for host in ("127.0.0.1", "127.0.0.1:8790", "localhost", "LOCALHOST:80", "[::1]:8790", "::1", "127.3.4.5"):
                self.assertTrue(net.is_local(host), host)
                self.assertTrue(urllib.request.proxy_bypass(host), host)
            for host in ("hub.example.com", "203.0.113.7", "localhost.example.com", "127.0.0.1.example.com", ""):
                self.assertFalse(net.is_local(host), host)
                self.assertFalse(urllib.request.proxy_bypass(host), host)
            self.assertTrue(urllib.request.proxy_bypass("example.org"))   # the machine's NO_PROXY, as before
            self.assertEqual(os.environ["NO_PROXY"], "example.org")

    def test_curl_gets_the_machines_list_plus_own_addresses(self):
        with mock.patch.dict(os.environ, DEAD_PROXY):
            self.assertEqual(net.no_proxy(), "example.org,127.0.0.1,localhost,::1")
        with mock.patch.dict(os.environ, {"NO_PROXY": "", "no_proxy": "localhost"}):
            self.assertEqual(net.no_proxy(), "localhost,127.0.0.1,::1")
        with mock.patch.dict(os.environ, {"NO_PROXY": "corp.example", "no_proxy": ""}):   # as in urllib: lowercase wins
            self.assertEqual(net.no_proxy(), "127.0.0.1,localhost,::1")
        with mock.patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"}):
            self.assertEqual(net.no_proxy(), "*")

    def test_every_hub_module_with_urllib_reaches_a_local_server_past_a_dead_proxy(self):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Ok)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        mods = hub_modules()
        self.assertIn("modules.browser.browsers", mods)
        env = {**os.environ, **DEAD_PROXY}
        for mod in mods:
            with self.subTest(mod):
                code = (f"import importlib, urllib.request; importlib.import_module({mod!r}); "
                        f"print(urllib.request.urlopen('http://127.0.0.1:{srv.server_port}/', timeout=5).read().decode())")
                r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True,
                                   timeout=60)
                self.assertEqual(r.stdout.strip(), "ok", r.stderr[-800:])


CERTS = os.path.join(ROOT, "tests", "fakes", "certs")


def ca_cert(name):
    """A self-signed test CA in DER: expired (2000–2001), current (2000–2125), future (2120–2125)."""
    with open(os.path.join(CERTS, name + ".pem"), encoding="ascii") as f:
        return ssl.PEM_cert_to_DER_cert(f.read())


def system_certs():
    """The machine's trusted certificates in DER: the Windows stores (all of them, past core.net's filter) or the
    default CA file."""
    if hasattr(ssl, "enum_certificates"):
        enum = getattr(ssl.enum_certificates, "hub_inner", ssl.enum_certificates)
        return [c for store in ("ROOT", "CA") for c, enc, _ in enum(store) if enc == "x509_asn"]
    path = ssl.get_default_verify_paths().cafile
    if not path or not os.path.isfile(path):
        return []
    with open(path, encoding="ascii", errors="replace") as f:
        text = f.read()
    return [ssl.PEM_cert_to_DER_cert(b) for b in re.findall(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----",
                                                               text, re.S)]


class WindowsStore(unittest.TestCase):
    """Windows: ssl takes the system stores ROOT and "Intermediate CAs" as trusted; an old copy of a CA there that is
    out of its dates (an expired cross-signed root) made OpenSSL build the chain through it — "certificate has
    expired" while a valid chain exists. Certificates out of their dates are left out of the stores: they cannot stand
    in any chain, so nothing new is trusted."""

    def test_a_context_does_not_load_store_certificates_outside_their_dates(self):
        certs = [(ca_cert(n), "x509_asn", True) for n in ("expired", "current", "future")]
        certs.append((b"\x30\x03not", "x509_asn", True))   # unreadable: the stdlib warns about it, as before
        code = ("import ssl, sys, warnings\n"
                "warnings.simplefilter('ignore')\n"
                f"certs = {certs!r}\n"
                "ssl.enum_certificates = lambda store: list(certs) if store == 'CA' else []\n"
                "from core import net\n"
                "sys.platform = 'win32'\n"   # ssl.load_default_certs reads the Windows stores only there
                "ctx = ssl.create_default_context()\n"
                "names = [dict(x[0] for x in c['subject']).get('commonName', '') for c in ctx.get_ca_certs()]\n"
                "print(sorted(n for n in names if n.startswith('Hub Test')))\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout.strip(), "['Hub Test current CA']", r.stderr[-800:])

    def test_the_dates_are_read_as_openssl_reads_them(self):
        def openssl_dates(der):
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.load_verify_locations(cadata=der)
            info = ctx.get_ca_certs()   # only CAs: OpenSSL does not show the dates of others
            return info and (ssl.cert_time_to_seconds(info[0]["notBefore"]), ssl.cert_time_to_seconds(info[0]["notAfter"]))
        for name in ("expired", "current", "future"):
            self.assertEqual(net.cert_dates(ca_cert(name)), openssl_dates(ca_cert(name)), name)
        shown = read = 0
        for der in system_certs():   # what the machine has; one ssl rejects or we cannot read is kept as before
            try:
                want = openssl_dates(der)
            except ssl.SSLError:
                continue
            if not want:
                continue
            shown += 1
            try:
                got = net.cert_dates(der)
            except (ValueError, IndexError):
                continue
            read += 1
            self.assertEqual(got, want)
        self.assertGreaterEqual(read, shown * 9 // 10)   # an odd one may stay unread, not the usual ones

    def test_an_unreadable_certificate_is_kept(self):
        now = 1_800_000_000   # 2027
        self.assertTrue(net.in_date(ca_cert("current"), now))
        self.assertFalse(net.in_date(ca_cert("expired"), now))
        self.assertFalse(net.in_date(ca_cert("future"), now))
        no_such_day = ca_cert("expired").replace(b"000101000000Z", b"000230000000Z", 1)   # notBefore 30.02.2000
        for junk in (b"", b"\x30", b"\x30\x03not", ca_cert("expired")[:60], b"\x30\x84\xff\xff\xff\xff", no_such_day):
            self.assertTrue(net.in_date(junk, now), junk)

    def test_a_valid_chain_from_the_server_wins_over_an_expired_copy_in_the_store(self):
        # the store: root One in ROOT, an expired copy of root Two cross-signed by One in "Intermediate CAs"; the server
        # sends leaf ← intermediate ← a valid cross-sign of Two by One — as with ISRG Root X2 after 15.09.2025
        try:
            dates = shutil.which("openssl") and "-not_after" in str(subprocess.run(
                ["openssl", "req", "-help"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30))
        except subprocess.TimeoutExpired:
            dates = False
        if not dates:
            self.skipTest("no openssl with -not_after (3.4+): cannot build a chain with dates")
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        for key in ("one", "two", "mid", "leaf"):
            subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", key + ".key"],
                           cwd=d, check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
        ca = ["-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign"]
        def cert(name, cn, key, issuer=None, until="21250101000000Z", ext=ca):
            """A certificate for `key`, signed by the certificate `issuer` and its key of the same name, or self-signed."""
            by = ["-CA", issuer + ".pem", "-CAkey", issuer + ".key"] if issuer else []
            subprocess.run(["openssl", "req", "-x509", "-new", "-key", key + ".key", *by, "-subj", "/CN=" + cn,
                            "-not_before", "20000101000000Z", "-not_after", until, *ext, "-out", name + ".pem"],
                           cwd=d, check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
            with open(os.path.join(d, name + ".pem"), encoding="ascii") as f:
                return f.read()
        one = cert("one", "Hub Test Root One", "one")
        old = cert("two-old", "Hub Test Root Two", "two", "one", until="20010101000000Z")
        chain = [cert("two", "Hub Test Root Two", "two", "one"), cert("mid", "Hub Test Intermediate", "mid", "two"),
                 cert("leaf", "localhost", "leaf", "mid",
                      ext=["-addext", "subjectAltName=DNS:localhost", "-addext", "basicConstraints=CA:FALSE"])]
        with open(os.path.join(d, "chain.pem"), "w", encoding="ascii") as f:
            f.write("".join(reversed(chain)))
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Ok)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(os.path.join(d, "chain.pem"), os.path.join(d, "leaf.key"))
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        stores = {"ROOT": [(ssl.PEM_cert_to_DER_cert(one), "x509_asn", True)],
                  "CA": [(ssl.PEM_cert_to_DER_cert(old), "x509_asn", True)]}
        def fetch(hook):
            code = ("import ssl, sys, urllib.request\n"
                    f"stores = {stores!r}\n"
                    "ssl.enum_certificates = lambda store: list(stores[store])\n"
                    f"{hook}\n"
                    "sys.platform = 'win32'\n"
                    "try:\n"
                    f"    print(urllib.request.urlopen('https://localhost:{srv.server_port}/', timeout=10).read().decode())\n"
                    "except Exception as e:\n"
                    "    print(e)\n")
            return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env={**os.environ, "NO_PROXY": "*", "no_proxy": "*"},
                                  capture_output=True, text=True, timeout=60).stdout.strip()
        self.assertIn("certificate has expired", fetch(""))   # the stdlib alone: the chain goes through the store's copy
        self.assertEqual(fetch("from core import net"), "ok")


if __name__ == "__main__":
    unittest.main()
