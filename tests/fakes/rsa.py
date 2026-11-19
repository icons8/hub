"""A throwaway RSA key made in the test run (no key material in the repository) that signs RS256 JWTs: for the tests
of every check of a platform's signed token (Cloudflare Access)."""
import base64
import hashlib
import json
import secrets

SHA256_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def _probable_prime(n):
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d, r = d // 2, r + 1
    for _ in range(24):
        x = pow(secrets.randbelow(n - 3) + 2, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits):
    while True:
        c = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if _probable_prime(c):
            return c


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Key:
    def __init__(self, kid="k1"):
        e = 65537
        while True:
            p, q = _prime(512), _prime(512)
            phi = (p - 1) * (q - 1)
            if p != q and phi % e:
                break
        self.n, self.e, self.d, self.kid = p * q, e, pow(e, -1, phi), kid

    def jwk(self):
        enc = lambda i: b64(i.to_bytes((i.bit_length() + 7) // 8, "big"))  # noqa: E731
        return {"kty": "RSA", "kid": self.kid, "alg": "RS256", "n": enc(self.n), "e": enc(self.e)}

    def sign(self, claims, alg="RS256", kid=None):
        head = b64(json.dumps({"alg": alg, "kid": kid or self.kid, "typ": "JWT"}).encode())
        body = b64(json.dumps(claims).encode())
        k = (self.n.bit_length() + 7) // 8
        tail = SHA256_INFO + hashlib.sha256(f"{head}.{body}".encode()).digest()
        em = b"\x00\x01" + b"\xff" * (k - len(tail) - 3) + b"\x00" + tail
        return f"{head}.{body}.{b64(pow(int.from_bytes(em, 'big'), self.d, self.n).to_bytes(k, 'big'))}"
