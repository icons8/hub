"""The Authenticode signature of a Windows program (PE: .exe, .dll), read without Windows: what the signer wrote into it
and whether it signed what it was given (app spec §6.3, slice A8). The stdlib only.

- digest(path) — the Authenticode hash of the file: every byte except the PE checksum field, the certificate table's
  directory entry and the certificate table itself; an unsigned file is padded with zeros to 8 bytes, as a signer pads
  it before appending the table (the same hash osslsigncode and signtool compute).
- signature(path) — the one signature at the end of the file: its digest algorithm and the digest it claims (from the
  SpcIndirectDataContent of the PKCS#7 SignedData). Refused: no signature, a table that does not end the file (bytes
  after it are not covered by the hash), more than one entry, a form other than PKCS#7.
- same_program(unsigned, signed) — the signed file is the given one plus a signature: byte for byte the same outside
  the checksum, the directory entry and the zero padding before the table, and the claimed digest is the given file's.

Whether the signature itself is valid (the certificate chain, the publisher, the timestamp) is not decided here: that
is osslsigncode's or Windows' word (tools/package/sign.py, shellswap.py)."""
import hashlib
import os
import struct

HEAD = 1 << 16                 # the PE headers are read from the first 64 KiB
# DER of the digest algorithms' OIDs (sha1, sha256, sha384, sha512)
ALGOS = {bytes.fromhex("06052b0e03021a"): "sha1",
         bytes.fromhex("0609608648016503040201"): "sha256",
         bytes.fromhex("0609608648016503040202"): "sha384",
         bytes.fromhex("0609608648016503040203"): "sha512"}
SIGNED_DATA = bytes.fromhex("06092a864886f70d010702")          # 1.2.840.113549.1.7.2
SPC_INDIRECT = bytes.fromhex("060a2b060104018237020104")       # 1.3.6.1.4.1.311.2.1.4
CERT_REVISION, CERT_PKCS = 0x0200, 0x0002


class Bad(Exception):
    """Not a program of this form, or a signature that does not hold: the text says why."""


def layout(path):
    """(size, checksum offset, certificate directory offset, table offset, table size) of a PE file."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        b = f.read(HEAD)
    if b[:2] != b"MZ" or len(b) < 0x40:
        raise Bad(f"{os.path.basename(path)} is not a program (no MZ)")
    pe = struct.unpack_from("<I", b, 0x3C)[0]
    if pe + 24 > len(b) or b[pe:pe + 4] != b"PE\0\0":
        raise Bad(f"{os.path.basename(path)}: no PE header")
    opt_size = struct.unpack_from("<H", b, pe + 20)[0]
    opt = pe + 24
    if opt + 2 > len(b):
        raise Bad(f"{os.path.basename(path)}: the optional header is cut")
    magic = struct.unpack_from("<H", b, opt)[0]
    if magic not in (0x10B, 0x20B):
        raise Bad(f"{os.path.basename(path)}: optional header magic {magic:#x}")
    dirs = opt + (96 if magic == 0x10B else 112)
    cert = dirs + 4 * 8
    if cert + 8 > opt + opt_size or cert + 8 > len(b) or struct.unpack_from("<I", b, dirs - 4)[0] < 5:
        raise Bad(f"{os.path.basename(path)}: no certificate directory")
    at, length = struct.unpack_from("<II", b, cert)
    if length and (at < cert + 8 or at + length > size):
        raise Bad(f"{os.path.basename(path)}: the certificate table points outside the file")
    return size, opt + 64, cert, at, length


def _chunks(f, start, end):
    f.seek(start)
    left = end - start
    while left > 0:
        chunk = f.read(min(left, 1 << 20))
        if not chunk:
            raise Bad("the file ended early")
        left -= len(chunk)
        yield chunk


def digest(path, algo="sha256"):
    """The Authenticode hash of the file (hex)."""
    size, checksum, cert, at, length = layout(path)
    end = at if length else size
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for start, stop in ((0, checksum), (checksum + 4, cert), (cert + 8, end)):
            for chunk in _chunks(f, start, stop):
                h.update(chunk)
    if not length:
        h.update(b"\0" * (-size % 8))
    return h.hexdigest()


def _der(b, i):
    """(tag, start of the value, end of the value) of the DER element at i."""
    if i + 2 > len(b):
        raise Bad("the signature is cut")
    tag, n = b[i], b[i + 1]
    i += 2
    if n == 0x80:
        raise Bad("the signature is not DER (an indefinite length)")
    if n & 0x80:
        k = n & 0x7F
        if not 0 < k <= 4 or i + k > len(b):
            raise Bad("the signature has a broken length")
        n = int.from_bytes(b[i:i + k], "big")
        i += k
    if i + n > len(b):
        raise Bad("the signature is cut")
    return tag, i, i + n


def _items(b, start, end):
    """The DER elements between start and end: [(tag, raw start, value start, value end)]."""
    out = []
    while start < end:
        tag, s, e = _der(b, start)
        out.append((tag, start, s, e))
        start = e
    return out


def claimed(pkcs7):
    """(algorithm, hex digest) the SpcIndirectDataContent of a PKCS#7 SignedData claims."""
    tag, s, e = _der(pkcs7, 0)
    info = _items(pkcs7, s, e) if tag == 0x30 else []
    if len(info) < 2 or pkcs7[info[0][1]:info[0][3]] != SIGNED_DATA or info[1][0] != 0xA0:
        raise Bad("the signature is not a PKCS#7 SignedData")
    tag, s, e = _der(pkcs7, info[1][2])
    signed = _items(pkcs7, s, e) if tag == 0x30 else []
    if len(signed) < 3 or signed[2][0] != 0x30:
        raise Bad("the SignedData has no content")
    content = _items(pkcs7, signed[2][2], signed[2][3])
    if len(content) < 2 or pkcs7[content[0][1]:content[0][3]] != SPC_INDIRECT or content[1][0] != 0xA0:
        raise Bad("the signed content is not an Authenticode SpcIndirectDataContent")
    tag, s, e = _der(pkcs7, content[1][2])
    spc = _items(pkcs7, s, e) if tag == 0x30 else []
    if len(spc) != 2 or spc[1][0] != 0x30:
        raise Bad("the SpcIndirectDataContent has no digest")
    digest_info = _items(pkcs7, spc[1][2], spc[1][3])
    if len(digest_info) != 2 or digest_info[0][0] != 0x30 or digest_info[1][0] != 0x04:
        raise Bad("the SpcIndirectDataContent's digest is not a DigestInfo")
    alg = _items(pkcs7, digest_info[0][2], digest_info[0][3])
    algo = ALGOS.get(bytes(pkcs7[alg[0][1]:alg[0][3]])) if alg and alg[0][0] == 0x06 else None
    if algo is None:
        raise Bad("the signature's digest algorithm is not one of sha1/sha256/sha384/sha512")
    value = bytes(pkcs7[digest_info[1][2]:digest_info[1][3]])
    if len(value) != hashlib.new(algo).digest_size:
        raise Bad(f"the claimed {algo} digest is {len(value)} bytes")
    return algo, value.hex()


def signature(path):
    """(algorithm, claimed hex digest, table offset) of the file's one signature."""
    size, _, _, at, length = layout(path)
    name = os.path.basename(path)
    if not length:
        raise Bad(f"{name} is not signed")
    if at + length != size:
        raise Bad(f"{name}: {size - at - length} bytes after the certificate table (not covered by the signature)")
    if at % 8:
        raise Bad(f"{name}: the certificate table is not aligned to 8 bytes")
    with open(path, "rb") as f:
        f.seek(at)
        table = f.read(length)
    entry, revision, kind = struct.unpack_from("<IHH", table, 0) if len(table) >= 8 else (0, 0, 0)
    if not 8 < entry <= length or length - entry >= 8:
        raise Bad(f"{name}: the certificate table holds {'no' if entry <= 8 else 'more than one'} entry")
    if (revision, kind) != (CERT_REVISION, CERT_PKCS):
        raise Bad(f"{name}: a certificate entry of revision {revision:#x}, type {kind:#x} (not PKCS#7)")
    if any(table[entry:]):
        raise Bad(f"{name}: the padding after the signature is not zeros")
    algo, value = claimed(memoryview(table)[8:entry])
    return algo, value, at


def same_program(unsigned, signed, algo="sha256"):
    """None when signed is unsigned plus one signature over it; else Bad with the first difference."""
    usize, uchecksum, ucert, _, ulength = layout(unsigned)
    if ulength:
        raise Bad(f"{os.path.basename(unsigned)} is already signed")
    got, value, at = signature(signed)
    if got != algo:
        raise Bad(f"the signature's digest is {got}, not {algo}")
    _, checksum, cert, _, _ = layout(signed)
    if (checksum, cert) != (uchecksum, ucert):
        raise Bad("the signed file's headers are laid out differently")
    if not 0 <= at - usize < 8:
        raise Bad(f"the signed file is {at - usize:+d} bytes longer before its signature (only a zero padding of < 8 is "
                  f"a signer's)")
    skip = ((checksum, checksum + 4), (cert, cert + 8))
    with open(unsigned, "rb") as a, open(signed, "rb") as b:
        pos = 0
        while pos < usize:
            x, y = a.read(1 << 20), b.read(1 << 20)
            n = min(len(x), len(y))
            if n == 0:
                raise Bad("a file ended early")
            for s, e in skip:   # the two fields a signer writes: not compared
                lo, hi = max(s - pos, 0), min(e - pos, n)
                if lo < hi:
                    x = x[:lo] + y[lo:hi] + x[hi:]
            if x[:n] != y[:n]:
                off = next(i for i in range(n) if x[i] != y[i])
                raise Bad(f"the signed file differs from the given one at byte {pos + off}")
            pos += n
            a.seek(pos)
            b.seek(pos)
        b.seek(usize)
        if any(b.read(at - usize)):
            raise Bad("the padding before the signature is not zeros")
    mine = digest(unsigned, algo)
    if mine != value:
        raise Bad(f"the signature claims {algo} {value}, and the given file is {mine}")
    return None
