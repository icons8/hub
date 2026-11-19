"""Who the hub pages ("Files" among them) let in, and where they listen. Shared by modules with HTTP.

- From the Mac (address 127.0.0.1), as before: only a localhost request (protection from DNS rebinding).
- From the tailnet (the page also listens on the Mac's tailnet IP, which the tailnet module writes to tailnet.json): the Host from tailnet.json
  and the login of whoever came, by `tailscale whois <address>`: only the Mac's owner, since the tailnet has others too.
Why not `tailscale serve`: it serves only by the *.ts.net name, and the owner's phone with private DNS does not find that name;
links are by IP.
"""
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse

from core import httpd


class Server(httpd.Server):
    """Hub pages server: a browser closing the connection (tab, reload) is not an error, we do not log it
    (30.09: 107 ConnectionResetError traces a day in hub-up.log, real errors were not visible behind them)."""

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, ConnectionAbortedError,
                                          TimeoutError)):   # the client is silent for conn_timeout (core/httpd.py)
            return
        super().handle_error(request, client_address)

LOCAL = ("localhost", "127.0.0.1", "::1")
PEERS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
TAGGED = "tagged-devices"   # the user Tailscale gives to tagged nodes: never an owner
WHOIS_TTL = 300   # an answer of whois (this login, nobody's node) holds this long
WHOIS_RETRY = 5   # no answer (timeout under load, error of the CLI) is asked again this soon: not an hour of 403 to the
                  # owner, not a run of the CLI for every request of a page either
WHOIS_TIMEOUT = 10   # a run of the CLI is given this long
_whois = {}  # address -> (login, until when it holds)
_asking = {}  # address -> threading.Event of the run of the CLI going on for it
_whois_lock = threading.Lock()


# a working tailnet.json is rewritten every tick; while tailscale hangs the tailnet part keeps it up to STALE_AFTER old
# and looks again a minute later, up to three calls of TIMEOUT each (modules/tailnet/run.py; tests.test_tailnet holds
# the sum under this): an older working record means the part no longer runs
FRESH = 480


def status(state_dir):
    """tailnet.json; a working record the tailnet part stopped rewriting (crashed, its lock held) reads as not working,
    with why: everyone who reads it — the page's and Files' admission, the board forwards, the Modules card, `hub
    status`, the links — would otherwise trust its last addresses for good, and a switch turned off meanwhile would
    change nothing."""
    st = record(state_dir)
    if isinstance(st, dict) and st.get("ok") and not fresh(st):
        from core import i18n
        return {"ok": False, "stale": True, "ts": st.get("ts"), "why": i18n.tr(i18n.owner(), "cli.tailnet.stale")}
    return st


def record(state_dir):
    """tailnet.json as the tailnet part wrote it ({} — none or unreadable): the part itself judges its own record."""
    try:
        with open(os.path.join(state_dir, "tailnet.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def fresh(st, limit=FRESH, skew=60):
    """Is a record written less than `limit` seconds ago — by default one the running tailnet part keeps (a clock set
    back by up to `skew` is tolerated; the part then rewrites it within seconds). No time, or a time of another shape
    — not."""
    ts = st.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return False
    try:
        age = time.time() - float(ts)
    except OverflowError:   # an integer too big for a float
        return False
    return math.isfinite(age) and -skew <= age < limit


def status_line(state_dir):
    """The `hub status` line while the tailnet does not work — off (until tailnet.enabled: true) or failing — with why
    and how to turn it on; None while it works or before the tailnet module ever ran."""
    st = status(state_dir)
    if not isinstance(st, dict) or not st or st.get("ok"):
        return None
    from core import i18n
    why = st.get("why")   # in the owner's language of when it was written; "off" — in this process's language
    why = why if isinstance(why, str) and why and not st.get("off") else i18n.t("cli.tailnet.off")
    return i18n.t("cli.status.tailnet", why=why)


def addresses(st):
    """Where a client can reach the hub page, the most direct first: [{kind: ip | name, url}]. It keeps the whole list, not one
    address: the tailnet IP changes when Tailscale is reinstalled on the Mac, the name does not. st is tailnet.json; with the
    tailnet off the list is empty."""
    found = []
    if st.get("ok"):
        for kind, key in (("ip", "urls"), ("name", "urls_dns")):
            urls = st.get(key)   # tailnet.json may be of another shape (edited by hand): no address, not a 500 of /api/about
            url = urls.get("web") if isinstance(urls, dict) else None
            if isinstance(url, str) and url:
                found.append({"kind": kind, "url": url})
    return found


def tailscale_whois(state_dir, peer):
    """The login behind a tailnet address, or None (nobody's, or not known now: never let in on a guess). One run of the
    CLI per address at a time: the requests of a page that come while it runs wait for its answer instead of each
    starting their own (under load a whois takes up to 10 s, and every request of the page would add a process)."""
    waited = False
    while True:
        with _whois_lock:
            hit = _whois.get(peer)
            if hit and time.time() < hit[1]:
                return hit[0]
            asking = _asking.get(peer)
            if asking is None:
                asking = _asking[peer] = threading.Event()
                break
        if waited:
            return None   # the run in flight outlived its timeout (a child holding its output): not known, not let in
        asking.wait(WHOIS_TIMEOUT + 5)   # then the answer is in _whois
        waited = True
    try:
        login, ttl = None, WHOIS_RETRY
        try:
            cli = status(state_dir).get("cli") or "tailscale"
            out = subprocess.run([cli, "whois", "--json", peer], capture_output=True, text=True, timeout=WHOIS_TIMEOUT,
                                 env={**os.environ, "SHLVL": os.environ.get("SHLVL") or "1"})  # see run.cli_env
            if out.returncode == 0:
                login, ttl = whois_login(json.loads(out.stdout)), WHOIS_TTL
        except (OSError, ValueError, subprocess.TimeoutExpired, AttributeError):
            pass
        with _whois_lock:
            _whois[peer] = (login, time.time() + ttl)   # counted from the end of the call: a timeout itself takes 10 s
        return login
    finally:
        with _whois_lock:
            _asking.pop(peer, None)
        asking.set()


def whois_login(data):
    """The person behind a tailnet address by `tailscale whois --json`. A tagged node (server, CI) is nobody's, whoever set the
    tag: its user is `tagged-devices`, shared by all tagged nodes in the tailnet."""
    if (data.get("Node") or {}).get("Tags"):
        return None
    login = (data.get("UserProfile") or {}).get("LoginName")
    return None if login == TAGGED else login


def owners(st):
    """Logins let in from the tailnet (tailnet.json of the tailnet module; `owner` is how earlier versions wrote it)."""
    listed = st.get("owners") if "owners" in st else [st.get("owner")]
    return {x for x in listed or () if isinstance(x, str) and x and x != TAGGED}


def owner_peer(state_dir, peer, whois=None):
    """Is the peer's tailnet address a device of one of the owners?"""
    allowed_logins = owners(status(state_dir))
    return bool(allowed_logins) and (whois or tailscale_whois)(state_dir, peer) in allowed_logins


# Headers a proxy or tunnel adds to what it passes on: cloudflared (Cf-*, X-Forwarded-For, Cdn-Loop), tailscale serve and
# funnel (X-Forwarded-*, Tailscale-*), ngrok, Caddy, Traefik, nginx with proxy_set_header. No browser and no program of
# the hub sends them, so such a request came from elsewhere even with peer 127.0.0.1 and Host localhost (a tunnel that
# rewrites Host would otherwise act as the person at the keyboard). nginx by default adds none: see SECURITY.md.
PROXY_HEADERS = ("forwarded", "via", "x-real-ip", "true-client-ip", "cdn-loop")
PROXY_PREFIXES = ("x-forwarded-", "cf-", "tailscale-")


def _name(host):
    return (host or "").strip().rsplit(":", 1)[0].strip("[]")


def proxied(headers):
    """Why a request to a local Host did not come from this machine: "proxy" (a header from PROXY_HEADERS/PREFIXES, in
    any case), "origin" (a browser page at another address: its Origin or Referer names a host that is not local, so a
    proxy rewrote the Host), otherwise None. A request a browser marks as cross-site or as typed by the user is not
    judged by its Origin: a link from a chat to the page is allowed (cross_site), and the page's own requests behind a
    proxy are same-origin. headers: the request's headers (http.client.HTTPMessage or a dict)."""
    names = [str(k).lower() for k in headers.keys()]
    if any(n in PROXY_HEADERS or n.startswith(PROXY_PREFIXES) for n in names):
        return "proxy"
    if headers.get("Sec-Fetch-Site") in ("cross-site", "none"):
        return None
    for key in ("Origin", "Referer"):
        value = headers.get(key)
        if not value or value.strip() == "null":   # null: a sandboxed frame or a redirect — no address to compare
            continue
        try:
            url = urllib.parse.urlsplit(value.strip())
            name = url.hostname
        except ValueError:
            return "origin"
        # only a web page can stand behind a proxy: an extension, a webview or an app scheme is not judged here
        if url.scheme in ("http", "https", "") and name not in LOCAL:
            return "origin"
    return None


def via_proxy(peer, headers):
    """A request from 127.0.0.1 that a proxy or tunnel passed on: the refusal says so, not "localhost only"."""
    return peer in PEERS and bool(proxied(headers))


def local(host, peer, headers=None):
    """A request from the Mac itself: address 127.0.0.1 and Host localhost (not a foreign site's page via DNS rebinding),
    and not passed on by a proxy or tunnel (proxied). Without headers — the call of a page server of an earlier release
    still in memory during an update: it keeps working, but the proxy check comes only after its restart."""
    return peer in PEERS and _name(host) in LOCAL and not (headers is not None and proxied(headers))


PROXY_PROGRAMS = ("cloudflared", "nginx", "caddy", "tailscaled", "ssh", "ngrok")
PROBE = "/api/proxy-probe"   # no such path: a server on the old rule answers 404, the new one refuses first


def refuses_proxied(port, timeout=5):
    """Does the server on 127.0.0.1:port already refuse a proxied request (hub doctor)? True — 403; False — it let it
    in (a process started before the update: the old rule until a restart); None — nothing answers there."""
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        c.request("GET", PROBE, headers={"Host": f"127.0.0.1:{port}", "X-Forwarded-For": "203.0.113.1"})
        r = c.getresponse()
        r.read()
        return r.status == 403
    except (OSError, http.client.HTTPException):
        return None
    finally:
        c.close()


def proxy_programs_in(text):
    """Names from PROXY_PROGRAMS in the output of `ss -tnp` (users:(("name",…))) or `lsof -F c` (lines c<name>)."""
    found = re.findall(r'\("([^"]+)",pid=', text) + [ln[1:] for ln in text.splitlines() if ln.startswith("c")]
    return sorted({n for n in found if n in PROXY_PROGRAMS})


def proxy_programs(port):
    """Programs from PROXY_PROGRAMS with a connection to the page port right now: a hint for hub doctor, not a lock (a
    proxy may hold no connection at this moment, and a program of this user may be named anything). ss where it
    exists (Linux), otherwise lsof (macOS); neither (Windows) — no hint."""
    import shutil
    if shutil.which("ss"):
        cmd = ["ss", "-Htnp", "state", "established", f"( dport = :{port} )"]
    elif shutil.which("lsof"):
        cmd = ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:ESTABLISHED", "-Fc"]
    else:
        return []
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return proxy_programs_in(out.stdout or "")


def allowed(state_dir, host, peer, whois=None, headers=None):
    """Let in: this machine itself (local), the owner from the tailnet, or a person a remote access provider names
    (core/remote.py: Cloudflare Access, off by default)."""
    from core import remote
    return remote.admit(state_dir, host, peer, headers=headers, whois=whois) is not None


HOST_PORT = re.compile(r"[A-Za-z0-9.\-]+:\d+")   # in a CSP header only a name or an IPv4 with a port
_origins = {}   # (state folder, port) -> (write time and size of tailnet.json, addresses, until when they hold)


def page_origins(state_dir, port):
    """Where the hub page on port `port` is opened from: localhost and, when the tailnet is connected, its tailnet addresses (the same
    Hosts it is let in by in allowed), for the frame-ancestors of "Files" that it embeds in its panel.
    No [::1]: an IPv6 literal is not a host-source in CSP, the browser drops the entry; a page on [::1] will not embed "Files".
    Asked on every "Files" response, so tailnet.json is re-read only when it changed."""
    try:
        f = os.stat(os.path.join(state_dir, "tailnet.json"))
        seen = (f.st_mtime_ns, f.st_size)
    except OSError:
        seen = None
    hit = _origins.get((state_dir, port))
    if hit and hit[0] == seen and time.time() < hit[2]:
        return list(hit[1])
    hosts = [f"{h}:{port}" for h in ("localhost", "127.0.0.1")]
    st = status(state_dir) if seen else {}
    until = float("inf")
    if st.get("ok"):
        hosts += [h for h in st.get("hosts") or [] if isinstance(h, str) and HOST_PORT.fullmatch(h)
                  and h.rsplit(":", 1)[1] == str(port)]
        until = float(st["ts"]) + FRESH   # a record nobody rewrites goes stale by the clock, not by the file
    _origins[(state_dir, port)] = (seen, ["http://" + h for h in hosts], until)
    return list(_origins[(state_dir, port)][1])


def serve_tailnet(make_server, port, state_dir, stop=None):
    """Also listen on the Mac's tailnet IP once the tailnet module reports it (Tailscale may have come up later than the page)."""
    def loop():
        bound = None
        while not (stop and stop.is_set()):
            ip = status(state_dir).get("ip") if status(state_dir).get("ok") else None
            if ip and ip != bound:
                try:
                    server = make_server(ip, port)
                    threading.Thread(target=server.serve_forever, daemon=True).start()
                    bound = ip
                    print(f"also on the tailnet: http://{ip}:{port}/", flush=True)
                except OSError:
                    pass
            time.sleep(30)
    threading.Thread(target=loop, daemon=True).start()


def cross_site(handler):
    """A browser request from a foreign site, except following a link: `<img>`, fetch, a form from another site are refused, even
    without reading the response (the request itself does something: looks up a path, runs git). Following a link (GET with
    `Sec-Fetch-Mode: navigate`) is allowed: links from chats and trackers lead to the page and to "Files". Clients without
    `Sec-Fetch-*` headers (an app, curl) are not a browser with a foreign page, this does not concern them."""
    if handler.headers.get("Sec-Fetch-Site") != "cross-site":
        return False
    return not (handler.command == "GET" and handler.headers.get("Sec-Fetch-Mode") == "navigate")
