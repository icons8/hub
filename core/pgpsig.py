"""A detached OpenPGP signature check without gpg (RFC 4880 / 9580, the v4 subset Anthropic uses for Claude Code
releases): the key is trusted by its pinned fingerprint only, the signature must be RSA with SHA-2 over a binary
document. The stdlib only — the same on Mac, Linux and Windows, where gpg is often absent (installer spec §8).

What is not checked, by design: key expiry and revocation from self-signatures, subkey bindings, the web of trust.
The trust anchor is the fingerprint compiled into the caller; a rotated or revoked key comes with a new fingerprint
and a new hub release (docs/decisions.md, 2026-11-01)."""
import base64
import binascii
import hashlib
import time

RSA = (1, 3)   # RSA (Encrypt or Sign), RSA Sign-Only
HASHES = {8: "sha256", 9: "sha384", 10: "sha512", 11: "sha224"}
# DER of DigestInfo before the digest (RFC 8017 §9.2, note 1)
DIGEST_INFO = {
    "sha224": bytes.fromhex("302d300d06096086480165030402040500041c"),
    "sha256": bytes.fromhex("3031300d060960864801650304020105000420"),
    "sha384": bytes.fromhex("3041300d060960864801650304020205000430"),
    "sha512": bytes.fromhex("3051300d060960864801650304020305000440"),
}
MIN_BITS = 2048
SIG_CREATED, SIG_EXPIRES, ISSUER, ISSUER_FPR = 2, 3, 16, 33
KNOWN_CRITICAL = {SIG_CREATED, SIG_EXPIRES}   # a critical subpacket we do not understand makes the signature invalid


class Invalid(Exception):
    """The data is not a valid signature by the pinned key (the reason is in the text, in English: it goes to logs)."""


def dearmor(data):
    """ASCII armor -> bytes; binary data is returned as is. The CRC line, when present, is checked."""
    if not data.lstrip().startswith(b"-----BEGIN PGP"):
        return data
    lines = data.decode("ascii", "replace").splitlines()
    try:
        start = next(i for i, x in enumerate(lines) if x.startswith("-----BEGIN PGP"))
        end = next(i for i, x in enumerate(lines) if i > start and x.startswith("-----END PGP"))
    except StopIteration:
        raise Invalid("armor without BEGIN/END lines") from None
    body = lines[start + 1:end]
    while body and body[0].strip():   # armor headers (Version: …) up to the empty line
        if ":" not in body[0]:
            break
        body = body[1:]
    crc = None
    text = []
    for x in body:
        x = x.strip()
        if x.startswith("=") and len(x) == 5:
            crc = x[1:]
        elif x:
            text.append(x)
    try:
        raw = base64.b64decode("".join(text), validate=True)
    except (binascii.Error, ValueError):
        raise Invalid("armor is not base64") from None
    if crc is not None and base64.b64decode(crc) != _crc24(raw).to_bytes(3, "big"):
        raise Invalid("armor checksum mismatch")
    return raw


def _crc24(data):
    crc = 0xB704CE
    for b in data:
        crc ^= b << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return crc & 0xFFFFFF


def packets(data):
    """[(tag, body)] of a binary OpenPGP stream; partial body lengths are refused (keys and signatures never use them)."""
    out, i = [], 0
    while i < len(data):
        head = data[i]
        if not head & 0x80:
            raise Invalid(f"not an OpenPGP packet at byte {i}")
        if head & 0x40:   # new format
            tag, i = head & 0x3F, i + 1
            if i >= len(data):
                raise Invalid("truncated packet header")
            first = data[i]
            if first < 192:
                size, i = first, i + 1
            elif first < 224:
                if i + 1 >= len(data):
                    raise Invalid("truncated packet header")
                size, i = ((first - 192) << 8) + data[i + 1] + 192, i + 2
            elif first == 255:
                size, i = int.from_bytes(data[i + 1:i + 5], "big"), i + 5
            else:
                raise Invalid("partial body lengths are not supported")
        else:             # old format
            tag, kind, i = (head >> 2) & 0x0F, head & 0x03, i + 1
            if kind == 3:
                raise Invalid("indeterminate packet length")
            n = (1, 2, 4)[kind]
            size, i = int.from_bytes(data[i:i + n], "big"), i + n
        if i + size > len(data):
            raise Invalid("truncated packet")
        out.append((tag, data[i:i + size]))
        i += size
    return out


def _mpi(body, i):
    if i + 2 > len(body):
        raise Invalid("truncated MPI")
    bits = int.from_bytes(body[i:i + 2], "big")
    n = (bits + 7) // 8
    if i + 2 + n > len(body):
        raise Invalid("truncated MPI")
    return int.from_bytes(body[i + 2:i + 2 + n], "big"), i + 2 + n


def keys(data):
    """{fingerprint (upper hex): (n, e)} of the v4 RSA keys (primary and subkeys) in a key block, armored or binary."""
    out = {}
    for tag, body in packets(dearmor(data)):
        if tag not in (6, 14) or len(body) < 6 or body[0] != 4 or body[5] not in RSA:
            continue
        n, i = _mpi(body, 6)
        e, _ = _mpi(body, i)
        fpr = hashlib.sha1(b"\x99" + len(body).to_bytes(2, "big") + body).hexdigest().upper()
        out[fpr] = (n, e)
    return out


def _subpackets(raw):
    out, i = [], 0
    while i < len(raw):
        first = raw[i]
        if first < 192:
            size, i = first, i + 1
        elif first < 255:
            if i + 1 >= len(raw):
                raise Invalid("truncated subpacket")
            size, i = ((first - 192) << 8) + raw[i + 1] + 192, i + 2
        else:
            size, i = int.from_bytes(raw[i + 1:i + 5], "big"), i + 5
        if size < 1 or i + size > len(raw):
            raise Invalid("truncated subpacket")
        out.append((raw[i] & 0x7F, bool(raw[i] & 0x80), raw[i + 1:i + size]))
        i += size
    return out


def _check_one(body, message, n, e, now):
    if len(body) < 6 or body[0] != 4:
        raise Invalid("only version 4 signatures are supported")
    sigtype, algo, halgo = body[1], body[2], body[3]
    if sigtype != 0x00:
        raise Invalid(f"signature type {sigtype:#04x}, a binary document (0x00) is expected")
    if algo not in RSA:
        raise Invalid(f"public key algorithm {algo}, RSA is expected")
    if halgo not in HASHES:
        raise Invalid(f"hash algorithm {halgo} is not accepted")
    hlen = int.from_bytes(body[4:6], "big")
    hashed_end = 6 + hlen
    if hashed_end + 2 > len(body):
        raise Invalid("truncated signature")
    hashed = _subpackets(body[6:hashed_end])
    ulen = int.from_bytes(body[hashed_end:hashed_end + 2], "big")
    i = hashed_end + 2 + ulen
    if i + 2 > len(body):
        raise Invalid("truncated signature")
    left16, (s, _) = body[i:i + 2], _mpi(body, i + 2)
    created = life = None
    for kind, critical, value in hashed:
        if critical and kind not in KNOWN_CRITICAL:
            raise Invalid(f"unknown critical subpacket {kind}")
        if kind == SIG_CREATED and len(value) == 4:
            created = int.from_bytes(value, "big")
        if kind == SIG_EXPIRES and len(value) == 4:
            life = int.from_bytes(value, "big")
    if created is None:
        raise Invalid("no signature creation time")
    if life and created + life < now:
        raise Invalid("the signature has expired")
    name = HASHES[halgo]
    h = hashlib.new(name)
    h.update(message)
    h.update(body[:hashed_end])
    h.update(b"\x04\xff" + hashed_end.to_bytes(4, "big"))
    digest = h.digest()
    if digest[:2] != left16:
        raise Invalid("the signature does not match the data")
    k = (n.bit_length() + 7) // 8
    if not 0 < s < n:
        raise Invalid("the signature value is out of range")
    em = pow(s, e, n).to_bytes(k, "big")
    t = DIGEST_INFO[name] + digest
    if k < len(t) + 11 or em != b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t:
        raise Invalid("the signature does not match the data")


def verify(key_block, signature, message, fingerprint, now=None):
    """None when one of the signatures in `signature` (a detached signature, armored or binary) is a valid RSA/SHA-2
    signature of `message` by the key with `fingerprint` from `key_block`; otherwise Invalid with the reason."""
    want = fingerprint.replace(" ", "").upper()
    found = keys(key_block)
    if want not in found:
        raise Invalid("the key with the expected fingerprint is not in the key file")
    n, e = found[want]
    if n.bit_length() < MIN_BITS:
        raise Invalid(f"the key is {n.bit_length()} bits, at least {MIN_BITS} are required")
    sigs = [body for tag, body in packets(dearmor(signature)) if tag == 2]
    if not sigs:
        raise Invalid("no signature packet")
    reasons = []
    for body in sigs:
        try:
            _check_one(body, message, n, e, time.time() if now is None else now)
            return None
        except Invalid as err:
            reasons.append(str(err))
    raise Invalid("; ".join(reasons))
