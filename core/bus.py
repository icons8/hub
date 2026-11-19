"""The hub bus (spec hubbus, slice S0): who speaks and who reads between hubs.

- Two keys per hub in `<settings>/bus/`: a signature key Ed25519 made and used by `ssh-keygen` (`-Y sign/verify`, the
  namespace agents-hub-bus-v1) and an exchange key X25519 of the `cryptography` library (32 raw bytes, `enc.key`). The
  identity is the fingerprint of both public keys (`hb1-…`), the check code for people is 20 digits of its hash.
- The content goes only sealed: HPKE (RFC 9180, base mode) DHKEM(X25519, HKDF-SHA256) + HKDF-SHA256 + ChaCha20-Poly1305
  of `cryptography` >= 47, `info` binds v, from, to, id, ts, exp; the plain text is padded to a size bucket. There is no
  "without encryption" mode and no cryptographic code of our own: only calls of the library and of ssh-keygen, hashlib for
  the fingerprint (tests/test_hubbus.NoOwnCrypto checks it by the syntax tree).
- The envelope is two strings: the exact `head` (signed as is) and its signature. The receiver takes the key from its own
  contact table by `from`, checks the signature, then the window and the repeat, then the limits, and only then decrypts
  (receive). The decisions are the daemon's; the fold (core/busstate.py) reads the facts and does not check again.

The library is needed only with the bus on: nothing here imports it at module level; it is looked for first in
`<state>/pylib` (the path is added only here, not for sessions) and then in the interpreter's own paths.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import types
import unicodedata

import platforms
from core import cryptolib

NS = "agents-hub-bus-v1"
V = 1                      # the envelope of version 1 (the card's head stays v1: older hubs read it)
V2 = 2                     # the envelope of version 2: a box with a class, an introduction with a link field
BUS_V = 2                  # the hub's protocol version, named in its card (`bus_v`) and to the relay (X-Hub-Bus-V)
MIN_LIB = (47, 0)
CARD_PREFIX = "hub-bus-card-1:"
INFO = b"agents-hub-bus-v1 box\0"
ID_TAG = b"agents-hub-bus-v1 id\0"
CODE_TAG = b"agents-hub-bus-v1 code\0"
SIGNED_TAG = b"agents-hub-bus-v1 signed sha256:"
COMMIT_TAG = b"agents-hub-bus-v1 commit\0"
SAS_TAG = b"agents-hub-bus-v1 sas\0"
SAS_DIGITS = 6             # the pair code (spec hubbus/ux §5.2): one guess per link, a chance of 10⁻⁶
LINK_TAG = b"agents-hub-bus-v1 link\0"
LINK_PREFIX = "hub-link-1:"
LINK_MAX = 8192            # the link line; the longest a hub makes is under 4096 (a Telegram message), tests check it
LINK_TTLS = (3600, 86400, 604800)
LINK_TTL = 86400
BUCKETS = (1024, 2048, 4096, 8192, 16384, 32768, 49152, 65536, 81920)
INNER_MAX = 65536          # the serialized inner JSON
ENVELOPE_MAX = 131072      # the whole envelope
INTRO_MAX = 8192           # the card / intro envelope
TTL = 7 * 86400            # exp − ts at most
AHEAD = 600                # ts may be this much ahead of our clock
RECEIPT_EVERY = 60         # a repeated envelope gets its receipt again at most once a minute
INTRO_CARDS = 20           # introduction cards waiting for the owner at once; more strangers wait for a free place
NENC = 32                  # the encapsulated key of X25519
TAG = 16
SAFE_MAX = 2 ** 53
FP = re.compile(r"hb1-[a-z2-7]{26}")
ID = re.compile(r"[0-9a-f]{32}")
AGENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SIG_HEAD = "-----BEGIN SSH SIGNATURE-----"
HEAD_KEYS = {"box": {"v", "t", "id", "ts", "exp", "from", "to", "ct"}, "intro": {"v", "t", "id", "ts", "exp", "card"}}
# version 2 (the contract with the relay, spec hubbus/ux §4.0): a box carries its class (counted before decryption); an
# introduction may carry at most one link field, each of exactly these keys
CLASSES = ("chat", "line", "liaison", "ctl")
LINK_FIELDS = {"link": {"id", "sig"}, "link_ack": {"id", "fpB", "intro_id"}, "link_refused": {"id", "fpB", "code"}}
LINK_CODES = ("expired", "used", "revoked")
LINK_CODE = re.compile(r"[a-z_]{1,32}")
CARD_KEYS = {"label", "sign_pub", "enc_pub", "relays", "agents", "bus_v"}
MESSAGE_KEYS = {"kind", "agent_from", "agent_to", "label", "thread", "in_reply_to", "seq", "text", "of"}
# the inner kinds of version 2, each of exactly these keys (a message: MESSAGE_KEYS and, only in v2, `for: "owner"`)
V2_KINDS = {"unlink": {"kind", "pair_id"}, "card_update": {"kind", "label", "agents"}}
RECEIPT_STATUS = ("ok", "held", "refused")
REFUSE_CODES = ("not_allowed", "rate", "unknown_thread", "declined", "expired")
LATER_KINDS = ("rotate", "revoke")   # valid in the format, handled by a later slice (S5)


class Error(Exception):
    """A refusal with a code (bad_format, bad_signature, bad_box, stale, …): the code goes into the fact, the text — to the
    owner."""

    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class Unavailable(Exception):
    """The library is missing, too old or broken: why, and the command that installs it."""

    def __init__(self, code, why):
        super().__init__(why)
        self.code = code


# --- the library ---------------------------------------------------------------------------------------------------

_LIB = None


def pylib(state_dir=None):
    """`<state>/pylib`: where `hub bus on --install` puts the library (core/cryptolib.py, shared with the Secrets vault)."""
    return cryptolib.pylib(state_dir)


def install_command(state_dir=None, python=None):
    """The command that puts cryptography >= 47 into `<state>/pylib` with the hub's interpreter (core/cryptolib.py)."""
    return cryptolib.install_command(state_dir, python, MIN_LIB)


def _import():
    import cryptography
    return cryptography


def _own_new_enough(metadata):
    return cryptolib.own_new_enough(metadata, MIN_LIB)


_version = cryptolib.version


def crypto(state_dir=None):
    """The library as one object (hpke, x25519, InvalidTag, version, suite), or Unavailable with the reason."""
    global _LIB
    if _LIB is not None:
        return _LIB
    from importlib import metadata
    # pylib only when the interpreter's own library is missing or old: an environment that carries it (runtime 2) keeps
    # its own over what the core put in for its older one (core/busd.heal_lib) — that may be of another Python
    cryptolib.use_pylib(state_dir, MIN_LIB)
    # the version from the package metadata first: an old library imported into this process stays there until restart,
    # so a hub that has just installed a new one into pylib must not have imported the old one while looking
    try:
        found = metadata.version("cryptography")
    except metadata.PackageNotFoundError:
        found = None
    if found is not None and _version(found) < MIN_LIB and "cryptography" not in sys.modules:
        raise Unavailable("old_lib", f"cryptography {found}: HPKE needs >= {MIN_LIB[0]}")
    try:
        lib = _import()
    except ImportError as e:
        raise Unavailable("no_lib", f"no cryptography: {e}") from None
    version = getattr(lib, "__version__", "?")
    if _version(version) < MIN_LIB:
        where = os.path.dirname(getattr(lib, "__file__", "") or "") or "?"
        raise Unavailable("old_lib", f"cryptography {version} ({where}): HPKE needs >= {MIN_LIB[0]}; already loaded in "
                                     "this process — a restart picks up a newer one") from None
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives import hpke
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
    except ImportError as e:
        raise Unavailable("no_hpke", f"cryptography {version} without HPKE: {e}") from None
    suite = hpke.Suite(hpke.KEM.X25519, hpke.KDF.HKDF_SHA256, hpke.AEAD.CHACHA20_POLY1305)
    _LIB = types.SimpleNamespace(hpke=hpke, x25519=x25519, ed25519=ed25519, InvalidTag=InvalidTag,
                                 InvalidSignature=InvalidSignature, version=version, suite=suite)
    return _LIB


# --- keys, fingerprint, check code ---------------------------------------------------------------------------------

def key_dir(config_dir=None):
    if config_dir is None:
        from core import catalog
        config_dir = catalog.config_dir()
    return os.path.join(config_dir, "bus")


def _ssh():
    path = shutil.which("ssh-keygen")
    if not path:
        raise Error("no_ssh", "no ssh-keygen (OpenSSH): on Windows — the OpenSSH Client component")
    return path


def _b64(b):
    return base64.b64encode(b).decode("ascii")


def _unb64(text, size=None):
    """Strict base64: the standard alphabet with padding, no line breaks, the canonical form only."""
    if not isinstance(text, str) or not text.isascii():
        raise Error("bad_format", "base64")
    try:
        raw = base64.b64decode(text, validate=True)
    except ValueError:
        raise Error("bad_format", "base64") from None
    if _b64(raw) != text or (size is not None and len(raw) != size):
        raise Error("bad_format", "base64")
    return raw


def _ed25519_raw(line):
    """32 raw bytes of an `ssh-ed25519 AAAA…` public key line."""
    parts = line.split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise Error("bad_key", "not an ed25519 key")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except ValueError:
        raise Error("bad_key", "not an ed25519 key") from None
    out, i = [], 0
    while i < len(blob):
        n = int.from_bytes(blob[i:i + 4], "big")
        out.append(blob[i + 4:i + 4 + n])
        i += 4 + n
    if len(out) != 2 or out[0] != b"ssh-ed25519" or len(out[1]) != 32:
        raise Error("bad_key", "not an ed25519 key")
    return out[1]


def openssh_pub(sign_pub):
    """The `ssh-ed25519 …` line of 32 raw bytes (for allowed_signers)."""
    name = b"ssh-ed25519"
    blob = len(name).to_bytes(4, "big") + name + len(sign_pub).to_bytes(4, "big") + sign_pub
    return "ssh-ed25519 " + _b64(blob)


def fingerprint(sign_pub, enc_pub):
    """`hb1-` + 26 base32 letters of SHA-256(tag ‖ the signature key ‖ the exchange key): both keys are in it."""
    digest = hashlib.sha256(ID_TAG + bytes(sign_pub) + bytes(enc_pub)).digest()
    return "hb1-" + base64.b32encode(digest).decode("ascii").lower().rstrip("=")[:26]


def code(fp):
    """The check code people compare by another channel: 20 digits in five groups of four."""
    n = int.from_bytes(hashlib.sha256(CODE_TAG + fp.encode("ascii")).digest()[:10], "big") % (10 ** 20)
    digits = f"{n:020d}"
    return "-".join(digits[i:i + 4] for i in range(0, 20, 4))


def commit(lid, nonce):
    """A's commitment to its nonce, carried in the link (32 bytes): the nonce stays hidden until A's answer reveals it."""
    return hashlib.sha256(COMMIT_TAG + f"{lid}\0{nonce}".encode("ascii")).digest()


def sas_code(fp_a, fp_b, lid, nonce_a, nonce_b):
    """The code of a pair linked by a link (spec hubbus/ux §5.2), the same on both pages — six digits of SHA-256 over
    both fingerprints by role (A made the link, B pasted it), the link's id, A's nonce (committed in the link, revealed
    as the id of its answer) and B's (the id of its introduction). Nobody fixes his part knowing the other's, so a man
    in the middle cannot grind keys for a code: one guess per link. 128 bits taken: the bias of `mod` is nil."""
    data = SAS_TAG + "\0".join((fp_a, fp_b, lid, nonce_a, nonce_b)).encode("ascii")
    digits = f"{int.from_bytes(hashlib.sha256(data).digest()[:16], 'big') % (10 ** SAS_DIGITS):0{SAS_DIGITS}d}"
    return digits[:3] + "-" + digits[3:]


SAS = re.compile(r"\d{3}-\d{3}")


def contact_code(contact):
    """The pair code to show for a contact: the code of the link that is its pair now; None — no link of it is done on
    this side yet (or it came the old way: the code of the other hub, `code(fp)`, is typed)."""
    sas = (contact or {}).get("sas")
    if isinstance(sas, dict) and sas.get("id") and sas.get("id") == contact.get("pair") \
            and isinstance(sas.get("code"), str) and SAS.fullmatch(sas["code"]):
        return sas["code"]
    return None


class Keys(types.SimpleNamespace):
    """folder, sign_key (the ssh private key file), sign_pub (32 bytes), enc_priv (the X25519 private key object),
    enc_pub (32 bytes), fp, lib."""


KEY_FILES = ("key.pub", "key", "enc.key")   # the order they are moved in: enc.key last, so a cut leaves an incomplete set


def generate(folder, state_dir=None):
    """Both keys (files 0600), synced, made in a folder next to it and moved into `folder` only when whole: the private
    key of the exchange goes in last, so a power cut leaves either no key files or an incomplete set (load: keys_broken,
    the owner decides) — never a set that looks whole but is not. Existing key files are never overwritten; other files
    in the folder do not matter. A work folder left by a killed process before (never used, never shown) is removed."""
    lib = crypto(state_dir)
    _ssh()   # no OpenSSH — no_ssh before anything is made
    if any(os.path.exists(os.path.join(folder, n)) for n in KEY_FILES):
        raise Error("keys_exist", folder)
    parent = os.path.dirname(folder) or "."
    os.makedirs(parent, exist_ok=True)
    stale = os.path.basename(folder) + ".new-"
    for name in os.listdir(parent):
        if name.startswith(stale) and os.path.isdir(os.path.join(parent, name)):
            platforms.current.rmtree(os.path.join(parent, name))
    work = tempfile.mkdtemp(dir=parent, prefix=stale)   # 0700
    try:
        key, enc = os.path.join(work, "key"), os.path.join(work, "enc.key")
        code, _, err = _keygen(["-q", "-t", "ed25519", "-N", "", "-C", "agents-hub-bus", "-f", key], b"", work)
        if code != 0 or not os.path.exists(key):
            raise Error("keygen", err[:300])
        os.chmod(key, 0o600)   # ssh-keygen makes it so; on Windows chmod only clears read-only, the ACL is ssh-keygen's
        sk = lib.x25519.X25519PrivateKey.generate()
        fd = os.open(enc, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(sk.private_bytes_raw())
        for name in KEY_FILES:
            with open(os.path.join(work, name), "r+b") as f:   # Windows flushes only a handle open for writing
                platforms.current.sync(f.fileno())
        platforms.current.makedirs_private(folder)
        for name in KEY_FILES:
            if os.path.exists(os.path.join(folder, name)):   # appeared meanwhile: never overwritten
                raise Error("keys_exist", folder)
            os.replace(os.path.join(work, name), os.path.join(folder, name))
        platforms.current.sync_dir(folder)
    finally:
        platforms.current.rmtree(work)
    return load(folder, state_dir)


def _sign_pub(folder):
    """The signing public key of the key files in `folder` (base64), or None: no library needed."""
    try:
        with open(os.path.join(folder, "key.pub"), encoding="ascii") as f:
            return _b64(_ed25519_raw(f.read()))
    except (OSError, UnicodeDecodeError, Error):
        return None


def retire_keys(folder):
    """Remove the hub's key files (`hub bus reset`, or keys of a retired fingerprint a cut left behind): the private
    exchange key first — a cut leaves an incomplete set, never one that looks whole. Other files stay."""
    for name in reversed(KEY_FILES):
        try:
            os.unlink(os.path.join(folder, name))
        except FileNotFoundError:
            pass
    if os.path.isdir(folder):
        platforms.current.sync_dir(folder)


def load(folder, state_dir=None):
    lib = crypto(state_dir)
    key, enc = os.path.join(folder, "key"), os.path.join(folder, "enc.key")
    if not any(os.path.exists(p) for p in (key, key + ".pub", enc)):
        raise Error("no_keys", folder)
    try:
        with open(key + ".pub", encoding="ascii") as f:
            sign_pub = _ed25519_raw(f.read())
        with open(enc, "rb") as f:
            raw = f.read()
    except (OSError, UnicodeDecodeError, Error):
        raise Error("keys_broken", f"{folder}: not a whole key set — the owner decides (move it aside to start anew)") \
            from None
    if not os.path.exists(key) or len(raw) != 32:
        raise Error("keys_broken", f"{folder}: not a whole key set — the owner decides (move it aside to start anew)")
    sk = lib.x25519.X25519PrivateKey.from_private_bytes(raw)
    enc_pub = sk.public_key().public_bytes_raw()
    return Keys(folder=folder, sign_key=key, sign_pub=sign_pub, enc_priv=sk, enc_pub=enc_pub,
                fp=fingerprint(sign_pub, enc_pub), lib=lib)


def _keygen(args, data, folder):
    """ssh-keygen with the data from a file on stdin and stderr into a file: (code, stdout, stderr text). Not pipes:
    OpenSSH for Windows (9.5) hangs reading data from a stdin pipe and hangs on a failed check while its stderr is a
    pipe (winstand 28.10); a file is the same on every OS. No SSH agent: `-Y sign` asks the agent first, and a password
    manager's agent (Bitwarden, 1Password) holds the default pipe or socket without answering — ssh-keygen hung its 30 s;
    the bus key has no passphrase and needs no agent."""
    data_file, err_file = os.path.join(folder, "data"), os.path.join(folder, "err")
    with open(data_file, "wb") as f:
        f.write(data)
    env = {k: v for k, v in os.environ.items() if k.upper() != "SSH_AGENT_PID"}
    env["SSH_AUTH_SOCK"] = ""
    with open(data_file, "rb") as stdin, open(err_file, "wb") as stderr:
        r = platforms.current.run([_ssh(), *args], 30, stdin=stdin, stdout=subprocess.PIPE, stderr=stderr, env=env)
    with open(err_file, "rb") as f:
        err = f.read().decode("utf-8", "replace").strip()
    return r.returncode, r.stdout or b"", err


def signed_bytes(data):
    """What ssh-keygen signs for data: the tag and SHA-256 of the exact bytes (the `head` as it came), not the bytes
    themselves — OpenSSH for Windows 9.5 hangs when stdin carries more than ~8 KB (winstand 28.10), and `-Y verify`
    takes the data only on stdin. The binding is the same: another byte — another digest."""
    return SIGNED_TAG + hashlib.sha256(bytes(data)).hexdigest().encode("ascii")


def ssh_sign(key_path, data, ns):
    """The SSH signature (armored text) of exactly these bytes under a namespace. Keep data short (a digest or a line):
    ssh-keygen for Windows hangs on more than ~8 KB of stdin."""
    with tempfile.TemporaryDirectory(prefix="hub-bus-") as d:
        code, out, err = _keygen(["-Y", "sign", "-f", key_path, "-n", ns], bytes(data), d)
    if code != 0:
        raise Error("sign", err[:300])
    return "\n".join(out.decode("ascii").strip().splitlines()) + "\n"   # Windows writes CRLF


def ssh_verify(sign_pub, data, sig, ns):
    """Is sig a signature of exactly these bytes by this key under the namespace (allowed_signers of the one key)."""
    with tempfile.TemporaryDirectory(prefix="hub-bus-") as d:
        allowed, sig_file = os.path.join(d, "allowed"), os.path.join(d, "sig")
        # LF as ssh-keygen writes them: the one of Git for Windows reads no signature from CRLF ("missing header")
        with open(allowed, "w", encoding="ascii", newline="\n") as f:
            f.write(f'hub namespaces="{ns}" {openssh_pub(sign_pub)}\n')
        with open(sig_file, "w", encoding="ascii", newline="\n") as f:
            f.write(sig)
        code, _, _ = _keygen(["-Y", "verify", "-f", allowed, "-I", "hub", "-n", ns, "-s", sig_file], bytes(data), d)
    return code == 0


def sign(key_path, data):
    """The SSH signature (armored text) of data under the bus namespace."""
    return ssh_sign(key_path, signed_bytes(data), NS)


def verify(sign_pub, data, sig):
    """Is sig a signature of data by this key under the bus namespace."""
    return ssh_verify(sign_pub, signed_bytes(data), sig, NS)


def ssh_check(keys):
    """The hub's key signs, the signature checks, a changed byte does not: None or the reason."""
    try:
        sig = sign(keys.sign_key, b"agents-hub-bus selfcheck")
        if not verify(keys.sign_pub, b"agents-hub-bus selfcheck", sig):
            return "ssh-keygen: the signature of the hub's key does not verify"
        if verify(keys.sign_pub, b"agents-hub-bus selfcheck!", sig):
            return "ssh-keygen: a changed text verifies"
    except (Error, OSError, ValueError) as e:
        return f"ssh-keygen: {e}"
    return None


# --- strict JSON ---------------------------------------------------------------------------------------------------

def _pairs(items):
    out = {}
    for k, v in items:
        if k in out:
            raise Error("bad_format", f"repeated key {k}")
        out[k] = v
    return out


def _no_float(text):
    raise Error("bad_format", "not an integer")


def _int(text):
    n = int(text)
    if abs(n) > SAFE_MAX:
        raise Error("bad_format", "number too big")
    return n


def strict(text):
    """JSON without repeated keys, fractions, NaN and numbers beyond 2^53 — one parser for every reading."""
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_float=_no_float, parse_int=_int,
                          parse_constant=_no_float)
    except Error:
        raise
    except (ValueError, TypeError, RecursionError):
        raise Error("bad_format", "not JSON") from None


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _is_int(v):
    return type(v) is int and 0 <= v <= SAFE_MAX


def _text(v, most):
    return isinstance(v, str) and len(v) <= most and not any(unicodedata.category(c) in ("Cc", "Cf") for c in v)


def parse_head(text):
    """The head as a dict, checked field by field; anything else — bad_format. The same function reads it before the
    signature (to pick the key by `from`) and after it."""
    if not isinstance(text, str) or len(text) > ENVELOPE_MAX:
        raise Error("bad_format", "head")
    h = strict(text)
    if not isinstance(h, dict) or h.get("t") not in HEAD_KEYS or set(h) != _head_keys(h):
        raise Error("bad_format", "keys")
    if not (_is_int(h["v"]) and h["v"] in (V, V2) and ID.fullmatch(str(h["id"])) and isinstance(h["id"], str)
            and _is_int(h["ts"]) and _is_int(h["exp"]) and h["ts"] < h["exp"] <= h["ts"] + TTL):
        raise Error("bad_format", "fields")
    if h["t"] == "box":
        if not all(isinstance(h[k], str) and FP.fullmatch(h[k]) for k in ("from", "to")):
            raise Error("bad_format", "from/to")
        n = len(_unb64(h["ct"]))
        if not NENC + BUCKETS[0] + TAG <= n <= NENC + BUCKETS[-1] + TAG:
            raise Error("bad_format", "ct size")
        if h["v"] == V2 and h["cls"] not in CLASSES:
            raise Error("bad_format", "cls")
    else:
        _card_fields(h["card"])
        for key in set(h) & set(LINK_FIELDS):
            _link_field(key, h[key])
    return h


def _head_keys(h):
    """The exact keys of this head: v1 as it was; v2 — a box with `cls`, an introduction with at most one link field."""
    t = h["t"]
    if h.get("v") != V2:
        return HEAD_KEYS[t]
    if t == "box":
        return HEAD_KEYS[t] | {"cls"}
    extra = set(h) & set(LINK_FIELDS)
    return HEAD_KEYS[t] | (extra if len(extra) == 1 else set())


def _link_field(key, d):
    """link {id, sig} | link_ack {id, fpB, intro_id} | link_refused {id, fpB, code} — exactly these keys and forms."""
    if not (isinstance(d, dict) and set(d) == LINK_FIELDS[key] and isinstance(d["id"], str) and ID.fullmatch(d["id"])):
        raise Error("bad_format", key)
    if key == "link":
        ok = isinstance(d["sig"], str) and d["sig"].isascii() and 0 < len(d["sig"]) <= 1024 \
            and not any(c.isspace() or not c.isprintable() for c in d["sig"])
    else:
        ok = isinstance(d["fpB"], str) and bool(FP.fullmatch(d["fpB"])) and (
            isinstance(d["intro_id"], str) and bool(ID.fullmatch(d["intro_id"])) if key == "link_ack"
            else isinstance(d["code"], str) and bool(LINK_CODE.fullmatch(d["code"])))
    if not ok:
        raise Error("bad_format", key)


def _card_fields(c):
    if not isinstance(c, dict) or set(c) != CARD_KEYS:
        raise Error("bad_format", "card keys")
    _unb64(c["sign_pub"], 32)
    _unb64(c["enc_pub"], 32)
    from core import busstate
    if not (busstate.label_ok(c["label"]) and _is_int(c["bus_v"])
            and isinstance(c["relays"], list) and len(c["relays"]) <= 8 and all(_text(r, 200) for r in c["relays"])
            and isinstance(c["agents"], dict) and len(c["agents"]) <= 50
            and all(isinstance(k, str) and AGENT.fullmatch(k) and _text(v, 200) for k, v in c["agents"].items())):
        raise Error("bad_format", "card fields")


def parse_envelope(raw):
    """(the head string as it came, the head dict, the signature) of envelope bytes."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > ENVELOPE_MAX:
        raise Error("bad_format", "size")
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        raise Error("bad_format", "utf-8") from None
    outer = strict(text)
    if not (isinstance(outer, dict) and set(outer) == {"head", "sig"} and isinstance(outer["head"], str)
            and isinstance(outer["sig"], str)):
        raise Error("bad_format", "envelope keys")
    sig = outer["sig"]
    if not (sig.isascii() and sig.startswith(SIG_HEAD) and len(sig) <= 2048):
        raise Error("bad_format", "signature")
    return outer["head"], parse_head(outer["head"]), sig


# --- the box -------------------------------------------------------------------------------------------------------

def info_of(head):
    """HPKE `info`: the tag, then v, from, to, id, ts, exp (and the class of a v2 box) as ASCII through 0x00 — another hub,
    id or class cannot open it."""
    keys = ("v", "from", "to", "id", "ts", "exp") + (("cls",) if "cls" in head else ())   # a v2 box always has it
    return INFO + b"\0".join(str(head[k]).encode("ascii") for k in keys)


def pad(plain):
    body = len(plain).to_bytes(4, "big") + plain
    for n in BUCKETS:
        if len(body) <= n:
            return body + bytes(n - len(body))
    raise Error("too_big", f"{len(plain)} bytes")


def unpad(padded):
    n = int.from_bytes(padded[:4], "big")
    if len(padded) < 4 or n > len(padded) - 4 or any(padded[4 + n:]):
        raise Error("bad_box", "padding")
    return padded[4:4 + n]


def seal_box(lib, enc_pub, plain, info):
    """enc ‖ cipher text ‖ tag of the padded plain text for the receiver's exchange key."""
    pk = lib.x25519.X25519PublicKey.from_public_bytes(bytes(enc_pub))
    return lib.suite.encrypt(pad(plain), pk, info=info)


def open_box(lib, enc_priv, blob, info):
    try:
        padded = lib.suite.decrypt(blob, enc_priv, info=info)
    except (lib.InvalidTag, ValueError, TypeError):
        raise Error("bad_box", "does not open") from None
    return unpad(padded)


def selfcheck(lib):
    """The library seals and opens, and refuses a changed byte, another `info` and another key: None or the reason.
    A decrypt that accepts a forgery fails it (tests/test_hubbus)."""
    try:
        sk = lib.x25519.X25519PrivateKey.generate()
        pk = sk.public_key().public_bytes_raw()
        plain, info = b"agents-hub-bus selfcheck", INFO + b"selfcheck"
        blob = seal_box(lib, pk, plain, info)
        if open_box(lib, sk, blob, info) != plain:
            return "a sealed text does not come back"
        if seal_box(lib, pk, plain, info) == blob:
            return "two seals of one text are equal"
        bad = [bytes(blob[:i]) + bytes([(blob[i] + 1) % 256]) + bytes(blob[i + 1:]) for i in (0, NENC + 3, len(blob) - 1)]
        tries = [(b, sk, info) for b in bad] + [(blob, sk, info + b"!"),
                                                 (blob, lib.x25519.X25519PrivateKey.generate(), info)]
        for b, k, i in tries:
            try:
                open_box(lib, k, b, i)
            except Error:
                continue
            return "a changed box opens"
    except Exception as e:  # noqa: BLE001 — any failure of the library is a failed self-check, not a crash
        return f"{type(e).__name__}: {e}"
    return None


# --- inner layer ---------------------------------------------------------------------------------------------------

def message(agent_from, text, agent_to=None, label="", thread=None, in_reply_to=None, seq=1):
    return {"kind": "message", "agent_from": agent_from, "agent_to": agent_to, "label": label, "thread": thread,
            "in_reply_to": in_reply_to, "seq": seq, "text": text, "of": None}


def encode_inner(inner):
    return _dumps(inner).encode("utf-8")


def _inner(blob, v=V):
    """The inner layer after decryption, checked; a later kind (rotate, revoke) — unsupported. The v2 parts (`for` of a
    message, unlink, card_update) only in an envelope of version 2: a v1 box with them is not a box of ours."""
    try:
        d = strict(blob.decode("utf-8"))
    except (Error, UnicodeDecodeError):
        raise Error("bad_box", "inner") from None
    kind = d.get("kind") if isinstance(d, dict) else None
    if kind in LATER_KINDS:
        raise Error("unsupported", kind)
    if kind in V2_KINDS:
        ok = v == V2 and set(d) == V2_KINDS[kind] and (
            isinstance(d["pair_id"], str) and bool(ID.fullmatch(d["pair_id"])) if kind == "unlink"
            else _catalog_ok(d["label"], d["agents"]))
    elif kind == "message":
        keys = set(d) - ({"for"} if v == V2 else set())
        ok = (keys == MESSAGE_KEYS and d.get("for", "owner") == "owner"
              and isinstance(d["agent_from"], str) and AGENT.fullmatch(d["agent_from"])
              and (d["agent_to"] is None or isinstance(d["agent_to"], str) and AGENT.fullmatch(d["agent_to"]))
              and _text(d["label"], 100) and isinstance(d["thread"], str) and ID.fullmatch(d["thread"])
              and (d["in_reply_to"] is None or isinstance(d["in_reply_to"], str) and ID.fullmatch(d["in_reply_to"]))
              and _is_int(d["seq"]) and isinstance(d["text"], str) and d["of"] is None)
    elif kind == "receipt":
        ok = (set(d) - {"code"} == {"kind", "of", "status"} and isinstance(d["of"], str) and ID.fullmatch(d["of"])
              and d["status"] in RECEIPT_STATUS
              and (("code" in d) == (d["status"] == "refused")) and d.get("code", "rate") in REFUSE_CODES)
    else:
        ok = False
    if not ok:
        raise Error("bad_box", "inner fields")
    return d


def _catalog_ok(label, agents):
    from core import busstate
    return (busstate.label_ok(label) and isinstance(agents, dict) and len(agents) <= 50
            and all(isinstance(k, str) and AGENT.fullmatch(k) and _text(v, 200) for k, v in agents.items()))


def class_of(inner):
    """The class of a v2 box for its inner layer: the owners' chat, the liaison's message, a control message. A receipt
    has the class of the message it confirms (None here: any of the classes that carry messages)."""
    kind = inner.get("kind")
    if kind == "message":
        return "chat" if inner.get("for") == "owner" else "liaison"
    if kind in V2_KINDS:
        return "ctl"
    return None


# --- envelopes -----------------------------------------------------------------------------------------------------

def _envelope(keys, head):
    text = _dumps(head)
    return _dumps({"head": text, "sig": sign(keys.sign_key, text.encode("utf-8"))}).encode("utf-8")


def seal(keys, contact, inner, now, id=None, cls=None):
    """(id, envelope bytes) of the inner layer for a contact: sealed for its exchange key, signed by ours. With a class
    — a box of version 2 (only for a contact whose hub reads it: busstate.effective_v)."""
    mid = id or secrets.token_hex(16)
    if inner.get("kind") == "message" and inner.get("thread") is None:
        inner = {**inner, "thread": mid}
    plain = encode_inner(inner)
    if len(plain) > INNER_MAX:
        raise Error("too_big", f"{len(plain)} bytes of {INNER_MAX}")
    ts = int(now)
    head = {"v": V2 if cls else V, "t": "box", "id": mid, "ts": ts, "exp": ts + TTL, "from": keys.fp,
            "to": contact["fp"], **({"cls": cls} if cls else {})}
    head["ct"] = _b64(seal_box(keys.lib, _unb64(contact["enc_pub"], 32), plain, info_of(head)))
    raw = _envelope(keys, head)
    if len(raw) > ENVELOPE_MAX:
        raise Error("too_big", f"envelope {len(raw)} bytes")
    return mid, raw


def card_of(keys, label, relays=(), agents=None):
    return {"label": label, "sign_pub": _b64(keys.sign_pub), "enc_pub": _b64(keys.enc_pub), "relays": list(relays),
            "agents": dict(agents or {}), "bus_v": BUS_V}


def hub_label(me, config=None):
    """The hub's name in its card — the other side's «Linked: <name>»: the label the owner gave the bus, else the hub's
    title in hub.json (the name of its page), else the machine's name (without the domain); one line up to 100
    characters without control or format characters (the card's rule). config — hub.json as the caller has it."""
    import socket
    title = config.get("title") if isinstance(config, dict) else None
    try:
        host = socket.gethostname().split(".")[0]
    except OSError:
        host = ""
    for got in ((me or {}).get("label"), title, host):
        text = " ".join("".join(c for c in str(got or "") if unicodedata.category(c) not in ("Cc", "Cf")
                                or c in "\t\n").split())[:100].strip()
        if text:
            return text
    return "Hub"


def make_card(keys, label, now, relays=(), agents=None):
    """The hub's card: one line the owner passes by any channel; signed by its own key (the root of trust is the code).
    Its head is of version 1 (any hub reads it); `bus_v` names the protocol the hub speaks."""
    ts = int(now)
    head = {"v": V, "t": "intro", "id": secrets.token_hex(16), "ts": ts, "exp": ts + TTL,
            "card": card_of(keys, label, relays, agents)}
    _card_fields(head["card"])
    return CARD_PREFIX + _b64(_envelope(keys, head))


def link_intro(keys, label, now, field, value, relays=(), id=None):
    """(id, envelope bytes) of an introduction of version 2 with one link field: B's «I came by the link» (`link`) or
    A's answer (`link_ack`, `link_refused`). The card in it names no agents: the catalog comes after the check."""
    ts = int(now)
    head = {"v": V2, "t": "intro", "id": id or secrets.token_hex(16), "ts": ts, "exp": ts + TTL,
            "card": card_of(keys, label, relays), field: value}
    _card_fields(head["card"])
    _link_field(field, value)
    return head["id"], _envelope(keys, head)


# --- the link (spec hubbus/ux §4.1) ---------------------------------------------------------------------------------

def _b64url(raw):
    return base64.urlsafe_b64encode(bytes(raw)).decode("ascii").rstrip("=")


def _unb64url(text):
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_-]*", text) or len(text) % 4 == 1:
        raise Error("bad_link", "base64url")
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if _b64url(raw) != text:
        raise Error("bad_link", "base64url")
    return raw


def make_link(keys, label, now, relay=None, ttl=LINK_TTL):
    """(the link text, the record A keeps, the permit's text or None for the transport's job file) —
    `hub-link-1:` + base64url(JSON {v, min_bus_v, id, card, relay, k, c, exp}):
    A's card without the agents catalog, a relay permit signed by A's key for the same term (None without a relay),
    the seed of a one-time Ed25519 key `k`, the commitment `c` to A's nonce for the pair code and the link's id. A
    keeps the public half of `k` (`pub`), its nonce and the permit's id; the link itself is a secret for one link and
    goes nowhere but to the owner."""
    from core import busrelay
    lib = keys.lib
    if ttl not in LINK_TTLS:
        raise Error("bad_ttl", str(ttl))
    sk = lib.ed25519.Ed25519PrivateKey.generate()
    pub = sk.public_key().public_bytes_raw()
    lid, exp, nonce = secrets.token_hex(16), int(now) + ttl, secrets.token_hex(16)
    card = make_card(keys, label, now, relays=[relay["ref"]] if relay and relay.get("ref") else [])
    invite = invite_id = None
    if relay:
        invite, invite_id, _ = busrelay.make_invite(keys, relay, now, exp=exp)
    body = {"v": 1, "min_bus_v": BUS_V, "id": lid, "card": card[len(CARD_PREFIX):], "relay": invite,
            "k": _b64url(sk.private_bytes_raw()), "c": _b64url(commit(lid, nonce)), "exp": exp}
    text = LINK_PREFIX + _b64url(_dumps(body).encode("utf-8"))
    return text, {"id": lid, "pub": _b64(pub), "exp": exp, "invite": invite_id, "nonce": nonce, "made_at": int(now)}, \
        invite


LINK_KEYS = {"v", "min_bus_v", "id", "card", "relay", "k", "c", "exp"}


def read_link(text, now):
    """The link's fields, checked: {id, exp, min_bus_v, card (read_card of A's card), invite (the permit's fields or
    None), invite_text, seed, commit (A's commitment, base64url), made_at (when A made it: its card's time)}.
    Error: bad_link (the form), old_hub (made for a newer protocol), stale (expired by our clock — informative: A
    counts the term by its own clock at receipt), bad_signature (the card)."""
    from core import busrelay
    text = "".join(str(text).split())
    if not text.startswith(LINK_PREFIX) or len(text) > LINK_MAX:
        raise Error("bad_link", "not a link")
    try:
        d = strict(_unb64url(text[len(LINK_PREFIX):]).decode("utf-8"))
    except UnicodeDecodeError:
        raise Error("bad_link", "utf-8") from None
    except Error as e:
        raise Error("bad_link", str(e)) from None
    if not (isinstance(d, dict) and set(d) == LINK_KEYS and _is_int(d["v"]) and _is_int(d["min_bus_v"])
            and isinstance(d["id"], str) and ID.fullmatch(d["id"]) and isinstance(d["card"], str)
            and isinstance(d["k"], str) and isinstance(d["c"], str) and _is_int(d["exp"])
            and (d["relay"] is None or isinstance(d["relay"], str))):
        raise Error("bad_link", "fields")
    if d["v"] != 1:
        raise Error("bad_link", "version")
    if d["min_bus_v"] > BUS_V:
        raise Error("old_hub", f"the link needs a hub of protocol {d['min_bus_v']}")
    seed = _unb64url(d["k"])
    if len(seed) != 32:
        raise Error("bad_link", "k")
    if len(_unb64url(d["c"])) != 32:
        raise Error("bad_link", "c")
    try:
        card = read_card(CARD_PREFIX + d["card"], now)
    except Error as e:
        if e.code == "stale":
            raise
        raise Error(e.code if e.code == "bad_signature" else "bad_link", str(e)) from None
    if now is not None and d["exp"] <= now:   # None: the core rereads the copy it checked when it was pasted
        raise Error("stale", "link")
    invite = None
    if d["relay"] is not None:
        try:
            invite = busrelay.read_invite(d["relay"])[0]
        except busrelay.Error as e:
            raise Error("bad_link", f"permit: {e}") from None
        if invite["by"] != card["fp"] or busrelay.ref(invite["relay"], invite["key"]) not in card["relays"]:
            raise Error("bad_link", "the permit is not of the hub of the card or its relay")
    return {"id": d["id"], "exp": d["exp"], "min_bus_v": d["min_bus_v"], "card": card, "invite": invite,
            "invite_text": d["relay"], "seed": d["k"], "commit": d["c"], "made_at": card["ts"]}


def _link_bytes(fp_a, fp_b, intro_id):
    return LINK_TAG + "\0".join((fp_a, fp_b, intro_id)).encode("ascii")


def link_proof(seed, fp_a, fp_b, intro_id, lib=None):
    """B's proof «I have the link»: the signature of the one-time key over A's and B's fingerprints and the id of the
    introduction envelope it goes in (base64 of 64 raw bytes) — the key itself never leaves B."""
    lib = lib or crypto()
    sk = lib.ed25519.Ed25519PrivateKey.from_private_bytes(_unb64url(seed) if isinstance(seed, str) else seed)
    return _b64(sk.sign(_link_bytes(fp_a, fp_b, intro_id)))


def link_proof_ok(pub, sig, fp_a, fp_b, intro_id, lib=None):
    lib = lib or crypto()
    try:
        pk = lib.ed25519.Ed25519PublicKey.from_public_bytes(_unb64(pub, 32))
        pk.verify(_unb64(sig, 64), _link_bytes(fp_a, fp_b, intro_id))
    except (Error, lib.InvalidSignature, ValueError, TypeError):
        return False
    return True


def read_card(text, now):
    """The card's fields and the hub's fingerprint and check code; a forged, foreign or expired card — Error."""
    text = "".join(str(text).split())
    if not text.startswith(CARD_PREFIX) or len(text) > 2 * INTRO_MAX:
        raise Error("bad_format", "not a hub card")
    raw = _unb64(text[len(CARD_PREFIX):])
    if len(raw) > INTRO_MAX:
        raise Error("bad_format", "card size")
    head_text, head, sig = parse_envelope(raw)
    if head["t"] != "intro":
        raise Error("bad_format", "not a card")
    c = head["card"]
    sign_pub, enc_pub = _unb64(c["sign_pub"], 32), _unb64(c["enc_pub"], 32)
    if not verify(sign_pub, head_text.encode("utf-8"), sig):
        raise Error("bad_signature", "card")
    if now is not None and (head["ts"] > now + AHEAD or head["exp"] <= now):   # None: the core rereads its own copy
        raise Error("stale", "card")
    fp = fingerprint(sign_pub, enc_pub)
    return {"fp": fp, "code": code(fp), "label": c["label"], "sign_pub": c["sign_pub"], "enc_pub": c["enc_pub"],
            "relays": c["relays"], "agents": c["agents"], "bus_v": c["bus_v"], "ts": head["ts"]}


def contact_fact(info, name, status, verified=False):
    """core.bus_contact_added of a checked card (read_card)."""
    return {"name": name, **{k: info[k] for k in ("fp", "label", "sign_pub", "enc_pub", "relays", "agents", "bus_v")},
            "status": status, "verified": bool(verified)}


def enabled_fact(keys, lib, label=None):
    return {"fp": keys.fp, "sign_pub": _b64(keys.sign_pub), "enc_pub": _b64(keys.enc_pub), "lib": lib.version,
            **({"label": label} if label else {})}


# --- receipt of an envelope: the daemon's decision -----------------------------------------------------------------

def precheck(text):
    """A finding in an incoming text that holds it for the owner: hidden and control characters (bidi, tag characters,
    zero-width), variation selectors, unassigned and private code points. Line breaks and tabs are text."""
    for c in text:
        if c in "\n\t":
            continue
        cat = unicodedata.category(c)
        o = ord(c)
        if cat in ("Cc", "Cf", "Co", "Cs", "Cn") or 0xFE00 <= o <= 0xFE0F or 0xE0100 <= o <= 0xE01EF:
            return f"U+{o:04X} ({cat})"
    return None


def _receipt_of(entry):
    result = entry.get("result") or ""
    if result.startswith("refused:"):
        return {"kind": "receipt", "status": "refused", "code": result.split(":", 1)[1]}
    return {"kind": "receipt", "status": "held" if result in ("held", "unverified") else "ok"}


def receive(bus_state, keys, raw, now):
    """The core's decision on an envelope from a transport: {"code", "fact": (type, data) | None, "reply": a receipt
    envelope | None}. The order of the spec (§5.7): format → known contact → signature and `to` → window → repeat →
    quarantine and the envelope limit (before decryption) → decryption → the message limit → muted → precheck. A
    receipt is never answered; nothing is decrypted before the signature; no receipt for what does not pass the
    signature (the sender does not learn that we see it). The receipt goes out after the fact is written."""
    from core import busstate
    size, digest = len(raw), hashlib.sha256(bytes(raw)).hexdigest()

    def out(code, fact=None, reply=None):
        return {"code": code, "fact": fact, "reply": reply}

    def rejected(code, **kw):
        return out(code, ("core.bus_rejected", {"code": code, "size": size, "sha256": digest, **kw}))

    try:
        head_text, head, sig = parse_envelope(raw)
    except Error:   # a head of an unknown version too: before any contact — nothing counted, no quarantine
        return rejected("bad_format")
    if head["t"] == "intro":   # an introduction through a transport: a card to the owner, never to the sessions
        if "link" in head:
            return _link_intro(bus_state, keys, head_text, head, sig, now, out, rejected)
        if "link_ack" in head or "link_refused" in head:
            return _link_answer(bus_state, keys, head_text, head, sig, now, out, rejected)
        return _intro(bus_state, keys, head_text, head, sig, now, out, rejected)
    fp, mid = head["from"], head["id"]
    name, contact = busstate.by_fp(bus_state, fp)
    if contact is None:
        return rejected("unknown_hub", hub=fp)
    if contact.get("status") in ("pending", "blocked"):   # before the signature: `named`, not `contact` — nothing counted
        return rejected("blocked", hub=fp, named=name, status=contact["status"])
    if head["to"] != keys.fp or not verify(_unb64(contact["sign_pub"], 32), head_text.encode("utf-8"), sig):
        return rejected("bad_signature", hub=fp)
    cls = head.get("cls")
    seen = {"hub": fp, "contact": name, "id": mid, "exp": head["exp"], **({"cls": cls} if cls else {})}
    if head["ts"] > now + AHEAD or head["exp"] <= now:
        return rejected("stale", **seen)
    entry = (bus_state.get("seen") or {}).get(busstate.seen_key(fp, mid))
    if entry is not None:
        if entry.get("kind") in ("receipt", "ctl"):
            return rejected("dup", **seen)
        again = now - entry.get("receipted", 0) >= RECEIPT_EVERY
        reply = seal(keys, contact, {**_receipt_of(entry), "of": mid}, now, cls=entry.get("cls"))[1] if again else None
        return {**rejected("dup", **seen, receipted=again), "reply": reply}
    if contact.get("quarantine"):
        return rejected("quarantine", **seen)
    try:
        inner = _inner(open_box(keys.lib, keys.enc_priv, _unb64(head["ct"]), info_of(head)), v=head["v"])
    except Error as e:
        return rejected(e.code if e.code == "unsupported" else "bad_box", **seen)
    if cls and (cls not in ("chat", "liaison", "line") if inner["kind"] == "receipt" else class_of(inner) != cls):
        return rejected("bad_cls", **seen)   # counted against the class the head named
    if inner["kind"] == "receipt":
        return out("receipt", ("core.bus_delivered", {**seen, "of": inner["of"], "status": inner["status"],
                                                      **({"code": inner["code"]} if "code" in inner else {})}))
    if inner["kind"] == "unlink":   # the other owner unlinked this very link (an old link's unlink removes nothing)
        if not contact.get("pair") or inner["pair_id"] != contact["pair"]:
            return rejected("stale_unlink", **seen)
        return out("unlinked", ("core.bus_contact_removed", {
            "name": name, "unlinked": {"fp": fp, "pair": inner["pair_id"], "label": busstate.shown(contact), "id": mid}}))
    if inner["kind"] == "card_update":   # the catalog and a new name: shown once the owner checked the pair (busstate)
        return out("card", ("core.bus_card_updated", {**seen, "label": inner["label"], "agents": inner["agents"]}))
    owner = inner.get("for") == "owner"
    muted = contact.get("status") == "muted"
    finding = None if muted else precheck(inner["text"]) or precheck(inner["label"])
    # the owners' chat of a hub of the previous release (`for: owner`) is, here, an ordinary message of that hub to our
    # liaison — «Tell the hub» from that side: held until the owner checks the code, like any message for the agents
    status = "muted" if muted else "unverified" if not contact.get("verified") else "ok"   # a finding is the liaison's to weigh
    data = {**seen, "ts": head["ts"], "status": status, "kind": "message", "size": size,
            **{k: inner[k] for k in ("agent_from", "agent_to", "label", "thread", "in_reply_to", "seq", "text")},
            "marker": marker_for(inner["text"]), **({"finding": finding} if finding else {}),
            **({"for": "owner"} if owner else {})}
    receipt = {"kind": "receipt", "of": mid, "status": "held" if status == "unverified" else "ok"}
    reply = seal(keys, contact, receipt, now, cls=cls)[1]
    # the fact keeps its own type: an older core (a rollback) refuses it whole instead of folding it the old way
    return out(status, ("core.bus_chat_received" if owner else "core.bus_received", data), reply)


def marker_for(text):
    """The random fence around another hub's words in the core's frame (core/liaison.fence): a text that holds the
    marker cannot close the fence — a marker found in it is drawn again."""
    while True:
        m = "bus-" + secrets.token_hex(4)
        if m not in text:
            return m


def _intro(bus_state, keys, head_text, head, sig, now, out, rejected):
    """An introduction envelope (open by design: the card, signed by its own key): checked like a card; one card to the
    owner per fingerprint a day; nothing for a hub that is already a contact or for our own card."""
    from core import busstate
    c = head["card"]
    try:
        sign_pub, enc_pub = _unb64(c["sign_pub"], 32), _unb64(c["enc_pub"], 32)
    except Error:
        return rejected("bad_format")
    fp = fingerprint(sign_pub, enc_pub)
    if fp != keys.fp and busstate.by_fp(bus_state, fp)[1] is None and fp not in (bus_state.get("intros") or {}) \
            and sum(1 for r in (bus_state.get("intros") or {}).values() if r.get("card")) >= INTRO_CARDS:
        return rejected("rate", hub=fp, limit="intro_cards")   # before the check: the owner's list is full
    if not verify(sign_pub, head_text.encode("utf-8"), sig):
        return rejected("bad_signature", hub=fp)
    if head["ts"] > now + AHEAD or head["exp"] <= now:
        return rejected("stale", hub=fp)
    if fp == keys.fp or busstate.by_fp(bus_state, fp)[1] is not None:
        return out("intro_known")
    seen = ((bus_state.get("intros") or {}).get(fp) or {}).get("ts")
    if seen is not None and now - seen < 86400:
        return out("intro_known")
    info = {"fp": fp, "code": code(fp), "label": c["label"], "sign_pub": c["sign_pub"], "enc_pub": c["enc_pub"],
            "relays": c["relays"], "agents": c["agents"], "bus_v": c["bus_v"]}
    return out("intro", ("core.bus_intro", info))


def _card_info(fp, c):
    return {"fp": fp, "code": code(fp), "label": c["label"], "sign_pub": c["sign_pub"], "enc_pub": c["enc_pub"],
            "relays": c["relays"], "agents": c["agents"], "bus_v": c["bus_v"]}


def _link_intro(bus_state, keys, head_text, head, sig, now, out, rejected):
    """B came by a link of ours (spec hubbus/ux §4.2 steps 6–7): the link is ours and its one-time key signed this very
    envelope for this pair, and our clock at receipt is within the term (B's `ts` proves the author, not the time).
    An unknown link, a wrong key, our own card, a blocked hub — nothing goes back; a revoked, used or expired link —
    a signed refusal; the link taken — one fact: the link used and the contact (the table «an existing contact»)."""
    from core import busstate
    c, lk = head["card"], head["link"]
    try:
        sign_pub, enc_pub = _unb64(c["sign_pub"], 32), _unb64(c["enc_pub"], 32)
    except Error:
        return rejected("bad_format")
    fp = fingerprint(sign_pub, enc_pub)
    rec = (bus_state.get("links") or {}).get(lk["id"])
    if rec is None or not rec.get("nonce"):   # before any check: a stranger guessing ids costs one look into the state
        return rejected("bad_link", hub=fp)
    if not verify(sign_pub, head_text.encode("utf-8"), sig):
        return rejected("bad_signature", hub=fp)
    if head["ts"] > now + AHEAD or head["exp"] <= now:
        return rejected("stale", hub=fp)
    if not link_proof_ok(rec.get("pub"), lk["sig"], keys.fp, fp, head["id"], keys.lib):
        return rejected("bad_link", hub=fp, link=lk["id"])
    if fp == keys.fp:
        return rejected("own_link", hub=fp, link=lk["id"])
    answer = {"id": lk["id"], "fp": fp, "intro": head["id"]}
    state = rec.get("state")
    if state == "used":
        # the same introduction again (its answer was lost) — the same ack once more; another introduction even of the
        # same hub would be a second nonce of B against the nonce A has revealed: the code is of the first, once
        if rec.get("used_fp") == fp and rec.get("intro") == head["id"]:
            return out("link_again", ("core.bus_link_ack_wanted", answer))
        return out("link_refused", ("core.bus_link_refused", {**answer, "code": "used"}))
    if state == "revoked":
        return out("link_refused", ("core.bus_link_refused", {**answer, "code": "revoked"}))
    if state != "open" or now > rec.get("exp", 0):
        return out("link_refused", ("core.bus_link_refused", {**answer, "code": "expired"}))
    name, contact = busstate.by_fp(bus_state, fp)
    if contact is not None and contact.get("status") == "blocked":   # the owner's block is stronger than a link
        return rejected("blocked", hub=fp, named=name, link=lk["id"])
    if contact is None:
        taken = set(bus_state.get("contacts") or {}) | {r.get("name") for r in (bus_state.get("intros") or {}).values()
                                                      if r.get("card")}
        name = local_name(c["label"], taken, fp, bus_state.get("gone"))
    sas = sas_code(keys.fp, fp, lk["id"], rec["nonce"], head["id"])
    return out("link_used", ("core.bus_link_used", {**answer, "name": name, "card": _card_info(fp, c), "sas": sas}))


def _link_answer(bus_state, keys, head_text, head, sig, now, out, rejected):
    """A's answer to our introduction by its link: signed by the key we pinned from the link, for our own join, our
    fingerprint and — for an ack — our own introduction (another hub's ack does not count for us)."""
    from core import busstate
    field = "link_ack" if "link_ack" in head else "link_refused"
    lk, c = head[field], head["card"]
    try:
        fp = fingerprint(_unb64(c["sign_pub"], 32), _unb64(c["enc_pub"], 32))
    except Error:
        return rejected("bad_format")
    name, contact = busstate.by_fp(bus_state, fp)
    j = (bus_state.get("joins") or {}).get(lk["id"])
    if contact is None or j is None or j.get("fp_a") != fp or lk["fpB"] != keys.fp:
        return rejected("bad_link", hub=fp)
    if not verify(_unb64(contact["sign_pub"], 32), head_text.encode("utf-8"), sig):
        return rejected("bad_signature", hub=fp)
    if head["ts"] > now + AHEAD or head["exp"] <= now:
        return rejected("stale", hub=fp)
    # an ack opens the commitment of the link: the id of this envelope is the nonce A committed to before it saw ours
    if field == "link_ack" and (lk["intro_id"] != j.get("intro_id") or not j.get("commit")
                                or _b64url(commit(lk["id"], head["id"])) != j["commit"]):
        return rejected("bad_link", hub=fp, link=lk["id"])
    if j.get("state") not in busstate.JOIN_OPEN:   # linked, refused, cancelled, forgotten: nothing more to take
        return out("link_known")
    if field == "link_ack":
        sas = sas_code(fp, keys.fp, lk["id"], head["id"], j["intro_id"])
        return out("linked", ("core.bus_join_step", {"id": lk["id"], "step": "linked", "hub": fp, "sas": sas}))
    return out("link_refused", ("core.bus_join_step", {"id": lk["id"], "step": "refused", "code": lk["code"], "hub": fp}))


def local_name(label, taken, fp="", gone=None):
    """A local name for a hub that introduced itself: the one it had before the owner removed it (`gone`, by its
    fingerprint — the hub is the same, its label may have changed since), else Latin letters and digits of its label,
    else `hub-` and the start of its fingerprint (a label in another script); free."""
    if isinstance(gone, dict) and gone.get(fp) and gone[fp] not in taken:
        return gone[fp]
    base = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", str(label)).encode("ascii", "ignore").decode()
                  .lower()).strip("-")[:24]
    if len(base) < 2 or not re.match(r"[a-z0-9]", base):
        base = "hub-" + (fp[4:8] if isinstance(fp, str) and FP.fullmatch(fp) else "x")
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    return name


def intro_envelope(keys, label, now, agents=None, relays=()):
    """Our own card as an envelope for a transport (the same signed `intro`, without the text prefix)."""
    text = make_card(keys, label, now, relays=relays, agents=agents)
    return _unb64(text[len(CARD_PREFIX):])


def message_inner(bus_state, o):
    """The inner layer of an outgoing message from its record: who wrote it (the source the core knows, or the
    liaison), the hint, the thread and the answer to the last incoming envelope of that thread."""
    thread = o.get("thread")
    last = None
    if thread:
        items = [x for x in (bus_state.get("inbox") or {}).values()
                 if x.get("thread") == thread and x.get("contact") == o.get("contact")]
        last = max(items, key=lambda x: x.get("ts", 0))["id"] if items else None
    th = (bus_state.get("threads") or {}).get(thread or "") or {}
    who = "owner" if o.get("press") else o["source"] if o.get("source") not in ("owner", None) else "liaison"
    return message(who, o["text"], agent_to=o.get("agent_to"), label=(bus_state.get("me") or {}).get("label") or "",
                   thread=thread, in_reply_to=last, seq=th.get("n", 0) + 1)


def receipt(keys, contact, of, status, now, code=None, cls=None):
    inner = {"kind": "receipt", "of": of, "status": status, **({"code": code} if code else {})}
    return seal(keys, contact, inner, now, cls=cls)


# --- the owner's requests: the daemon's decisions ------------------------------------------------------------------

def _failed(of, code, reason=""):
    return ("core.bus_request_failed", {"of": of, "code": code, "reason": str(reason)[:300]})


def ready_keys(state_dir=None, config_dir=None, make=False):
    """The hub's keys, checked: (keys, None) or (None, (code, reason)). make — create them if there are none."""
    try:
        lib = crypto(state_dir)
    except Unavailable as e:
        return None, (e.code, str(e))
    folder = key_dir(config_dir)
    try:
        try:
            keys = load(folder, state_dir)
        except Error as e:
            if e.code != "no_keys" or not make:
                raise
            keys = generate(folder, state_dir)
    except Error as e:
        return None, (e.code, str(e))
    why = selfcheck(lib) or ssh_check(keys)
    if why:
        return None, ("selfcheck", why)
    return keys, None


def decide(action, bus_state, state_dir, now, config_dir=None):
    """The facts the core writes on the owner's request (core/busstate: bus.on_requested, bus.contact_requested)."""
    of = action.get("of")
    if action["do"] == "bus_on":
        keys, why = ready_keys(state_dir, config_dir, make=True)
        # `hub bus reset` was cut before its files went: whole keys of a retired fingerprint, or a set the cut left
        # incomplete while the core knows no identity at all — never taken back, made anew
        left = (not why and keys.fp in (bus_state.get("retired") or [])) or (
            why and why[0] == "keys_broken" and _sign_pub(key_dir(config_dir)) in (bus_state.get("retired_sign") or []))
        if left:
            retire_keys(key_dir(config_dir))
            keys, why = ready_keys(state_dir, config_dir, make=True)
        if why:
            return [_failed(of, *why)]
        me = bus_state.get("me") or {}
        if me.get("fp") and me["fp"] != keys.fp:
            return [_failed(of, "key_changed", f"the keys on disk are {keys.fp}, the core knows {me['fp']}")]
        label = action.get("label") or bus_state.get("name") or me.get("label")
        return [("core.bus_enabled", {**enabled_fact(keys, keys.lib, label), "of": of})]
    contacts = bus_state.get("contacts") or {}
    op, name = action.get("op"), action.get("name")
    if op == "intro":
        rec = (bus_state.get("intros") or {}).get(action.get("fp") or "")
        if rec is None:
            return [_failed(of, "no_contact", action.get("fp"))]
        if name in contacts:   # taken since the card was made: the next free one, the owner sees it in the list
            name = local_name(rec.get("label"), set(contacts), rec.get("fp"), bus_state.get("gone"))
        status = action.get("status")
        # the owner pressed "the code is checked — accept": the code came in this very card, from the envelope's key
        fact = {"name": name, **{k: rec.get(k) for k in ("fp", "label", "sign_pub", "enc_pub", "relays", "agents", "bus_v")},
                "status": status, "verified": status == "known", "of": of}
        return [("core.bus_contact_added", fact)]
    if op == "link":
        if name not in contacts:
            return [_failed(of, "no_contact", name)]
        return [("core.bus_contact_linked", {"name": name, "person": action.get("person"), "of": of})]
    if op == "add":
        try:
            info = read_card(action.get("card") or "", now)
        except Error as e:
            return [_failed(of, e.code, e)]
        if info["fp"] == (bus_state.get("me") or {}).get("fp"):
            return [_failed(of, "own_card")]
        from core import busstate
        known = busstate.by_fp(bus_state, info["fp"])[0]
        if known:
            return [_failed(of, "known_hub", known)]
        if name in contacts:
            return [_failed(of, "name_taken", name)]
        given = action.get("code")
        if given and _digits(given) != _digits(info["code"]):
            return [_failed(of, "code_mismatch")]
        status = "known" if given else "pending"
        return [("core.bus_contact_added", {**contact_fact(info, name, status, verified=bool(given)), "of": of})]
    contact = contacts.get(name)
    if contact is None:
        return [_failed(of, "no_contact", name)]
    from core import busstate
    # a code the owner typed: the code of the pair (both pages show it, once its link is done on this side) or, the
    # old way, the code of the other hub
    codes = tuple(_digits(x) for x in (contact_code(contact), code(contact["fp"])) if x)
    if op == "remove":
        facts = []
        if contact.get("pair") and busstate.effective_v(bus_state, contact) >= V2:
            keys, why = ready_keys(state_dir, config_dir)   # best effort: the contact goes whatever happens to the notice
            if not why:
                mid, raw = seal(keys, contact, {"kind": "unlink", "pair_id": contact["pair"]}, now, cls="ctl")
                facts.append(("core.bus_out", out_fact("unlink", name, contact["fp"], mid, raw)))
        return facts + [("core.bus_contact_removed", {"name": name, "of": of})]
    if op == "verify":   # «Matches»: the owner compared the pair code on both screens; a typed code is checked too
        given = action.get("code")
        if not given and contact_code(contact) is None:   # no pair code on both pages (the old way, not done): typed
            return [_failed(of, "code_needed")]
        if given and _digits(given) not in codes:
            return [_wrong_code(of, bus_state, contact, given)]
        return [("core.bus_contact_set", {"name": name, "status": contact.get("status"), "of": of, "decided": False,
                                          "verified": True})]
    status, given = action.get("status") or contact.get("status"), action.get("code")
    if status == "known" and not contact.get("verified"):
        if not given:
            return [_failed(of, "code_needed")]
        if _digits(given) not in codes:
            return [_wrong_code(of, bus_state, contact, given)]
    return [("core.bus_contact_set", {"name": name, "status": status, "of": of, "decided": bool(action.get("status")),
                                      **({"verified": True} if status == "known" else {})})]


def _wrong_code(of, bus_state, contact, given):
    """The refusal for a typed code that is none of the codes of this contact. A pair code (6 digits) typed while this
    side still waits for it is not «wrong»: the other page shows it before this one does (it comes with the other
    hub's answer) — the hub's own code (20 digits) is what is typed until then."""
    from core import busstate
    pair = re.fullmatch(r"\s*\d{3}[\s-]?\d{3}\s*", str(given)) and contact_code(contact) is None \
        and busstate.waiting_code(bus_state, contact)
    return _failed(of, "pair_code_unknown" if pair else "code_mismatch")


def out_fact(kind, name, fp, mid, raw, **extra):
    """core.bus_out of a one-shot envelope (an introduction, a receipt, an unlink, a card update): handed — done."""
    head = json.loads(json.loads(raw)["head"])
    return {"out": f"{kind[0]}{mid[:16]}", "kind": kind, "id": mid, "contact": name, "fp": fp, "exp": head["exp"],
            "size": len(raw), "envelope": base64.b64encode(raw).decode("ascii"), **extra}


def _digits(text):
    return re.sub(r"\D", "", str(text))


def start_check(bus_state, state_dir, config_dir=None):
    """At the core's start with the bus on: the library, the keys and the self-check again. Broken — core.bus_key_failed
    (a transport does not start on crypto_ready = false); restored — core.bus_enabled again."""
    if not bus_state.get("enabled"):
        return []
    keys, why = ready_keys(state_dir, config_dir)
    me = bus_state.get("me") or {}
    if not why and me.get("fp") != keys.fp:
        why = ("key_changed", f"the keys on disk are {keys.fp}, the core knows {me.get('fp')}")
    if why:
        same = bus_state.get("crypto_ready") is False and (bus_state.get("failed") or {}).get("code") == why[0]
        return [] if same else [("core.bus_key_failed", {"code": why[0], "reason": str(why[1])[:300]})]
    if bus_state.get("crypto_ready"):
        return []
    return [("core.bus_enabled", enabled_fact(keys, keys.lib, me.get("label")))]
