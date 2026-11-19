"""RS256 JSON Web Tokens checked on stdlib: the signature only — the claims (issuer, audience, time) are the caller's.

RSA here only verifies a public signature: m = s^e mod n compared, whole, with the PKCS#1 v1.5 encoding of the SHA-256
digest built here (RFC 8017, 8.2.2). Nothing of the decrypted block is parsed: the signature forgeries against RSA
verifiers came from parsers of that block. Used by the cloudflare remote access provider (Cloudflare Access tokens,
modules/cloudflare/remote.py).
"""
import base64
import hashlib
import hmac
import json

SHA256_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def b64url(text):
    text = text.encode() if isinstance(text, str) else text
    return base64.urlsafe_b64decode(text + b"=" * (-len(text) % 4))


def rsa_ok(n, e, message, signature):
    """RSASSA-PKCS1-v1_5 with SHA-256."""
    k = (n.bit_length() + 7) // 8
    if len(signature) != k or k < 64:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    tail = SHA256_INFO + hashlib.sha256(message).digest()
    return hmac.compare_digest(em, b"\x00\x01" + b"\xff" * (k - len(tail) - 3) + b"\x00" + tail)


def parse(token):
    """-> (header, claims, signed part, signature) or None."""
    try:
        head, body, sig = str(token).split(".")
        out = json.loads(b64url(head)), json.loads(b64url(body)), f"{head}.{body}".encode(), b64url(sig)
    except (ValueError, TypeError):
        return None
    return out if isinstance(out[0], dict) and isinstance(out[1], dict) else None


def key_numbers(jwk):
    """(n, e) of an RSA JWK, or None."""
    try:
        return int.from_bytes(b64url(jwk["n"]), "big"), int.from_bytes(b64url(jwk["e"]), "big")
    except (KeyError, ValueError, TypeError, AttributeError):
        return None
