"""The page's files without a revalidation on every open.

index.html, as the server sends it, points its stylesheets, scripts and icons at /static/v/<version>/…, where the
version is the content of the whole static/ folder; relative imports and url(../fonts/…) stay under the same prefix by
themselves. An import map sends the plain /static/js/… (plugins and tests import page modules by that address) to the
same versioned modules, so a module is never loaded twice. A file under a version is cached for good (immutable) only
when the bytes sent are that version's bytes; otherwise (an update in the middle of a load, a version that is not on
disk) it goes as before, revalidated. The file on disk, index.html, stays with the plain addresses: a page server
started before an update serves it as it is."""
import hashlib
import json
import os
import re
import threading
import time

PREFIX = "/static/v/"
FOREVER = "private, max-age=31536000, immutable"
FRESH = 2.0   # seconds a walk is reused by file requests; the page itself always walks anew
VERSIONED = re.compile(r"/(?:static|vendor)/v/([0-9a-f]{12})/([^?#]+)")

_lock = threading.Lock()
_walks = {}   # static folder -> (when the walk began, version, {relative path: ((size, mtime, ctime, inode), sha1)})


def walk(static, fresh=None):
    """(version, sums) of the static folder: sums maps a relative path ("js/main.js") to the sha1 of its content. A
    file is read again only when its size, times or inode changed. fresh — reuse a walk no older than that many
    seconds; None — walk now."""
    with _lock:
        got = _walks.get(static)
    if got and fresh is not None and time.time() - got[0] < fresh:
        return got[1], got[2]
    old = got[2] if got else {}
    began = time.time()
    h, sums = hashlib.sha1(), {}
    for base, dirs, files in os.walk(static):
        dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x != "__pycache__")
        for f in sorted(files):
            if f.startswith("."):
                continue   # .DS_Store, editor swap files: not served, must not change the version
            path = os.path.join(base, f)
            rel = os.path.relpath(path, static).replace(os.sep, "/")
            try:
                st = os.stat(path)
                key = (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)
                entry = old.get(rel)
                if not entry or entry[0] != key:
                    with open(path, "rb") as fh:
                        entry = (key, hashlib.sha1(fh.read()).hexdigest())
            except OSError:
                continue
            sums[rel] = entry
            h.update(f"{rel}:{entry[1]}\n".encode())
    ver = h.hexdigest()[:12] if sums else None
    with _lock:
        # a walk that began before a newer one finished must not overwrite it (it may name the files before an update)
        if static not in _walks or _walks[static][0] <= began:
            _walks[static] = (began, ver, sums)
    return ver, sums


def forget(static):
    """The disk no longer matches the last walk: the next request walks anew."""
    with _lock:
        _walks.pop(static, None)


TAGS = re.compile(r'(<link rel="stylesheet" href="|<script src=")/static/')
ENTRY = '<script type="module" src="/static/js/main.js"></script>'


def page(html, ver, vendor=None, hints=None):
    """index.html with its files under the version and the import map first in <head>; without a version — as it is.
    The entry module is imported by its plain address, through the map: a browser without import maps (Safari before
    16.4) then loads every module by the plain addresses, as before — never one module twice, under two addresses.
    vendor — the version of the vendor/ folder, for its scripts. hints — a function of the address prefix of the page's
    modules ("/static/v/<ver>/" or "/static/") returning the <link> tags that announce the module graph (preload.py):
    they replace the preload of the entry alone, and in a page without a version they still go before </head>."""
    if not ver or ENTRY not in html or "<head>" not in html:
        tags = hints("/static/") if hints else ""   # an entry of another shape: as it was, rather than one module under two addresses
        return html.replace("</head>", tags + "</head>", 1) if tags else html
    prefix = f"{PREFIX}{ver}/"
    imports = json.dumps({"imports": {"/static/js/": f"{prefix}js/"}})
    tags = (hints(prefix) if hints else "") or f'<link rel="modulepreload" href="{prefix}js/main.js">\n'
    html = html.replace(ENTRY, '<script type="module">import "/static/js/main.js";</script>', 1)
    # stylesheets and scripts — what the first screen waits for; icons keep their plain addresses: an installed app's
    # icon whose address changed with every update would ask the phone to update the app
    html = TAGS.sub(lambda m: m.group(1) + PREFIX + ver + "/", html)
    if vendor:
        html = html.replace('src="/vendor/', f'src="/vendor/v/{vendor}/')
    # the entry by its versioned address for the browser's preload scanner: an inline import is invisible to it, and
    # behind the parser-blocking vendor scripts the modules started a round trip later on an empty cache
    return html.replace("<head>", f'<head>\n<script type="importmap">{imports}</script>\n' + tags, 1)


def split(path):
    """(version, relative path) of a versioned address (/static/v/… or /vendor/v/…), or None."""
    m = VERSIONED.fullmatch(path)
    return (m.group(1), m.group(2)) if m else None


def cache_for(folder, ver, rel, data):
    """Cache-Control for the bytes data of folder/rel sent under version ver: for good only if they are that
    version's bytes."""
    cur, sums = walk(folder, FRESH)
    sha = hashlib.sha1(data).hexdigest()
    entry = sums.get(rel)
    if cur == ver and entry and entry[1] == sha:
        return FOREVER, sha
    if cur == ver:
        forget(folder)   # the walk named this version, but the file is already another: walk anew next time
    return "no-cache", sha
