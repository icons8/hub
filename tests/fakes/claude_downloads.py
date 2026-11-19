"""A fake of Anthropic's Claude Code downloads for the tests of core/claudeinstall.py: a local http server with
`claude-code-releases/<channel>`, `<version>/manifest.json(.sig)`, `<version>/<platform>/claude[.exe]` and
`keys/claude-code.asc`, plus a release key of its own (pure Python RSA, deterministic) that signs the manifests the
way Anthropic's gpg does: a v4 RSA/SHA-512 detached signature over a binary document.

Knobs of the server (attributes, changeable between requests): `pointer` — the body of the channel files,
`pointer_status`, `manifest`/`signature` bytes, `binary`, `drop_after` — close the connection after so many bytes of
the binary (once per entry of the list), `fail_with` — statuses instead of the binary, `ranges` — honour Range; `requests` — (path, Range header) of each request."""
import hashlib
import json
import os
import random
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REAL = os.path.join(HERE, "claude_release")   # Anthropic's own key, manifest 2.1.309 and its signature (2026-11-01)


def _probable_prime(rng, bits):
    small = [p for p in range(3, 2000, 2) if all(p % d for d in range(3, int(p ** 0.5) + 1, 2))]
    while True:
        n = rng.getrandbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if any(n % p == 0 for p in small):
            continue
        d, r = n - 1, 0
        while d % 2 == 0:
            d, r = d // 2, r + 1
        for _ in range(24):
            x = pow(rng.randrange(2, n - 1), d, n)
            if x in (1, n - 1):
                continue
            for _ in range(r - 1):
                x = pow(x, 2, n)
                if x == n - 1:
                    break
            else:
                break
        else:
            return n


def _mpi(x):
    return struct.pack(">H", x.bit_length()) + x.to_bytes((x.bit_length() + 7) // 8, "big")


def _packet(tag, body):
    if len(body) < 192:
        head = bytes([len(body)])
    elif len(body) < 8384:
        n = len(body) - 192
        head = bytes([(n >> 8) + 192, n & 0xFF])
    else:
        head = b"\xff" + struct.pack(">I", len(body))
    return bytes([0xC0 | tag]) + head + body


def _sub(kind, value, critical=False):
    body = bytes([kind | (0x80 if critical else 0)]) + value
    return bytes([len(body)]) + body


class Key:
    """An RSA release key: `armored()` — the public key block, `sign(data)` — a detached binary signature."""
    _cache = {}

    def __init__(self, seed=1, bits=2048):
        if (seed, bits) not in Key._cache:
            rng = random.Random(seed)
            e = 65537
            while True:
                p, q = _probable_prime(rng, bits // 2), _probable_prime(rng, bits // 2)
                phi = (p - 1) * (q - 1)
                if p != q and phi % e:
                    break
            Key._cache[(seed, bits)] = (p * q, e, pow(e, -1, phi))
        self.n, self.e, self.d = Key._cache[(seed, bits)]
        self.body = b"\x04" + struct.pack(">I", 1700000000) + b"\x01" + _mpi(self.n) + _mpi(self.e)
        self.fingerprint = hashlib.sha1(b"\x99" + struct.pack(">H", len(self.body)) + self.body).hexdigest().upper()

    def armored(self):
        import base64
        raw = _packet(6, self.body) + _packet(13, b"Test Release Signing <test@example.test>")
        text = base64.b64encode(raw).decode()
        lines = "\n".join(text[i:i + 64] for i in range(0, len(text), 64))
        return f"-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n{lines}\n-----END PGP PUBLIC KEY BLOCK-----\n".encode()

    def sign(self, data, hash_algo=10, sigtype=0, created=None, expires=None, critical=None):
        name = {8: "sha256", 10: "sha512"}[hash_algo]
        prefix = {"sha256": "3031300d060960864801650304020105000420",
                  "sha512": "3051300d060960864801650304020305000440"}[name]
        hashed = _sub(2, struct.pack(">I", int(created if created is not None else time.time()) - 60))
        hashed += _sub(33, b"\x04" + bytes.fromhex(self.fingerprint))
        if expires is not None:
            hashed += _sub(3, struct.pack(">I", expires))
        if critical is not None:
            hashed += _sub(critical, b"\x00", critical=True)
        head = bytes([4, sigtype, 1, hash_algo]) + struct.pack(">H", len(hashed)) + hashed
        h = hashlib.new(name)
        h.update(data)
        h.update(head)
        h.update(b"\x04\xff" + struct.pack(">I", len(head)))
        digest = h.digest()
        t = bytes.fromhex(prefix) + digest
        k = (self.n.bit_length() + 7) // 8
        em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
        s = pow(int.from_bytes(em, "big"), self.d, self.n)
        unhashed = _sub(16, bytes.fromhex(self.fingerprint)[-8:])
        body = head + struct.pack(">H", len(unhashed)) + unhashed + digest[:2] + _mpi(s)
        return _packet(2, body)


def manifest(ver, binaries):
    """A manifest like Anthropic's: {platform: bytes of claude[.exe]}."""
    return json.dumps({"version": ver, "platforms": {
        plat: {"binary": "claude.exe" if plat.startswith("win32") else "claude",
               "checksum": hashlib.sha256(b).hexdigest(), "size": len(b)} for plat, b in binaries.items()}},
        indent=2).encode()


class Server:
    """The fake downloads at http://127.0.0.1:<port> (`url`), signed by `key`."""

    def __init__(self, key, ver="2.1.400", binaries=None, channel="stable"):
        self.key, self.ver = key, ver
        self.binaries = binaries or {}
        self.pointer, self.pointer_status, self.channel = ver.encode() + b"\n", 200, channel
        self.manifest = manifest(ver, self.binaries)
        self.signature = key.sign(self.manifest)
        self.key_file = key.armored()
        self.drop_after = []   # bytes of the binary after which a request is cut off, one entry per request
        self.fail_with = []    # statuses the binary requests get instead of the file (503…), one entry per request
        self.ranges = True
        self.requests = []
        self.lock = threading.Lock()
        server = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                server._get(self)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def resign(self):
        self.signature = self.key.sign(self.manifest)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _send(self, h, status, body, headers=()):
        h.send_response(status)
        for k, v in headers:
            h.send_header(k, v)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def _get(self, h):
        rng = h.headers.get("Range")
        with self.lock:
            self.requests.append((h.path, rng))
        base = "/claude-code-releases/"
        if h.path == "/keys/claude-code.asc":
            return self._send(h, 200, self.key_file)
        if h.path in (base + "stable", base + "latest"):
            return self._send(h, self.pointer_status, self.pointer)
        prefix = f"{base}{self.ver}/"
        if h.path == prefix + "manifest.json":
            return self._send(h, 200, self.manifest)
        if h.path == prefix + "manifest.json.sig":
            return self._send(h, 200, self.signature) if self.signature is not None else self._send(h, 404, b"")
        for plat, data in self.binaries.items():
            if h.path == f"{prefix}{plat}/{'claude.exe' if plat.startswith('win32') else 'claude'}":
                return self._binary(h, data, rng)
        return self._send(h, 404, b"<html>not found</html>")

    def _binary(self, h, data, rng):
        start = 0
        if rng and self.ranges and rng.startswith("bytes=") and rng.endswith("-"):
            start = int(rng[6:-1])
            if start >= len(data):
                return self._send(h, 416, b"")
        body = data[start:]
        with self.lock:
            cut = self.drop_after.pop(0) if self.drop_after else None
            failure = self.fail_with.pop(0) if self.fail_with else None
        if failure:
            return self._send(h, failure, b"<html>busy</html>")
        status = 206 if start else 200
        h.send_response(status)
        if start:
            h.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        if cut is None:
            h.wfile.write(body)
            return
        h.wfile.write(body[:cut])
        h.wfile.flush()
        h.close_connection = True
        try:
            import socket
            h.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


FAKE_CLAUDE = """import os, sys
VERSION = {version!r}
if sys.argv[1:2] == ["--version"]:
    home = os.environ.get("HOME") if os.name != "nt" else os.environ.get("USERPROFILE")
    with open(os.path.join(home, "ran-version.txt"), "a", encoding="utf-8") as f:
        f.write(os.path.abspath(sys.argv[0]) + "\\n")
    print(VERSION + " (Claude Code)")
    sys.exit(0)
if sys.argv[1:2] == ["install"]:
    home = os.environ.get("HOME") if os.name != "nt" else os.environ.get("USERPROFILE")
    dest = os.path.join(home, ".local", "bin")
    os.makedirs(dest, exist_ok=True)
    with open(os.path.abspath(sys.argv[0]), "rb") as f:   # bytes: a text copy turns \\n into \\r\\n on Windows
        me = f.read()
    name = "claude.exe" if os.name == "nt" else "claude"
    put = os.path.join(dest, name)
    if os.environ.get("FAKE_INSTALL_VERSIONS"):   # as the real one on POSIX: the build in versions, the command a link to it
        put = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share"), "claude",
                           "versions", VERSION)
        os.makedirs(os.path.dirname(put), exist_ok=True)
    if os.environ.get("FAKE_INSTALL_TOUCH"):      # another program rewrites this file meanwhile (npm's own update)
        with open(os.environ["FAKE_INSTALL_TOUCH"], "a", encoding="utf-8") as f:
            f.write("updated by npm\\n")
    if os.environ.get("FAKE_INSTALL_LINK"):       # the command left a link to this file
        if os.path.lexists(os.path.join(dest, name)):
            os.remove(os.path.join(dest, name))
        os.symlink(os.environ["FAKE_INSTALL_LINK"], os.path.join(dest, name))
    elif not os.environ.get("FAKE_INSTALL_SKIP"):   # SKIP: exit 0 and nothing put in place
        with open(put, "wb") as f:
            f.write(me + os.environ.get("FAKE_INSTALL_TAMPER", "").encode())
        os.chmod(put, 0o755)
        if put != os.path.join(dest, name):
            if os.path.lexists(os.path.join(dest, name)):
                os.remove(os.path.join(dest, name))
            os.symlink(put, os.path.join(dest, name))
    with open(os.path.join(home, "install-args.txt"), "a", encoding="utf-8") as f:
        f.write(" ".join(sys.argv[1:]) + "\\n")
    print("Setting up Claude Code... done")
    sys.exit(int(os.environ.get("FAKE_INSTALL_EXIT", "0")))
sys.exit(3)
"""


def fake_claude(version):
    """The bytes of a fake claude: Python with a shebang; `install` copies itself to ~/.local/bin, `--version` prints."""
    import sys
    return f"#!{sys.executable}\n".encode() + FAKE_CLAUDE.format(version=version).encode()
