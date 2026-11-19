"""The page's password: a hub's page and "Files" open only after the password, from any place — this machine, the
tailnet, Cloudflare Access. Set in "Settings -> General -> Hubs on this computer" (the row of each hub) or by
`hub password set`; off by default on every hub.

The sign-in has two steps (core/remote.admit): first who came — a request of this machine, or the owner as a remote
access provider names him (everybody else is refused before any form); then, when the hub has a password, the cookie
the sign-in form gives.

- The file: <config>/page-password (0600 in the settings folder): PBKDF2-SHA256 of the password with its salt and
  rounds, and the secret that signs the cookies. The file is the switch: no file — no password. A file that cannot be
  read as one is "broken": nobody gets in until `hub password set|off` in the terminal (closed, never open).
- The cookie: the hub's own name (core/instance.cookie_name: the page and "Files" of one hub share it, two hubs on
  127.0.0.1 do not), its value "v1.<issued>.<HMAC of the issue time>", 30 days, renewed by the page load once a day.
  Changing the password makes a new secret: every cookie of every device is out.
- The tries: one counter per hub in <config>/page-password.tries, whatever checks the password (the page's form, the
  row of a neighbouring hub, "Files"): 5 wrong in a row lock the form for a minute, each wrong one after that doubles it
  up to an hour; a right one clears it. Browsers already signed in are not touched.
- The old sign-in by a link with a key (core/pagekey.py): a hub that had it on and has no password yet is in the state
  "key" — its old cookie and link work as before, until a password is set or the protection is taken off. Setting a
  password puts a new random page-key next to it that nobody sees: a release rolled back to a code that knows no
  password finds the page closed by the key, not open.
- The local token: <state>/page-local (0600, new at every start of the page) lets the hub's own `hub secret fill|add` reach
  POST /api/secrets/fill and /add of its page without the cookie, those paths only: the file is read by the owner's OS account
  alone, which can decrypt the secrets' store anyway; it opens neither the page nor /api/send.

Neither the password nor the secret is ever written to the journal, a log or an event."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

FILE = "page-password"
TRIES = "page-password.tries"
LOCAL = "page-local"
KDF, ROUNDS = "pbkdf2_sha256", 600_000   # OWASP's figure for PBKDF2-SHA256; the file names them (a later raise needs no move)
MIN_LEN, MAX_LEN = 8, 256
MAX_AGE = 30 * 86400   # a cookie lives this long after its last renewal
RENEW = 86400          # the page load renews a cookie older than this
SKEW = 60              # an issue time this far in the future still counts (clocks)
FREE = 5               # wrong tries before the first lock
LOCK, LOCK_MAX = 60, 3600
STATES = ("open", "password", "key", "broken")
BROKEN = {}            # the file is there but holds no password: closed
_cache = {}            # path -> ((inode, mtime, size), record): read on every request
_check = threading.Lock()   # one PBKDF2 at a time in this process: parallel tries queue, not multiply
_tries_lock = threading.Lock()   # a try reads the counter, checks and writes it as one step: parallel ones see each other
_writing = threading.Lock()


def folder_of(folder=None):
    if folder:
        return folder
    from core import catalog
    return catalog.config_dir()


def path(folder=None):
    return os.path.join(folder_of(folder), FILE)


def _valid(rec):
    try:
        return (rec.get("v") == 1 and rec.get("kdf") == KDF and isinstance(rec.get("iter"), int)
                and 1000 <= rec["iter"] <= 10_000_000 and len(base64.b64decode(rec["salt"], validate=True)) >= 16
                and len(base64.b64decode(rec["hash"], validate=True)) == 32
                and len(bytes.fromhex(rec["cookie"])) == 32)
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def read(folder=None):
    """The record; None — no password (no file); BROKEN — a file that is not one (or not readable)."""
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
            rec = json.loads(f.read(4096).decode("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        rec = None
    rec = rec if isinstance(rec, dict) and _valid(rec) else BROKEN
    _cache[p] = (sig, rec)
    return rec


def state(folder=None):
    """open | password | key (the old link with a key on, no password yet) | broken."""
    rec = read(folder)
    if rec:
        return "password"
    if rec is not None:
        return "broken"
    from core import pagekey
    return "key" if pagekey.read(folder) is not None else "open"


def _derive(password, salt, rounds):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)


def fits(password):
    """None — a password this hub takes; else the i18n key of why not."""
    if not isinstance(password, str) or len(password) < MIN_LEN:
        return "web.pass.too_short"
    if len(password) > MAX_LEN or "\x00" in password:
        return "web.pass.too_long"
    return None


def matches(password, folder=None, rec=None):
    """The password is this hub's (constant time; one PBKDF2 at a time in this process). No counter: see attempt()."""
    rec = read(folder) if rec is None else rec
    if not rec or not isinstance(password, str) or fits(password) == "web.pass.too_long":
        return False
    with _check:
        got = _derive(password, base64.b64decode(rec["salt"]), rec["iter"])
    return hmac.compare_digest(got, base64.b64decode(rec["hash"]))


def _write(p, data):
    """JSON for the owner only (0600 from the first byte) and whole: core/catalog.write_private."""
    from core import catalog
    os.makedirs(os.path.dirname(p), exist_ok=True)
    catalog.write_private(p, data)


def set_password(password, folder=None, now=None):
    """Set or change the password: a new random page-key first (a rollback to a code that knows no password finds the
    page closed; a cut between the two steps leaves a key nobody has — closed too), then the record. Every cookie of
    the old secret is out. -> the record."""
    why = fits(password)
    if why:
        raise ValueError(why)
    from core import pagekey
    salt = secrets.token_bytes(16)
    rec = {"v": 1, "kdf": KDF, "iter": ROUNDS, "salt": base64.b64encode(salt).decode(),
           "hash": base64.b64encode(_derive(password, salt, ROUNDS)).decode(), "cookie": secrets.token_hex(32),
           "at": int(time.time() if now is None else now)}
    with _writing:
        pagekey.write(pagekey.new(), folder)
        _write(path(folder), rec)
        _clear_tries(folder)
    return rec


def take_off(folder=None):
    """No password and no key any more: the record first, then the key (a cut between them leaves the key nobody has:
    closed). True if there was either."""
    from core import pagekey
    with _writing:
        try:
            os.unlink(path(folder))
            had = True
        except FileNotFoundError:
            had = False
        had = pagekey.off(folder) or had
        _clear_tries(folder)
    return had


# --- the tries ---------------------------------------------------------------------------------------------------

def _tries_path(folder=None):
    return os.path.join(folder_of(folder), TRIES)


def _tries(folder=None):
    try:
        with open(_tries_path(folder), encoding="utf-8") as f:
            got = json.load(f)
        fails, until = int(got.get("fails") or 0), float(got.get("until") or 0)
        return max(0, fails), until
    except (OSError, ValueError, AttributeError, TypeError, OverflowError):
        return 0, 0.0


def _clear_tries(folder=None):
    try:
        os.unlink(_tries_path(folder))
    except OSError:
        pass


def wait(folder=None, now=None):
    """Seconds the form is still locked for, 0 — not locked."""
    now = time.time() if now is None else now
    return max(0, int(_tries(folder)[1] - now + 0.999))


def attempt(password, folder=None, now=None):
    """A try of the password with the hub's counter: -> ("ok" | "wrong" | "locked" | "none", seconds to wait).
    "none" — the hub has no password (or a broken file): nothing to sign in to."""
    now = time.time() if now is None else now
    rec = read(folder)
    if not rec:
        return "none", 0
    with _tries_lock:   # in this process tries go one by one, each after the previous one's count is written
        left = wait(folder, now)
        if left:
            return "locked", left
        if matches(password, folder, rec):
            _clear_tries(folder)
            return "ok", 0
        fails = _tries(folder)[0] + 1
        lock = min(LOCK_MAX, LOCK * 2 ** (fails - FREE)) if fails >= FREE else 0
        try:
            _write(_tries_path(folder), {"fails": fails, "until": now + lock})
        except OSError:
            pass
    return "wrong", lock


def said(verdict, wait):
    """What a refused try says: (i18n key, params) — the sign-in form and the row of the hubs alike."""
    minutes = max(1, (int(wait) + 59) // 60)
    if verdict == "locked":
        return "web.pass.locked", {"minutes": minutes}
    return ("web.pass.wrong_locked", {"minutes": minutes}) if wait else ("web.pass.wrong", {})


# --- the cookie --------------------------------------------------------------------------------------------------

def cookie_name():
    from core import pagekey
    return pagekey.cookie_name()


def _sign(rec, issued):
    return hmac.new(bytes.fromhex(rec["cookie"]), f"page\0{issued}".encode(), "sha256").hexdigest()


def issued(header, rec, name=None, now=None):
    """The issue time of a valid sign-in cookie of this hub in the Cookie header, or None. Other programs on this host
    may set a cookie of the same name: any valid one counts."""
    if not rec or not header:
        return None
    now = time.time() if now is None else now
    name = name or cookie_name()
    best = None
    for part in str(header).split(";"):
        k, eq, v = part.strip().partition("=")
        if not eq or k.strip() != name:
            continue
        ver, _, rest = v.strip().strip('"').partition(".")
        at, _, sig = rest.partition(".")
        if ver != "v1" or not (at.isascii() and at.isdigit()) or len(at) > 12:
            continue
        at = int(at)
        if not (now - MAX_AGE <= at <= now + SKEW) or not hmac.compare_digest(sig.encode(), _sign(rec, at).encode()):
            continue
        best = at if best is None else max(best, at)
    return best


def set_cookie(rec, name=None, now=None, secure=False):
    at = int(time.time() if now is None else now)
    return (f"{name or cookie_name()}=v1.{at}.{_sign(rec, at)}; Path=/; Max-Age={MAX_AGE}; HttpOnly; SameSite=Lax"
            + ("; Secure" if secure else ""))


def clear_cookie(name=None, secure=False):
    return f"{name or cookie_name()}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" + ("; Secure" if secure else "")


def renewal(headers, folder=None, now=None, secure=False):
    """The Set-Cookie that renews this browser's sign-in (a page load with a cookie older than RENEW), or None."""
    rec = read(folder)
    if not rec or headers is None:
        return None
    now = time.time() if now is None else now
    at = issued(headers.get("Cookie"), rec, now=now)
    return set_cookie(rec, now=now, secure=secure) if at is not None and now - at >= RENEW else None


def let_in(headers, remote=False, folder=None):
    """The second step of the sign-in for a request the first one let in (this machine, or the owner by a provider):
    True — no password, or this browser signed in. State "key": from this machine the old cookie of the key, from
    elsewhere as before (no key). headers None (an earlier release's server in memory): only an open hub."""
    st = state(folder)
    if st == "open":
        return True
    if st == "key":
        if remote:
            return True
        from core import pagekey
        return headers is not None and pagekey.let_in(headers, folder)
    if st == "broken" or headers is None:
        return False
    return issued(headers.get("Cookie"), read(folder)) is not None


def mark_secret(folder=None):
    """What signs the marks of "Files" reports (/render/~<mark>/…, core/pagekey.render_mark): the cookie secret of the
    password, the key in the state "key", None — an open hub (no mark needed)."""
    st = state(folder)
    if st == "password":
        return read(folder)["cookie"]
    if st == "key":
        from core import pagekey
        return pagekey.read(folder) or None
    return None


# --- the local token of the hub's own commands --------------------------------------------------------------------

LOCAL_PATHS = ("/api/secrets/fill", "/api/secrets/add")   # `hub secret fill` and `hub secret add` (spec d2441132)


def new_local(state_dir):
    """At the start of the page: a new token in <state>/page-local (0600). -> the token."""
    token = secrets.token_hex(32)
    _write(os.path.join(state_dir, LOCAL), {"token": token})
    return token


def local_token(state_dir):
    try:
        with open(os.path.join(state_dir, LOCAL), encoding="utf-8") as f:
            got = json.loads(f.read(400)).get("token")
    except (OSError, ValueError, AttributeError):
        return ""
    return got if isinstance(got, str) and len(got) == 64 else ""


def local_ok(headers, state_dir, path_):
    """The hub's own command from this machine on one of LOCAL_PATHS with the token of this start of the page."""
    if path_ not in LOCAL_PATHS or headers is None:
        return False
    mine, asked = local_token(state_dir), headers.get("X-Hub-Local") or ""
    return bool(mine) and hmac.compare_digest(asked.encode("utf-8", "replace"), mine.encode())


def local_header(state_dir):
    """For `hub secret fill|add`: {"X-Hub-Local": token}, {} when the page has not started."""
    token = local_token(state_dir)
    return {"X-Hub-Local": token} if token else {}


# --- every hub of this OS account ---------------------------------------------------------------------------------

def unprotected(knows=None, state_dir=None, rows=None):
    """The hubs of this OS account on this machine without a password, this one too: their instance names ("" — the
    main hub), in the order of core/instancecli.rows; [] — every one has a password. A hub without one opens the others
    (its "Files" read the whole disk of the account, its sessions run as it), so a password holds only when this list
    is empty. knows(row) -> False: the hub's running code knows no password — open whatever its files say. rows —
    core/instancecli.rows already read by the caller."""
    from core import instancecli, runtime
    state_dir = state_dir or runtime.state_dir()
    out, mine = [], False
    for row in instancecli.rows() if rows is None else rows:
        me = bool(row.get("state")) and os.path.realpath(row["state"]) == os.path.realpath(state_dir)
        mine = mine or me
        folder = None if me else row.get("config_dir")
        st = state(folder) if me or folder else "open"
        if st != "password" or (not me and knows is not None and not knows(row)):
            out.append(row.get("name") or "")
    if not mine and state() != "password":   # this hub outside the registry (a copy, a stand)
        from core import instance
        out.insert(0, instance.name(state_dir) or "")
    return out
