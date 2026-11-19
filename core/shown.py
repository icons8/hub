"""What sessions showed the owner — what the page's /api/file serves besides attachments: a file sent by a session
(hub send-file, owner.file) of any type; an image whose path the session named in a step or text — images only.
Exactly these paths, not the folder around them.

Over the whole journal history, not the current one: rotation (journal.rotate) carries only the tail into the new
journal, and before 17.10 files sent earlier answered 404 (on a live hub, of 734 files in the history 237 opened). A
journal archive does not change: its shown set is parsed once — into a file next to it (<archive>.shown.json, in the
background at the first question; not in hubd: rotation runs on its turn), the current journal is read on. Parsing is
by bytes: lines without "owner.file" and without an image extension are not parsed (the whole journal read by the page
reader is hundreds of MB of memory, here only the paths themselves).

One rule for the page and for modules (hubmod.servable): what /api/file answers is what the index will say."""
import json
import os
import re
import sys
import threading
import time
from urllib.parse import unquote

import platforms
from core import journal

LIMIT = 20000   # paths per journal or archive; beyond that the oldest are forgotten
RETRY = 600     # an archive failed to parse — do not try again for this many seconds
SKIP = 64 << 10  # an unfinished line of another type longer than this is skipped, not accumulated
# an image by an absolute path in a session step or text; not svg: it can contain a script
EXT = re.compile(r"\.(?:png|jpe?g|gif|webp)$", re.I)
# an absolute path: from "/", "~" or with a Windows drive (C:\x, C:/x — otherwise no session image was served there)
DRIVE = re.compile(r"[A-Za-z]:[\\/]")
PAGE_DRIVE = re.compile(r"/?[A-Za-z]:[\\/]")   # a Windows drive as the page writes it: file:///C:/x is "/C:/x"
ABS = re.compile(r"(?:~|/|[A-Za-z]:[\\/])")
IMAGE = re.compile(r"(?:~|/|\b[A-Za-z]:[\\/])[^\s\"'`<>()\[\]{}|,;]*\.(?:png|jpe?g|gif|webp)(?!\w)", re.I)
TYPES = ("owner.file", "turn.tool", "turn.tool_result", "session.said")
SIDE = ".shown.json"
VERSION = 2
# a journal line where the rule can find anything: everything note finds contains one of these
_HIT = re.compile(rb'owner\.file|\.(?:png|jpe?g|gif|webp)', re.I)
_TYPE = re.compile(rb'"type":"([^"]{1,80})"')


def _strings(x):
    """Strings of a tool input (file_path, command…), each on its own: in json.dumps a newline is "\\n"."""
    if isinstance(x, str):
        return [x]
    if isinstance(x, dict):
        return [s for v in x.values() for s in _strings(v)]
    if isinstance(x, list):
        return [s for v in x for s in _strings(v)]
    return []


def found(kind, data):
    """What an event (accepted by the core) makes shown: [(path, "file" | "image")]."""
    if kind == "owner.file":
        return [(str(data["path"]), "file")] if data.get("path") else []
    if kind not in TYPES:
        return []
    texts = _strings(data.get("input")) if kind == "turn.tool" else [str(data.get("text") or "")]
    out = []
    for text in texts:
        text = text[:50000]   # long output (a read file): the path to a screenshot in it is closer to the start
        if EXT.search(text.strip()) and ABS.match(text.strip()):
            out.append((text.strip(), "image"))   # the whole path (file_path), spaces in the name included
        elif "." in text:
            # file:///x%20y.png is the same /x y.png the page will ask for
            out += [(_from_url(p) if p.startswith("/") else p, "image") for p in IMAGE.findall(text)]
    return out


def _from_url(p):
    """A path from "/…" in text: file:///x%20y.png is /x y.png; file:///C:/x.png and file://C:\\x.png keep the drive,
    without the "/"."""
    p = p.lstrip("/")
    return unquote(p if DRIVE.match(p) else "/" + p)


# an option's preview that is only a path to a picture: absolute, ~/ or file://; raster (and svg: only the page's Files shows it)
PREVIEW_PATH = re.compile(r"(?:file://)?(?:~|/|[A-Za-z]:[\\/])\S[^\n]*\.(?:png|jpe?g|gif|webp|svg)", re.I)


def preview_image(text):
    """An AskUserQuestion option's `preview` that is a path to a picture (one line: /abs/x.png, ~/x.png, file:///x.png) —
    the path as the disk knows it; any other text is a sketch, shown as it is (None)."""
    s = text.strip() if isinstance(text, str) else ""
    if len(s) > 1024 or not PREVIEW_PATH.fullmatch(s):
        return None
    if s.startswith("file://"):
        s = _from_url(s[7:])
    return os.path.expanduser(s)


def key(path):
    """The index key. A path with a Windows drive has one form, however the session wrote it or the page asked for it
    (file:///d:/x, "D:\\x", "/D:/x" is a URL path): "D:/x" — an uppercase drive letter, the separator "/", no leading
    "/". Others are as is."""
    if not PAGE_DRIVE.match(path):
        return path
    path = path.lstrip("/").replace("\\", "/")
    return path[0].upper() + path[1:]


def note(shown, kind, data):
    """Add to shown (path -> how) what the event showed: a sent file ("file") is stronger than a named one, new ones go
    to the end; beyond LIMIT the oldest are forgotten."""
    for path, how in found(kind, data if isinstance(data, dict) else {}):
        for p in {key(path), key(os.path.expanduser(path))}:
            how = "file" if "file" in (how, shown.pop(p, None)) else how
            shown[p] = how
    while len(shown) > LIMIT:
        shown.pop(next(iter(shown)))


def scan(buf, shown, limit=None, end=None):
    """Journal lines (the bytes of buf up to end — whole lines) into shown; limit — parse only those started before it
    (the next one after them is needed whole). What the core rejected is not shown: hubd writes core.rejected as the
    next line in the same write."""
    end = len(buf) if end is None else end
    limit = end if limit is None else limit
    pos = 0
    while pos < limit:
        m = _HIT.search(buf, pos, end)
        if not m:
            return
        start = buf.rfind(b"\n", 0, m.start()) + 1
        if start >= limit:
            return
        stop = buf.find(b"\n", m.end(), end)
        stop = end if stop < 0 else stop
        pos = stop + 1
        if _other(buf, start, stop):   # a state snapshot is megabytes: do not parse
            continue
        try:
            ev = json.loads(buf[start:stop])
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get("type") not in TYPES:
            continue
        cut = buf.find(b"\n", pos, end)
        nxt = buf[pos:end if cut < 0 else cut] if pos < end else b""
        if b'"core.rejected"' in nxt[:300]:   # the type is at the start of the line
            try:
                after = json.loads(nxt)
                if after.get("type") == "core.rejected" and (after.get("data") or {}).get("of") == ev.get("seq"):
                    continue
            except (ValueError, AttributeError):
                pass
        note(shown, ev["type"], ev.get("data"))


def _settled(buf):
    """Up to where in buf (whole lines and maybe a fragment of a write) lines are final: a fragment after the last
    whole line is the start of the same write, which may hold its rejection; it will wait."""
    end = buf.rfind(b"\n") + 1
    if end == len(buf):
        return end
    return buf.rfind(b"\n", 0, end - 1) + 1 if end else 0


def _kind(buf, start, stop):
    """The type of the line at start (up to stop) from its first bytes; not visible — None."""
    kind = _TYPE.search(buf, start, min(stop, start + 300))
    return kind and kind[1].decode("utf-8", "replace")


def _other(buf, start, stop):
    """The line at start (up to stop) is not from TYPES."""
    kind = _kind(buf, start, stop)
    return bool(kind) and kind not in TYPES


def _read(f, shown, chunk=1 << 20, whole=False):
    """What is shown in f from the current place to the end goes into shown, in portions; whole — the file is complete
    (an archive): the last line is final. A line of another type longer than a portion (a state snapshot, 15 MB) is not
    accumulated in memory: the type is visible at its start, the rest is skipped up to the end of the line. Returns
    (up to where parsing is final, how much was read) — in bytes from the starting place."""
    base, carry = 0, b""   # base is the place where carry starts
    while True:
        got = f.read(chunk)
        buf = carry + got
        last = buf.rfind(b"\n") + 1
        if not got:   # the end: in an archive a fragment is no event, in the journal it is a write's start and will wait
            settled = last if whole else _settled(buf)
            scan(buf, shown, settled, last)
            return base + settled, base + len(buf)
        kind = _kind(buf, last, len(buf)) if len(buf) - last > SKIP else None
        if kind and kind not in TYPES and kind != "core.rejected":
            # after the last whole one comes a line of another type and not a rejection: nothing to wait for, scan
            # does not look at the next line
            scan(buf, shown, last, last)
            start, base = base + last, base + len(buf)
            while True:
                got = f.read(chunk)
                nl = got.find(b"\n")
                if not got or nl >= 0:
                    break
                base += len(got)
            if not got:   # the line is still being written: parsed up to its start
                return start, base
            base += nl + 1
            carry = got[nl + 1:]
            continue
        settled = buf.rfind(b"\n", 0, last - 1) + 1 if last else 0   # the last whole one needs the next
        if settled:
            scan(buf, shown, settled, last)
        base += settled
        carry = buf[settled:]


def of_file(path, chunk=1 << 20):
    """What is shown in a journal file, whole, in portions: an archive can be hundreds of MB, the page lives long."""
    shown = {}
    with open(path, "rb") as f:
        _read(f, shown, chunk, whole=True)
    return shown


def write_side(archive, shown):
    """The archive parse goes into a file next to it (by replacement: a reader never sees half of it)."""
    tmp = f"{archive}{SIDE}.{os.getpid()}.{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"v": VERSION, "shown": shown}, f, ensure_ascii=False, separators=(",", ":"))
    platforms.current.replace(tmp, archive + SIDE)


def read_side(archive):
    """The archive parse from disk; none or another version — None."""
    try:
        with open(archive + SIDE, encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(got, dict) or got.get("v") != VERSION or not isinstance(got.get("shown"), dict):
        return None
    return {str(k): v for k, v in got["shown"].items() if v in ("file", "image")}


def side_of(archive):
    """The archive parse: from disk, otherwise parse and write (if the write fails, it is still an answer)."""
    got = read_side(archive)
    if got is None:
        got = of_file(archive)
        try:
            write_side(archive, got)
        except OSError:
            pass
    return got


class Index:
    """What is shown over the whole history of the state_dir journal: archives by their parses, the current journal by
    reading on in portions. Thread-safe; unparsed archives are parsed in the background in this process (in portions:
    the memory peak is one portion), the first time after an update takes tens of seconds. What was read from the
    journal before rotation stays until its archive is parsed."""

    def __init__(self, state_dir, background=True):
        self.state_dir = state_dir
        self.path = os.path.join(state_dir, "journal.jsonl")
        self.attachments = os.path.join(state_dir, "attachments")   # as modules/web/attach.root
        self.background = background
        self.lock, self.build_lock = threading.Lock(), threading.RLock()
        self.parts = {}         # an archive (or a journal read before rotation) -> its shown set; no eviction
        self.listed = None      # mtime of the archive folder at the last full listing
        self.failed = {}        # an archive -> when its parse failed: retry no earlier than RETRY
        self.building = None
        self.live, self.ino, self.offset, self.seen = {}, None, 0, -1   # the current journal: its shown set, offset read

    def _merge(self, name, got):
        with self.lock:
            self.parts.setdefault(name, got)

    def _archives(self):
        """New archives: a ready parse at once, unparsed ones in the background (or here if the background is off)."""
        folder = os.path.join(self.state_dir, journal.ARCHIVE)
        try:
            mtime = os.stat(folder).st_mtime_ns
        except OSError:
            return
        with self.build_lock:
            building = self.building and self.building.is_alive()
            with self.lock:
                failed = dict(self.failed)
            if mtime == self.listed and (building or all(time.time() - t < RETRY for t in failed.values())):
                return   # an archive that failed is retried after RETRY, even without a new archive in the folder
            missing, names = [], set()
            for path in journal.archives(self.state_dir):
                name = os.path.basename(path)
                names.add(name)
                if name in self.parts or time.time() - failed.get(name, 0) < RETRY:
                    continue
                got = read_side(path)
                if got is None:
                    missing.append(path)
                else:
                    self._merge(name, got)
            with self.lock:   # a parsed archive (parse written by another process) and a vanished one are no longer "failed"
                for name in [n for n in self.failed if n in self.parts or n not in names]:
                    del self.failed[name]
            self.listed = mtime   # the background will parse the missing ones; when it finishes — list again
            if not missing:
                if not self.failed and not (self.building and self.building.is_alive()):
                    with self.lock:   # all archives are parsed: what was read before rotations is in them
                        for k in [k for k in self.parts if k.startswith("journal@")]:
                            del self.parts[k]
                return
            if not self.background:
                self._build(missing)
                self.listed = None
            elif not (self.building and self.building.is_alive()):
                self.building = threading.Thread(target=self._build, args=(missing[::-1],), daemon=True)
                self.building.start()

    def _build(self, paths):
        for path in paths:   # from new to old: the freshest sent goes first
            try:
                self._merge(os.path.basename(path), side_of(path))
                with self.lock:
                    self.failed.pop(os.path.basename(path), None)   # the retry after RETRY worked — the index is whole again
            except Exception as e:  # noqa: BLE001 — one archive failed: the rest go on, the cause goes to the log
                with self.lock:
                    self.failed[os.path.basename(path)] = time.time()
                print(f"shown: archive {os.path.basename(path)} is not parsed — {e!r}", file=sys.stderr, flush=True)
        with self.build_lock:
            self.listed = None

    def _current(self, chunk=1 << 20):
        with self.lock:
            try:
                st = os.stat(self.path)
            except FileNotFoundError:
                return
            if st.st_ino != self.ino or st.st_size < self.offset:
                # the journal started anew (rotation): what was read stays until its archive is parsed;
                # truncated (repair) — start over
                if self.live and st.st_ino != self.ino:
                    self.parts[f"journal@{self.ino}"] = self.live
                self.live, self.ino, self.offset, self.seen = {}, st.st_ino, 0, -1
                self.listed = None
            if st.st_size == self.seen:   # nothing appended — do not read (the page asks once per path in a row)
                return
            try:
                with open(self.path, "rb") as f:
                    f.seek(self.offset)
                    done, read = _read(f, self.live, chunk)
            except FileNotFoundError:
                return
            self.seen = self.offset + read
            self.offset += done

    def refresh(self):
        self._current()
        self._archives()

    def follow(self, stop, every=1.0):
        """Keep reading the journal on the fly (the page): what was read before rotation stays while its archive is
        parsed in the background."""
        while not stop.is_set():
            try:
                self.refresh()
            except Exception as e:  # noqa: BLE001 — a failure does not bring the page down: the next question answers
                print(f"shown: {e!r}", file=sys.stderr, flush=True)
            stop.wait(every)

    def state(self):
        """Whether the index is complete: "ready" — all archives are parsed, no background run; "building" — the
        background is parsing the unparsed ones (their paths are None for now); "partial" — an archive parse failed
        (retry no earlier than RETRY), its paths are None."""
        self.refresh()
        if self.building and self.building.is_alive():
            return "building"
        with self.lock:
            return "partial" if self.failed else "ready"

    def how(self, path, fresh=True):
        """How a session showed the path (as asked and after realpath): "file", "image" or None. fresh=False — without
        reading the journal on (a batch of paths reads it once — servable_many)."""
        if fresh:
            self.refresh()
        real = os.path.realpath(platforms.current.page_path(path))
        with self.lock:
            got = [d.get(p) for d in (self.live, *self.parts.values()) for p in {key(path), key(real)}]
        return "file" if "file" in got else ("image" if any(got) else None)

    def shown(self, path, fresh=True):
        """What a session showed the owner (the real path) or None: a sent file, and for an image also one it named;
        the file exists on disk."""
        if not isinstance(path, str) or not path or "\0" in path:
            return None
        real = os.path.realpath(platforms.current.page_path(path))
        how = self.how(path, fresh)
        ok = how == "file" or (how and EXT.search(real))
        return real if ok and os.path.isfile(real) else None

    def servable(self, path, fresh=True):
        """The path /api/file serves (real, after realpath), or None: an owner attachment (attachments/) or one shown
        by a session (shown)."""
        if not isinstance(path, str) or not path or "\0" in path:
            return None
        real = os.path.realpath(platforms.current.page_path(path))
        root = os.path.realpath(self.attachments)
        if real.startswith(root + os.sep) and os.path.isfile(real):
            return real
        return self.shown(path, fresh)

    def servable_many(self, paths):
        """{path: servable(path)} — the journal is read on once per batch, not per path."""
        self.refresh()
        return {p: self.servable(p, fresh=False) for p in paths}
