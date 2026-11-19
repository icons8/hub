"""One HTTP(S) request through the owner's proxy (core/net.connection): core/fetch, Cloudflare's API, the stats report,
the conduit gateway and the bus relay go out the same way. urllib cannot pass a proxy that itself listens on TLS
(HTTPS_PROXY=https://…): it sends a bare CONNECT and reads the proxy's TLS alert as a status line (2026-11-06).
Plain http goes through the machine's http proxy too, as with urllib (core/net.connection sends it directly: a local
server). No redirects here — a caller that follows them (core/fetch, conduit) does it itself; no headers of its own.
A file of its own: a process of the previous release keeps its core/net in memory and imports this file fresh from disk
(tests/test_old_page_server), so it uses only what core/net has had since v1.7.0 (proxy_for, proxy_auth, connection);
a fix inside those reaches such a process at its restart."""
import http.client
import urllib.parse

from core import net


def send(method, url, body=None, headers=None, timeout=30):
    """(connection, response): the caller reads the answer and closes the connection. OSError on any network or
    HTTP-protocol failure (a refusal, a timeout, TLS, a proxy's refusal of the CONNECT, an answer that is not HTTP) —
    the connection is closed then."""
    u = urllib.parse.urlsplit(url)
    try:
        port = u.port
    except ValueError:
        port = -1
    if u.scheme not in ("http", "https") or not u.hostname or port == -1:
        raise OSError(f"not an http(s) address: {url}")
    headers = dict(headers or {})
    target = (u.path or "/") + (f"?{u.query}" if u.query else "")
    port = port or (443 if u.scheme == "https" else 80)
    try:
        # NO_PROXY may name the host with its port, as urllib matched it
        proxy = net.proxy_for("http", u.netloc.rpartition("@")[2]) if u.scheme == "http" else None
        if proxy:   # plain http through the machine's proxy, as urllib did: the whole address in the request line
            tls = proxy.scheme == "https"
            conn = (http.client.HTTPSConnection if tls else http.client.HTTPConnection)(
                proxy.hostname, proxy.port or (443 if tls else 80), timeout=timeout)
            target = urllib.parse.urlunsplit((u.scheme, u.netloc.rpartition("@")[2], u.path or "/", u.query, ""))
            auth = net.proxy_auth(proxy)
            if auth:
                headers["Proxy-Authorization"] = auth
        else:
            conn = net.connection(u.scheme, u.hostname, port, timeout)
    except (ValueError, TypeError) as e:
        # a proxy setting that is not an address; TypeError — the same setting read by the core/net of a process that
        # has not restarted since the update (a proxy with no host reaches http.client)
        raise OSError(f"the proxy setting: {type(e).__name__}: {e}") from e
    try:
        conn.request(method, target, body, headers)
        return conn, conn.getresponse()
    except OSError:
        conn.close()
        raise
    except http.client.HTTPException as e:
        conn.close()
        # the name only: the text of BadStatusLine is whatever came instead of a status line (a TLS alert's bytes)
        raise OSError(type(e).__name__) from e
    except BaseException:
        conn.close()
        raise


def request(method, url, body=None, headers=None, timeout=30, limit=None):
    """(status, headers, the answer — up to `limit` bytes, all of it without one); OSError as in send()."""
    conn, resp = send(method, url, body, headers, timeout)
    try:
        return resp.status, resp.headers, resp.read() if limit is None else resp.read(limit)
    except http.client.HTTPException as e:   # IncompleteRead: the answer broke off
        raise OSError(type(e).__name__) from e
    finally:
        conn.close()
