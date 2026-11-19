#!/usr/bin/env python3
"""The hub bus relay: a small store-and-forward server for hubs that cannot reach each other directly.

A hub puts a sealed, signed envelope for another hub; the other hub takes it with a long poll and acknowledges it. The
relay never sees a text: the content is encrypted by the sending hub's core for the receiving hub's key, the relay
stores bytes until their expiry and checks only the outer form. What it does see — who writes to whom, when, the size
(rounded to a bucket), which hubs are registered and who invited whom — is listed in README.md.

Registration is by invitation. The operator makes the first one on the server (`server.py bootstrap`); a registered
hub then makes its own (`hub bus relay invite`, a request signed by its key) within a quota. One invitation — one
registration, seven days. A relay for everyone also has the open line (`server.py open`): one operator's invitation
any number of hubs register by, until `server.py open --off`. The operator sees who invited whom (`server.py hubs`)
and removes a hub (`server.py remove`).

Every request but /healthz and /v1/hello is signed by the hub's key (`ssh-keygen -Y sign`, namespace
agents-hub-relay-v1) over a line that names this relay's key, the method, the path, the time, a nonce and the body's
digest; a nonce is kept for ten minutes, so a request cannot be repeated. Every answer to a signed request is signed
by the relay's own key over the request's nonce, the status and the body's digest: a hub pins that key from the
invitation and does not take another server at the same address for its relay.

Two versions of the bus's envelopes go through side by side (spec hubbus-ux §4.0): v1 as the first hubs send them,
and v2 — a box names its class (`cls`: chat, line, liaison, ctl), an introduction may carry the link it came by, the
answer to it or a refusal. A hub announces its bus version by the X-Hub-Bus-V header on every signed request (none —
version 1); a registered hub asks another's with GET /v1/peer.

Python 3.12+ standard library and `ssh-keygen` (OpenSSH ≥ 8.1); the cryptography library, if installed (the image
has it), signs and checks in this process — the same text, without two runs of ssh-keygen a request. Settings:
HUB_RELAY_DB (the SQLite file; the relay key lies next to it), HUB_RELAY_PORT (8080), HUB_RELAY_HOST (0.0.0.0),
HUB_RELAY_URL (the public address, for the operator's invitations), HUB_RELAY_TRUST_PROXY=1 (a proxy in front sets
X-Forwarded-For), HUB_RELAY_POLLS (long polls at once, 64), HUB_RELAY_JOINS_DAY (joins by members' invitations a day,
50; 0 — no cap), HUB_RELAY_OPEN_DAY and HUB_RELAY_OPEN_ADDR_DAY (joins by the open line a day — the relay's, one
address's: 500, 5; 0 — no cap), HUB_RELAY_HUB_MINUTE and HUB_RELAY_HUB_BURST (a hub's signed requests a minute, at once:
120, 300), HUB_RELAY_DB_MAX_MB (the database's ceiling, 8192), HUB_RELAY_SIGNER=ssh-keygen (no library even if
installed). Connections at once — 1024 (never fewer than two a long poll) and 64 from one address — are constants:
over them a connection is refused 503 at once, by the accepting thread (README: Limits).

  server.py serve                 run the relay
  server.py bootstrap [--url URL] the first hub's invitation: one line on stdout, what to do with it on stderr; only
                                  while no hub is here (later a new hub comes with a link from a hub already here);
                                  asked again before the first hub came — the same line
  server.py invite [--url URL]    an invitation from the operator (prints one line)
  server.py open [--days N] [--url URL]   the open line: any hub registers by it (a year by default; asked again —
                                  the same line)
  server.py open --off            close the open line: it lets no one in, the hubs that came by it stay
  server.py hubs                  registered hubs: fingerprint, label, invited by, joined, queue
  server.py invites               invitations: who made them, whether used and by whom
  server.py remove <fingerprint> [--tree]   remove a hub (with --tree — and every hub it invited, and theirs): its
                                  requests are refused, its queue and unused invitations are dropped
  server.py key                   the relay's public key
  server.py check                 sign and verify with the relay key (the image health check)
"""
import base64
import contextlib
import hashlib
import http.server
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import typing
import urllib.parse

NS = "agents-hub-relay-v1"
REQUEST_TAG = "agents-hub-relay-v1 request"
RESPONSE_TAG = "agents-hub-relay-v1 response"
BIND_TAG = "agents-hub-relay-v1 bind"
INVITE_TAG = "agents-hub-relay-v1 invite"
INVITE_PREFIX = "hub-relay-invite-1:"
INVITE_MAX = 4096
ID_TAG = b"agents-hub-bus-v1 id\0"          # the hub's fingerprint (the same as the hub core computes)
FP = re.compile(r"hb1-[a-z2-7]{26}")
ID = re.compile(r"[0-9a-f]{32}")
NONCE = re.compile(r"[0-9a-f]{32}")
BUCKETS = (1024, 2048, 4096, 8192, 16384, 32768, 49152, 65536, 81920)   # the padded sizes of a box
NENC, TAG = 32, 16
ENVELOPE_MAX = 131072
INTRO_MAX = 8192
TTL = 7 * 86400
AHEAD = 600                # an envelope's ts may be this much ahead of the relay's clock
WINDOW = 300               # a request's time: ± five minutes
NONCE_KEEP = 600           # a nonce is kept ten minutes from the request's time
BODY_MAX = 256 * 1024
QUEUE_TO = (2000, 256 * 2 ** 20)      # envelopes, bytes waiting for one hub
QUEUE_PAIR = (500, 64 * 2 ** 20)      # of them from one sender
INTROS_TO, INTROS_PAIR = 500, 10      # introductions waiting for one hub, of them from one sender: they come from any
#                                       registered hub (an open relay — anyone), so they have their own room and never
#                                       take the boxes' place; one sender's share is small: fifty fill it, not ten
INTROS_DAY = 100           # new introductions of one sender a day, to all hubs (a hub introduces itself once a contact)
LOOSE_PER_MINUTE = 30      # unsigned or failed requests from one address (kept in memory only, never written)
HUB_MINUTE, HUB_BURST = 120, 300   # a registered hub's signed requests: refilled so many a minute, so many at once (in
#                                    memory); HUB_RELAY_HUB_MINUTE, HUB_RELAY_HUB_BURST set them (the load stand)
# the failures counted against an address: who is asking or how (not «the receiver does not take it», «full» or the
# relay's own day being over), and joins over the address's own cap (three signature checks each)
LOOSE_CODES = ("unsigned", "bad_signature", "clock", "replay", "unknown_hub", "removed", "bad_invite", "bad_request",
               "not_found", "body", "joins_addr")
INVITE_TTL = 7 * 86400
OPEN_DAYS = 365            # the term of the open line (`server.py open`) by default
ONE, OPEN, FIRST = 0, 1, 2  # invites.multi: a one-time invitation, the open line (many hubs), the first hub's line
#                             (`bootstrap`: one-time, asked again — the same; never an `invite` handed to someone)
INVITE_MIN = 600           # an invitation's shortest term: a hub cannot churn records faster than its live quota
EXPIRED_KEEP = 86400       # an invitation that expired unused is kept a day (a late join hears «expired»), then dropped
INVITES_LIVE = 5           # unused invitations of a hub at once
INVITES_MONTH = 20         # invitations of a hub used in 30 days together with its live unused ones
JOINS_DAY = 50             # hubs joined by members' invitations in a day, the whole relay (the operator's are not counted);
#                            HUB_RELAY_JOINS_DAY sets it, 0 — no cap
OPEN_DAY = 500             # hubs joined by the open line in a day, the whole relay; HUB_RELAY_OPEN_DAY, 0 — no cap
OPEN_ADDR_DAY = 5          # of them from one address (an IPv6 site's /48; kept in memory only); HUB_RELAY_OPEN_ADDR_DAY
USED_KEEP = 30 * 86400     # a used invitation is kept for the quota's month, then dropped
DB_MAX_MB = 8192           # the space the database takes, at most (a volume of 10 GiB keeps room for its write-ahead
#                            log): over it new letters, introductions, hubs and longer allow lists are refused;
#                            HUB_RELAY_DB_MAX_MB sets it
WAIT_MAX = 25
POLLS = 64                 # long polls at once (the server); HUB_RELAY_POLLS sets it
POLLS_HUB = 2              # long polls at once (one hub)
CONNS = 1024               # connections at once, the whole process (each holds a thread), never fewer than two for every
#                            long poll (HUB_RELAY_POLLS): over it a connection is refused 503 `connections` at once, by
#                            the accepting thread, with nothing of it read
CONNS_ADDR = 64            # of them from one address (an IPv6 address by its /64; 503 `connections_addr`): a hub holds two
#                            long polls and a few sends, so a few hubs behind one address fit; the loopback and private
#                            networks are not one client (UNCOUNTED). Behind a trusted proxy every
#                            connection is the proxy's: the cap counts the requests under way of the address it names
READ_FOR = 30              # seconds to read one request — its line, headers and body together (the socket's 15 s are for
#                            one read: a client sending a byte at a time kept its place as long as it liked)
REFUSED_FOR, REFUSED_MAX = 2, 64   # a refused connection is read for so long before it is closed (a close with the
#                                    request unread sends a reset, and the client may lose the 503), so many at once
BATCH = (32, 4 * 2 ** 20)  # envelopes and bytes in one inbox answer
ALLOW_MAX = 1000
ACK_MAX = 256
LABEL_MAX = 100
BUS_V = 2                  # the newest bus version whose envelopes the relay takes (said by /v1/hello)
BUS_V_HEADER = re.compile(r"[1-9][0-9]?")
CLASSES = ("chat", "line", "liaison", "ctl")   # a v2 box's class: the receiving core counts by it before it opens
# what a v2 introduction may carry, at most one: the link B came by (the one-time key's signature), A's answer bound
# to that B and its introduction, or A's refusal
LINK_KEYS = {"link": {"id", "sig"}, "link_ack": {"id", "fpB", "intro_id"}, "link_refused": {"id", "fpB", "code"}}
LINK_SIG = re.compile(r"[!-~]{1,1024}")
LINK_CODE = re.compile(r"[a-z_]{1,32}")


class Req(typing.NamedTuple):
    """A request whose signature checked: who, its nonce and time, the bus version it announced (no header — 1)."""
    fp: str
    nonce: str
    ts: int
    bus_v: int


def _setting(name, default, least=0):
    """A number from the environment (the operator's setting): ASCII digits, not below `least`; otherwise the default,
    and a word on stderr when something was set."""
    value = os.environ.get(name, "")
    if value.isascii() and value.isdigit() and int(value) >= least:
        return int(value)
    if value:
        print(f"relay: {name}={value!r} is not a number of {least} or more: {default} instead", file=sys.stderr,
              flush=True)
    return default


class Refused(Exception):
    def __init__(self, status, code, text=""):
        super().__init__(text or code)
        self.status, self.code = status, code


# --- ssh-keygen ----------------------------------------------------------------------------------------------------

def _ssh():
    path = shutil.which("ssh-keygen")
    if not path:
        raise RuntimeError("no ssh-keygen (OpenSSH client)")
    return path


def _run(args, data):
    with tempfile.TemporaryFile() as stdin:
        stdin.write(data)
        stdin.seek(0)
        r = subprocess.run([_ssh(), *args], stdin=stdin, capture_output=True, timeout=30)
    return r.returncode, r.stdout, r.stderr


def openssh_pub(raw):
    return "ssh-ed25519 " + base64.b64encode(_pub_blob(raw)).decode("ascii")


def raw_pub(line):
    parts = line.split()
    blob = base64.b64decode(parts[1], validate=True) if len(parts) >= 2 and parts[0] == "ssh-ed25519" else b""
    out, i = [], 0
    while i + 4 <= len(blob):
        n = int.from_bytes(blob[i:i + 4], "big")
        out.append(blob[i + 4:i + 4 + n])
        i += 4 + n
    if len(out) != 2 or out[0] != b"ssh-ed25519" or len(out[1]) != 32:
        raise ValueError("not an ed25519 key")
    return out[1]


def sign_ssh(key_path, data):
    code, out, err = _run(["-Y", "sign", "-f", key_path, "-n", NS], data)
    if code != 0:
        raise RuntimeError(f"ssh-keygen sign: {err.decode('utf-8', 'replace')[:200]}")
    return "\n".join(out.decode("ascii").strip().splitlines()) + "\n"


def verify_ssh(pub_raw, data, sig):
    with tempfile.TemporaryDirectory(prefix="relay-") as d:
        allowed, sig_file = os.path.join(d, "allowed"), os.path.join(d, "sig")
        # LF as ssh-keygen writes them: the one of Git for Windows reads no signature from CRLF ("missing header")
        with open(allowed, "w", encoding="ascii", newline="\n") as f:
            f.write(f'hub namespaces="{NS}" {openssh_pub(pub_raw)}\n')
        with open(sig_file, "w", encoding="ascii", newline="\n") as f:
            f.write(sig)
        code, _, _ = _run(["-Y", "verify", "-f", allowed, "-I", "hub", "-n", NS, "-s", sig_file], data)
    return code == 0


# --- the same signatures in this process (the cryptography library, if installed) -----------------------------------
# Two runs of ssh-keygen a request cost milliseconds; the library signs and checks Ed25519 in tens of microseconds. The
# text is ssh-keygen's own SSHSIG (PROTOCOL.sshsig): the hubs' ssh-keygen reads the answers, the relay reads their
# requests, nothing on the wire changes. HUB_RELAY_SIGNER=ssh-keygen keeps the old way.

SSHSIG = b"SSHSIG"
SIG_ARMOR = ("-----BEGIN SSH SIGNATURE-----", "-----END SSH SIGNATURE-----")
_keys = {}   # key file -> the loaded private key: read once


def _string(b):
    return len(b).to_bytes(4, "big") + b


def _strings(blob):
    """The SSH strings of a blob, in order; a cut one — ValueError."""
    out, i = [], 0
    while i < len(blob):
        if i + 4 > len(blob):
            raise ValueError("cut")
        n = int.from_bytes(blob[i:i + 4], "big")
        if i + 4 + n > len(blob):
            raise ValueError("cut")
        out.append(blob[i + 4:i + 4 + n])
        i += 4 + n
    return out


def _signed(data, reserved=b"", hash_alg=b"sha512"):
    return SSHSIG + _string(NS.encode("ascii")) + _string(reserved) + _string(hash_alg) + \
        _string(hashlib.new(hash_alg.decode("ascii"), data).digest())


def _pub_blob(raw):
    return _string(b"ssh-ed25519") + _string(raw)


def sign_lib(key_path, data):
    from cryptography.hazmat.primitives import serialization
    key = _keys.get(key_path)
    if key is None:
        with open(key_path, "rb") as f:
            key = _keys[key_path] = serialization.load_ssh_private_key(f.read(), password=None)
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    blob = SSHSIG + (1).to_bytes(4, "big") + _string(_pub_blob(raw)) + _string(NS.encode("ascii")) + _string(b"") + \
        _string(b"sha512") + _string(_string(b"ssh-ed25519") + _string(key.sign(_signed(data))))
    text = base64.b64encode(blob).decode("ascii")
    return "\n".join((SIG_ARMOR[0], *(text[i:i + 70] for i in range(0, len(text), 70)), SIG_ARMOR[1])) + "\n"


def verify_lib(pub_raw, data, sig):
    """What `ssh-keygen -Y verify` says for this one key and this namespace: the signer's key is that key, the namespace
    ours, the hash sha256 or sha512, and the Ed25519 signature checks."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        lines = [x.strip() for x in sig.strip().splitlines()]
        if len(lines) < 3 or (lines[0], lines[-1]) != SIG_ARMOR:
            return False
        blob = base64.b64decode("".join(lines[1:-1]), validate=True)
        if not blob.startswith(SSHSIG) or blob[6:10] != (1).to_bytes(4, "big"):
            return False
        pub, namespace, reserved, hash_alg, signature = _strings(blob[10:])
        kind, raw = _strings(signature)
        if (pub != _pub_blob(pub_raw) or namespace != NS.encode("ascii") or hash_alg not in (b"sha256", b"sha512")
                or kind != b"ssh-ed25519" or len(raw) != 64):
            return False
        Ed25519PublicKey.from_public_bytes(pub_raw).verify(raw, _signed(data, reserved, hash_alg))
        return True
    except (ValueError, TypeError, InvalidSignature):
        return False


def _the_library():
    """(whether the library signs here, why not): `serve` says the why, `check` fails on it unless it was chosen."""
    if os.environ.get("HUB_RELAY_SIGNER") == "ssh-keygen":
        return False, "HUB_RELAY_SIGNER=ssh-keygen"
    try:
        from cryptography.hazmat.primitives import serialization  # noqa: F401
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # noqa: F401
        return True, ""
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as e:  # noqa: BLE001 — not installed, or a broken build (its Rust binding panics: a
        #                         BaseException) whose binding does not load
        return False, f"no cryptography library ({type(e).__name__}: {e})"


LIBRARY, NO_LIBRARY = _the_library()
sign, verify = (sign_lib, verify_lib) if LIBRARY else (sign_ssh, verify_ssh)


NAT64 = ipaddress.ip_network("64:ff9b::/96")


def client_key(addr, v6=48):
    """The address a per-address limit counts: an IPv4 address as it is (also mapped or behind NAT64); IPv6 — its
    network of `v6` bits: a host holds a /64 (the failed requests count by it — one hub's faults do not lock out its
    neighbours), a subscriber a /48 or a /56 (the open line's joins count by it — per address, each /64 of it would be
    another address)."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return addr
    if ip.version == 4:
        return str(ip)
    if ip.ipv4_mapped:
        return str(ip.ipv4_mapped)
    if ip in NAT64:
        return str(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return str(ipaddress.ip_network(f"{ip}/{v6}", strict=False))


class Counts(dict):
    """Counters in memory, never written: what `drop` says is spent goes when the table has doubled since its last
    look (no scan at each request); past `most` entries even so — the ones `keep` names stay."""

    def __init__(self, most=200000):
        super().__init__()
        self.look_at, self.most = 10000, most

    def prune(self, drop, keep=None):
        if len(self) <= self.look_at:
            return
        for k in [k for k, v in self.items() if drop(v)]:
            del self[k]
        if len(self) > self.most and keep is not None:   # a flood of new keys: those at their limit stay
            for k in [k for k, v in self.items() if not keep(v)]:
                del self[k]
        self.look_at = max(10000, 2 * len(self))


def fingerprint(sign_pub, enc_pub):
    digest = hashlib.sha256(ID_TAG + sign_pub + enc_pub).digest()
    return "hb1-" + base64.b32encode(digest).decode("ascii").lower().rstrip("=")[:26]


def request_line(relay_key, method, target, ts, nonce, body):
    """What a hub signs: the relay's key (a request to one relay is no good to another), method, path with query, time,
    nonce, the body's digest."""
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
    """What the hub's own key signs once, at join: «this key speaks for me on this relay». The hub's identity key stays
    in its core; the transport holds only the key it signs requests with."""
    return "\n".join((BIND_TAG, relay_key, fp, transport_pub)).encode("ascii")


def invite_line(payload):
    return INVITE_TAG.encode("ascii") + b"\n" + payload


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64url(text):
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_-]*", text):
        raise ValueError("base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def invite_text(payload, sig):
    return INVITE_PREFIX + _b64url(payload) + "." + _b64url(sig.encode("ascii"))


def read_invite(text):
    """(fields, the exact payload bytes, the signature) of an invitation line; a wrong form — ValueError. Fields: v,
    relay (the address), key (the relay's key), id, by (the hub that made it, or operator), exp."""
    if not isinstance(text, str):
        raise ValueError("not text")
    text = "".join(text.split())
    if not text.startswith(INVITE_PREFIX) or len(text) > INVITE_MAX:
        raise ValueError("not an invitation")
    body, _, sig = text[len(INVITE_PREFIX):].partition(".")
    payload, sig = _unb64url(body), _unb64url(sig).decode("ascii")
    d = _strict(payload.decode("utf-8"))
    if not (isinstance(d, dict) and set(d) == {"v", "relay", "key", "id", "by", "exp"} and d["v"] == 1
            and type(d["v"]) is int and isinstance(d["relay"], str) and 0 < len(d["relay"]) <= 200
            and isinstance(d["key"], str) and isinstance(d["id"], str) and ID.fullmatch(d["id"])
            and isinstance(d["by"], str) and (d["by"] == "operator" or FP.fullmatch(d["by"]))
            and type(d["exp"]) is int and sig.startswith("-----BEGIN SSH SIGNATURE-----")):
        raise ValueError("invitation fields")
    unb64(d["key"], 32)
    return d, payload, sig


def unb64(text, size=None):
    if not isinstance(text, str) or not text.isascii():
        raise ValueError("base64")
    raw = base64.b64decode(text, validate=True)
    if base64.b64encode(raw).decode("ascii") != text or (size is not None and len(raw) != size):
        raise ValueError("base64")
    return raw


# --- the envelope's outer form (the relay reads nothing else) ------------------------------------------------------

def _pairs(items):
    out = {}
    for k, v in items:
        if k in out:
            raise ValueError("repeated key")
        out[k] = v
    return out


def _strict(text):
    return json.loads(text, object_pairs_hook=_pairs, parse_float=lambda t: (_ for _ in ()).throw(ValueError()),
                      parse_constant=lambda t: (_ for _ in ()).throw(ValueError()))


HEAD_KEYS = {"box": {"v", "t", "id", "ts", "exp", "from", "to", "ct"}, "intro": {"v", "t", "id", "ts", "exp", "card"}}


def _head_ok(h):
    """Whether the head's set of fields is one of the known forms: v1 as before; v2 — a box with its class, an
    introduction with at most one of the link's fields, each of them with exactly its own fields."""
    if not (isinstance(h, dict) and h.get("t") in HEAD_KEYS and type(h.get("v")) is int and h["v"] in (1, BUS_V)):
        return False
    base, extra = HEAD_KEYS[h["t"]], set(h) - HEAD_KEYS[h["t"]]
    if not base <= set(h):
        return False
    if h["v"] == 1:
        return not extra
    if h["t"] == "box":
        return extra == {"cls"} and isinstance(h["cls"], str) and h["cls"] in CLASSES
    if not extra:
        return True
    if len(extra) != 1 or not extra <= set(LINK_KEYS):
        return False
    name = extra.pop()
    d = h[name]
    if not (isinstance(d, dict) and set(d) == LINK_KEYS[name]
            and isinstance(d["id"], str) and ID.fullmatch(d["id"])):
        return False
    if name == "link":
        return isinstance(d["sig"], str) and bool(LINK_SIG.fullmatch(d["sig"]))
    if not (isinstance(d["fpB"], str) and FP.fullmatch(d["fpB"])):
        return False
    if name == "link_ack":
        return isinstance(d["intro_id"], str) and bool(ID.fullmatch(d["intro_id"]))
    return isinstance(d["code"], str) and bool(LINK_CODE.fullmatch(d["code"]))


def outer(raw, now):
    """(t, from, to, id, exp) of an envelope; anything else — Refused bad_envelope. The relay checks the form, the size
    bucket and the time; the signature and the content are the receiving core's business."""
    bad = Refused(400, "bad_envelope")
    if len(raw) > ENVELOPE_MAX:
        raise bad
    try:
        env = _strict(raw.decode("utf-8"))
        if not (isinstance(env, dict) and set(env) == {"head", "sig"} and isinstance(env["head"], str)
                and isinstance(env["sig"], str) and env["sig"].startswith("-----BEGIN SSH SIGNATURE-----")):
            raise bad
        h = _strict(env["head"])
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise bad from None
    if not (_head_ok(h) and isinstance(h["id"], str) and ID.fullmatch(h["id"])
            and type(h["ts"]) is int and type(h["exp"]) is int and h["ts"] < h["exp"] <= h["ts"] + TTL
            and h["ts"] <= now + AHEAD and h["exp"] > now):
        raise bad
    if h["t"] == "box":
        if not all(isinstance(h[k], str) and FP.fullmatch(h[k]) for k in ("from", "to")):
            raise bad
        try:
            n = len(unb64(h["ct"]))
        except ValueError:
            raise bad from None
        if n - NENC - TAG not in BUCKETS:
            raise bad
        return "box", h["from"], h["to"], h["id"], h["exp"]
    if len(raw) > INTRO_MAX or not isinstance(h["card"], dict):
        raise bad
    try:
        fp = fingerprint(unb64(h["card"].get("sign_pub"), 32), unb64(h["card"].get("enc_pub"), 32))
    except ValueError:
        raise bad from None
    return "intro", fp, None, h["id"], h["exp"]


# --- the store -----------------------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS hubs (fp TEXT PRIMARY KEY, sign_pub TEXT NOT NULL, transport_pub TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '', invited_by TEXT NOT NULL, invite TEXT NOT NULL, joined INTEGER NOT NULL, removed INTEGER,
  left INTEGER, bus_v INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS invites (id TEXT PRIMARY KEY, by TEXT NOT NULL, made INTEGER NOT NULL, exp INTEGER NOT NULL,
  signer TEXT NOT NULL, used_by TEXT, used INTEGER, multi INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS withdrawn (id TEXT PRIMARY KEY, by TEXT NOT NULL, at INTEGER NOT NULL, exp INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS allow (hub TEXT NOT NULL, sender TEXT NOT NULL, PRIMARY KEY (hub, sender));
CREATE TABLE IF NOT EXISTS queue (dst TEXT NOT NULL, src TEXT NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL,
  body BLOB NOT NULL, size INTEGER NOT NULL, exp INTEGER NOT NULL, put INTEGER NOT NULL, PRIMARY KEY (dst, src, id));
CREATE TABLE IF NOT EXISTS intros (dst TEXT NOT NULL, src TEXT NOT NULL, day INTEGER NOT NULL, n INTEGER NOT NULL,
  PRIMARY KEY (dst, src, day));
CREATE TABLE IF NOT EXISTS nonces (nonce TEXT PRIMARY KEY, exp INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS intros_src ON intros (src, day);
CREATE INDEX IF NOT EXISTS hubs_joined ON hubs (joined);
"""


class Store:
    """SQLite in WAL mode; one connection behind a lock, short transactions."""

    def __init__(self, path):
        self.path = path
        folder = os.path.dirname(os.path.abspath(path))
        os.makedirs(folder, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        have = {r[1] for r in self.db.execute("PRAGMA table_info(hubs)")}
        if "left" not in have:   # a database of the first S2 code
            self.db.execute("ALTER TABLE hubs ADD COLUMN left INTEGER")
        if "bus_v" not in have:   # a database of the code before L2: every hub was version 1
            self.db.execute("ALTER TABLE hubs ADD COLUMN bus_v INTEGER NOT NULL DEFAULT 1")
        if "multi" not in {r[1] for r in self.db.execute("PRAGMA table_info(invites)")}:   # before the open line
            self.db.execute("ALTER TABLE invites ADD COLUMN multi INTEGER NOT NULL DEFAULT 0")
        self.lock = threading.Lock()
        self.swept = 0

    @contextlib.contextmanager
    def tx(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")

    def one(self, sql, *args):
        with self.lock:
            return self.db.execute(sql, args).fetchone()

    def all(self, sql, *args):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def sweep(self, now):
        """Expired envelopes and nonces, the introductions' counts of past days, invitations a day after they expired
        unused, used ones after the quota's month (a hub's record keeps the id), the marks of withdrawn invitations
        after their term: at most once a minute."""
        if now - self.swept < 60:
            return
        self.swept = now
        with self.tx() as db:
            db.execute("DELETE FROM queue WHERE exp <= ?", (now,))
            db.execute("DELETE FROM invites WHERE used_by IS NULL AND exp <= ?", (now - EXPIRED_KEEP,))
            db.execute("DELETE FROM invites WHERE used_by IS NOT NULL AND used <= ? AND exp <= ?",
                       (now - USED_KEEP, now))
            # a day past the term, as an unused invitation: a closed open line still counts its day's joins
            db.execute("DELETE FROM withdrawn WHERE exp <= ?", (now - EXPIRED_KEEP,))
            db.execute("DELETE FROM nonces WHERE exp <= ?", (now,))
            db.execute("DELETE FROM intros WHERE day < ?", (now // 86400 - 1,))


def key_path(db_path):
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "relay.key")


def relay_key(db_path):
    """The relay's own key (made once, next to the database): (private key file, base64 of the public key)."""
    path = key_path(db_path)
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".new"
        for p in (tmp, tmp + ".pub"):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(p)
        r = subprocess.run([_ssh(), "-q", "-t", "ed25519", "-N", "", "-C", "agents-hub-relay", "-f", tmp],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError(f"ssh-keygen: {r.stderr.decode('utf-8', 'replace')[:200]}")
        os.replace(tmp + ".pub", path + ".pub")
        os.replace(tmp, path)
    with open(path + ".pub", encoding="ascii") as f:
        return path, base64.b64encode(raw_pub(f.read())).decode("ascii")


# --- the relay -----------------------------------------------------------------------------------------------------

class Relay:
    def __init__(self, db_path, clock=time.time):
        self.store = Store(db_path)
        self.key_file, self.key = relay_key(db_path)
        self.clock = clock
        self.wait_lock = threading.Lock()   # the long polls' counters and each hub's waiting
        self.polls, self.polls_of = 0, {}
        self.waits = {}                     # fp -> [the Condition its long polls wait on, how many letters woke it]
        self.polls_max = _setting("HUB_RELAY_POLLS", POLLS, least=1)
        self.joins_day = _setting("HUB_RELAY_JOINS_DAY", JOINS_DAY)
        self.open_day = _setting("HUB_RELAY_OPEN_DAY", OPEN_DAY)
        self.open_addr_day = _setting("HUB_RELAY_OPEN_ADDR_DAY", OPEN_ADDR_DAY)
        self.hub_minute = _setting("HUB_RELAY_HUB_MINUTE", HUB_MINUTE, least=1)
        self.hub_burst = _setting("HUB_RELAY_HUB_BURST", HUB_BURST, least=1)
        self.db_max = _setting("HUB_RELAY_DB_MAX_MB", DB_MAX_MB, least=1) * 2 ** 20
        self.loose = Counts()               # address (/64) -> (minute, failed requests)
        self.rate_lock = threading.Lock()
        self.ticks = time.monotonic         # the hubs' buckets refill by it
        self.buckets = Counts()             # fp -> (requests left, when counted)
        self.joins_from = Counts()          # address (/48) -> the times of its joins by the open line in 24 hours: read
#                                             in the join's transaction, written after its commit (under rate_lock)

    def now(self):
        return int(self.clock())

    # rate: a minute's window per address (unsigned or failed requests) — in memory, never written
    def _spend(self, table, who, most):
        minute = self.now() // 60
        with self.rate_lock:   # past minutes go; of the current ones those over the limit always stay
            table.prune(lambda v: v[0] != minute, keep=lambda v: v[1] >= most)
            m, n = table.get(who, (minute, 0))
            n = n + 1 if m == minute else 1
            table[who] = (minute, n)
        if n > most:
            raise Refused(429, "rate")

    def loose_spend(self, addr):
        self._spend(self.loose, client_key(addr, 64), LOOSE_PER_MINUTE)

    def loose_check(self, addr):
        """An address over its minute of failed requests is refused before any signature is checked."""
        m, n = self.loose.get(client_key(addr, 64), (None, 0))
        if m == self.now() // 60 and n >= LOOSE_PER_MINUTE:
            raise Refused(429, "rate")

    def hub_spend(self, fp, spend=True):
        """A registered hub's signed request, counted after its signature checked and its nonce is new (another's
        requests in its name, a repeated one spend nothing of it): a bucket of hub_burst, refilled hub_minute a minute —
        a hub's own pace and its bursts (a backlog handed at once) pass, one hub cannot keep the store's lock for
        everyone. spend=False only looks — before the signature: a hub over its rate costs no check (its own state;
        whoever writes in its name changes nothing of it)."""
        now, rate = self.ticks(), self.hub_minute / 60
        with self.rate_lock:   # a full bucket is as good as none
            self.buckets.prune(lambda v: v[0] + (now - v[1]) * rate >= self.hub_burst)
            n, t = self.buckets.get(fp, (self.hub_burst, now))
            n = min(self.hub_burst, n + (now - t) * rate)
            if spend:
                self.buckets[fp] = (n - 1 if n >= 1 else n, now)
        if n < 1:
            raise Refused(429, "rate", "too many requests of this hub: try later")

    def full(self, db, more=0):
        """Refused `full` if the database's pages in use (its free pages are not counted: they take the next writes)
        with `more` bytes would go over db_max."""
        pages, free, size = (db.execute(f"PRAGMA {p}").fetchone()[0] for p in ("page_count", "freelist_count",
                                                                               "page_size"))
        if (pages - free) * size + more > self.db_max:
            raise Refused(507, "full", "the relay's storage is full: try later")

    def open_caps(self, db, addr, now):
        """A join by the open line, in the last 24 hours: so many for the whole relay (the hubs whose record names an
        open line — a live one or one closed by `open --off`, whose mark stays) and from one address (in memory: the
        relay writes no address; a restart forgets them)."""
        if self.open_addr_day and addr is not None:
            if sum(t > now - 86400 for t in self.joins_from.get(client_key(addr), ())) >= self.open_addr_day:
                raise Refused(403, "joins_addr", "too many hubs joined from this address today: try tomorrow")
        if self.open_day:
            n = db.execute("SELECT COUNT(*) FROM hubs WHERE joined > ? AND (invite IN (SELECT id FROM invites WHERE "
                           "multi = ?) OR invite IN (SELECT id FROM withdrawn WHERE by = 'operator'))",
                           (now - 86400, OPEN)).fetchone()[0]
            if n >= self.open_day:
                raise Refused(403, "joins_open", "this relay took its hubs for today: try tomorrow")

    def joined_from(self, addr, now):
        self.joins_from.prune(lambda times: all(t <= now - 86400 for t in times))
        key = client_key(addr)
        live = [t for t in self.joins_from.get(key, ()) if t > now - 86400]
        self.joins_from[key] = live[-self.open_addr_day:] + [now]

    # the request's signature
    def authenticate(self, method, target, headers, body, join=None):
        """The Req of a signed request — or Refused. The signature is by the hub's transport key, the one its own key
        bound at join; for join itself the keys come from the body (see join). The bus version (X-Hub-Bus-V) is not
        in the signed line: a hub of version 2 talks to a relay of the code before it as before, and the only one who
        could change a header on the way is whoever stands behind the TLS — the operator or the proxy the operator put
        in front, who can drop or delay any request anyway; a changed version only lowers what peers send (v1)."""
        fp, ts, nonce, sig = (headers.get(h) for h in ("X-Hub-Fp", "X-Hub-Time", "X-Hub-Nonce", "X-Hub-Sig"))
        if not (fp and FP.fullmatch(fp) and ts and ts.isdigit() and nonce and NONCE.fullmatch(nonce) and sig):
            raise Refused(401, "unsigned")
        version = headers.get("X-Hub-Bus-V")
        if version is not None and not BUS_V_HEADER.fullmatch(version):
            raise Refused(400, "bad_request", "X-Hub-Bus-V: the hub's bus version, a number")
        if abs(int(ts) - self.now()) > WINDOW:
            raise Refused(401, "clock", "the request's time is more than five minutes off the relay's")
        try:
            sig_text = base64.b64decode(sig, validate=True).decode("ascii")
        except ValueError:
            raise Refused(401, "unsigned") from None
        if join is not None:
            pub = join
        else:
            row = self.store.one("SELECT transport_pub, removed, left FROM hubs WHERE fp = ?", fp)
            if row is None or (row[2] is not None and row[1] is None):
                raise Refused(403, "unknown_hub", "this hub is not registered here: join with an invitation")
            if row[1] is not None:
                raise Refused(403, "removed", "the operator removed this hub")
            pub = unb64(row[0], 32)
        self.hub_spend(fp, spend=False)
        if not verify(pub, request_line(self.key, method, target, ts, nonce, body), sig_text):
            raise Refused(401, "bad_signature")
        if self.store.one("SELECT 1 FROM nonces WHERE nonce = ?", nonce):   # its operation refuses it too; this one
            raise Refused(409, "replay")                                      # spends nothing of the hub's rate
        self.hub_spend(fp)
        # a version above what this relay takes is kept as the relay's own: peers read the version usable here
        return Req(fp, nonce, int(ts), min(int(version or 1), BUS_V))

    def spend_nonce(self, db, req, version=True):
        """The request's nonce, once — and in the same transaction the version the request announced: the version
        follows the requests (a hub rolled back to v1 sends none, and the relay says 1 from its next request on), and a
        repeated request is refused before it could set an old one."""
        try:
            db.execute("INSERT INTO nonces (nonce, exp) VALUES (?, ?)", (req.nonce, req.ts + NONCE_KEEP))
        except sqlite3.IntegrityError:
            raise Refused(409, "replay") from None
        if version:   # join writes it itself, only when the hub is in (a refused join changes no record)
            db.execute("UPDATE hubs SET bus_v = ? WHERE fp = ? AND bus_v != ?", (req.bus_v, req.fp, req.bus_v))

    def check_invite(self, db, text, now):
        """The invitation's record — (id, by, multi) — if the text is one this relay knows, unused (or the open line,
        which many use), in time, signed by the hub that made it (or by the relay for the operator's), and that hub is
        still here; otherwise Refused."""
        try:
            data, payload, sig = read_invite(text)
        except ValueError:
            raise Refused(400, "bad_invite", "not an invitation") from None
        if data["key"] != self.key:
            raise Refused(403, "bad_invite", "the invitation is for another relay")
        row = db.execute("SELECT by, exp, used_by, signer, multi FROM invites WHERE id = ?", (data["id"],)).fetchone()
        if row is None:
            withdrawn = db.execute("SELECT 1 FROM withdrawn WHERE id = ?", (data["id"],)).fetchone()
            raise Refused(403, "bad_invite", "the invitation was withdrawn" if withdrawn else "no such invitation")
        if row[0] != data["by"] or row[1] != data["exp"]:
            raise Refused(403, "bad_invite", "no such invitation")
        if row[2] is not None:
            raise Refused(403, "bad_invite", "the invitation was used")
        if row[1] <= now:
            raise Refused(403, "bad_invite", "the invitation expired")
        if row[0] != "operator":
            by = db.execute("SELECT removed, left FROM hubs WHERE fp = ?", (row[0],)).fetchone()
            if by is None or by[0] is not None or by[1] is not None:
                raise Refused(403, "bad_invite", "the hub that made the invitation is not here")
            joined = db.execute("SELECT COUNT(*) FROM invites WHERE by != 'operator' AND used > ?",
                                (now - 86400,)).fetchone()[0]
            if self.joins_day and joined >= self.joins_day:
                raise Refused(403, "joins_day", "too many hubs joined by members' invitations today: try tomorrow")
        if not verify(unb64(row[3], 32), invite_line(payload), sig):
            raise Refused(403, "bad_invite", "the invitation's signature does not check")
        return data["id"], row[0], row[4] == OPEN

    # operations: each runs in one transaction with the nonce
    def join(self, req, body, addr=None):
        """Registration: the hub's key (in the body; the fingerprint is computed from both of its keys) binds its
        transport key; a hub already here binds a new transport key without an invitation (a lost answer, a new
        installation of the client) — only its own key can do that. `addr`: the caller's (client_key) — the open line's
        cap per address."""
        fp = req.fp
        b = _json(body)
        label, invite = b.get("label", ""), b.get("invite")
        if not (isinstance(label, str) and len(label) <= LABEL_MAX
                and (invite is None or isinstance(invite, str) and len(invite) <= INVITE_MAX)):
            raise Refused(400, "bad_request")
        now = self.now()
        with self.store.tx() as db:
            self.spend_nonce(db, req, version=False)
            row = db.execute("SELECT removed, invited_by, left FROM hubs WHERE fp = ?", (fp,)).fetchone()
            if row is not None and row[0] is not None:
                return 403, {"code": "removed", "error": "the operator removed this hub"}
            if row is not None and row[2] is None:
                db.execute("UPDATE hubs SET transport_pub = ?, label = ?, bus_v = ? WHERE fp = ?",
                           (b["transport_pub"], label, req.bus_v, fp))
                return 200, {"fp": fp, "already": True, "invited_by": row[1]}
            if invite is None:
                return 403, {"code": "bad_invite", "error": "a new hub joins with an invitation"}
            iid, by, multi = self.check_invite(db, invite, now)
            if multi:
                self.open_caps(db, addr, now)
            self.full(db, len(body))   # about the record's size
            if row is not None:   # it left and comes back: the record stays under its first inviter (a tree removal
                # still finds it); the new invitation is in the invitations, used by it
                db.execute("UPDATE hubs SET sign_pub = ?, transport_pub = ?, label = ?, invite = ?, joined = ?, "
                           "left = NULL, bus_v = ? WHERE fp = ?",
                           (b["sign_pub"], b["transport_pub"], label, iid, now, req.bus_v, fp))
            else:
                db.execute("INSERT INTO hubs (fp, sign_pub, transport_pub, label, invited_by, invite, joined, bus_v) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (fp, b["sign_pub"], b["transport_pub"], label, by, iid, now, req.bus_v))
            # the use is written in the join's own transaction: the quota (see invite) counts it from this moment;
            # the open line stays open (many hubs come by it)
            if not multi:
                db.execute("UPDATE invites SET used_by = ?, used = ? WHERE id = ?", (fp, now, iid))
        if multi and addr is not None and self.open_addr_day:   # after the commit: a join that failed costs nothing
            with self.rate_lock:
                self.joined_from(addr, now)
        return 200, {"fp": fp, "invited_by": by}

    def invite(self, req, body):
        """A hub records an invitation it made and signed with its own key: the relay checks the signature, that the
        invitation names this relay and this hub, the term and the quota. The quota: at most INVITES_LIVE unused and
        live at once, and the invitations used in 30 days (by the time of use) together with the live ones at most
        INVITES_MONTH — a live one is a reserved place, so joins at once never go past the month's number; one that
        expired unused or was withdrawn spends nothing."""
        fp = req.fp
        text = _json(body).get("invite")
        try:
            data, payload, sig = read_invite(text)
        except ValueError:
            raise Refused(400, "bad_invite", "not an invitation") from None
        now = self.now()
        if (data["by"] != fp or data["key"] != self.key
                or not now + INVITE_MIN <= data["exp"] <= now + INVITE_TTL + AHEAD):
            raise Refused(400, "bad_invite", "the invitation must name this relay, this hub and a term of ten minutes "
                                             "to seven days")
        sign_pub = self.store.one("SELECT sign_pub FROM hubs WHERE fp = ?", fp)[0]
        if not verify(unb64(sign_pub, 32), invite_line(payload), sig):
            raise Refused(400, "bad_invite", "the invitation is not signed by this hub's key")
        with self.store.tx() as db:
            self.spend_nonce(db, req)
            live = db.execute("SELECT COUNT(*) FROM invites WHERE by = ? AND used_by IS NULL AND exp > ?",
                              (fp, now)).fetchone()[0]
            used = db.execute("SELECT COUNT(*) FROM invites WHERE by = ? AND used > ?",
                              (fp, now - 30 * 86400)).fetchone()[0]
            if live >= INVITES_LIVE or used + live >= INVITES_MONTH:
                return 429, {"code": "quota", "error": f"at most {INVITES_LIVE} unused invitations at once and "
                                                      f"{INVITES_MONTH} used and unused in 30 days"}
            if (db.execute("SELECT 1 FROM invites WHERE id = ?", (data["id"],)).fetchone()
                    or db.execute("SELECT 1 FROM withdrawn WHERE id = ?", (data["id"],)).fetchone()):
                return 409, {"code": "conflict", "error": "an invitation with this id exists"}
            self.full(db, len(text))
            db.execute("INSERT INTO invites (id, by, made, exp, signer) VALUES (?, ?, ?, ?, ?)",
                       (data["id"], fp, now, data["exp"], sign_pub))
        return 200, {"id": data["id"], "exp": data["exp"]}

    def invites(self, req, body):
        fp = req.fp
        with self.store.tx() as db:
            self.spend_nonce(db, req)
            rows = db.execute("SELECT id, made, exp, used_by, used FROM invites WHERE by = ? ORDER BY made", (fp,)).fetchall()
        return 200, {"invites": [{"id": r[0], "made": r[1], "exp": r[2], "used_by": r[3], "used": r[4]} for r in rows]}

    def revoke(self, req, body):
        """The hub withdraws an invitation it made and nobody used: its place in the quota is free, and the invitation
        lets no one in. The record goes (relay code of before this, rolled back to, finds no such invitation either), and
        a mark of it stays until its term: the same signed text, recorded again, is a conflict — a withdrawn link does
        not come back. A used one is not withdrawn — it became a hub's registration, which only the
        operator removes (`server.py remove`); another hub's invitation is answered as one that does not exist."""
        fp, now = req.fp, self.now()
        iid = _json(body).get("id")
        if not (isinstance(iid, str) and ID.fullmatch(iid)):
            raise Refused(400, "bad_request")
        with self.store.tx() as db:
            self.spend_nonce(db, req)
            row = db.execute("SELECT used_by, exp FROM invites WHERE id = ? AND by = ?", (iid, fp)).fetchone()
            if row is None:
                return 404, {"code": "no_invite", "error": "no such invitation of this hub"}
            if row[0] is not None:
                return 409, {"code": "used", "error": "the invitation was used: the hub it let in is a member now, "
                                                      "only the relay's operator removes it"}
            db.execute("DELETE FROM invites WHERE id = ? AND by = ? AND used_by IS NULL", (iid, fp))
            db.execute("INSERT OR REPLACE INTO withdrawn (id, by, at, exp) VALUES (?, ?, ?, ?)", (iid, fp, now, row[1]))
        return 200, {"revoked": True}

    def peer(self, req, query):
        """A registered hub asks another's bus version (spec hubbus-ux §4.0): {fp, bus_v}. A hub that is not here, left
        or was removed gets one and the same answer."""
        q = urllib.parse.parse_qs(query, keep_blank_values=True)
        fp = q.get("fp") or []
        if set(q) != {"fp"} or len(fp) != 1 or not FP.fullmatch(fp[0]):
            raise Refused(400, "bad_request")
        with self.store.tx() as db:
            self.spend_nonce(db, req)
            row = db.execute("SELECT bus_v FROM hubs WHERE fp = ? AND removed IS NULL AND left IS NULL",
                             (fp[0],)).fetchone()
        if row is None:
            return 404, {"code": "unknown_to", "error": "no such hub here"}
        return 200, {"fp": fp[0], "bus_v": row[0]}

    def allow(self, req, body):
        fp = req.fp
        senders = _json(body).get("from")
        if not (isinstance(senders, list) and len(senders) <= ALLOW_MAX
                and all(isinstance(s, str) and FP.fullmatch(s) for s in senders)):
            raise Refused(400, "bad_request")
        with self.store.tx() as db:
            self.spend_nonce(db, req)
            more = len(set(senders)) - db.execute("SELECT COUNT(*) FROM allow WHERE hub = ?", (fp,)).fetchone()[0]
            if more > 0:
                self.full(db, more * 64)   # a longer list is new space (two fingerprints a row); the same or a
                #                            shorter one always goes
            db.execute("DELETE FROM allow WHERE hub = ?", (fp,))
            db.executemany("INSERT OR IGNORE INTO allow (hub, sender) VALUES (?, ?)", [(fp, s) for s in senders])
        return 200, {"allowed": len(set(senders))}

    def send(self, req, body):
        fp = req.fp
        b = _json(body)
        to = b.get("to")
        if not (isinstance(to, str) and FP.fullmatch(to)):
            raise Refused(400, "bad_request")
        try:
            raw = unb64(b.get("envelope"))
        except ValueError:
            raise Refused(400, "bad_envelope") from None
        now = self.now()
        kind, src, dst, mid, exp = outer(raw, now)
        if src != fp:
            return 403, {"code": "not_yours", "error": "the envelope is not from the hub that signed the request"}
        if kind == "box" and dst != to:
            return 400, {"code": "bad_envelope", "error": "the envelope is for another hub"}
        with self.store.tx() as db:
            self.spend_nonce(db, req)
            row = db.execute("SELECT removed, left FROM hubs WHERE fp = ?", (to,)).fetchone()
            if row is None or row[0] is not None or row[1] is not None:
                return 404, {"code": "unknown_to", "error": "the receiver is not registered here"}
            have = db.execute("SELECT body FROM queue WHERE dst = ? AND src = ? AND id = ?", (to, fp, mid)).fetchone()
            if have is not None:
                if bytes(have[0]) == raw:
                    return 200, {"stored": "already"}
                return 409, {"code": "conflict", "error": "another envelope with this id is waiting"}
            if kind == "box" and db.execute("SELECT 1 FROM allow WHERE hub = ? AND sender = ?",
                                            (to, fp)).fetchone() is None:
                return 403, {"code": "not_allowed", "error": "the receiver does not take envelopes from this hub"}
            n_to, size_to = db.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM queue WHERE dst = ? AND kind = ?",
                                       (to, kind)).fetchone()
            n_pair, size_pair = db.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM queue WHERE dst = ? AND src = ? "
                                           "AND kind = ?", (to, fp, kind)).fetchone()
            most_to, most_pair = (QUEUE_TO, QUEUE_PAIR) if kind == "box" else ((INTROS_TO, QUEUE_TO[1]),
                                                                               (INTROS_PAIR, QUEUE_PAIR[1]))
            if (n_to + 1 > most_to[0] or size_to + len(raw) > most_to[1] or n_pair + 1 > most_pair[0]
                    or size_pair + len(raw) > most_pair[1]):
                return 503, {"code": "busy", "error": "the receiver's queue is full: try later"}
            day = now // 86400
            if kind == "intro" and db.execute("SELECT COALESCE(SUM(n), 0) FROM intros WHERE src = ? AND day = ?",
                                              (fp, day)).fetchone()[0] >= INTROS_DAY:
                return 403, {"code": "intros_day", "error": "this hub introduced itself to too many hubs today: "
                                                             "try tomorrow"}
            self.full(db, len(raw))
            db.execute("INSERT INTO queue (dst, src, id, kind, body, size, exp, put) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (to, fp, mid, kind, raw, len(raw), exp, now))
            if kind == "intro":   # the day's count (the code before the rooms read the same table the same way)
                db.execute("INSERT INTO intros (dst, src, day, n) VALUES (?, ?, ?, 1) ON CONFLICT (dst, src, day) "
                           "DO UPDATE SET n = n + 1", (to, fp, day))
        self._wake(to)
        return 200, {"stored": "new"}

    def _wake(self, *fps):
        """The long polls of these hubs look again: a letter wakes its receiver alone (waking every poll would send
        each of them to the database under the store's one lock — hundreds of looks for one letter)."""
        with self.wait_lock:
            for fp in fps:
                if fp in self.waits:
                    self.waits[fp][1] += 1
                    self.waits[fp][0].notify_all()

    def _waiting(self, fp):
        now = self.now()
        rows = self.store.all("SELECT src, id, body FROM queue WHERE dst = ? AND exp > ? ORDER BY put, src, id LIMIT ?",
                              fp, now, BATCH[0])
        out, size = [], 0
        for src, mid, body in rows:
            if out and size + len(body) > BATCH[1]:
                break
            size += len(body)
            out.append({"from": src, "id": mid, "envelope": base64.b64encode(bytes(body)).decode("ascii")})
        return out

    def inbox(self, req, wait):
        fp = req.fp
        with self.store.tx() as db:
            self.spend_nonce(db, req)
        with self.wait_lock:
            if self.polls >= self.polls_max or self.polls_of.get(fp, 0) >= POLLS_HUB:
                return 503, {"code": "busy", "error": "too many long polls"}
            self.polls += 1
            self.polls_of[fp] = self.polls_of.get(fp, 0) + 1
            mine = self.waits.setdefault(fp, [threading.Condition(self.wait_lock), 0])
        try:
            end = time.monotonic() + wait
            while True:
                with self.wait_lock:
                    woken = mine[1]
                items = self._waiting(fp)
                left = end - time.monotonic()
                if items or left <= 0:
                    return 200, {"items": items}
                with self.wait_lock:
                    if mine[1] == woken:   # no letter came while it looked: wait (a letter after this wakes it)
                        mine[0].wait(min(left, 5))
        finally:
            with self.wait_lock:
                self.polls -= 1
                self.polls_of[fp] -= 1
                if not self.polls_of[fp]:
                    del self.polls_of[fp]
                    self.waits.pop(fp, None)

    def ack(self, req, body):
        fp = req.fp
        items = _json(body).get("items")
        if not (isinstance(items, list) and len(items) <= ACK_MAX
                and all(isinstance(x, list) and len(x) == 2 and isinstance(x[0], str) and FP.fullmatch(x[0])
                        and isinstance(x[1], str) and ID.fullmatch(x[1]) for x in items)):
            raise Refused(400, "bad_request")
        with self.store.tx() as db:
            self.spend_nonce(db, req)
            n = 0
            for src, mid in items:
                n += db.execute("DELETE FROM queue WHERE dst = ? AND src = ? AND id = ?", (fp, src, mid)).rowcount
        return 200, {"removed": n}

    def leave(self, req, body):
        fp = req.fp
        with self.store.tx() as db:
            self.spend_nonce(db, req)
            _drop(db, fp, leaving=True)
            db.execute("UPDATE hubs SET left = ? WHERE fp = ?", (self.now(), fp))
        self._wake(fp)
        return 200, {"left": fp}

    # the operator (commands on the server, not over the network)
    def operator_line(self, url, iid, exp):
        """The text of an operator's invitation already recorded: the same fields signed by the relay's key. Ed25519
        signs the same bytes the same way, so a line asked again is the line printed before."""
        data = {"v": 1, "relay": url, "key": self.key, "id": iid, "by": "operator", "exp": exp}
        payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return invite_text(payload, sign(self.key_file, invite_line(payload)))

    def operator_invite(self, url):
        """A new one-time invitation from the operator: one line."""
        return self.operator_record(url)[0]

    def operator_record(self, url, days=None, kind=ONE, again=False):
        """An invitation from the operator, signed by the relay's key: one line. kind OPEN — the open line: many hubs
        register by it, for `days` (a year by default), until `open --off`; FIRST — the first hub's line. again — the
        live unused one of the same kind if there is one, looked up and made in one transaction: two commands at once
        get one line (for the same address — the address is in the line). → (the line, its term, whether made now)."""
        now = self.now()
        with self.store.tx() as db:
            row = db.execute("SELECT id, exp FROM invites WHERE by = 'operator' AND multi = ? AND used_by IS NULL "
                             "AND exp > ? ORDER BY exp DESC LIMIT 1", (kind, now)).fetchone() if again else None
            if row is None:
                row = (secrets.token_hex(16), now + (days * 86400 if days else OPEN_DAYS * 86400 if kind == OPEN
                                                     else INVITE_TTL))
                db.execute("INSERT INTO invites (id, by, made, exp, signer, multi) VALUES (?, 'operator', ?, ?, ?, ?)",
                           (row[0], now, row[1], self.key, kind))
                made = True
            else:
                made = False
        return self.operator_line(url, *row), row[1], made

    def close_open(self):
        """`open --off`: every open line lets no one in, now and after a rollback to the code before it (the record
        goes, a mark stays until its term — as a withdrawn invitation); the hubs that came by it stay."""
        now = self.now()
        with self.store.tx() as db:
            rows = db.execute("SELECT id, exp FROM invites WHERE multi = ?", (OPEN,)).fetchall()
            for iid, exp in rows:
                db.execute("DELETE FROM invites WHERE id = ?", (iid,))
                db.execute("INSERT OR REPLACE INTO withdrawn (id, by, at, exp) VALUES (?, 'operator', ?, ?)",
                           (iid, now, exp))
        return len(rows)

    def remove(self, fp, tree=False):
        """The hub (and with tree — every hub it invited, and theirs) is removed: requests refused, queues both ways,
        allow lists and unused invitations dropped; the record stays, so a removed fingerprint does not join again."""
        removed = []
        with self.store.tx() as db:
            todo = [fp]
            while todo:
                f = todo.pop()
                row = db.execute("SELECT removed FROM hubs WHERE fp = ?", (f,)).fetchone()
                if row is None or f in removed:
                    continue
                _drop(db, f)
                db.execute("UPDATE hubs SET removed = COALESCE(removed, ?) WHERE fp = ?", (self.now(), f))
                removed.append(f)
                if tree:
                    todo += [r[0] for r in db.execute("SELECT fp FROM hubs WHERE invited_by = ?", (f,)).fetchall()]
        self._wake(*removed)
        return removed


def _drop(db, fp, leaving=False):
    """A hub's queue, its allow list and its unused invitations. Removed: also what it sent and its place in the other
    hubs' allow lists — it does not come back. Leaving: what it already handed stays for its receivers (they check it
    themselves), its contacts still allow it; the record stays too, so a tree removal still finds whom it invited."""
    db.execute("DELETE FROM queue WHERE dst = ?" + ("" if leaving else " OR src = ?"), (fp,) if leaving else (fp, fp))
    db.execute("DELETE FROM allow WHERE hub = ?" + ("" if leaving else " OR sender = ?"), (fp,) if leaving else (fp, fp))
    db.execute("DELETE FROM invites WHERE by = ? AND used_by IS NULL", (fp,))


def _json(body):
    try:
        d = _strict(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Refused(400, "bad_request") from None
    if not isinstance(d, dict):
        raise Refused(400, "bad_request")
    return d


# --- HTTP ----------------------------------------------------------------------------------------------------------

POSTS = {"/v1/invite": "invite", "/v1/invite/revoke": "revoke", "/v1/allow": "allow", "/v1/send": "send",
         "/v1/ack": "ack", "/v1/leave": "leave"}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "agents-hub-relay/1"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    relay = None   # set by serve()

    def setup(self):
        super().setup()
        self.request.settimeout(15)   # a slow client does not hold a thread

    def handle_one_request(self):
        """One request with its reading clock (READ_FOR): a request not read by then is cut — a long poll's waiting and
        the answer are not counted."""
        clocks = self.server.clocks
        clocks.read(self.connection, READ_FOR)
        try:
            super().handle_one_request()
        finally:
            if clocks.done(self.connection, forget=True):
                self.close_connection = True

    def log_message(self, fmt, *args):   # no addresses in the log
        pass

    def answer(self, status, payload, nonce=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain" if isinstance(payload, bytes) else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if status >= 400:   # the rest of a refused request may still be unread: the connection does not go on
            self.send_header("Connection", "close")
            self.close_connection = True
        if nonce:
            h = self.headers
            got = request_digest(h.get("X-Hub-Fp"), h.get("X-Hub-Time"), nonce, h.get("X-Hub-Sig"), self.command,
                                 self.path, self.req_body)
            sig = sign(self.relay.key_file, response_line(nonce, status, body, got))
            self.send_header("X-Relay-Sig", base64.b64encode(sig.encode("ascii")).decode("ascii"))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = self.headers.get("Content-Length")
        if n is None or not n.isdigit() or int(n) > BODY_MAX:
            raise Refused(413 if n and n.isdigit() else 411, "body")
        body = self.rfile.read(int(n))
        if len(body) != int(n):   # the client went, or its reading clock cut it: nothing to answer
            raise ConnectionError("the body ended early")
        return body

    def addr(self):
        """The address for the per-address limits: the proxy's word only when the operator says one stands in front —
        the last of X-Forwarded-For, the one that proxy wrote (README: which proxy writes what)."""
        if trusting():
            return self.headers.get("X-Forwarded-For", "").split(",")[-1].strip() or self.client_address[0]
        return self.client_address[0]

    def handle_one(self, method):
        relay = self.relay
        target = self.path
        path, _, query = target.partition("?")
        nonce = self.headers.get("X-Hub-Nonce")
        nonce = nonce if nonce and NONCE.fullmatch(nonce) else None
        self.req_body = b""
        try:
            if self.server.clocks.was_cut(self.connection):   # by its reading clock: what was read is not a request
                self.close_connection = True
                return
            relay.store.sweep(relay.now())
            if method == "GET" and path == "/healthz":
                return self.answer(200, b"ok")
            if method == "GET" and path == "/v1/hello":
                return self.answer(200, {"v": 1, "key": relay.key, "bus_v": BUS_V})
            relay.loose_check(self.addr())   # an address over its failures gets nothing read and nothing signed
            body = self.req_body = self.body() if method == "POST" else b""
            if self.server.clocks.done(self.connection):   # read: a long poll's waiting is not the reading's time
                raise ConnectionError("cut by the reading clock")
            with self.server.asking(self.addr()):
                return self.route(method, target, path, query, body, nonce)
        except Refused as e:
            if e.code in LOOSE_CODES:
                with contextlib.suppress(Refused):
                    relay.loose_spend(self.addr())
            signed = e.status not in (411, 413, 429) and e.code != "connections_addr"
            return self.answer(e.status, {"code": e.code, **({"error": str(e)} if str(e) != e.code else {})},
                               nonce=nonce if signed else None)
        except (OSError, TimeoutError):
            self.close_connection = True
        except Exception as e:  # noqa: BLE001 — an error answers 500 and does not stop the server
            print(f"relay: {method} {path}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            try:
                self.answer(500, {"code": "error"}, nonce=nonce)
            except Exception:  # noqa: BLE001 — signing failed too: an unsigned 500 (the hub reads it as «try later»)
                with contextlib.suppress(Exception):
                    self.answer(500, {"code": "error"})

    def route(self, method, target, path, query, body, nonce):
        relay = self.relay
        if method == "POST" and path == "/v1/join":
            b = _json(body)
            try:
                sign_pub, enc_pub = unb64(b.get("sign_pub"), 32), unb64(b.get("enc_pub"), 32)
                transport_pub, bind = unb64(b.get("transport_pub"), 32), unb64(b.get("bind")).decode("ascii")
            except (ValueError, UnicodeDecodeError):
                raise Refused(400, "bad_request") from None
            fp = fingerprint(sign_pub, enc_pub)
            if fp != self.headers.get("X-Hub-Fp"):
                raise Refused(400, "bad_request", "the fingerprint is not of these keys")
            if not verify(sign_pub, bind_line(relay.key, fp, b["transport_pub"]), bind):
                raise Refused(401, "bad_signature", "the hub's key did not bind this transport key")
            req = relay.authenticate(method, target, self.headers, body, join=transport_pub)
            return self.answer(*relay.join(req, body, self.addr()), nonce=nonce)
        if method == "POST" and path in POSTS:
            req = relay.authenticate(method, target, self.headers, body)
            return self.answer(*getattr(relay, POSTS[path])(req, body), nonce=nonce)
        if method == "GET" and path == "/v1/inbox":
            q = urllib.parse.parse_qs(query)
            wait = (q.get("wait") or ["0"])[0]
            if not wait.isdigit():
                raise Refused(400, "bad_request")
            req = relay.authenticate(method, target, self.headers, body)
            return self.answer(*relay.inbox(req, min(int(wait), WAIT_MAX)), nonce=nonce)
        if method == "GET" and path == "/v1/invites":
            req = relay.authenticate(method, target, self.headers, body)
            return self.answer(*relay.invites(req, body), nonce=nonce)
        if method == "GET" and path == "/v1/peer":
            req = relay.authenticate(method, target, self.headers, body)
            return self.answer(*relay.peer(req, query), nonce=nonce)
        raise Refused(404, "not_found")

    def do_GET(self):
        self.handle_one("GET")

    def do_POST(self):
        self.handle_one("POST")


def trusting():
    """HUB_RELAY_TRUST_PROXY=1: a proxy in front writes the client's address (README: which proxy writes what)."""
    return os.environ.get("HUB_RELAY_TRUST_PROXY") == "1"


# a connection's address that is not one client of the internet: a proxy in front without HUB_RELAY_TRUST_PROXY (an
# ingress, a load balancer in a cluster's private network, the machine's own), a stand — every client at once; the
# address's cap does not count them
UNCOUNTED = (ipaddress.IPv4Network((127 << 24, 8)), ipaddress.IPv6Network((1, 128)),   # the loopback
             ipaddress.IPv4Network((10 << 24, 8)), ipaddress.IPv4Network((172 << 24 | 16 << 16, 12)),
             ipaddress.IPv4Network((192 << 24 | 168 << 16, 16)))                         # the private ones (RFC 1918)


def counted(addr):
    """The key a connection counts by for its address's cap: an IPv6 address by its /64; None — not counted
    (UNCOUNTED)."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return addr
    ip = getattr(ip, "ipv4_mapped", None) or ip   # ::ffff:127.0.0.1 is the loopback too
    if any(ip in net for net in UNCOUNTED):
        return None
    return client_key(addr, 64)


class Held:
    """How many connections (or requests) are held at once — in all, and by an address's key (None: not counted)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.n, self.of = 0, {}

    def take(self, who, most=None, most_each=None):
        """None if taken, otherwise the refusal's code."""
        with self.lock:
            if most is not None and self.n >= most:
                return "connections"
            if who is not None and most_each is not None and self.of.get(who, 0) >= most_each:
                return "connections_addr"
            self.n += 1
            if who is not None:
                self.of[who] = self.of.get(who, 0) + 1
        return None

    def give(self, who):
        with self.lock:
            self.n -= 1
            if who is not None:
                self.of[who] -= 1
                if not self.of[who]:
                    del self.of[who]


class Clocks:
    """One thread for the connections' clocks, while it has any: a request still being read at its deadline is cut
    (the socket shut: the reading thread gets its end at once), a refused connection is read for REFUSED_FOR and
    closed."""
    TICK = 0.1

    def __init__(self):
        self.lock = threading.Lock()
        self.reading, self.refused = {}, {}   # socket -> its deadline (monotonic)
        self.cut = set()                      # sockets their clock shut, until their request's end
        self.thread = None

    def read(self, sock, seconds):
        with self.lock:
            self.reading[sock] = time.monotonic() + seconds
            self._run()

    def was_cut(self, sock):
        with self.lock:
            return sock in self.cut

    def done(self, sock, forget=False):
        """The request is read (no clock for it any more): True if its clock had cut it already. Under the same lock as
        the cut, so a request either is cut before this or never."""
        with self.lock:
            self.reading.pop(sock, None)
            was = sock in self.cut
            if forget:
                self.cut.discard(sock)
        return was

    def refuse(self, sock):
        """False when REFUSED_MAX are being read already: the caller closes it at once."""
        with self.lock:
            if len(self.refused) >= REFUSED_MAX:
                return False
            self.refused[sock] = time.monotonic() + REFUSED_FOR
            self._run()
        return True

    def _run(self):   # under the lock
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, name="relay-clocks", daemon=True)
            self.thread.start()

    def _loop(self):
        while True:
            time.sleep(self.TICK)
            now = time.monotonic()
            with self.lock:
                for sock in [s for s, t in self.reading.items() if t <= now]:
                    del self.reading[sock]
                    self.cut.add(sock)
                    with contextlib.suppress(OSError):
                        sock.shutdown(socket.SHUT_RDWR)   # the reading thread gets its end at once
                refused = list(self.refused.items())
            for sock, until in refused:
                done = until <= now
                try:
                    for _ in range(16):   # a client that keeps sending does not keep the clocks here
                        if done:
                            break
                        done = not sock.recv(65536)   # b"" — the client closed
                except BlockingIOError:
                    pass
                except OSError:
                    done = True
                if done:
                    with contextlib.suppress(OSError):
                        sock.close()
                    with self.lock:
                        self.refused.pop(sock, None)
            with self.lock:
                if not self.reading and not self.refused:
                    self.thread = None
                    return


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128
    conns = CONNS   # serve() raises it to two a long poll

    def __init__(self, *args, **kw):
        self.held, self.asked, self.clocks, self.whose = Held(), Held(), Clocks(), {}
        super().__init__(*args, **kw)

    def process_request(self, request, client_address):
        """A connection over the caps is refused here, at once: no thread, nothing of it read, nothing signed."""
        who = None if trusting() else counted(client_address[0])
        why = self.held.take(who, self.conns, CONNS_ADDR)
        if why:
            return self.refuse(request, why)
        self.whose[request] = who
        try:
            super().process_request(request, client_address)
        except BaseException:   # the thread did not start — the place is not taken
            self.whose.pop(request, None)
            self.held.give(who)
            raise

    def process_request_thread(self, request, client_address):
        who = self.whose.pop(request, None)
        try:
            super().process_request_thread(request, client_address)   # closes the socket in finally
        finally:
            self.held.give(who)

    @contextlib.contextmanager
    def asking(self, addr):
        """Behind a trusted proxy: one request under way of the address it names (the connection is the proxy's)."""
        who = client_key(addr, 64) if trusting() else None   # the address the proxy names: always one client
        if who is None:
            yield
            return
        if self.asked.take(who, most_each=CONNS_ADDR):
            raise Refused(503, "connections_addr", "too many requests from this address at once: try later")
        try:
            yield
        finally:
            self.asked.give(who)

    def refuse(self, request, code):
        body = json.dumps({"code": code, "error": ("too many connections at once" if code == "connections" else
                                                   "too many connections from this address at once") + ": try later"},
                          separators=(",", ":")).encode("ascii")
        head = ("HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n")
        try:
            request.setblocking(False)
            request.send(head.encode("ascii") + body)
            request.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        else:
            if self.clocks.refuse(request):
                return
        self.shutdown_request(request)

    def handle_error(self, request, client_address):
        """Not socketserver's traceback with the client's address: no addresses in the log; a connection that broke
        says nothing."""
        e = sys.exc_info()[1]
        if not isinstance(e, OSError):
            print(f"relay: a connection: {type(e).__name__}: {e}", file=sys.stderr, flush=True)


def room_for(conns):
    """The connections the process's file limit leaves room for: the soft limit raised up to the hard one (a soft 1024
    is common), and when even that is short — fewer connections, said on stderr (accept failing for want of a file
    keeps refusing no one: the 503 needs the connection's file)."""
    want = conns + REFUSED_MAX + 128   # the listener, the database, ssh-keygen's pipes, a refused one being read
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (ImportError, ValueError, OSError):   # no such limit here (Windows)
        return conns
    if soft == resource.RLIM_INFINITY or soft >= want:
        return conns
    with contextlib.suppress(ValueError, OSError):   # not raised (a sandbox, the system's own ceiling): the soft one
        raised = want if hard == resource.RLIM_INFINITY else min(want, hard)
        resource.setrlimit(resource.RLIMIT_NOFILE, (raised, hard))
        soft = raised
    if soft >= want:
        return conns
    room = max(16, soft - REFUSED_MAX - 128)
    print(f"relay: the file limit is {soft}: {room} connections at once, not {conns}", file=sys.stderr, flush=True)
    return room


def listen_port(value):
    """HUB_RELAY_PORT: a port — or, in a pod, what Kubernetes puts under that name for a Service named hub-relay
    (tcp://<ip>:<port>, the docker links' variable): that port is the Service's, so the relay keeps its own 8080."""
    value = (value or "").strip()
    if not value:
        return 8080
    if re.fullmatch(r"tcp://[^\s/]+:[0-9]{1,5}", value):
        print(f"relay: HUB_RELAY_PORT={value} is a Kubernetes Service's link, not a port: listening on 8080",
              file=sys.stderr, flush=True)
        return 8080
    if value.isascii() and value.isdigit() and 0 < int(value) < 65536:
        return int(value)
    raise SystemExit(f"relay: HUB_RELAY_PORT={value!r} is not a port")


def serve(db_path, host="0.0.0.0", port=8080, ready=None, clock=time.time, signals=False):
    """The relay until it is stopped. signals (the command `serve`, in the main thread): SIGTERM and SIGINT stop it at
    once — a pod is stopped by SIGTERM, and a process with PID 1 has no default for it (without this it waits for the
    kill at the end of the grace period); the database is closed under the store's lock and the process ends there, so
    no request still running meets a closed database."""
    listening = []
    if signals:
        def stop(*_):
            if listening:   # shutdown() waits for serve_forever: from another thread
                threading.Thread(target=listening[0].shutdown, daemon=True).start()
            else:           # still opening the database: nothing is served yet
                os._exit(0)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, stop)
    relay = Relay(db_path, clock=clock)
    handler = type("RelayHandler", (Handler,), {"relay": relay})
    srv = Server((host, port), handler)
    srv.conns = room_for(max(CONNS, 2 * relay.polls_max))
    print(f"relay: listening on {host}:{srv.server_address[1]}, key {relay.key}", flush=True)
    if not LIBRARY:   # after the line above: whoever reads the first line for the key reads it as before
        print(f"relay: signing with ssh-keygen, two runs a request — {NO_LIBRARY}; a relay for many hubs needs the "
              "library (relay/requirements.txt)", file=sys.stderr, flush=True)
    listening.append(srv)
    if ready:
        ready(srv)
    srv.serve_forever()
    srv.server_close()
    if signals:
        relay.store.lock.acquire()   # after the transaction under way, and never released: the process ends here
        relay.store.db.close()       # the last connection folds the write-ahead log into the file
        print("relay: stopped", flush=True)
        os._exit(0)


# --- the operator's commands ---------------------------------------------------------------------------------------

def check_url(url):
    """The relay's public address as a hub takes it (the same rule as the hub core's): https to any host, plain http
    only to this machine; a host, a port and a path at most. None if a hub would not take it."""
    if not isinstance(url, str) or len(url) > 200 or any(c.isspace() for c in url):
        return None
    try:
        u = urllib.parse.urlsplit(url)
        host, _ = u.hostname, u.port   # a port that is not a number or out of range raises here
    except ValueError:
        return None
    if u.scheme not in ("https", "http") or not host or u.username or u.password or u.query or u.fragment:
        return None
    if u.scheme == "http":
        try:
            local = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        if not local:
            return None
    return url.rstrip("/")


BOOTSTRAP_HINT = """The line above is the invitation for the first hub on this relay: one hub, seven days.
On that hub paste it where a link is pasted (Settings, Hubs) or give it to `hub bus relay join`.
Every next hub comes with a link from a hub already here; this relay's operator is not needed again."""


OPEN_HINT = """The line above lets any hub register on this relay (one line for everyone, until `server.py open --off`).
Hubs that came by it reach each other by links, as on any relay. Publish it where your hubs' owners take it."""


def _when(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts)) + "Z" if ts else "-"


def main(argv):
    db_path = os.environ.get("HUB_RELAY_DB") or os.path.join(os.getcwd(), "relay.sqlite3")
    cmd, args = (argv[0], argv[1:]) if argv else ("serve", [])
    if cmd == "serve":
        serve(db_path, os.environ.get("HUB_RELAY_HOST", "0.0.0.0"), listen_port(os.environ.get("HUB_RELAY_PORT")),
              signals=True)
        return 0
    if cmd == "check":   # with the library: what it signs, the hubs' ssh-keygen must read too
        if not LIBRARY and os.environ.get("HUB_RELAY_SIGNER") != "ssh-keygen":   # the image has it: lost is a fault
            print(f"check: {NO_LIBRARY} — install relay/requirements.txt, or choose ssh-keygen: "
                  "HUB_RELAY_SIGNER=ssh-keygen", file=sys.stderr)
            return 1
        path, key = relay_key(db_path)
        line = response_line("0" * 32, 200, b"check", "0" * 64)
        sig, pub = sign(path, line), base64.b64decode(key)
        return 0 if verify(pub, line, sig) and (sign is sign_ssh or verify_ssh(pub, line, sig)) else 1
    relay = Relay(db_path)
    if cmd == "key":
        print(relay.key)
        return 0
    if cmd == "open":   # --off alone; otherwise --days N and --url URL — any other word is refused, never guessed
        if "--off" in args:
            if args != ["--off"]:
                print("open --off takes nothing else", file=sys.stderr)
                return 2
            n = relay.close_open()
            print(f"the open line is closed ({n}): it lets no one in; the hubs that came by it stay", file=sys.stderr)
            return 0
        rest = list(args)
        for flag in ("--days", "--url"):
            if flag in rest[:-1]:
                i = rest.index(flag)
                del rest[i:i + 2]
        if rest:
            print(f"open: unknown {' '.join(rest)!r} — open [--days N] [--url URL] or open --off", file=sys.stderr)
            return 2
    if cmd in ("invite", "bootstrap", "open"):
        url = args[args.index("--url") + 1] if "--url" in args[:-1] else os.environ.get("HUB_RELAY_URL")
        if not url:
            print("the relay's public address: --url https://… or HUB_RELAY_URL", file=sys.stderr)
            return 2
        good = check_url(url)
        if not good:
            print(f"a hub would not take this address: {url!r} — https://host[:port][/path], plain http only to "
                  "this machine", file=sys.stderr)
            return 2
        if cmd == "open":
            days = args[args.index("--days") + 1] if "--days" in args[:-1] else None
            if days is not None and not (days.isascii() and days.isdigit() and 0 < int(days) <= 3660):
                print("--days: a number of days, 1 to 3660", file=sys.stderr)
                return 2
            line, exp, made = relay.operator_record(good, int(days) if days else None, kind=OPEN, again=True)
            print(line)
            print(OPEN_HINT, file=sys.stderr)
            if days and not made:
                print(f"this is the live open line, good until {_when(exp)}: --days does not change it. Another term: "
                      "server.py open --off, then server.py open --days N", file=sys.stderr)
            return 0
        if cmd == "bootstrap":
            here = relay.store.one("SELECT COUNT(*) FROM hubs WHERE removed IS NULL AND left IS NULL")[0]
            if here:
                print(f"{here} hub(s) are already on this relay: a new hub comes with a link from one of them. "
                      "An operator's invitation anyway: server.py invite", file=sys.stderr)
                return 1
            # the first hub's line printed before and not used yet (the output was lost, the install cut): the same
            # line again — not a second first hub
            print(relay.operator_record(good, kind=FIRST, again=True)[0])
            print(BOOTSTRAP_HINT, file=sys.stderr)
            return 0
        print(relay.operator_invite(good))
        return 0
    if cmd == "hubs":
        rows = relay.store.all("SELECT h.fp, h.label, h.invited_by, h.joined, h.removed, h.left, COUNT(q.id) FROM hubs h "
                               "LEFT JOIN queue q ON q.dst = h.fp GROUP BY h.fp ORDER BY h.joined")
        for fp, label, by, joined, removed, left, n in rows:
            print(f"{fp}  {label!r}  invited by {by}  joined {_when(joined)}  queue {n}"
                  + (f"  REMOVED {_when(removed)}" if removed else "") + (f"  LEFT {_when(left)}" if left and not removed else ""))
        return 0
    if cmd == "invites":
        for iid, by, made, exp, used_by, used, multi in relay.store.all(
                "SELECT id, by, made, exp, used_by, used, multi FROM invites ORDER BY made"):
            state = f"used by {used_by} {_when(used)}" if used_by else ("expired" if exp <= relay.now() else "open")
            state += "  (the open line: many hubs)" if multi == OPEN else ""
            print(f"{iid[:8]}…  by {by}  made {_when(made)}  {state}")
        for iid, by, at in relay.store.all("SELECT id, by, at FROM withdrawn ORDER BY at"):
            print(f"{iid[:8]}…  by {by}  withdrawn {_when(at)}")
        return 0
    if cmd == "remove" and args and FP.fullmatch(args[0]) and set(args[1:]) <= {"--tree"}:
        gone = relay.remove(args[0], tree="--tree" in args)
        if not gone:
            print("no such hub", file=sys.stderr)
            return 1
        for fp in gone:
            print(f"removed {fp}")
        return 0
    print(__doc__.split("\n\n")[-1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
