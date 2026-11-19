"""Whether the Claude API answers again, for a turn the network cut (reducer: net_cut): hubd fires the continuation timer
only then. Honest behind a proxy and the hub's accounts gateway: an open TCP connection to a proxy, or to a gateway on
this machine, proves nothing, so the probe is one HTTPS request to the API itself, through the sessions' proxy and with
their trust (NODE_EXTRA_CA_CERTS). Any answer of the API below 500 (HEAD / gives 404) means it is reachable; a proxy's own
failure (502, 503, 504), a refused CONNECT, a TLS error or a timeout mean it is not. No key, no body. The stdlib only."""
import http.client
import ssl
import threading
import time
import urllib.parse

from core import net

UPSTREAM = "https://api.anthropic.com"
EVERY = 15      # seconds between probes while a cut turn waits (DNS without a network may hang for tens of seconds)
TIMEOUT = 10


def target(env):
    """(scheme, host, port) the probe asks: the sessions' own ANTHROPIC_BASE_URL, else the API. An address on this
    machine (the hub's accounts gateway, a local endpoint) answers without the network — then the API itself."""
    u = urllib.parse.urlsplit(env.get("ANTHROPIC_BASE_URL") or UPSTREAM)
    if u.scheme not in ("http", "https") or not u.hostname or net.is_local(u.hostname):
        u = urllib.parse.urlsplit(UPSTREAM)
    return u.scheme, u.hostname, u.port or (443 if u.scheme == "https" else 80)


def trust(env):
    """The system's certificates plus NODE_EXTRA_CA_CERTS, as claude has them (a corporate proxy that replaces TLS)."""
    ctx = ssl.create_default_context()
    extra = env.get("NODE_EXTRA_CA_CERTS")
    if extra:
        try:
            ctx.load_verify_locations(cafile=extra)
        except (OSError, ssl.SSLError):
            pass
    return ctx


def ask(env, method, path, timeout=TIMEOUT, body=None):
    """One request to the API through the sessions' proxy (env): its status, or None when nothing answered (a proxy's refused CONNECT, a TLS error, a timeout, a setting that is not an address)."""
    scheme, host, port = target(env)
    try:
        proxy = net.proxy_from(env, scheme, host)
        ctx = trust(env) if scheme == "https" else None
        conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx) if scheme == "https" else \
            http.client.HTTPConnection(host, port, timeout=timeout)
    except ValueError:   # a proxy setting that is not an address
        return None
    try:
        if proxy:
            conn.sock = net.tls_via(proxy, host, port, timeout, ctx) if scheme == "https" else \
                net.connect(proxy, host, port, timeout)
        headers = {"User-Agent": "agents-hub-netprobe"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=headers)
        return conn.getresponse().status
    except (OSError, http.client.HTTPException, ValueError):
        return None
    finally:
        conn.close()


def reachable(env, timeout=TIMEOUT):
    """One HEAD / to the API through the sessions' proxy (env): True if the API answered."""
    status = ask(env, "HEAD", "/", timeout)
    return status is not None and status < 500


def allowed(env, timeout=TIMEOUT):
    """For a turn the API refused this network for (403 "Request not allowed": the VPN is off): True once the network is let
    in. The request that is cut is the one made with no key to the messages path (no model is called: the key is looked
    at after the network): any 403 to it is the network still refused (with the network let in, no key is a 401); any
    other answer below 500 means the network is let in."""
    status = ask(env, "POST", "/v1/messages", timeout, body=b"{}")
    return status is not None and status < 500 and status != 403


class Prober:
    """The last probe's answer; a new probe runs in the background no more often than EVERY: the core's loop does not
    wait for the network. env — a function giving the sessions' environment for each probe."""

    def __init__(self, env, probe=reachable, clock=time.monotonic):
        self.env, self.probe, self.clock = env, probe, clock
        self.ok, self.at, self.next, self.thread = False, None, 0.0, None

    def up(self):
        """Whether the API answered at the last probe, if it is fresh (an answer an hour old says nothing of now)."""
        if not (self.thread and self.thread.is_alive()) and self.clock() >= self.next:
            self.next = self.clock() + EVERY

            def run():
                try:
                    ok = bool(self.probe(self.env()))
                except Exception:   # a broken environment is "not reachable", not a dead core thread
                    ok = False
                self.ok, self.at = ok, self.clock()
            self.thread = threading.Thread(target=run, name="netprobe", daemon=True)
            self.thread.start()
        return self.ok and self.at is not None and self.clock() - self.at <= 2 * EVERY
