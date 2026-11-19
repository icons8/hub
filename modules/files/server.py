"""The "Files" module: project folders in the browser, to view and fix a file (a session wrote a decisions doc, but the owner
had no way to open it). A separate process, the core does not know about it; the hub page and Telegram
only place links `/open?path=<path>[&cwd=<session folder>]`, and the path is looked up as in paths.py.

The folders are projects from hub.json (`projects`), their git worktrees (`<project>@<folder>`) and `files.roots` ({name: path}); nothing outside them (realpath, links
do not lead outside), `.git` is hidden. A request from the Mac itself (127.0.0.1, Host localhost) also gets the root "Mac" (/), the whole disk
(no need to check access to projects for a 127.0.0.1 entry); from the tailnet, projects only. Editing is checked: if the file changed since it was opened, 409, we do not overwrite.
Who can: requests on localhost; from the tailnet (it also listens on the Mac's tailnet IP, the tailnet module), only the Mac's owner
(modules/tailnet/access.py). Stdlib; markdown is rendered by the same marked + DOMPurify as the hub page.
HTML files as a page: /render/<root>/<path> in an iframe with CSP sandbox (without allow-same-origin, see RENDER_CSP).
"""
import gzip
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, quote, unquote, urlparse

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
import platforms  # noqa: E402
from core import catalog, coretext, delegations, i18n, journal, langdata, reducer, specmode, spool, views  # noqa: E402
from core.coretext import T  # noqa: E402
from core.reducer import NAME  # noqa: E402
from core.daemon import default_state_dir  # noqa: E402
from modules.files import paths  # noqa: E402
from modules.tailnet import access  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
WEB = os.path.join(REPO, "modules", "web")  # shared styles and vendor files of the hub page, read only
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
         ".woff2": "font/woff2"}
HIDDEN = {".git", "node_modules", "__pycache__", ".ds_store"}   # lower case: hidden() compares without case


def hidden(name):
    """A name Files never shows or opens. Without case: on a macOS disk a request for ".GIT" opens .git (realpath keeps
    the request's spelling there), and a folder may be named so on disk"""
    return name.lower() in HIDDEN


def _node(path):
    """(device, inode) of what a path opens, or None: no such path, or a disk that gives no inode numbers (0 would make
    every two paths "the same")."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return (st.st_dev, st.st_ino) if st.st_ino else None


def git_alias(base, real):
    """Whether a folder on the way from base to real is the .git folder next to it under another name. A disk may open
    one entry by several spellings, and realpath keeps the request's: case on macOS (hidden() covers it), and an old
    HFS+ disk ignores zero-width characters (".git" with one inside opens .git) — the name check cannot list those, the
    disk can. Only a name with a character beyond ASCII is compared: case is hidden()'s, and Windows' short and
    trailing-dot spellings realpath turns into the real name — so an ordinary path costs no stat."""
    cur = base
    for part in os.path.relpath(real, base).split(os.sep):
        if part in ("", os.curdir):
            continue
        nxt = os.path.join(cur, part)
        if not part.isascii():
            node = _node(nxt)
            if node and node == _node(os.path.join(cur, ".git")):
                return True
        cur = nxt
    return False


MAX_READ = 2 * 1024 * 1024
MAX_WRITE = 2 * 1024 * 1024
PORT = 8788
THEME_ID = re.compile(r"[\w:.-]{1,200}|system")   # a theme id as /api/themes gives it ("plugin:file.json")
WEB_PORT = 8787   # the hub page, unless HUB_WEB_PORT is set (a second hub has its own port); it may embed "Files"
WORKTREES_TTL = 30
PENDING_MAX = 2000   # events a thread poll may leave unfolded (track()); past it the state is folded
STATE_TTL = 3   # s: a request that only looks takes the state read this long ago (Files.state(stale_ok=True))
MAC = "Mac"  # the "whole disk" root, only for requests from the Mac itself
# root name in "Files" addresses (#/<PROOF>/...) and in links of earlier feeds, the word of the Russian texts (language
# data, core/langdata.py); hub shot and hub video write here, not into the session's repo: the picture from the feed is
# visible away from the Mac too
PROOF = langdata.legacy("proof_root")
SPECS = "specs"   # the root of executors' specs (core/specmode.DIR): <state>/specs
# /render: html and its neighbours as a page in an iframe ("Files" and the hub page: the page's origins follow
# frame-ancestors 'self'). sandbox without allow-same-origin: the document has an opaque origin, its
# scripts read neither the "Files" API (POST requires X-Hub, and the server does not approve CORS), nor localStorage, nor the module's cookie
RENDER_CSP = "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox allow-forms; frame-ancestors 'self'"
# report links open in a new tab, not inside the "Files" window (08.10); anchors #... and javascript: stay in place. The tab has no
# sandbox: a foreign site works as usual, and /render in it again comes under its own CSP sandbox; noopener so that an
# opened site cannot replace the report in the "Files" window via window.opener
RENDER_LINKS = (b'\n<script id="files-links">addEventListener("click", (e) => { const a = e.target.closest && '
                b'e.target.closest("a[href]"); const h = a && a.getAttribute("href"); if (h && !/^(#|javascript:)/i.test(h)) '
                b'{ a.target = "_blank"; a.rel = "noopener"; } }, true);</script>\n')
MAX_RENDER = 64 * 1024 * 1024  # an html report is read into memory (a script is appended to it); everything else in chunks
CHUNK = 256 * 1024  # a file is served in chunks: a gigabyte video is not read into memory whole
# types missing from Python 3.12 mimetypes (or different there): iPhone photos, phone videos, markdown
EXT_TYPES = {".heic": "image/heic", ".heif": "image/heif", ".avif": "image/avif", ".mov": "video/quicktime",
             ".m4v": "video/mp4", ".mkv": "video/x-matroska", ".m4a": "audio/mp4", ".opus": "audio/ogg",
             ".flac": "audio/flac", ".md": "text/markdown",
             ".webp": "image/webp"}   # on Windows mimetypes takes types from the registry, and webp may be unknown there (11.10)
# a file from /raw is shown by the browser itself (image, its own video player, pdf), and its document needs its own images and video;
# no scripts: svg or html opened by a link run nothing
RAW_CSP = "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; form-action 'none'"
HEIC = {".heic", ".heif"}  # Chrome does not render them: we show a jpeg copy (sips on the Mac)
JPEG_DAYS = 7   # the jpeg copies of HEIC photos live this long after they are made (cache/files-jpeg)


def sha(data):
    return hashlib.sha256(data).hexdigest()[:16]


def mime_of(path):
    return EXT_TYPES.get(os.path.splitext(path)[1].lower()) or mimetypes.guess_type(path)[0] or ""


def web_port():
    """Port of this hub's page: HUB_WEB_PORT (digits only), otherwise the anchor of an instance (core/instance.py),
    otherwise 8787 (WEB_PORT)."""
    from core import instance
    return instance.port("web")


def _count(value):
    """A non-negative integer from a request field (3 or "3"), otherwise None. Not str.isdigit: it lets "²" through, but
    int() does not."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value >= 0 else None
    return int(value) if isinstance(value, str) and value.isascii() and value.isdigit() else None


def kind_of(path):
    """File kind for viewing: image, video, audio, pdf by type; anything else None (text or not is decided by the content)."""
    mime = mime_of(path)
    if mime.startswith(("image/", "video/", "audio/")):
        return mime.split("/")[0]
    return "pdf" if mime == "application/pdf" else None


def byte_range(header, size):
    """`Range: bytes=...` -> (from, to) inclusive; None: serve the whole file (no header, unclear, several
    ranges); False: a range past the end of the file (416)."""
    m = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*", header or "")
    if not m or not (m[1] or m[2]):
        return None
    if not m[1]:  # bytes=-N: the last N bytes
        n = int(m[2])
        return (max(0, size - n), size - 1) if n and size else False
    start, end = int(m[1]), min(int(m[2]) if m[2] else size - 1, size - 1)
    return (start, end) if start <= end else False


def disposition(kind, name):
    """Content-Disposition with the file name: Cyrillic and spaces per RFC 5987, plus an ASCII name next to it for old clients."""
    plain = "".join(c if 32 <= ord(c) < 127 and c not in '"\\;' else "_" for c in name) or "file"
    return f"{kind}; filename=\"{plain}\"; filename*=UTF-8''{quote(name, safe='')}"


def say(lang, key, **params):
    """Text for a person in the request's language: lang is a code or a function that returns it (the language is computed only when the text
    is needed)."""
    return i18n.tr(lang() if callable(lang) else lang, key, **params)


def _drop_older(folder, seconds, keep=None):
    """Remove files of folder older than seconds, except keep (the cache of jpeg copies does not grow without end)."""
    for n in os.listdir(folder):
        p = os.path.join(folder, n)
        try:
            if p != keep and time.time() - os.path.getmtime(p) > seconds:
                os.unlink(p)
        except OSError:
            pass


THUMB_STEPS, THUMB_PIXELS, THUMB_SLOTS = (120, 240, 480, 960, 1920, 2048), 64_000_000, 2   # widths on offer; the most pixels a source may have; scalers at once
THUMB_RETRY = 3600   # a refusal that may pass (sips out of time, a refused width) is tried again after this long
THUMB_WAIT = 2   # seconds a request waits for a scaler, then the original goes: a waiting request holds one of the page's connection slots
_thumb_slots = threading.BoundedSemaphore(THUMB_SLOTS)
_thumb_locks, _thumb_locks_lock = {}, threading.Lock()   # one key — one scaler: identical requests wait for the first; key -> [lock, requests holding or waiting]
BMP_DIB = (12, 40, 52, 56, 64, 108, 124)   # the sizes of a BMP's second header


def raster(real):
    """Whether the file's CONTENT is a raster picture (png, jpeg, webp, bmp, tiff, heic): decoders pick the format
    from the bytes, not from the name, so a file named x.png may be anything else."""
    try:
        with open(real, "rb") as f:
            head = f.read(32)
    except OSError:
        return False
    bmp = head[:2] == b"BM" and len(head) >= 18 and head[6:10] == b"\0\0\0\0" \
        and int.from_bytes(head[14:18], "little") in BMP_DIB   # "BM" alone is also the start of a text
    return head.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"II*\x00", b"MM\x00*")) or bmp \
        or head[:4] == b"RIFF" and head[8:12] == b"WEBP" \
        or head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1", b"msf1")


def thumb_width(raw):
    """The asked width rounded UP to one of THUMB_STEPS (a few sizes: the cache does not fill with every width a
    client may ask), or None when it is not a positive number."""
    try:
        w = int(raw)
    except (TypeError, ValueError):
        return None
    return next((x for x in THUMB_STEPS if x >= w), THUMB_STEPS[-1]) if w > 0 else None


def _known(path):
    """A refusal mark (catalog.write_private writes it whole: requests read marks without a lock) — what it says and
    its age in seconds; (None, inf): no mark, or one that cannot be read (no refusal then)."""
    try:
        with open(path) as f:
            return json.load(f), time.time() - os.fstat(f.fileno()).st_mtime
    except (OSError, ValueError):
        return None, float("inf")


def _scale(sips, real, st, w, stem, none, none_w, folder):
    """thumbnail()'s scaler run, in a slot: (copy, st) or None. The copy is `<stem>.png` for a picture with an alpha
    channel (a window screenshot with its shadow: a jpeg has no transparency, sips paints it white), else `<stem>.jpg`."""
    def same():
        now = os.stat(real)
        return (now.st_mtime_ns, now.st_size, now.st_ino) == (st.st_mtime_ns, st.st_size, st.st_ino)
    if not same():
        return None   # rewritten while this request waited (the scaler it waited for gave up on it): no run
    tmp = f"{stem}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        try:
            size = subprocess.run([sips, "-g", "pixelWidth", "-g", "pixelHeight", "-g", "hasAlpha", real],
                                  capture_output=True, text=True, timeout=30).stdout
        except subprocess.TimeoutExpired:
            catalog.write_private(none, "late")   # without a mark every view would hold a slot 30 s again
            return None
        dims = {k: int(v) for k, v in re.findall(r"pixel(Width|Height): (\d+)", size)}
        fmt, out = ("png", stem + ".png") if re.search(r"hasAlpha: yes", size) else ("jpeg", stem + ".jpg")
        verdict = "x"   # header unreadable, or too many pixels
        if len(dims) == 2 and dims["Width"] * dims["Height"] <= THUMB_PIXELS:
            verdict = dims["Width"]
            if dims["Width"] > w:
                try:
                    done = subprocess.run([sips, "--resampleWidth", str(w), "-s", "format", fmt, real, "--out", tmp],
                                          capture_output=True, timeout=30)
                except subprocess.TimeoutExpired:
                    catalog.write_private(none_w, "late")   # this width, for THUMB_RETRY: the picture's width stays known
                    return None
                if not same():
                    return None   # rewritten while it was scaled: the copy is of the old picture
                if done.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) < st.st_size:
                    catalog.write_private(none, verdict)   # the source's width: asked for wider, it is not run again
                    os.replace(tmp, out)
                    _drop_older(folder, JPEG_DAYS * 86400, keep=out)
                    return out, st
                # this width only: a copy not smaller than its source is for good, a failed scale for THUMB_RETRY
                catalog.write_private(none_w, "x" if done.returncode == 0 and os.path.isfile(tmp) else "late")
        catalog.write_private(none, verdict)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return None


def thumbnail(state_dir, real, w):
    """(copy, st): a copy of the picture `real`, w wide — a png (`.png`) when the picture has an alpha channel, else a
    jpeg (`.jpg`) — in the same private cache as the HEIC copies (cache/files-jpeg), and the os.stat of the source the
    copy was made from (the page's ETag is of THIS stat, so the
    copy of an old picture never goes out under the tag of a newer one); or None: not a raster picture by its content,
    not wider than w (never enlarged), over THUMB_PIXELS (sips would take ~1 GB), no sips (macOS only: the other
    scalers pick the decoder from the content, and one of them hands files to Ghostscript), the file changed while
    it was scaled, or sips failed. A refusal by the header (its width, or `x`: unreadable, too many pixels) is a fact
    about the source and is remembered once in `<key>.none`, whatever width is asked later; a copy not smaller than
    its source refuses that width (`<key>-<w>.none`); sips failed or out of time (`late`: a busy machine is no fact
    about the picture) refuse the picture (reading the header) or the width (scaling) for THUMB_RETRY. At most THUMB_SLOTS
    scalers run at once; a request waits for one at most THUMB_WAIT (then None: the page serves the original) and
    looks into the cache again once it has it."""
    sips = shutil.which("sips")
    if not sips or not raster(real):
        return None
    try:
        st = os.stat(real)
        folder = os.path.join(state_dir, "cache", "files-jpeg")
        platforms.current.makedirs_private(folder)
        # "|a": copies made before the format followed the alpha channel (a jpeg, white where the picture was
        # transparent) are under other keys and are never served again; they go with the cache's week
        src = hashlib.sha1(f"{real}|{st.st_mtime_ns}|{st.st_size}|{st.st_ino}|a".encode()).hexdigest()[:16]
        stem, none, none_w = (os.path.join(folder, n) for n in (f"{src}-{w}", f"{src}.none", f"{src}-{w}.none"))

        def cached():
            for out in (stem + ".png", stem + ".jpg"):
                if os.path.isfile(out):
                    os.utime(out)   # a copy in use is kept: the week counts from its last view
                    return out, st
            for mark, too_wide in ((none, True), (none_w, False)):
                known, age = _known(mark)
                if known == "x" or too_wide and type(known) is int and known <= w or known == "late" and age < THUMB_RETRY:
                    raise LookupError   # remembered, of the picture or of this width: no tool run
            return None

        try:
            found = cached()
        except LookupError:
            return None
        if found:
            return found
        with _thumb_locks_lock:
            entry = _thumb_locks.setdefault(stem, [threading.Lock(), 0])
            entry[1] += 1
        lock = entry[0]
        until = time.monotonic() + THUMB_WAIT
        try:
            if not lock.acquire(timeout=THUMB_WAIT):
                return None   # the same picture is being scaled for longer than a request waits: the original
            try:
                if not _thumb_slots.acquire(timeout=max(0, until - time.monotonic())):
                    return None   # every scaler is busy: the original
                try:
                    try:
                        found = cached()   # the one we waited for may have made it
                    except LookupError:
                        return None
                    return found or _scale(sips, real, st, w, stem, none, none_w, folder)
                finally:
                    _thumb_slots.release()
            finally:
                lock.release()
        finally:
            with _thumb_locks_lock:   # the lock leaves with its last request: a newcomer must not start a second scaler
                entry[1] -= 1
                if not entry[1]:
                    _thumb_locks.pop(stem, None)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def inside(real, base):
    return real == base or real.startswith(base.rstrip(os.sep) + os.sep)


# a comment is only about this file (owner decision 2026-10-10: file comments are just comments; starting a session
# on work stays in the hub interface as before, so as not to mix them; distinguish a question from an edit command: if
# it is a command and the owner is sure, edit at once; if not, ask)
# The words to the session are the core's phrases (core/coretext.py): it reads the English, the owner the same in his
# language.
SCOPE = T("core.files.scope", "The comment is only about this file. A question — answer it. A clear command to fix "
          "something — edit this file at once and write what you changed; if you are not sure whether it is a command "
          "or what exactly to edit — ask here and edit after a \"yes\". Do not start work wider than this file (tasks, "
          "code, other files, commands) from a comment — answer that it is set in the session's chat.")
AT_LINE = T("core.files.at_line", " (line {line})")
FIRST = T("core.files.comment", "The owner's comment on the file `{path}`, on the piece \"{quote}\"{at}:\n\n{text}\n\n"
          "This is a comment thread in \"Files\": the result of this turn will show under the comment.")
AGAIN = T("core.files.reply", "The owner's reply in the comment thread on the file `{path}`, on the piece "
          "\"{quote}\"{at}:\n\n{text}\n\nThe result of this turn will show in the thread.")


class Files:
    def __init__(self, config=None, state_dir=None):
        self.state_dir = state_dir or default_state_dir()
        self.config = config or (lambda: catalog.config_or_last(self.state_dir))
        self._worktrees = {}  # project folder -> (when, [worktree folders])
        self._state, self._offset, self._read_at = reducer.empty(), 0, 0.0  # core state for comments: whom to send them to
        self._pending = []   # events read (and tracked) but not folded into the state yet: track()
        self._cmid, self._cturn = {}, {}  # thread message -> thread; session -> thread of its current turn
        self._lock = threading.RLock()  # the journal and thread files: one at a time
        self.reveal = lambda path: subprocess.Popen(platforms.current.reveal_command(path), stdin=subprocess.DEVNULL,
                                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @property
    def desktop(self):
        """The id of the hub machine's file manager, None without a desktop (platforms.*.file_manager); asked once."""
        with self._lock:
            if not hasattr(self, "_desktop"):
                self._desktop = platforms.current.file_manager()
            return self._desktop

    @desktop.setter
    def desktop(self, value):
        self._desktop = value

    # --- folders and paths -------------------------------------------------------------------------------------------
    def roots(self, local=False):
        """Project folders; a request from the Mac itself also gets "Mac" (/): at their own Mac the owner sees everything."""
        cfg = self.config()
        out = {name: p.get("path") for name, p in (cfg.get("projects") or {}).items() if isinstance(p, dict)}
        out.update((cfg.get("files") or {}).get("roots") or {})
        out.setdefault(PROOF, os.path.join(self.state_dir, "proof"))  # screenshots and videos of hub shot / hub video
        # executors' specs (core/specmode.py): every spec in one place, once the first one is made
        out.setdefault(SPECS, os.path.join(self.state_dir, specmode.DIR))
        out = {n: os.path.realpath(os.path.expanduser(p)) for n, p in out.items() if p and os.path.isdir(os.path.expanduser(p))}
        for name, base in list(out.items()):  # a project's worktree is also its folder: sessions write their paths in them
            for wt in self.worktrees(base):
                if wt != base and wt not in out.values():
                    out.setdefault(f"{name}@{os.path.basename(wt)}", wt)
        if local:
            out.setdefault(MAC, os.path.abspath(os.sep))   # Windows: the disk root ("C:\\"), not "\\"
        return out

    def _spec_tasks(self, real, st):
        """[(open, created, delegation, spec, spec file)] of the tasks whose spec (or its accepted copy) is the file real."""
        found = []
        for d in st["delegations"].values():
            sp = d.get("spec") if isinstance(d.get("spec"), dict) else None
            if sp and isinstance(sp.get("path"), str):
                here = os.path.realpath(specmode.abs_path(self.state_dir, sp["path"]))
                if real in (here, os.path.join(os.path.dirname(here), specmode.ACCEPTED)):
                    found.append((specmode.is_open(d), d.get("created") or 0, d, sp, here))
        return found

    def spec_of(self, real):
        """The task whose spec (or its accepted copy) this file is: {task, executor, state, ready, accepted…}, or None. A
        folder may carry several tasks' specs over time (--spec-dir): the open one wins, otherwise the latest."""
        base = os.path.realpath(os.path.join(self.state_dir, specmode.DIR))
        if not real.startswith(base + os.sep):
            return None
        st = self.state(stale_ok=True)
        found = self._spec_tasks(real, st)
        if not found and time.time() - os.path.getmtime(real) < STATE_TTL * 10:   # a spec made a moment ago is not in the state of a moment ago
            st = self.state()
            found = self._spec_tasks(real, st)
        if not found:
            return None
        _, _, d, sp, here = max(found, key=lambda x: x[:2])
        sess = (st.get("sessions") or {}).get(d.get("session"))
        return {"task": d["id"], "executor": d.get("session"), "state": sp.get("state"),
                "ready": specmode.awaiting(d, sess),   # waits for the decision now: the strip's buttons come with it
                "copy": real != here, "at": sp.get("at"), "by": sp.get("by"), "accepted_sha": sp.get("sha256")}

    spec_wait = 5.0   # how long "Accept" waits for the core's decision (tests without hubd: 0)

    def spec_act(self, body, lang=None):
        """POST /api/spec {delegation, action: accept|drop} from the file's strip (specmode.request)."""
        lang = lang or self.lang
        did = str(body.get("delegation") or "")
        code, out = specmode.request(self.state_dir, self.state(), did, body.get("action"), channel="web",
                                     wait=self.spec_wait)
        if out.get("error"):
            return 409, {"error": say(lang, "files.spec_closed", id=did) if out["error"] == "closed"
                         else coretext.human(out["error"], lang)}
        return code, out

    def worktrees(self, base):
        """Folders of the repo's `git worktree list`; cached for WORKTREES_TTL seconds so git is not run on every request."""
        now = time.monotonic()
        hit = self._worktrees.get(base)
        if hit and now - hit[0] < WORKTREES_TTL:
            return hit[1]
        try:
            out = subprocess.run([*paths.GIT, "-C", base, "worktree", "list", "--porcelain"], capture_output=True,
                                 text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        found = [os.path.realpath(line[len("worktree "):]) for line in out.splitlines() if line.startswith("worktree ")]
        found = [p for p in found if os.path.isdir(p)]
        self._worktrees[base] = (now, found)
        return found

    def resolve(self, root, rel, local=False):
        """(root, inner path) -> real path or None: no way outside the root, into hidden files, or through links outside."""
        base = self.roots(local).get(root)
        if not base or os.path.isabs(rel or ""):
            return None
        real = os.path.realpath(os.path.join(base, rel or ""))
        if not inside(real, base):
            return None
        # the path as asked too: a .git that is a link to a folder under another name is hidden by the name asked
        # (normpath: "b/.git/../x" asks for b/x; the separators are this system's — a "\\" in a POSIX name is a letter)
        asked = os.path.normpath(rel or os.curdir).split(os.sep)
        if any(hidden(p) for p in [*asked, *os.path.relpath(real, base).split(os.sep)]) or git_alias(base, real):
            return None
        return real

    def locate(self, abspath, local=False):
        """Absolute path -> (root, inner path); the deepest root if projects are nested."""
        if not abspath:
            return None  # realpath("") is the process folder, not "no file"
        asked = os.path.normpath(os.path.expanduser(abspath))
        if any(hidden(p) for p in asked.split(os.sep)):   # the name asked: resolve() below sees only where a link leads
            return None
        real = os.path.realpath(asked)
        best = None
        for name, base in self.roots(local).items():
            if inside(real, base) and (best is None or len(base) > len(best[1])):
                best = (name, base)
        if not best or not os.path.exists(real):
            return None
        # the inner path uses "/": it goes into the page address (#/root/path); on Windows relpath would give "\\" (%5C)
        rel = "" if real == best[1] else os.path.relpath(real, best[1]).replace(os.sep, "/")
        return (best[0], rel) if self.resolve(best[0], rel, local) else None

    # --- actions -----------------------------------------------------------------------------------------------
    def lang(self, header=None, st=None):
        """A person's language: hub.json -> owner -> hub before languages -> browser (header) -> English (core/i18n.py human).
        st is the state already read (in the middle of reading the journal, do not re-read it)."""
        try:
            cfg = self.config()
        except (OSError, ValueError):
            cfg = {}
        st = i18n.lang_facts(self.state(stale_ok=True) if st is None else st)
        return i18n.human(cfg, st, header, page=header is not None)

    def listing(self, root, rel, local=False, lang=None):
        lang = lang or self.lang
        real = self.resolve(root, rel, local)
        if not real or not os.path.isdir(real):
            return 404, {"error": say(lang, "files.no_folder")}
        entries = []
        for name in sorted(os.listdir(real), key=lambda n: (not os.path.isdir(os.path.join(real, n)), n.lower())):
            full = os.path.join(real, name)
            if hidden(name):
                continue
            st = os.lstat(full)
            entries.append({"name": name, "dir": os.path.isdir(full), "size": st.st_size, "mtime": st.st_mtime,
                            "kind": None if os.path.isdir(full) else kind_of(name) or "file"})
        return 200, {"root": root, "path": rel or "", "entries": entries}

    def read(self, root, rel, local=False, lang=None):
        lang = lang or self.lang
        real = self.resolve(root, rel, local)
        if not real or not os.path.isfile(real):
            return 404, {"error": say(lang, "files.no_file")}
        size = os.path.getsize(real)
        out = {"root": root, "path": real, "rel": rel, "size": size, "mtime": os.path.getmtime(real),
               "mime": mime_of(real), "kind": kind_of(real)}
        if out["kind"]:  # image, video, audio, pdf are shown by the browser via /raw, no need to read them here
            return 200, {**out, "text": None}
        if size > MAX_READ:
            return 200, {**out, "kind": "binary", "text": None, "why": say(lang, "files.too_big", mb=MAX_READ // 1024 // 1024)}
        with open(real, "rb") as f:
            data = f.read()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return 200, {**out, "kind": "binary", "text": None, "sha": sha(data), "why": say(lang, "files.not_text")}
        spec = self.spec_of(real)
        if spec:   # an executor's spec: the strip with "Accept" / "Drop" (files.js); its full sum is what is accepted
            spec["sha256"] = hashlib.sha256(data).hexdigest()
            # the accepted copy lies next to it, under the same root this file was opened by
            spec["accepted_href"] = "#/" + "/".join(quote(p) for p in [root, *rel.split("/")[:-1], specmode.ACCEPTED])
            if spec["by"] == str(delegations.BY_OWNER):   # the owner is "you" in the reader's language
                spec["by"] = say(lang, "files.spec_by_you")
            spec["changed"] = bool(spec["accepted_sha"]) and not spec["copy"] and spec["sha256"] != spec["accepted_sha"]
        return 200, {**out, "kind": "text", "text": text, "sha": sha(data), **({"spec": spec} if spec else {})}

    def jpeg(self, real):
        """HEIC (iPhone photo) as a jpeg copy: Chrome does not render it. sips exists on every Mac; if absent, None.
        The copy lives in the hub's private state folder (cache/files-jpeg, 0700: under a fixed name in the shared temp
        folder another user could plant a picture first), keyed by the file's path and time: it is not recomputed a
        second time. Copies not viewed for JPEG_DAYS go when a new one is made."""
        sips = shutil.which("sips")
        if not sips:
            return None
        st = os.stat(real)
        folder = os.path.join(self.state_dir, "cache", "files-jpeg")
        platforms.current.makedirs_private(folder)
        key = hashlib.sha1(f"{real}|{st.st_mtime}|{st.st_size}".encode()).hexdigest()[:16]
        out = os.path.join(folder, key + ".jpg")
        if os.path.isfile(out):
            try:
                os.utime(out)   # a copy in use is kept: the week counts from its last view
            except OSError:
                pass
            return out
        tmp = f"{out}.{os.getpid()}.{threading.get_ident()}.tmp"   # two requests for one photo: each its own
        try:
            done = subprocess.run([sips, "-s", "format", "jpeg", real, "--out", tmp], capture_output=True, timeout=30)
            if done.returncode != 0 or not os.path.isfile(tmp):   # a failed sips leaves no copy for a week
                raise OSError(f"sips: {done.returncode}")
            os.replace(tmp, out)
        except (OSError, subprocess.SubprocessError):
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return None
        _drop_older(folder, JPEG_DAYS * 86400, keep=out)
        return out

    def reveal_file(self, body, local=False, lang=None):
        """Show the file in its folder (Finder), only on this computer: the window opens on its screen."""
        if not self.desktop:   # a hub on a server: no screen to open a window on
            return 409, {"error": say(lang or self.lang, "files.reveal_no_desktop")}
        real = self.resolve(str(body.get("root") or ""), str(body.get("path") or ""), local)
        if not real or not os.path.exists(real):
            return 404, {"error": say(lang or self.lang, "files.no_file")}
        self.reveal(real)
        return 200, {"ok": True}

    def write(self, body, local=False, lang=None):
        lang = lang or self.lang
        real = self.resolve(str(body.get("root") or ""), str(body.get("path") or ""), local)
        text = body.get("text")
        if not real or not os.path.isfile(real) or not isinstance(text, str):
            return 404, {"error": say(lang, "files.no_file")}
        data = text.encode("utf-8")
        if len(data) > MAX_WRITE:
            return 400, {"error": say(lang, "files.too_large")}
        with open(real, "rb") as f:
            now = sha(f.read())
        if body.get("sha") != now:
            return 409, {"error": say(lang, "files.changed_meanwhile"), "sha": now}
        # a new temp file (O_EXCL): a link planted in the folder under a predictable name does not redirect the write to a foreign file
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(real), prefix="." + os.path.basename(real) + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.chmod(tmp, os.stat(real).st_mode & 0o777)
            os.replace(tmp, real)
        except BaseException:
            os.unlink(tmp)
            raise
        return 200, {"ok": True, "sha": sha(data), "mtime": os.path.getmtime(real)}

    # --- server ------------------------------------------------------------------------------------------------
    # --- file comments (owner decision 2026-10-01: like in Google Docs, select pieces, comment and ask to
    # rewrite): the page collects them, here they go to the session in one message through the core's mailbox ---------------------------
    def state(self, stale_ok=False):
        """Core state from the journal, reading on from the last position (the core snapshot comes first). Under a lock: the server
        is multithreaded, and two reads from one position moved the position twice and lost session replies (01.10).
        stale_ok: for a request that only looks (the language, the session list): the state read less than STATE_TTL
        ago is good enough, the journal is not read on and the state is not copied again (a busy hub writes events all
        the time, and each of the page's four requests of one open copied the whole state, 0.3 s on 30 MB)."""
        with self._lock:
            if stale_ok and self._offset and time.monotonic() - self._read_at < STATE_TTL:
                return self._state
            return self._read_state()

    def _saved(self, path):
        """(seq, offset, state) of the state file of this journal (journal.save_state), or None: the state the commands start from.
        A periodic snapshot is written only after the journal grew by the weight of the state, so on a big hub the last
        snapshot lags by tens of thousands of events, and folding them from it took 34 s."""
        try:
            snap = journal.read_state_file(self.state_dir)
            if snap["first"] != journal._first_seq(path) or (
                    hasattr(journal, "of_this_journal") and not journal.of_this_journal(snap, path)):
                return None
            return snap["seq"], snap["offset"], snap["state"]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    @staticmethod
    def _joins(lines, seq, offset):
        """Whether the state file (folded up to the event seq, journal offset) is of this journal: a line starts at the
        offset, and it is event seq + 1, or the journal ends there (as journal.load_state checks), and the line before it is
        event seq."""
        if offset > len(lines) or (offset and lines[offset - 1:offset] != b"\n"):
            return False
        line = lines[offset:].split(b"\n", 1)[0]
        before = lines[lines.rfind(b"\n", 0, offset - 1) + 1:offset - 1] if offset else b""   # the line the file ends at
        try:
            return (json.loads(line)["seq"] == seq + 1 if line.strip() else offset == len(lines)) and \
                (not offset or json.loads(before)["seq"] == seq)
        except (ValueError, KeyError, TypeError):
            return False

    def track(self):
        """Read the journal on for the session replies to comment threads, without folding the events into the state (they
        wait for the next state(): a thread poll every few seconds must not copy the whole state each time). The first
        read is whole, as state()."""
        with self._lock:
            if self._offset:
                self._read_state(fold=False)
            else:
                self._read_state()

    def _read_state(self, fold=True):
        path = os.path.join(self.state_dir, "journal.jsonl")
        ino = journal.identity(path)
        if ino != getattr(self, "_ino", ino):   # the journal was started anew (journal.rotate): read the new one from the start, not from the old position
            self._state, self._offset, self._pending = reducer.empty(), 0, []   # (12.10: after rotation, session replies to threads vanished silently)
            fold = True   # and the empty state is not left for the requests that take a state of a moment ago
        self._ino = ino
        cold = not self._offset
        try:
            with open(path, "rb") as f:
                f.seek(self._offset)
                chunk = f.read()
        except FileNotFoundError:
            return self._state
        end = chunk.rfind(b"\n")
        events = []
        for line in chunk[:end].split(b"\n") if end >= 0 else []:
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        for ev in events:  # session replies to comment threads come from all events, including those before the snapshot
            self._ctrack(ev)
        events, self._pending = self._pending + events, []
        if not fold and len(events) < PENDING_MAX:
            self._pending = events
            self._offset += end + 1 if end >= 0 else 0
            return self._state
        state, own = self._state, False   # own: built here, no other request has it yet, nothing to copy
        # from the last core snapshot: folding the whole journal takes seconds (the first request hung)
        snaps = [i for i, ev in enumerate(events) if ev.get("type") == "core.snapshot" and (ev.get("data") or {}).get("state")]
        snap_at = events[snaps[-1]].get("seq", 0) if snaps else 0
        # at the first read the state file and the events after it are fewer to fold than those after the last snapshot
        # (unless the snapshot is newer): the same state, the commands start from it
        saved = self._saved(path) if cold and events else None
        if saved and saved[0] >= snap_at and self._joins(chunk[:end + 1], saved[0], saved[1]):
            state, events, own = reducer.loaded({**reducer.empty(), **saved[2], "seq": saved[0]}), \
                [ev for ev in events if ev.get("seq", 0) > saved[0]], True
        elif snaps:
            state, events, own = reducer.restore(events[snaps[-1]]), events[snaps[-1] + 1:], True
        # the event's outcome is recorded in the journal: one copy per read-on, not per event (18.10: ~25 ms per event on a
        # large hub, the first open waited tens of seconds); a copy because other requests read the previous state
        # without a lock (sessions, session_cwd)
        self._state = reducer.fold(state if own else reducer.copy_state(state), events) if events else state
        self._offset += end + 1 if end >= 0 else 0
        self._read_at = time.monotonic()
        return self._state

    def session_cwd(self, name, cwd=None):
        """The folder from which /open looks up a relative path: the session's own folder, by its name. A folder from the address only
        if it is already the folder of some session or project (links made before the session name was in them): git is
        run in it, and a folder named by a foreign site could have been prepared with harmful repo settings."""
        for stale_ok in (True, False):   # a session made a moment ago is not in the state of a moment ago
            st = self.state(stale_ok=stale_ok)["sessions"]
            if NAME.fullmatch(name or "") and (st.get(name) or {}).get("cwd"):
                return st[name]["cwd"]
            if cwd:
                real = os.path.realpath(os.path.expanduser(cwd))
                known = {os.path.realpath(s["cwd"]) for s in st.values() if s.get("cwd")} | set(self.roots().values())
                if real in known:
                    return real
        return None

    def sessions(self, path=""):
        """Who can be sent to: first the sessions whose folder is above the file (or in a worktree of its repo), then the rest."""
        real = os.path.realpath(path) if path else ""
        out = []
        for s in self.state(stale_ok=True)["sessions"].values():
            if s["state"] in ("removed", "closed", "failed"):
                continue
            cwd = os.path.realpath(s.get("cwd") or "/nonexistent")
            here = bool(real) and (real.startswith(cwd + os.sep) or
                                   real.startswith(cwd.split(os.sep + ".claude" + os.sep + "worktrees")[0] + os.sep))
            # in the words of the page snapshot: a parked one is free, a finished executor is asleep
            out.append({"name": s["name"], "state": views.shown(s["state"]), "cwd": s.get("cwd"), "here": here,
                        "hub": s.get("kind") == "hub"})
        spec = self.spec_of(real) if real else None
        if spec:   # a spec's comments go to its executor first (core/specmode.py)
            for s in out:
                s["here"] = s["here"] or s["name"] == spec["executor"]
        out.sort(key=lambda s: (not (spec and s["name"] == spec["executor"]), not s["here"], not s["hub"], s["name"]))
        return out

    # comment threads: one thread file per file, session replies come from the journal (its turn result for a thread message)
    def _cfile(self, key):
        return os.path.join(self.state_dir, "comments", key + ".json")

    def _cload(self, key):
        try:
            with open(self._cfile(key)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"path": None, "threads": []}

    def _csave(self, key, data):
        os.makedirs(os.path.dirname(self._cfile(key)), exist_ok=True)
        with open(self._cfile(key) + ".tmp", "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(self._cfile(key) + ".tmp", self._cfile(key))

    @staticmethod
    def _ckey(real):
        return hashlib.sha1(real.encode()).hexdigest()[:12]

    def _creply(self, tid, who, text, seq):
        key = tid.split(".")[0]
        data = self._cload(key)
        for t in data["threads"]:
            if t["id"] == tid and not any(m.get("seq") == seq for m in t["messages"]):
                t["messages"].append({"who": who, "text": text, "ts": time.time(), "seq": seq})
                self._csave(key, data)

    def _ctrack(self, ev):
        """A thread message (data.comment) -> its turn in the session -> the turn result is a reply in the thread."""
        t, d, name = ev.get("type"), ev.get("data") or {}, ev.get("entity")
        if t == "message.received" and d.get("comment"):
            self._cmid[f"m{ev['seq']}"] = d["comment"]
        elif t == "turn.started":
            tid = self._cmid.pop(d.get("marker") or "", None)
            if tid:
                self._cturn[name] = tid
            else:
                self._cturn.pop(name, None)
        elif t == "permission.requested" and name in self._cturn and d.get("tool") == "AskUserQuestion":
            # the session still asked via a form in the middle of a thread turn: the question goes into the thread too, not only as a card in the feed (10.10)
            qs = (d.get("input") or {}).get("questions") or []
            text = "\n\n".join(f"{q.get('question', '')}\n" + " / ".join(o.get("label", "") for o in q.get("options") or [])
                                for q in qs if isinstance(q, dict))
            if text.strip():
                self._creply(self._cturn[name], name, say(self.lang(st=self._state), "files.question_card", text=text), ev["seq"])
        elif t == "turn.result" and name in self._cturn:
            tid = self._cturn.pop(name)
            if (d.get("text") or "").strip() and not d.get("is_error"):
                self._creply(tid, name, d["text"], ev["seq"])

    def threads(self, root, rel, local=False, lang=None):
        real = self.resolve(root, rel, local)
        if not real or not os.path.isfile(real):
            return 404, {"error": say(lang or self.lang, "files.no_file")}
        self.track()  # read the journal on: fresh session replies
        return 200, self._cload(self._ckey(real))["threads"]

    def comment(self, body, local=False, lang=None):
        """A new comment on a piece (quote) or a reply in a thread (thread), sent as a message to the thread's session."""
        with self._lock:
            return self._comment(body, local, lang or self.lang)

    def _comment(self, body, local, lang):
        real = self.resolve(str(body.get("root") or ""), str(body.get("path") or ""), local)
        if not real or not os.path.isfile(real):
            return 400, {"error": say(lang, "files.comment_no_file")}
        text = str(body.get("text") or "").strip()
        if not text:
            return 400, {"error": say(lang, "files.comment_empty")}
        key = self._ckey(real)
        self.state()
        data = self._cload(key)
        data["path"] = real
        if body.get("thread"):
            th = next((t for t in data["threads"] if t["id"] == body["thread"]), None)
            if not th:
                return 400, {"error": say(lang, "files.no_thread")}
            name = th["session"]
        else:
            name = str(body.get("session") or "")
            th = {"id": f"{key}.{os.urandom(4).hex()}", "quote": str(body.get("quote") or "")[:2000],
                  "nth": _count(body.get("nth")) or 0, "line": _count(body.get("line")),
                  "session": name, "created": time.time(), "resolved": False, "messages": []}
        sess = self.state()["sessions"].get(name)
        if not sess or sess["state"] in ("removed", "closed", "failed"):
            return 400, {"error": say(lang, "files.no_session", name=name or say(lang, "files.session_none"))}
        q = " ".join(th["quote"].split())
        q = q if len(q) <= 300 else q[:300] + "…"
        at = AT_LINE.format(line=th["line"]) if th.get("line") else ""
        msg = (AGAIN if th["messages"] else FIRST).format(path=real, quote=q, at=at, text=text) + " " + SCOPE
        th["messages"].append({"who": "owner", "text": text, "ts": time.time()})
        th["resolved"] = False
        if not body.get("thread"):
            data["threads"].append(th)
        self._csave(key, data)
        spool.put(os.path.join(self.state_dir, "spool"), "message.received", None,
                  {"to": name, "text": str(msg)[:20000], "attachments": [], "comment": th["id"], "channel": "web",
                   "owner": True, **({"l10n": coretext.marks(msg)} if len(msg) <= 20000 and coretext.marks(msg) else {})},
                  src="files")   # the core's phrases around his words: the feed shows them in his language
        return 202, th

    def resolve_thread(self, body, local=False, lang=None):
        with self._lock:
            return self._resolve_thread(body, local, lang or self.lang)

    def _resolve_thread(self, body, local, lang):
        real = self.resolve(str(body.get("root") or ""), str(body.get("path") or ""), local)
        if not real:
            return 400, {"error": say(lang, "files.no_file")}
        key = self._ckey(real)
        data = self._cload(key)
        for t in data["threads"]:
            if t["id"] == body.get("thread"):
                t["resolved"] = not body.get("reopen")
                self._csave(key, data)
                return 200, t
        return 400, {"error": say(lang, "files.no_thread")}

    # --- hub page themes: its choice is in localStorage on its port and not visible from here (another origin); the theme list is
    # the same as the page's, through the same modules/web code (read only: its process and journal are not touched) -------------
    def theme_source(self):
        if not hasattr(self, "_themes"):
            from modules.web.server import Web

            files = self

            class Themes(Web):  # only the theme methods: theme_sources, theme_file, theme_art, extensions, ext_config
                def __init__(self):  # without Web.__init__: it reads the journal and builds the feed history
                    self.state_dir, self.config = files.state_dir, files.config
                    self.req = threading.local()   # language of texts (Web.tx): there is no request, so the hub's language, for "Files"

                def hub_lang(self, cfg=None):
                    return files.lang()

            self._themes = Themes()
        return self._themes

    def handler(self):
        files = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def allowed(self):
                """localhost is as for the hub page; from the tailnet, only the owner (modules/tailnet/access.py)."""
                return access.allowed(files.state_dir, self.headers.get("Host"), self.client_address[0],
                                      headers=self.headers)

            def lang(self):
                """Page language for this request (as for the hub page)."""
                return files.lang(self.headers.get("Accept-Language"))

            def proxied(self):
                """The refusal's key for a request a proxy passed on: Cloudflare's when its provider is on and claimed it."""
                from core import remote
                by = remote.refused_by(files.state_dir, self.headers.get("Host"), self.client_address[0], self.headers)
                return "files.cloudflare" if by == "cloudflare" else "files.proxied"

            def known(self):
                """Who came is known — this machine, or the owner by a provider (core/remote.identify): the first step."""
                from core import remote
                return remote.identify(files.state_dir, self.headers.get("Host"), self.client_address[0],
                                       self.headers) is not None

            def render_mark(self, root, rel):
                """The mark of a report's folder while the hub is signed in to (a password, or the old key), else None."""
                from core import pagekey, pagepass
                key = pagepass.mark_secret()
                if not (key and self.known()):
                    return None
                parts = rel.split("/")
                return pagekey.render_mark(key, root, "/".join(parts[:-1]), time.time() // 86400)

            def render_marked(self, path):
                """/render/~<mark>/<root>/<rel> -> /render/<root>/<rel> when the mark is this hub's for the folder of
                <rel> or one above it (within the root), of today or yesterday, and the request is of this machine;
                otherwise None. No "..", ".", empty, backslashed or slashed (%2F) segment: the folder the mark names is the
                folder."""
                from core import pagekey, pagepass
                mark, _, rest = path[len("/render/~"):].partition("/")
                root, _, rel = rest.partition("/")
                parts = [unquote(x) for x in rel.split("/")]
                key = pagepass.mark_secret()
                if not (key and rel and self.known()) or \
                        any(x in ("", ".", "..") or "\\" in x or "/" in x for x in parts):
                    return None
                today = time.time() // 86400
                for day in (today, today - 1):
                    for depth in range(len(parts) - 1, -1, -1):
                        if pagekey.same(mark, pagekey.render_mark(key, unquote(root), "/".join(parts[:depth]), day)):
                            return "/render/" + rest
                return None

            def signin_needed(self):
                """Only the page's password is missing (core/pagepass.py): 401, sign in on the hub's page."""
                from core import remote
                return remote.needs_signin(files.state_dir, self.headers.get("Host"), self.client_address[0],
                                           self.headers)

            def refused(self, key):
                """The refusal's key: a request whose only lack is the sign-in hears where to sign in."""
                return "files.signin" if self.signin_needed() else key

            def local(self):
                """From the Mac itself, the whole disk (root "Mac"); from the tailnet, project folders only."""
                return access.local(self.headers.get("Host"), self.client_address[0], headers=self.headers)

            def framing(self, headers):
                """Embed "Files" only into itself and the hub page (a file in its panel), not a foreign site: that one
                would plant it under its own click. The response's own frame-ancestors (/render, "Files" only) is left alone."""
                csp = headers.get("Content-Security-Policy")
                if csp and "frame-ancestors" in csp:
                    return headers
                port = web_port()
                rule = " ".join(["frame-ancestors 'self'", *access.page_origins(files.state_dir, port)])
                return {**headers, "Content-Security-Policy": f"{csp}; {rule}" if csp else rule}

            def send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
                data = body if isinstance(body, bytes) else (
                    body.encode() if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode())
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                if "Cache-Control" not in (extra or {}):   # the "Files" page is its own (tag and no-cache)
                    self.send_header("Cache-Control", "no-store")
                for k, v in self.framing(extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def send_file(self, real, headers):
                """A file in chunks, with `Range` (video seeks, Safari does not play it without 206); the browser closed
                the connection in the middle of a clip (seeking), which is normal, not an error."""
                size = os.path.getsize(real)
                rng = byte_range(self.headers.get("Range"), size)
                if rng is False:
                    return self.send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"})
                start, end = rng or (0, size - 1)
                self.send_response(206 if rng else 200)
                for k, v in {"Content-Type": headers.pop("Content-Type"), "Content-Length": str(max(0, end - start + 1)),
                             "Accept-Ranges": "bytes", "Cache-Control": "no-store", **self.framing(headers)}.items():
                    self.send_header(k, v)
                if rng:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                if self.command == "HEAD":
                    return
                with open(real, "rb") as f:
                    f.seek(start)
                    left = end - start + 1
                    while left > 0:
                        chunk = f.read(min(CHUNK, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)

            def page(self):
                """index.html of "Files" in the page's language (static text rendered by the server, core/i18n.py render_html); the tag comes from
                the file, catalogs, language and port of the hub page: a language change does not take the old copy from the browser cache."""
                path, lang, port = os.path.join(STATIC, "index.html"), self.lang(), web_port()
                st = os.stat(path)
                reveal = files.desktop if self.local() else None   # the button: this computer's file manager, if any
                tag = f'W/"{st.st_mtime_ns:x}-{st.st_size:x}-{i18n.stamp(lang)}-{port}-{reveal or ""}"'
                if tag in [t.strip() for t in (self.headers.get("If-None-Match") or "").split(",")]:
                    self.send_response(304)
                    self.send_header("ETag", tag)
                    self.send_header("Cache-Control", "no-cache")
                    for k, v in self.framing({}).items():
                        self.send_header(k, v)
                    return self.end_headers()
                with open(path, encoding="utf-8") as f:
                    page = i18n.render_html(f.read(), lang)
                # the "Hub" button leads to this hub's page: a second hub has its own port
                page = page.replace("<html ", f'<html data-hub-port="{port}"' + (f' data-reveal="{reveal}"' if reveal else "") + " ", 1)
                return self.send(200, page, "text/html; charset=utf-8", {"ETag": tag, "Cache-Control": "no-cache"})

            def static(self, base, rel):
                """A page file, as for the hub page: the browser keeps it and asks (no-cache), and if unchanged, 304 without
                a body; text is gzipped. Otherwise every file open pulled everything anew (18.10: ~0.4 MB, 4 MB with mermaid)."""
                real = os.path.realpath(os.path.join(base, rel))
                kind = TYPES.get(os.path.splitext(real)[1])
                if not kind or not real.startswith(os.path.realpath(base) + os.sep) or not os.path.isfile(real):
                    return self.send(404, {"error": say(self.lang, "files.no_file")})
                st = os.stat(real)
                tag = f'W/"{st.st_mtime_ns:x}-{st.st_size:x}"'
                head = {"ETag": tag, "Cache-Control": "no-cache", "Vary": "Accept-Encoding"}
                if tag in [t.strip() for t in (self.headers.get("If-None-Match") or "").split(",")]:
                    self.send_response(304)
                    for k, v in self.framing(head).items():
                        self.send_header(k, v)
                    return self.end_headers()
                with open(real, "rb") as f:
                    data = f.read()
                if kind.startswith(("text/", "image/svg+xml")) and len(data) >= 1024 and self.gzip_ok():
                    data, head["Content-Encoding"] = gzip.compress(data, 6, mtime=0), "gzip"
                return self.send(200, data, kind, head)

            def gzip_ok(self):
                for part in (self.headers.get("Accept-Encoding") or "").split(","):
                    name, _, q = part.partition(";")
                    if name.strip() == "gzip":
                        return not re.fullmatch(r"\s*q=0(\.0*)?\s*", q)
                return False

            def do_GET(self):
                url = urlparse(self.path)
                # a /render report's own files under the page's password: the sandbox sends no cookie, the mark in the
                # path (core/pagekey.render_mark, signed by core/pagepass.mark_secret) stands for it — only for whoever
                # the first step lets in (this machine, the owner by a provider) and only under its root
                marked = url.path.startswith("/render/~") and self.render_marked(url.path)
                if marked:
                    url = url._replace(path=marked)
                if not marked and not self.allowed():
                    if self.signin_needed():
                        return self.send(401, {"error": say(self.lang, "files.signin"), "code": "signin"})
                    if access.via_proxy(self.client_address[0], self.headers):
                        return self.send(403, {"error": say(self.lang, self.proxied())})
                    return self.send(403, {"error": say(self.lang, self.refused("files.no_access"))})
                if url.path.startswith("/render/~"):   # a mark that is not this hub's: as any refused request
                    return self.send(403, {"error": say(self.lang, self.refused("files.no_access"))})
                # a /render report in a sandbox has an opaque origin: the browser sends its css and images as cross-site
                if access.cross_site(self) and not url.path.startswith("/render/"):
                    return self.send(403, {"error": say(self.lang, "files.cross_site")})
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                if url.path in ("/", "/index.html"):
                    return self.page()
                if url.path == "/i18n.json":   # strings of the hub page scripts that "Files" takes (/web/js/i18n.js)
                    lang = self.lang()
                    tag = f'W/"{i18n.stamp(lang)}"'   # a reload gets 304 without a body, as for the hub page
                    if tag in [t.strip() for t in (self.headers.get("If-None-Match") or "").split(",")]:
                        self.send_response(304)
                        self.send_header("ETag", tag)
                        self.send_header("Cache-Control", "no-cache")
                        return self.end_headers()
                    return self.send(200, i18n.page_bundle(lang), extra={"Cache-Control": "no-cache", "ETag": tag})
                if url.path.startswith("/static/"):
                    return self.static(STATIC, url.path[len("/static/"):])
                if url.path.startswith("/web/"):  # styles and icons of the hub page
                    return self.static(os.path.join(WEB, "static"), url.path[len("/web/"):])
                if url.path.startswith("/vendor/"):
                    return self.static(os.path.join(WEB, "vendor"), url.path[len("/vendor/"):])
                if url.path == "/favicon.svg":
                    with open(os.path.join(WEB, "favicon.svg"), "rb") as f:
                        return self.send(200, f.read(), "image/svg+xml")
                if url.path == "/open":
                    found = paths.find(q.get("path", ""), files.session_cwd(q.get("session", ""), q.get("cwd")),
                                       extra=[os.path.dirname(catalog.config_path(files.state_dir))])
                    hit = files.locate(found, self.local()) if found else None
                    if not hit and q.get("alt"):   # the source of a sent file was deleted: its copy is in the attachments (hub send-file)
                        from modules.web import attach
                        kept = attach.inside(files.state_dir, q["alt"])
                        hit = files.locate(kept, self.local()) if kept else None
                    if not hit:
                        return self.send(404, say(self.lang, "files.open_not_found"), "text/plain; charset=utf-8")
                    # to a line; which session it was opened from: file comments go to it
                    extra = [f"line={q['line']}"] if q.get("line", "").isdigit() else []
                    if NAME.fullmatch(q.get("session", "")):
                        extra.append("session=" + q["session"])
                    if q.get("embed") == "1":  # in the hub page's embedded window: no folder list
                        extra.append("embed=1")
                        if q.get("chrome") == "hub":  # and the page draws the window header itself (md-panel, 28.10)
                            extra.append("chrome=hub")
                        if THEME_ID.fullmatch(q.get("theme", "")):  # the hub page's theme (theme.js takes ?theme= from the address)
                            extra.append("theme=" + quote(q["theme"], safe=""))
                    return self.send(302, b"", "text/plain", {"Location": "/#/" + quote(hit[0]) + "/" + quote(hit[1])
                                                              + ("?" + "&".join(extra) if extra else "")})
                if url.path == "/api/comments":
                    return self.send(*files.threads(q.get("root", ""), q.get("path", ""), self.local(), self.lang))
                if url.path == "/api/sessions":
                    return self.send(200, files.sessions(q.get("path", "")))
                if url.path == "/api/stat":
                    real = files.resolve(q.get("root", ""), q.get("path", ""), self.local())
                    if not real or not os.path.isfile(real):
                        return self.send(404, {"error": say(self.lang, "files.no_file")})
                    st = os.stat(real)
                    spec = files.spec_of(real)   # an open spec's strip follows the executor's turns (specmode.awaiting)
                    return self.send(200, {"mtime": st.st_mtime, "size": st.st_size,
                                           **({"spec_ready": spec["ready"]} if spec and spec["state"] == "open"
                                              and not spec["copy"] else {})})
                if url.path == "/api/themes":
                    return self.send(200, files.theme_source().themes())
                if url.path in ("/api/themes/art", "/api/themes/file"):  # art, icons and font of the theme, those declared in it
                    from modules.web.server import ART_TYPES, KIT_TYPES
                    web = files.theme_source()
                    art = url.path.endswith("/art")
                    real = web.theme_art(q.get("id", "")) if art else web.theme_file(q.get("id", ""), q.get("f", ""))
                    if not real:
                        return self.send(404, {"error": say(self.lang, "files.no_theme_file")})
                    with open(real, "rb") as f:  # svg as an opened document: no scripts
                        return self.send(200, f.read(), (ART_TYPES if art else KIT_TYPES)[os.path.splitext(real)[1].lower()],
                                         {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
                                          "X-Content-Type-Options": "nosniff"})
                if url.path == "/api/roots":
                    return self.send(200, [{"name": n, "path": p} for n, p in sorted(files.roots(self.local()).items())])
                if url.path == "/api/list":
                    return self.send(*files.listing(q.get("root", ""), q.get("path", ""), self.local(), self.lang))
                if url.path == "/api/read":
                    return self.send(*files.read(q.get("root", ""), q.get("path", ""), self.local(), self.lang))
                if url.path == "/raw":  # images and download
                    if not q.get("root") and os.path.isabs(q.get("path", "")):   # Windows: "C:\\...", not "/"
                        # by full path: an image from a session step on the hub page; permissions as for /open
                        hit = files.locate(q["path"], self.local())
                        q = {**q, "root": hit[0], "path": hit[1]} if hit else {}   # download, as: the same
                    real = files.resolve(q.get("root", ""), q.get("path", ""), self.local())
                    if not real or not os.path.isfile(real):
                        return self.send(404, {"error": say(self.lang, "files.no_file")})
                    name, kind = os.path.basename(real), mime_of(real) or "application/octet-stream"
                    kind += "; charset=utf-8" if kind.startswith("text/") else ""  # markdown in a tab without garbled text
                    if q.get("as") == "jpeg" and os.path.splitext(real)[1].lower() in HEIC:  # iPhone photo in Chrome
                        real, kind, name = files.jpeg(real), "image/jpeg", os.path.splitext(name)[0] + ".jpg"
                        if not real:
                            return self.send(415, {"error": say(self.lang, "files.heic_no_sips")})
                    # "Download" is attachment; without it, inline but with a name: "Save as" will not offer "raw".
                    # In a sandbox, except pdf: Chrome's viewer does not open in it. A foreign site cannot read the file (CORP)
                    csp = RAW_CSP + ("" if kind == "application/pdf" else "; sandbox")
                    return self.send_file(real, {"Content-Type": kind, "Content-Security-Policy": csp,
                                                 "Cross-Origin-Resource-Policy": "same-site",
                                                 "X-Content-Type-Options": "nosniff", "Content-Disposition":
                                                 disposition("attachment" if q.get("download") else "inline", name)})
                if url.path.startswith("/render/"):  # an html file as a page (in a "Files" iframe) and its neighbours by links
                    root, _, rel = url.path[len("/render/"):].partition("/")
                    mark = None if marked else self.render_mark(unquote(root), "/".join(unquote(p) for p in rel.split("/")))
                    if mark:   # the page key is on: the report goes to its marked address, its own files follow it there
                        return self.send(303, b"", "text/plain", {"Location": f"/render/~{mark}/{root}/{rel}" + (
                            "?" + url.query if url.query else ""), "Referrer-Policy": "no-referrer"})
                    real = files.resolve(unquote(root), "/".join(unquote(p) for p in rel.split("/")), self.local())
                    if not real or not os.path.isfile(real):
                        return self.send(404, {"error": say(self.lang, "files.no_file")})
                    kind = mime_of(real) or "application/octet-stream"
                    kind += "; charset=utf-8" if kind.startswith("text/") or kind.endswith(("javascript", "json")) else ""
                    # the hub page may frame a report too: a question card shows an option's html report as a live
                    # preview (cards.js fileEl); a foreign site still may not
                    frame = " ".join(access.page_origins(files.state_dir, web_port()))
                    head = {"Content-Security-Policy": RENDER_CSP + (" " + frame if frame else ""),
                            "X-Content-Type-Options": "nosniff",
                            "Referrer-Policy": "no-referrer"}   # sites the report links to do not see its address
                    if not kind.startswith("text/html"):  # css, images, video of the report in chunks, video with seeking
                        return self.send_file(real, {"Content-Type": kind, **head})
                    if os.path.getsize(real) > MAX_RENDER:
                        return self.send(404, {"error": say(self.lang, "files.no_file")})
                    with open(real, "rb") as f:  # session reports are UTF-8; without <meta charset> Cyrillic would break
                        return self.send(200, f.read() + RENDER_LINKS, kind, head)
                self.send(404, {"error": say(self.lang, "files.no_page")})

            def do_POST(self):
                let_in = self.allowed()
                if not let_in or self.headers.get("X-Hub") != "1" or access.cross_site(self):
                    self.close_connection = True   # the body was not read, so the connection is unfit for the next request
                    if not let_in and self.signin_needed():
                        return self.send(401, {"error": say(self.lang, "files.signin"), "code": "signin"})
                    if access.via_proxy(self.client_address[0], self.headers) and not let_in:
                        return self.send(403, {"error": say(self.lang, self.proxied())})
                    return self.send(403, {"error": say(self.lang, self.refused("files.not_from_page") if not let_in
                                                        else "files.not_from_page")})
                try:
                    body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), MAX_WRITE * 2)) or b"{}")
                except ValueError:
                    return self.send(400, {"error": say(self.lang, "files.not_json")})
                if not isinstance(body, dict):   # every action reads the object's fields
                    return self.send(400, {"error": say(self.lang, "files.not_json_object")})
                if urlparse(self.path).path == "/api/reveal":
                    if not self.local():   # from a phone the Finder window would open on the Mac, not for whoever pressed
                        return self.send(403, {"error": say(self.lang, "files.reveal_local_only")})
                    return self.send(*files.reveal_file(body, True, self.lang))
                if urlparse(self.path).path == "/api/write":
                    return self.send(*files.write(body, self.local(), self.lang))
                if urlparse(self.path).path == "/api/comment":
                    return self.send(*files.comment(body, self.local(), self.lang))
                if urlparse(self.path).path == "/api/comment/resolve":
                    return self.send(*files.resolve_thread(body, self.local(), self.lang))
                if urlparse(self.path).path == "/api/spec":   # "Accept" / "Drop" on a spec's strip
                    return self.send(*files.spec_act(body, self.lang))
                self.send(404, {"error": say(self.lang, "files.no_action")})

        return Handler

    def serve(self, port=PORT, host="127.0.0.1"):
        server = access.Server((host, port), self.handler())
        return server


def main():
    from core import instance
    port = instance.port("files")
    files = Files()
    files.desktop   # the file manager is asked once, here, not inside the first page request
    server = files.serve(port)
    print(f"hub files: http://127.0.0.1:{port}/", flush=True)
    access.serve_tailnet(lambda ip, p: files.serve(p, ip), port, files.state_dir)  # and from a phone, through the tailnet
    server.serve_forever()


if __name__ == "__main__":
    main()
