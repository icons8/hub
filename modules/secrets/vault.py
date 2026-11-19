"""The hub's own vault of secrets (spec d2345345): one file `<state>/secrets/vault.json` (0600 in a folder 0700), each
value sealed with AES-256-GCM of the `cryptography` library (core/cryptolib.py) — no cryptographic code of our own.

- In the open: the name, the sites (origins) a value may be put into, whether it may go into commands, a note, whether
  there is a login, when it changed. Sessions see these in `hub secret list`.
- Sealed (`box`): {"login", "password"}; a nonce of 12 random bytes per write; the additional data binds the name, so a
  box moved under another name does not open.
- The key is never in the state folder: the macOS login Keychain, a DPAPI-sealed file on Windows, a file 0600 in the
  settings folder on Linux (platforms.current.secret_key). A copy of the state folder (stands, archives, backups of it
  alone) does not open the values; a settings folder inside the state folder is refused.
- Only the page process writes (under its lock, temp file -> fsync -> replace -> fsync of the folder); the others read.
- A value never leaves this module except as the return of `value()`: not in an exception, a log or a fact.
- Who set the value and from where (`by`: a session's name and its file, spec d2441132) is in the open; the last
  HISTORY values before it stay sealed in `history` for the owner to return (`restore`) — `value()` never reads them."""
import base64
import datetime
import json
import os
import re
import tempfile
import threading
import urllib.parse

import platforms

V = 1
FOLDER = "secrets"
FILE = "vault.json"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
FIELDS = ("password", "username")
VALUE_MAX = 4096
VALUE_MIN = 4          # the output mask of `hub secret exec` replaces the value: a shorter one would cut ordinary text
NOTE_MAX = 200
SITES_MAX = 20
SOURCE_MAX = 300       # the file a session took a value from, as the tab shows it
HISTORY = 3            # the previous values the owner can return
KEY = re.compile(r"^[^\x00-\x1f\x7f]{1,64}$")   # the .env line or JSON field a value came from (`hub secret add --key`)
WHO = re.compile(r"^(|:terminal|[A-Za-z0-9][A-Za-z0-9._-]{0,39})$")   # "" — the owner on the page
AAD = b"hub-secrets-v1\0"
_lock = threading.Lock()


class Error(Exception):
    """A refusal with a catalog key (cli.secrets.*) and its parameters — never a value."""

    def __init__(self, key, **params):
        super().__init__(key)
        self.key, self.params = key, params


def folder(state_dir):
    return os.path.join(state_dir, FOLDER)


def path(state_dir):
    return os.path.join(folder(state_dir), FILE)


def config_dir(state_dir):
    """The settings folder of the hub that owns this state folder (core/catalog.config_dir, with the instance anchor)."""
    from core import catalog
    if os.environ.get("HUB_CONFIG_DIR"):
        return os.environ["HUB_CONFIG_DIR"]
    from core import instance
    return instance.config_dir(state_dir) or catalog.config_dir()


def account(state_dir):
    """The Keychain account of this hub: its instance name, "main" for the main hub."""
    from core import instance
    data = instance.load(state_dir)
    return data["name"] if data else "main"


def origin_of(text):
    """An origin from what a person types: `gitlab.example.com` -> `https://gitlab.example.com`; "" if it is not one."""
    text = (text or "").strip()
    if text and "://" not in text:
        text = "https://" + text
    u = urllib.parse.urlparse(text)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
        return ""
    try:
        port = u.port
    except ValueError:
        return ""
    host = u.hostname.lower()
    if ":" in host:   # IPv6: as a browser writes it in an origin
        host = f"[{host}]"
    else:
        try:   # an IDN as the browser's location.origin has it (punycode)
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return ""
    default = {"http": 80, "https": 443}[u.scheme]
    return f"{u.scheme}://{host}" + (f":{port}" if port and port != default else "")


# --- the file --------------------------------------------------------------------------------------------------------

def load(state_dir):
    """The file as it is: {"v", "items"}; an empty vault when there is none. A file that is not a vault is Error."""
    try:
        with open(path(state_dir), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"v": V, "items": {}}
    except (OSError, ValueError):
        raise Error("cli.secrets.vault_broken") from None
    if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
        raise Error("cli.secrets.vault_broken")
    return data


def _cut(text, most):
    """A long path as the tab shows it: its start and its end (the file's name) around "…"."""
    if len(text) <= most:
        return text
    tail = most // 2
    return text[:most - tail - 1] + "…" + text[-tail:]


def _by(by):
    """Who set a value and when: {"who"} ("" — the owner on the page, ":terminal", a session's name), "ts", and "source"
    (a file, "stdin") with its "key", or "restored" (the owner returned this version). Anything else — the owner."""
    if not (isinstance(by, dict) and isinstance(by.get("who"), str) and WHO.match(by["who"])):
        return {"who": ""}
    out = {"who": by["who"]}
    if isinstance(by.get("source"), str) and by["source"]:
        out["source"] = _cut(by["source"], SOURCE_MAX)
    if isinstance(by.get("key"), str) and KEY.match(by["key"]):
        out["key"] = by["key"]
    if isinstance(by.get("ts"), str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", by["ts"]):
        out["ts"] = by["ts"]
    if isinstance(by.get("restored"), str) and re.fullmatch(r"[0-9a-f]{8}", by["restored"]):
        out["restored"] = by["restored"]
    return out


def _history(it):
    return [h for h in it.get("history") or [] if isinstance(h, dict) and isinstance(h.get("box"), str) and h["box"]
            and isinstance(h.get("id"), str) and re.fullmatch(r"[0-9a-f]{8}", h["id"])][:HISTORY]


def _version(it):
    """The current value of a record as an entry of its history (sealed, as it is)."""
    by = _by(it.get("by"))
    return {"id": os.urandom(4).hex(), "box": it["box"], "login": it.get("login") is True,
            "changed": by.get("ts") or (it.get("changed") if isinstance(it.get("changed"), str) else ""), "by": by}


def _meta(name, it):
    if not (isinstance(name, str) and NAME.match(name) and isinstance(it, dict)):
        return None
    sites = [s for s in it.get("sites") or [] if isinstance(s, str) and origin_of(s) == s][:SITES_MAX]
    note = it.get("note") if isinstance(it.get("note"), str) else ""
    return {"name": name, "sites": sites, "commands": it.get("commands") is True, "note": note[:NOTE_MAX],
            "login": it.get("login") is True, "changed": it.get("changed") if isinstance(it.get("changed"), str) else "",
            "value": isinstance(it.get("box"), str) and bool(it["box"]), "by": _by(it.get("by")),
            "prev": [{"id": h["id"], "changed": h["changed"] if isinstance(h.get("changed"), str) else "",
                      "by": _by(h.get("by"))} for h in _history(it)]}


def items(state_dir):
    """{name: what may be shown} — without the boxes."""
    out = {}
    for name, it in load(state_dir)["items"].items():
        m = _meta(name, it)
        if m:
            out[name] = m
    return out


def _write(state_dir, data):
    d = folder(state_dir)
    platforms.current.makedirs_private(d)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=FILE + ".", suffix=".tmp")   # 0600 at once
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            platforms.current.sync(f.fileno())
        platforms.current.replace(tmp, path(state_dir))
        platforms.current.sync_dir(d)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- the key -----------------------------------------------------------------------------------------------------------

def _inside(child, parent):
    child, parent = os.path.realpath(child), os.path.realpath(parent)
    return os.path.normcase(child) == os.path.normcase(parent) or \
        os.path.normcase(child).startswith(os.path.normcase(parent.rstrip(os.sep) + os.sep))


def key(state_dir, create=False):
    """The vault key (32 bytes) or None. Error: the key would lie inside the state folder, or the OS store refused."""
    conf = config_dir(state_dir)
    keyfile = platforms.current.secret_key_file(conf)
    if keyfile and _inside(keyfile, state_dir):
        raise Error("cli.secrets.key_inside", folder=conf)
    try:
        return platforms.current.secret_key(account(state_dir), conf, create)
    except (OSError, ValueError) as e:
        raise Error("cli.secrets.key_store", error=type(e).__name__) from None


def lib(state_dir):
    from core import cryptolib
    try:
        return cryptolib.aesgcm(state_dir)
    except cryptolib.Unavailable:
        raise Error("cli.secrets.no_lib") from None


def status(state_dir):
    """"ok", "no_lib", "lost" (values are there, the key is not), "key_inside", "broken" (the file or the key store)."""
    try:
        lib(state_dir)
        sealed = next(((n, it["box"]) for n, it in load(state_dir)["items"].items()
                       if isinstance(it, dict) and isinstance(it.get("box"), str) and it["box"]), None)
        k = key(state_dir)
    except Error as e:
        return {"cli.secrets.no_lib": "no_lib", "cli.secrets.key_inside": "key_inside"}.get(e.key, "broken")
    if sealed and k is None:
        return "lost"
    if sealed:   # a key that is there but not the vault's (a copied folder, a replaced key file) is lost all the same
        try:
            _open(state_dir, k, *sealed)
        except Error:
            return "lost"
    return "ok"


def _seal(state_dir, k, name, box):
    aes = lib(state_dir)
    nonce = os.urandom(12)
    raw = json.dumps(box, ensure_ascii=False).encode("utf-8")
    return base64.b64encode(nonce + aes.AESGCM(k).encrypt(nonce, raw, AAD + name.encode())).decode("ascii")


def _open(state_dir, k, name, sealed):
    aes = lib(state_dir)
    try:
        raw = base64.b64decode(sealed.encode("ascii"), validate=True)
        box = json.loads(aes.AESGCM(k).decrypt(raw[:12], raw[12:], AAD + name.encode()).decode("utf-8"))
    except (ValueError, aes.InvalidTag):
        raise Error("cli.secrets.lost") from None
    if not isinstance(box, dict):
        raise Error("cli.secrets.lost")
    return {"login": box.get("login") if isinstance(box.get("login"), str) else "",
            "password": box.get("password") if isinstance(box.get("password"), str) else ""}


def value(state_dir, name, field):
    """The one field of one secret: the only place a value comes out. Error: no such secret or field, the key is lost."""
    it = load(state_dir)["items"].get(name)
    m = _meta(name, it) if it is not None else None
    if not m or not m["value"]:
        raise Error("cli.secrets.not_listed")
    if field == "username" and not m["login"]:
        raise Error("cli.secrets.no_login", name=name)
    k = key(state_dir)
    if k is None:
        raise Error("cli.secrets.lost")
    got = _open(state_dir, k, name, it["box"])["login" if field == "username" else "password"]
    if not got:
        raise Error("cli.secrets.not_listed")
    return got


def check_value(text, what="password"):
    """A value as the page sends it: one line, VALUE_MIN..VALUE_MAX characters, no control characters."""
    if not isinstance(text, str) or not (VALUE_MIN if what == "password" else 1) <= len(text) <= VALUE_MAX \
            or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise Error("cli.secrets.bad_value" if what == "password" else "cli.secrets.bad_login",
                    min=VALUE_MIN, max=VALUE_MAX)
    return text


def save(state_dir, name, password=None, login=None, sites=None, commands=None, note=None, now=None, by=None,
         owners_parts=False, replaced=None):
    """Add or change a secret; None = leave as it is (a new one needs a password). login "" removes the login. by —
    who changes it (_by; None — the owner on the page). A changed value or login puts the previous one into the
    history. owners_parts — the sites and "into commands" of an existing secret are the owner's: a request to change
    them is refused (a session's `hub secret add`, spec d2441132 §2.4). replaced — a dict filled with who set the value
    this one replaced and when ({"who", "changed"}), read under the same lock. Returns the names of the changed parts
    (for the fact `secret.saved`), never the values."""
    if not (isinstance(name, str) and NAME.match(name)):
        raise Error("cli.secrets.bad_name")
    if password is not None:
        check_value(password)
    if login not in (None, ""):
        check_value(login, "login")
    if sites is not None:
        if not isinstance(sites, list) or len(sites) > SITES_MAX:
            raise Error("cli.secrets.bad_site", site="")
        clean = []
        for s in sites:
            o = origin_of(s) if isinstance(s, str) else ""
            if not o:
                raise Error("cli.secrets.bad_site", site=str(s)[:80])
            if o not in clean:
                clean.append(o)
        sites = clean
    if note is not None and (not isinstance(note, str) or len(note) > NOTE_MAX):
        raise Error("cli.secrets.bad_note", max=NOTE_MAX)
    with _lock:
        data = load(state_dir)
        old = data["items"].get(name)
        old = old if isinstance(old, dict) else None
        if old is None and password is None:
            raise Error("cli.secrets.bad_value", min=VALUE_MIN, max=VALUE_MAX)
        if owners_parts and old is not None and (sites is not None or commands is not None):
            raise Error("cli.secrets.add_owner_parts", name=name)
        changed = []
        it = dict(old or {"sites": [], "commands": False, "note": ""})
        if password is not None or login is not None:
            sealed = any(isinstance(it2, dict) and it2.get("box") for it2 in data["items"].values())
            # a lost key with values still sealed: a new key would hide them for good — "Start over" first
            k = key(state_dir, create=not sealed)
            if k is None or sealed and status(state_dir) == "lost":
                raise Error("cli.secrets.lost")
            if old and old.get("box"):
                box = _open(state_dir, k, name, old["box"])
            elif password is None:   # no value yet (a lost key started over): a login alone would seal an empty one
                raise Error("cli.secrets.bad_value", min=VALUE_MIN, max=VALUE_MAX)
            else:
                box = {"login": "", "password": ""}
            if password is not None and password != box["password"]:
                box["password"] = password
                changed.append("value")
            if login is not None and login != box["login"]:
                box["login"] = login
                changed.append("login")
            it["box"] = _seal(state_dir, k, name, box)
            it["login"] = bool(box["login"])
        for part, new in (("sites", sites), ("commands", commands if commands is None else commands is True),
                          ("note", note)):
            if new is not None and it.get(part) != new:
                it[part] = new
                changed.append(part)
        if old is None:
            changed = ["new"] + changed
        value = "value" in changed or "login" in changed
        if value and old and isinstance(old.get("box"), str) and old["box"]:
            it["history"] = ([_version(old)] + _history(old))[:HISTORY]
            if replaced is not None:
                replaced.update(who=it["history"][0]["by"]["who"], changed=it["history"][0]["changed"])
        stamp = _stamp(now)
        if value:   # who set the value: a note or a switch changed later does not make it theirs
            it["by"] = {**_by(by), "ts": stamp}
        if changed:
            it["changed"] = stamp
            data["items"][name] = it
            data["v"] = V
            _write(state_dir, data)
        return changed


def _stamp(now=None):
    return (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def restore(state_dir, name, version, now=None):
    """The owner returns a previous value (its history id): it becomes the current one, the current one goes first into
    the history — a return can be returned. False if there is no such secret or version. No key needed: the boxes move
    sealed as they are."""
    with _lock:
        data = load(state_dir)
        it = data["items"].get(name)
        if not isinstance(it, dict):
            return False
        hist = _history(it)
        chosen = next((h for h in hist if h["id"] == version), None)
        if chosen is None:
            return False
        hist.remove(chosen)
        if isinstance(it.get("box"), str) and it["box"]:
            hist.insert(0, _version(it))
        # the version keeps who set it and from where; "restored" says the owner returned it (the card's line goes)
        it.update(box=chosen["box"], login=chosen.get("login") is True, history=hist[:HISTORY], changed=_stamp(now),
                  by={**_by(chosen.get("by")), "ts": _stamp(now), "restored": version})   # ts: current since now
        _write(state_dir, data)
        return True


def remove(state_dir, name):
    """Remove a secret; False if there was none."""
    with _lock:
        data = load(state_dir)
        if name not in data["items"]:
            return False
        del data["items"][name]
        _write(state_dir, data)
        return True


def start_over(state_dir, now=None):
    """The key is lost: the file goes aside as vault.lost-<date>.json, the names, sites and switches stay without values."""
    with _lock:
        data = load(state_dir)
        stamp = (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y%m%d-%H%M%S")
        aside = os.path.join(folder(state_dir), f"vault.lost-{stamp}.json")
        platforms.current.replace(path(state_dir), aside)
        kept = {n: {k: v for k, v in it.items() if k not in ("box", "login", "history", "by")} for n, it in data["items"].items()
                if isinstance(it, dict)}
        _write(state_dir, {"v": V, "items": kept})
        return aside
