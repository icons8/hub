"""The sign-in to the page by a link with a key — replaced by the page's password (core/pagepass.py). What is left:

- the state "key" of a hub that had it on and has no password yet: its cookie and its link work as before (from this
  machine; from elsewhere it never needed the key), until a password is set or the protection is taken off;
- the key a password puts next to itself, which nobody sees: a release rolled back to a code that knows no password
  finds the page closed by this key, and its `hub ui` opens the link;
- the marks of "Files" reports (render_mark), signed by core/pagepass.mark_secret.

- The key: 32 random bytes as 64 hex digits in <config>/page-key (0600 in the settings folder 0700). A file that is
  not a key (cut, edited by hand) lets nobody in from this machine in the state "key".
- The cookie: its name is the hub's (core/instance.cookie_name: cookies are per host, not per port — the page and
  "Files" of one hub share it, two hubs on 127.0.0.1 do not), its value is the key; HttpOnly, SameSite=Lax, 400 days.
- The key is never written to the journal, a log or an event.

The hub's modules are imported lazily, inside the functions (the folder, the cookie name): the bot, the CLI and the page
read the key with the same few lines."""
import hmac
import os
import re
import secrets
import tempfile

FILE = "page-key"
KEY = re.compile(r"[0-9a-f]{64}")
BROKEN = ""          # the file is there but holds no key: on, and nobody from this machine gets in
MAX_AGE = 400 * 86400   # the longest a browser keeps a cookie
_cache = {}          # path -> ((inode, mtime, size), key): read on every request of the page


def config_dir():
    from core import catalog
    return catalog.config_dir()


def path(folder=None):
    return os.path.join(folder or config_dir(), FILE)


def read(folder=None):
    """The key; None — the setting is off (no file); BROKEN — on, but the file is not a key (or not readable)."""
    p = path(folder)
    try:
        st = os.stat(p)
    except FileNotFoundError:
        return None
    except OSError:
        return BROKEN
    sig = (st.st_ino, st.st_mtime_ns, st.st_size)
    hit = _cache.get(p)
    if hit and hit[0] == sig:
        return hit[1]
    try:
        with open(p, "rb") as f:
            text = f.read(200).decode("ascii").strip()
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        text = ""
    key = text if KEY.fullmatch(text) else BROKEN
    _cache[p] = (sig, key)
    return key


def on(folder=None):
    return read(folder) is not None


def new():
    return secrets.token_hex(32)


def write(key, folder=None):
    """Turn the setting on with this key (or put a new key in place): whole and 0600 from the first byte."""
    if not KEY.fullmatch(key or ""):
        raise ValueError("not a page key")
    from platforms import current
    p = path(folder)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=FILE + ".", suffix=".tmp")   # 0600 at once
    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(key + "\n")
        current.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def off(folder=None):
    """Turn the setting off: True if it was on."""
    try:
        os.unlink(path(folder))
        return True
    except FileNotFoundError:
        return False


def cookie_name():
    """The cookie of this hub (core/instance.cookie_name: hub_main for the main hub, hub_<name> for an instance)."""
    from core import instance
    return instance.cookie_name(instance.name())   # the instance of this process's HUB_STATE_DIR, None — the main hub


def same(a, b):
    """Two keys are one, compared in constant time."""
    return isinstance(a, str) and isinstance(b, str) and hmac.compare_digest(a.encode("utf-8", "replace"), b.encode())


def cookie_ok(header, key, name=None):
    """Does the Cookie header carry this hub's cookie with the key? A cookie of the same name another program on this
    host set (its path, an old key) does not shadow ours: any matching value counts."""
    if not key or not header:
        return False
    name = name or cookie_name()
    for part in str(header).split(";"):
        k, eq, v = part.strip().partition("=")
        if eq and k.strip() == name and same(v.strip().strip('"'), key):
            return True
    return False


def set_cookie(key, name=None):
    """The Set-Cookie value that signs this browser in."""
    return f"{name or cookie_name()}={key}; Path=/; Max-Age={MAX_AGE}; HttpOnly; SameSite=Lax"


def cookie_header(folder=None):
    """For the hub's own commands that call the page from this machine (hub secret): the Cookie header with the key,
    {} while the setting is off."""
    key = read(folder)
    return {"Cookie": f"{cookie_name()}={key}"} if key else {}


def render_mark(key, root, folder, day):
    """The mark in the address of a "Files" report (/render/~<mark>/<root>/<folder>/…): its page runs in a sandbox with
    an opaque origin, so its css and images go without the cookie; the mark stands for it — for the report's folder
    and below only (its script may read the address and hand it on), for the day it was opened (and the next), and it
    changes with the key."""
    msg = "\0".join(("render", str(root), str(folder), str(int(day))))
    return hmac.new(key.encode(), msg.encode("utf-8", "replace"), "sha256").hexdigest()[:32]


def link(port, key, host="localhost"):
    return f"http://{host}:{int(port)}/?key={key}"


OPENER = "page-open.html"   # in the state folder: the link handed to the browser without the key in its command line


def opener(state_dir, url):
    """A browser started with the link in its arguments shows the key to every account of the machine (ps,
    /proc/<pid>/cmdline): `hub ui` hands it a file of this account instead (0600 in the state folder), which goes on
    to the link. Returns the file's path."""
    page = ('<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>Hub by Icons8</title>'
            f'<meta http-equiv="refresh" content="0;url={url}"><script>location.replace("{url}")</script>\n')
    p = os.path.join(state_dir, OPENER)
    fd, tmp = tempfile.mkstemp(dir=state_dir, prefix=OPENER + ".", suffix=".tmp")   # 0600 at once
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(page)
    from platforms import current
    current.replace(tmp, p)
    return p


def asked(query):
    """The key from the query of a link (?key=…), or None."""
    from urllib.parse import parse_qs
    got = parse_qs(query or "").get("key") or [""]
    return got[0] if len(got) == 1 and KEY.fullmatch(got[0]) else None


def let_in(headers, folder=None):
    """For a request from this machine: True — let in (the setting is off, or it carries the cookie); False — not.
    headers None (an earlier release's server in memory calls without them) with the setting on: not let in."""
    key = read(folder)
    if key is None:
        return True
    if headers is None or not key:
        return False
    return cookie_ok(headers.get("Cookie"), key)
