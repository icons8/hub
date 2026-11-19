"""The relay transport of the hub bus (spec hubbus §5.9, §5.11): how a hub talks to a relay server (relay/server.py) —
the invitation, the transport key, signed requests and signed answers. Shared by the owner's commands
(`hub bus relay …`, core/buscli.py) and the transport process (modules/bus_relay/run.py).

No relay address is in the code: a hub learns its relay only from an invitation the owner pastes (and the relays of
its contacts only from their cards). The invitation names the relay's address and the relay's key; the key is pinned —
every answer to a signed request must carry the relay's signature, so another server at the same address is not taken
for the relay.

Two keys, two jobs: the hub's own key (the bus identity, core/bus.py, `<settings>/bus/`) signs once at join a line
«this transport key speaks for me on this relay» and signs the invitations the hub makes; the transport key
(`<settings>/bus-relay/key`) signs every request. The transport process reads only the transport key, never the hub's:
it cannot sign an envelope of the bus, and the bus keys' folder can be closed to it. Both under the namespace
agents-hub-relay-v1, not the bus's agents-hub-bus-v1: no signature of one protocol is good in the other.
"""
import base64
import hashlib
import ipaddress
import json
import os
import platforms
import re
import secrets
import time
import urllib.parse

from core import bus, net, nethttp  # noqa: F401 — net: the hub reaches a relay on this machine past any proxy

NS = "agents-hub-relay-v1"
REQUEST_TAG = "agents-hub-relay-v1 request"
RESPONSE_TAG = "agents-hub-relay-v1 response"
BIND_TAG = "agents-hub-relay-v1 bind"
INVITE_TAG = "agents-hub-relay-v1 invite"
INVITE_PREFIX = "hub-relay-invite-1:"
INVITE_MAX = 4096
INVITE_TTL = 7 * 86400
ANSWER_MAX = 8 * 2 ** 20
WAIT = 25            # seconds of a long poll
ID = re.compile(r"[0-9a-f]{32}")
# answers that come unsigned (the relay's limits before any check, a proxy in front of it): only «try later»
UNSIGNED = {411: "too_big", 413: "too_big", 429: "rate", 500: "error", 502: "unavailable", 503: "unavailable",
            504: "unavailable"}


class Error(Exception):
    """A refusal of the relay or of the way to it: code (the relay's code, or `network`, `not_relay`, `bad_answer`),
    status (HTTP, 0 without an answer)."""

    def __init__(self, code, text="", status=0):
        super().__init__(text or code)
        self.code, self.status = code, status


# --- lines that are signed -------------------------------------------------------------------------------------------

def request_line(relay_key, method, target, ts, nonce, body):
    return "\n".join((REQUEST_TAG, relay_key, method, target, str(ts), nonce,
                      hashlib.sha256(body).hexdigest())).encode("ascii")


def request_digest(fp, ts, nonce, sig, method, target, body):
    """The request as it was sent (its headers, method, path and body): an answer is signed for this one request — a
    request changed on the way (another signature, fingerprint, path) gets an answer the hub does not take."""
    return hashlib.sha256("\n".join((fp or "", str(ts or ""), nonce, sig or "", method, target,
                                     hashlib.sha256(body).hexdigest())).encode("utf-8")).hexdigest()


def response_line(nonce, status, body, request):
    return "\n".join((RESPONSE_TAG, nonce, str(status), hashlib.sha256(body).hexdigest(), request)).encode("ascii")


def bind_line(relay_key, fp, transport_pub):
    return "\n".join((BIND_TAG, relay_key, fp, transport_pub)).encode("ascii")


def invite_line(payload):
    return INVITE_TAG.encode("ascii") + b"\n" + payload


# --- the address and the invitation ----------------------------------------------------------------------------------

def check_url(url):
    """The relay's address as the hub will use it, or Error bad_url: https to any host; plain http only to this
    machine (a stand) — elsewhere the invitation and the metadata would go in the clear."""
    if not isinstance(url, str) or len(url) > 200 or any(c.isspace() for c in url):
        raise Error("bad_url", "the relay's address")
    u = urllib.parse.urlsplit(url)
    try:
        u.port   # a port out of range or not a number: urlsplit takes it, the request would fail later
    except ValueError:
        raise Error("bad_url", "the relay's address: the port") from None
    if u.scheme not in ("https", "http") or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise Error("bad_url", "the relay's address: https, a host, a port and a path at most")
    if u.scheme == "http":
        try:
            local = u.hostname == "localhost" or ipaddress.ip_address(u.hostname).is_loopback
        except ValueError:
            local = False
        if not local:
            raise Error("bad_url", "plain http only to this machine; a relay elsewhere — https")
    return url.rstrip("/")


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64url(text):
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_-]*", text):
        raise Error("bad_invite", "base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def read_invite(text, signed=True):
    """(fields, the exact payload bytes, the signature) of an invitation line; a wrong form — Error bad_invite. Fields:
    v, relay (the address), key (the relay's key, base64), id, by (the hub that made it, or operator), exp. Whether it
    is real, unused and in time only the relay knows. signed=False — the fields only, whatever stands for the signature:
    the journal masks it (core/redact), and the core reads the relay from that copy."""
    if not isinstance(text, str):
        raise Error("bad_invite", "not an invitation")
    text = "".join(text.split())
    if not text.startswith(INVITE_PREFIX) or len(text) > INVITE_MAX:
        raise Error("bad_invite", "not an invitation")
    body, _, sig = text[len(INVITE_PREFIX):].partition(".")
    try:
        payload = _unb64url(body)
        sig = _unb64url(sig).decode("ascii") if signed else bus.SIG_HEAD
        d = bus.strict(payload.decode("utf-8"))
    except (bus.Error, ValueError):
        raise Error("bad_invite", "not an invitation") from None
    if not (isinstance(d, dict) and set(d) == {"v", "relay", "key", "id", "by", "exp"} and type(d["v"]) is int
            and d["v"] == 1 and isinstance(d["relay"], str) and isinstance(d["key"], str)
            and isinstance(d["id"], str) and ID.fullmatch(d["id"]) and isinstance(d["by"], str)
            and (d["by"] == "operator" or bus.FP.fullmatch(d["by"])) and type(d["exp"]) is int
            and sig.startswith(bus.SIG_HEAD)):
        raise Error("bad_invite", "the invitation's fields")
    try:
        bus._unb64(d["key"], 32)
    except bus.Error:
        raise Error("bad_invite", "the relay's key") from None
    d["relay"] = check_url(d["relay"])
    return d, payload, sig


def invite_text(payload, sig):
    return INVITE_PREFIX + _b64url(payload) + "." + _b64url(sig.encode("ascii"))


def make_invite(keys, relay, now, exp=None):
    """A new invitation from this hub, signed by its own key: (text, id, exp). The relay records it (POST /v1/invite).
    exp — the term of a link that carries it (not longer than an invitation's)."""
    data = {"v": 1, "relay": relay["url"], "key": relay["key"], "id": secrets.token_hex(16), "by": keys.fp,
            "exp": min(int(exp), int(now) + INVITE_TTL) if exp else int(now) + INVITE_TTL}
    payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return invite_text(payload, bus.ssh_sign(keys.sign_key, invite_line(payload), NS)), data["id"], data["exp"]


def ref(url, key):
    """How the relay is named in the hub's card: the address and the relay's key after `#`."""
    return f"{url}#{_b64url(base64.b64decode(key))}"


def read_ref(text):
    """{url, key} of a card's relay entry, or None (another kind of address)."""
    url, _, key = str(text).partition("#")
    try:
        raw = _unb64url(key)
        if len(raw) != 32:
            return None
        return {"url": check_url(url), "key": base64.b64encode(raw).decode("ascii")}
    except Error:
        return None


# --- the transport key -----------------------------------------------------------------------------------------------

def key_dir(config_dir=None):
    if config_dir is None:
        from core import catalog
        config_dir = catalog.config_dir()
    return os.path.join(config_dir, "bus-relay")


def transport_key(config_dir=None, make=False):
    """(the private key file, the public key base64) of the transport key; make — create it once (0600, the folder
    0700). None if there is none and make is false."""
    folder = key_dir(config_dir)
    path = os.path.join(folder, "key")
    if not os.path.exists(path) or not os.path.exists(path + ".pub"):
        if not make:
            return None
        platforms.current.makedirs_private(folder)
        for p in (path, path + ".pub"):
            if os.path.exists(p):
                os.unlink(p)   # half a pair (a cut) is made anew: nothing was bound to it
        code, _, err = bus._keygen(["-q", "-t", "ed25519", "-N", "", "-C", "agents-hub-bus-relay", "-f", path], b"",
                                   folder)
        for name in ("data", "err"):
            try:
                os.unlink(os.path.join(folder, name))
            except OSError:
                pass
        if code != 0 or not os.path.exists(path):
            raise Error("keygen", err[:300])
        os.chmod(path, 0o600)
    with open(path + ".pub", encoding="ascii") as f:
        return path, base64.b64encode(bus._ed25519_raw(f.read())).decode("ascii")


# --- the client --------------------------------------------------------------------------------------------------------

class Client:
    """Signed requests to one relay: its address, its pinned key, our fingerprint and the transport key file."""

    def __init__(self, url, key, fp, key_file, timeout=30):
        self.url, self.key, self.fp, self.key_file, self.timeout = check_url(url), key, fp, key_file, timeout
        self.relay_pub = base64.b64decode(key)

    def _http(self, method, target, body, headers, timeout, limit):
        """(status, the answer up to `limit` bytes, X-Relay-Sig) of one request to the relay, through the owner's proxy
        the way the hub goes elsewhere (core/net): urllib cannot pass a proxy that itself listens on TLS
        (HTTPS_PROXY=https://…) — it reads the proxy's TLS alert as a status line (2026-11-06). The relay does not
        redirect, nor does this. No answer — Error network."""
        try:
            status, head, got = nethttp.request(method, self.url + target, body, headers, timeout, limit)
        except (OSError, ValueError) as e:   # refused, timeout, DNS, TLS, a broken answer (a proxy's TLS alert)
            raise Error("network", f"{type(e).__name__}: {e}"[:300]) from None
        return status, got, head.get("X-Relay-Sig")

    def call(self, method, path, payload=None, query="", wait=0):
        target = path + (f"?{query}" if query else "")
        body = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ts, nonce = int(time.time()), secrets.token_hex(16)
        sig = bus.ssh_sign(self.key_file, request_line(self.key, method, target, ts, nonce, body), NS)
        sig64 = base64.b64encode(sig.encode("ascii")).decode("ascii")
        # X-Hub-Bus-V: the protocol of this hub with every signed request (not signed: a relay of the old code checks
        # the old line); a request without it — a hub rolled back — sets the relay's record back to 1
        headers = {"Content-Type": "application/json", "User-Agent": "agents-hub-bus-relay/1", "X-Hub-Fp": self.fp,
                   "X-Hub-Time": str(ts), "X-Hub-Nonce": nonce, "X-Hub-Sig": sig64, "X-Hub-Bus-V": str(bus.BUS_V)}
        mine = request_digest(self.fp, ts, nonce, sig64, method, target, body)
        status, got, rsig = self._http(method, target, body if method == "POST" else None, headers, self.timeout + wait,
                                       ANSWER_MAX + 1)
        if len(got) > ANSWER_MAX:
            raise Error("bad_answer", "the answer is too big", status)
        try:
            sig_text = base64.b64decode(rsig or "", validate=True).decode("ascii")
        except ValueError:
            sig_text = ""
        if not (sig_text and bus.ssh_verify(self.relay_pub, response_line(nonce, status, got, mine), sig_text, NS)):
            if status in UNSIGNED:   # a limit or a proxy in front of the relay: a refusal to try later, never a success
                raise Error(UNSIGNED[status], f"the relay or its proxy answered {status}", status)
            raise Error("not_relay", f"the answer ({status}) is not signed by the relay's key from the invitation",
                        status)
        try:
            data = json.loads(got.decode("utf-8")) if got else {}
        except (UnicodeDecodeError, ValueError):
            raise Error("bad_answer", "not JSON", status) from None
        if not isinstance(data, dict):
            raise Error("bad_answer", "not an object", status)
        if status != 200:
            raise Error(str(data.get("code") or "refused"), str(data.get("error") or data.get("code") or status), status)
        return data

    def join(self, keys, invite, label):
        """Registration with an invitation (None for a hub already here: binds the transport key anew)."""
        _, tpub = transport_key_of(self.key_file)
        bind = bus.ssh_sign(keys.sign_key, bind_line(self.key, keys.fp, tpub), NS)
        return self.call("POST", "/v1/join", {
            "invite": invite, "sign_pub": bus._b64(keys.sign_pub), "enc_pub": bus._b64(keys.enc_pub),
            "transport_pub": tpub, "bind": base64.b64encode(bind.encode("ascii")).decode("ascii"),
            "label": str(label or "")[:100]})

    def join_prepared(self, body):
        """Registration with a body the core prepared (the binding signed by the hub's key in the core's process — the
        transport never reads the hub's key): a link's permit, our keys, the transport key, the label."""
        return self.call("POST", "/v1/join", {k: body[k] for k in ("invite", "sign_pub", "enc_pub", "transport_pub",
                                                                    "bind", "label")})

    def invite(self, text):
        return self.call("POST", "/v1/invite", {"invite": text})

    def invites(self):
        return self.call("GET", "/v1/invites").get("invites") or []

    def revoke(self, invite_id):
        """Drop an unused permit of ours (a link revoked or no longer needed): True — dropped; False — already used
        (409 used: it became a hub's registration, only the operator removes it) or unknown (404 no_invite)."""
        try:
            return bool(self.call("POST", "/v1/invite/revoke", {"id": invite_id}).get("revoked"))
        except Error as e:
            if e.code in ("used", "no_invite"):
                return False
            raise

    def peer(self, fp):
        """The protocol version the relay last heard from a registered hub (None — not registered here)."""
        try:
            v = self.call("GET", "/v1/peer", query=f"fp={fp}").get("bus_v")
        except Error as e:
            if e.code == "unknown_to":
                return None
            raise
        return v if type(v) is int and v >= 1 else 1

    def hello(self):
        """The relay's own protocol version (`bus_v` of GET /v1/hello; a relay without it — 1). Unsigned: a lie can only
        keep v2 envelopes back or make the relay refuse them, never open anything."""
        status, got, _ = self._http("GET", "/v1/hello", None, {"User-Agent": "agents-hub-bus-relay/1"}, self.timeout,
                                    4096)
        if not 200 <= status < 300:
            raise Error("network", f"HTTP {status}")
        try:
            data = json.loads(got.decode("utf-8"))
        except ValueError as e:
            raise Error("network", f"{type(e).__name__}: {e}"[:300]) from None
        v = data.get("bus_v") if isinstance(data, dict) else None
        return v if type(v) is int and v >= 1 else 1

    def allow(self, fps):
        return self.call("POST", "/v1/allow", {"from": sorted(fps)})

    def send(self, to, raw):
        return self.call("POST", "/v1/send", {"to": to, "envelope": base64.b64encode(raw).decode("ascii")})

    def inbox(self, wait=WAIT):
        return self.call("GET", "/v1/inbox", query=f"wait={int(wait)}", wait=wait).get("items") or []

    def ack(self, items):
        return self.call("POST", "/v1/ack", {"items": [list(x) for x in items]})

    def leave(self):
        return self.call("POST", "/v1/leave", {})


def transport_key_of(key_file):
    with open(key_file + ".pub", encoding="ascii") as f:
        return key_file, base64.b64encode(bus._ed25519_raw(f.read())).decode("ascii")
