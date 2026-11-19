"""A signed, disposable checkpoint of the web's complete journal reader.

Unlike hub show's bounded recent reader, this retains every timeline and receipt
the live Tail owns. The journal remains authoritative; any mismatch replays it.
"""
import hashlib
import hmac
import os
import pickle
import sys
import tempfile
import time
from pathlib import Path

from core import i18n, journal, views

FORMAT = 2
# the file: HMAC-SHA256 of the rest, then MAGIC, the header length (4 bytes), the pickled header (format, code, language,
# journal identity, offset, prefix hash) and the pickled reader. The reader is pickled under the Tail's read lock (it is
# mutated under it); the journal prefix hash, the header and the write - outside it, so page requests do not wait on them
MAGIC = b"HRC2"
INTERVAL = 300.0
STORE = os.path.join("cache", "web-reader.pickle")


def code_signature():
    """All fold/view dependencies, catalogs and language data, not only the entry-point mtimes."""
    root = Path(__file__).resolve().parent.parent
    files = (sorted((root / "core").rglob("*.py")) + sorted((root / "i18n").glob("*.json"))
             + sorted((root / "i18n" / "data").glob("*.json")))
    if not files or not (root / "core" / "views.py").is_file():
        raise OSError("Reader sources unavailable")
    digest = hashlib.sha256(repr(sys.version_info[:3]).encode())
    for path in files:
        digest.update(str(path.relative_to(root)).replace(os.sep, "/").encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def prefix(path, offset, identity):
    """Validate the exact consumed bytes, including in-place edits and truncation."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        st = os.fstat(f.fileno())
        if (st.st_dev, st.st_ino) != identity or st.st_size < offset:
            raise ValueError("Different journal")
        remaining = offset
        while remaining:
            data = f.read(min(remaining, 1 << 20))
            if not data:
                raise ValueError("Truncated journal")
            digest.update(data)
            remaining -= len(data)
    return digest.digest()


class Checkpoint:
    def __init__(self, journal_path):
        self.path = journal_path
        self.folder = os.path.dirname(journal_path) or "."
        self.store = os.path.join(self.folder, STORE)
        self.next_write = 0.0
        try:
            self.code = code_signature()
        except OSError:
            self.code = None

    def restore(self):
        from core.tail import _cache_key
        try:
            # Most new hubs have no checkpoint: no key creation and no second source hash to discover that miss.
            # The hash in __init__ labels the code this process loaded; the one here - that the disk still holds it.
            with open(self.store, "rb") as f:
                signed = f.read()
            if self.code is None or code_signature() != self.code:
                return None
            secret = _cache_key(self.folder)
            # Authenticate before unpickling: a foreign file must never execute. A view, not a slice: no second copy.
            body = memoryview(signed)[32:]
            if not hmac.compare_digest(signed[:32], hmac.new(secret, body, hashlib.sha256).digest()):
                return None
            if body[:4] != MAGIC:
                return None
            size = int.from_bytes(body[4:8], "big")
            saved = pickle.loads(body[8:8 + size])
            if (saved["format"], saved["code"], saved["lang"]) != (FORMAT, self.code, i18n.current()):
                return None
            offset = saved["offset"]
            if type(offset) is not int or offset <= 0:
                return None
            if prefix(self.path, offset, saved["identity"]) != saved["prefix"]:
                return None
            reader = pickle.loads(body[8 + size:])
            if not isinstance(reader, views.Reader):
                return None
            # the restored point was just written: the next write - after the interval, not on the first new event
            self.next_write = time.monotonic() + INTERVAL
            return reader, offset, saved["identity"][1]
        except Exception:  # noqa: BLE001 - optional cache: missing, corrupt, old or unwritable
            return None

    def capture(self, reader, offset, ino):
        """Under the Tail's read lock: if a write is due - the pickled reader and what it consumed, for write; else None."""
        now = time.monotonic()
        if now < self.next_write:
            return None
        self.next_write = now + INTERVAL
        try:
            if self.code is None or offset <= 0:
                return None
            blob = pickle.dumps(reader, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as e:  # noqa: BLE001 - failure to cache must not stop the journal reader
            _failed(e)
            return None
        return blob, offset, ino, i18n.current()

    def write(self, job):
        """Outside the read lock: signs and writes what capture took."""
        from core.tail import _cache_key
        blob, offset, ino, lang = job
        tmp = None
        try:
            # A running process can outlive a source update. Never label its old
            # in-memory reader with the new source signature.
            if code_signature() != self.code:
                return
            st = os.stat(self.path)
            if st.st_ino != ino:
                return   # the journal was rotated meanwhile: the next write is under the new one
            identity = (st.st_dev, st.st_ino)
            try:
                consumed = prefix(self.path, offset, identity)
            except ValueError:
                return   # rotated or replaced since the stat: a skip like the one above, not a failure
            header = pickle.dumps({"format": FORMAT, "code": self.code, "lang": lang, "identity": identity,
                                   "offset": offset, "prefix": consumed}, protocol=pickle.HIGHEST_PROTOCOL)
            head = MAGIC + len(header).to_bytes(4, "big") + header
            mac = hmac.new(_cache_key(self.folder), head, hashlib.sha256)
            mac.update(blob)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.store), prefix=".web-reader.")
            with os.fdopen(fd, "wb") as f:
                f.write(mac.digest())
                f.write(head)
                f.write(blob)
            os.replace(tmp, self.store)
            tmp = None
            if journal.identity(self.path) != ino:   # rotated while writing (hub journal scrub removes the file after
                try:                                 # its rotation): do not leave the old journal's reader behind
                    os.unlink(self.store)
                except FileNotFoundError:
                    pass
        except Exception as e:  # noqa: BLE001 - failure to cache must not stop the journal reader
            _failed(e)
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


def _failed(e):
    # once per interval at most (capture moves next_write first): a line in the page log instead of a silent miss
    print(f"web: reader checkpoint not saved - {type(e).__name__}: {e}", file=sys.stderr, flush=True)
