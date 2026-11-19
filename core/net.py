"""Network through the owner's proxy — shared by the Telegram bot and the account gateway (stdlib only).

urllib supports a proxy only via http://: a bare CONNECT, and TLS only up to the target. A proxy that itself listens
on TLS (HTTPS_PROXY=https://…) does not see such a CONNECT and drops the connection. curl and browsers wrap CONNECT in
TLS up to the proxy, and inside it — one more TLS up to the target; here the same is done by hand (owner decision 2026-10-03).

The hub reaches itself (127.0.0.1, localhost, ::1) directly under any machine proxy, even if they are not in its
NO_PROXY: on import the module extends `urllib.request.proxy_bypass` (used by urllib and the Telegram bot), and for
curl there is `no_proxy()`. The process environment is not changed — sessions inherit it. Core code using urllib or
http.client's TLS imports this module (guarded by tests/test_net.py).

On Windows the module also leaves out-of-date certificates out of the system stores ssl trusts (`_store_in_date`)."""
import base64
import datetime
import io
import os
import re
import socket
import ssl
import time
import urllib.parse
import urllib.request

LOCAL = ("127.0.0.1", "localhost", "::1")


def is_local(host):
    """The address is the machine itself: localhost, 127.0.0.0/8, ::1; host — as in urllib (`host[:port]`, IPv6 in brackets)."""
    host = (host or "").strip()
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    host = host.lower().rstrip(".")
    if host in ("localhost", "::1"):
        return True
    parts = host.split(".")
    return len(parts) == 4 and parts[0] == "127" and all(p.isdigit() and int(p) < 256 for p in parts)


def no_proxy(env=None):
    """The machine's NO_PROXY (lowercase wins, as in urllib) plus our own addresses — for curl `--noproxy`, which
    replaces the list from the environment rather than extending it."""
    env = os.environ if env is None else env
    own = env["no_proxy"] if "no_proxy" in env else env.get("NO_PROXY", "")   # an empty lowercase one cancels the uppercase
    items = [x.strip() for x in own.split(",") if x.strip()]
    if "*" in items:
        return "*"
    return ",".join(items + [h for h in LOCAL if h not in items])


def _direct_local():
    machine = urllib.request.proxy_bypass
    if getattr(machine, "hub_local", False):
        return
    def proxy_bypass(host):
        return is_local(host) or machine(host)
    proxy_bypass.hub_local = True
    urllib.request.proxy_bypass = proxy_bypass


_direct_local()


def _der(der, i, tag):
    """A DER element at i with the expected tag: the start and the end of its value."""
    if der[i] != tag:
        raise ValueError(f"tag {der[i]:#x} at {i}, expected {tag:#x}")
    n, i = der[i + 1], i + 2
    if n & 0x80:
        k = n & 0x7F
        n, i = int.from_bytes(der[i:i + k], "big"), i + k
    if i + n > len(der):
        raise ValueError("cut short")
    return i, i + n


def cert_dates(der):
    """notBefore and notAfter of an X.509 certificate in DER, in seconds since the epoch (RFC 5280 4.1: Certificate →
    tbsCertificate → [0] version (absent in v1), serialNumber, signature, issuer, validity); ValueError or IndexError
    if it is not one."""
    i, _ = _der(der, 0, 0x30)
    i, _ = _der(der, i, 0x30)
    if der[i] == 0xA0:
        i = _der(der, i, 0xA0)[1]
    for tag in (0x02, 0x30, 0x30):
        i = _der(der, i, tag)[1]
    i, _ = _der(der, i, 0x30)
    out = []
    for _ in range(2):
        tag = der[i]   # UTCTime YYMMDDHHMMSSZ (YY ≥ 50 — 19YY) or GeneralizedTime YYYYMMDDHHMMSSZ
        if tag not in (0x17, 0x18):
            raise ValueError(f"time tag {tag:#x}")
        start, i = _der(der, i, tag)
        text = der[start:i].decode("ascii")
        if tag == 0x17:
            text = ("19" if text[:2] >= "50" else "20") + text
        if not re.fullmatch(r"\d{14}Z", text):
            raise ValueError(f"time {text!r}")
        when = datetime.datetime(int(text[:4]), int(text[4:6]), int(text[6:8]), int(text[8:10]), int(text[10:12]),
                                 int(text[12:14]), tzinfo=datetime.timezone.utc)   # no such date — ValueError
        out.append(int(when.timestamp()))
    return tuple(out)


def in_date(der, now):
    """The certificate is within its dates at `now`; an unreadable one counts as within (ssl decides about it as before)."""
    try:
        start, end = cert_dates(der)
    except (ValueError, IndexError):
        return True
    return start <= now <= end


def _store_in_date():
    """Windows: `ssl.load_default_certs` trusts the system stores ROOT and "Intermediate CAs", taken through
    `ssl.enum_certificates`. An old copy of a CA there that is out of its dates (an expired cross-signed root) makes
    OpenSSL build the chain through it when it is the only copy in the stores — "certificate has expired" while the
    server sends a valid chain. Such copies are left out: OpenSSL checks the dates of every certificate in a chain, so
    an out-of-date one cannot stand in any — nothing new is trusted, the names and chains are checked as before. The
    rest of the stdlib's choice (encoding, purpose, broken certificates) stays its own. Elsewhere there is no
    `ssl.enum_certificates`, nothing changes."""
    enum = getattr(ssl, "enum_certificates", None)
    if enum is None or hasattr(enum, "hub_inner"):
        return
    def enum_certificates(store_name):
        now = time.time()
        return [c for c in enum(store_name) if c[1] != "x509_asn" or in_date(c[0], now)]
    enum_certificates.hub_inner = enum
    ssl.enum_certificates = enum_certificates


_store_in_date()


def proxy_auth(proxy):
    """Proxy-Authorization from the proxy's urlsplit (login and password in the URL — with %-codes, as in curl) or None."""
    if not proxy.username:
        return None
    raw = f"{urllib.parse.unquote(proxy.username)}:{urllib.parse.unquote(proxy.password or '')}".encode()
    return "Basic " + base64.b64encode(raw).decode()


class InnerTLS:
    """A second TLS layer over an already-TLS socket (a tunnel to the proxy). A plain `ssl.wrap_socket(tunnel)` does
    not work here: inside it does `tunnel.detach()` — a bare file descriptor bypassing the proxy layer's encryption,
    the second handshake goes in clear text into the already encrypted channel (the proxy sees garbage — "unexpected
    message"). Via `ssl.MemoryBIO`: encryption is in memory, and the encrypted bytes are moved by hand through
    `tunnel.sendall/recv`."""

    def __init__(self, tunnel, host, ctx=None):
        self._tunnel = tunnel
        self._io_refs, self._closed = 0, False   # like socket.socket: the tunnel lives while makefile() files do
        self._in, self._out = ssl.MemoryBIO(), ssl.MemoryBIO()
        self._obj = (ctx or ssl.create_default_context()).wrap_bio(self._in, self._out, server_hostname=host)
        self._handshake()

    def _pump_out(self):
        data = self._out.read()
        if data:
            self._tunnel.sendall(data)

    def _pump_in(self):
        chunk = self._tunnel.recv(65536)
        if chunk:
            self._in.write(chunk)
        else:
            self._in.write_eof()

    def _handshake(self):
        while True:
            try:
                self._obj.do_handshake()
                self._pump_out()
                return
            except ssl.SSLWantReadError:
                self._pump_out()
                self._pump_in()

    def send(self, data):
        self._obj.write(data)
        self._pump_out()
        return len(data)
    sendall = send

    def recv(self, n):
        while True:
            try:
                return self._obj.read(n)
            except ssl.SSLWantReadError:
                self._pump_in()
            except (ssl.SSLZeroReturnError, ssl.SSLEOFError):   # SSLEOFError: closed without a TLS goodbye — the end,
                return b""                                      # as a plain ssl socket reads it (suppress_ragged_eofs)

    def recv_into(self, buf, nbytes=0):
        data = self.recv(nbytes or len(buf))
        buf[:len(data)] = data
        return len(data)

    def makefile(self, mode="rb", *a, **kw):  # http.client reads the response from here — as with a plain socket.socket
        self._io_refs += 1
        return io.BufferedReader(socket.SocketIO(self, mode))

    def settimeout(self, t):
        self._tunnel.settimeout(t)

    def gettimeout(self):
        return self._tunnel.gettimeout()

    def shutdown(self, how):
        self._tunnel.shutdown(how)

    def close(self):
        # http.client closes the connection on "Connection: close" before it reads the body: as with a real
        # socket.socket, the tunnel is really closed only when the last makefile() file is closed
        self._closed = True
        if self._io_refs <= 0:
            self._tunnel.close()

    def _decref_socketios(self):   # socket.SocketIO.close() calls this when the file is closed
        if self._io_refs > 0:
            self._io_refs -= 1
        if self._closed:
            self.close()

    def fileno(self):
        return self._tunnel.fileno()


def connect(proxy, host, port, timeout, ctx=None):
    """A socket to host:port through the proxy's CONNECT (urlsplit): an https:// proxy — TLS up to it, the CONNECT
    response is read byte by byte (TLS to the target will follow in the same socket — do not bite off extra). TLS to
    the target is up to the caller (wrap_socket, or InnerTLS if the proxy is already TLS)."""
    raw = socket.create_connection((proxy.hostname, proxy.port or (443 if proxy.scheme == "https" else 80)), timeout)
    try:
        sock = (ctx or ssl.create_default_context()).wrap_socket(raw, server_hostname=proxy.hostname) \
            if proxy.scheme == "https" else raw
    except BaseException:
        raw.close()   # TLS to the proxy did not work out — nobody needs the raw socket
        raise
    target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"   # an IPv6 address — in brackets
    req = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
    auth = proxy_auth(proxy)
    if auth:
        req += f"Proxy-Authorization: {auth}\r\n"
    try:
        sock.sendall((req + "\r\n").encode())
        resp = b""
        while not resp.endswith(b"\r\n\r\n"):
            chunk = sock.recv(1)
            if not chunk:
                raise OSError("the proxy broke off CONNECT without an answer")
            resp += chunk
        status = resp.split(b"\r\n", 1)[0].decode("ascii", "replace")
        if " 200" not in status:
            raise OSError(f"the proxy refused CONNECT {target}: {status.strip()}")
    except BaseException:
        sock.close()   # refusal, drop or timeout — there will be no tunnel, do not keep the socket
        raise
    sock.settimeout(timeout)
    return sock


def tls_via(proxy, host, port, timeout, ctx=None):
    """TLS to host:port through the proxy (like curl --proxy …): an https:// proxy — TLS in TLS, http:// — TLS in CONNECT."""
    ctx = ctx or ssl.create_default_context()
    sock = connect(proxy, host, port, timeout, ctx)
    try:
        return InnerTLS(sock, host, ctx) if proxy.scheme == "https" else ctx.wrap_socket(sock, server_hostname=host)
    except BaseException:
        sock.close()   # TLS to the target did not work out — do not keep the tunnel to the proxy
        raise


def proxy_for(scheme, host):
    """The proxy for a host, as in urllib: from the environment, except NO_PROXY hosts and the machine itself (urlsplit or
    None). `host:port` without a scheme is an http:// proxy, as urllib takes it; a setting with no host — ValueError."""
    url = urllib.request.getproxies().get(scheme)
    if not url or urllib.request.proxy_bypass(host):
        return None
    proxy = urllib.parse.urlsplit(url if "://" in url else "http://" + url)
    if not proxy.hostname:
        raise ValueError(f"the proxy setting is not an address: {proxy.scheme}://…")
    return proxy


def proxy_from(env, scheme, host):
    """The proxy for a scheme from a given environment (the sessions', not this process's), as urllib takes it: lowercase
    outranks, NO_PROXY bypasses (urlsplit or None). The accounts gateway and core/netprobe."""
    proxies = {}
    for k, v in sorted(env.items(), key=lambda kv: kv[0].islower()):   # lowercase last, it outranks
        if k.lower().endswith("_proxy") and v:
            proxies[k[:-6].lower()] = v
    url = proxies.get(scheme)
    if not url or urllib.request.proxy_bypass_environment(host, proxies):
        return None
    return urllib.parse.urlsplit(url if "://" in url else "http://" + url)


def connection(scheme, host, port, timeout):
    """An http.client connection to host:port through the owner's proxy, as the Telegram bot makes it: an https:// proxy —
    our own TLS-in-TLS tunnel (tls_via), an http:// one — http.client's CONNECT; plain http (a local server) — directly
    (core/nethttp sends plain http to an outside host through the machine's http proxy). NO_PROXY may name the host
    with its port, as urllib matched it."""
    import http.client
    if scheme != "https":
        return http.client.HTTPConnection(host, port, timeout=timeout)
    proxy = proxy_for("https", f"[{host}]:{port}" if ":" in host else f"{host}:{port}")
    if not proxy:
        return http.client.HTTPSConnection(host, port, timeout=timeout)
    if proxy.scheme == "https":
        conn = http.client.HTTPSConnection(host, port, timeout=timeout)
        conn.sock = tls_via(proxy, host, port, timeout)   # connect() is not needed: the socket is ready
        return conn
    auth = proxy_auth(proxy)
    conn = http.client.HTTPSConnection(proxy.hostname, proxy.port or 80, timeout=timeout)
    conn.set_tunnel(host, port, headers={"Proxy-Authorization": auth} if auth else None)
    return conn
