#!/usr/bin/env python3
"""The delivery server of the hub app's releases: the code layers, the environments (the app installers) and the install
scripts of the team build, the list of what is current (releases.json) and the open minimum version (min.json). One
file, the Python 3.12+ standard library.

Who may do what:
- GET /healthz and GET /min.json — anyone: a hub below the minimum has to learn it even when its key no longer reads;
- GET|HEAD /v1/releases.json, /v1/layer/<file>, /v1/app/<file>, /v1/script/<file> — the build key (`Authorization:
  Bearer <key>`, or `?key=<key>` in the address: the link to an installer in a team chat); a broken download continues
  with Range;
- GET|HEAD /i/<key>/install.sh and /i/<key>/install.ps1 — the build key in the address (a line pasted into a terminal
  has no header): the current script, with the key written into it (below);
- PUT — only the release key, which lives on the machine that makes the releases (tools/package/upload.py);
- POST /v1/stand — the build key: "this hub stands on <release>" (below).

The server keeps no key: HUB_DELIVER_READ_KEYS and HUB_DELIVER_WRITE_KEYS hold the sha256 (hex) of the keys,
comma-separated (two at once while a key is being changed). A key in an address never reaches the log: ?key= is cut
from a logged path, and in /i/<key>/… the key is not written (any path that looks like one is logged as /i/-).

The install scripts (one-line install): the files install-<version>.sh and .ps1 of /v1/script/ are published like the
others (never replaced; ASCII only, at most 256 KiB), and releases.json names the current ones in "scripts":
{"install.sh": {url, sha256, size, version}, "install.ps1": {...}}. GET /i/<key>/install.sh finds the key among the
build keys (not among the release keys: that one never goes into a script) and answers the current script as text/plain;
charset=utf-8, no-store, with the key written into it: where the script has the mark @@HUB_DELIVER_KEY@@ the key stands
in its place (a PowerShell param() block has to stay first), else a first line is put in front — HUB_DELIVER_KEY=<key>
for .sh (after a #! line), $HUB_DELIVER_KEY = '<key>' for .ps1. Whatever else is asked — an unknown key, a release key,
nothing published — is a 404 with no body and no hint.
The environments (the archive of a platform) are files of /v1/app/; releases.json names them in
"runtime": {"shells": {<platform>: {url, sha256, size, runtime}}}, the platform one of PLATFORMS.

Writing:
- PUT /v1/(layer|app|script)/<file>?offset=N&size=S&sha256=H — the file's bytes from N; one body is at most 64 MiB (a proxy in
  front, such as Cloudflare, takes at most 100 MB), so a big file goes in parts. Parts gather in uploads/; the file is
  published when it is whole and its bytes give H. A published file is never replaced: the same bytes again — 200
  "same", other bytes — 409. A part that does not start where the gathered bytes end — 409 with X-Offset (where to go
  on). 201 — stored, 202 — a part taken, 413 — a body over the limit, 422 — the sum does not match (the parts are
  dropped), 507 — no room.
- PUT /v1/releases.json — changes only from the version read: If-Match with its ETag, or If-None-Match: * for the
  first one (else 428/412). Every {url, sha256, size} in it must name a stored file with that sum and size.
- PUT /min.json — {"team"|"open": {"version", "reason": {language: text}, "since": "YYYY-MM-DD", "stops": [...]}},
  under the same If-Match / If-None-Match: * as releases.json.
Stands (slice A3n): a hub that came up on a release says so, once, with a random id of its own (the build key is one for
the whole team, so it cannot tell hubs apart): POST /v1/stand {"id": 32-64 hex, "version": "1.9.0" | null} — null takes
the hub off (the owner returned to the earlier release). The server keeps sha256(id) -> (release, when) in stands.json,
never the id and never in the log; a hub counts once and on one release (a new word moves it), and a word older than 30
days counts for nothing (the hubs say it again weekly). A reader of GET /v1/releases.json (the build key) finds
"stands": N in each channel, the number of hubs on that channel's release; the release key gets the document as stored
(it writes back what it read). 507 — the list is full (5000 hubs).
Every write and refused write is a line in log.jsonl (time, path, size, sum, result, the writer's address).

Settings: HUB_DELIVER_DATA (/data), HUB_DELIVER_PORT (8080), HUB_DELIVER_HOST (0.0.0.0), HUB_DELIVER_TRUST_PROXY=1 (the
writer's address from CF-Connecting-IP / X-Forwarded-For).

  server.py serve      run the server
  server.py keygen     a new key on stdout, its sha256 (for the settings) on stderr — run it where the key will live
  server.py check      the data folder is writable (the image health check)
  server.py du         what is stored and how big
"""
import contextlib
import datetime
import hashlib
import hmac
import http.server
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse

MAX_BODY = 64 << 20          # one PUT body
MAX_SIZE = 4 << 30           # one file
MAX_JSON = 1 << 20           # releases.json
MAX_MIN = 64 << 10           # min.json
SPARE = 512 << 20            # room left on the volume after a file
KINDS = ("layer", "app", "script")
PLATFORMS = ("mac-arm64", "mac-x64", "linux-x64", "linux-arm64", "windows-x64", "windows-arm64")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,199}")
SHA = re.compile(r"[0-9a-f]{64}")
VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}")
WORD = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
FLAVORS = ("team", "open")
BLOCK = 1 << 20
STAND_ID = re.compile(r"[0-9a-f]{32,64}")
MAX_STAND = 8 << 10          # one POST /v1/stand body
MAX_SCRIPT = 256 << 10       # one install script
SCRIPT_NAME = re.compile(r"install-[0-9A-Za-z][0-9A-Za-z.+-]{0,63}\.(sh|ps1)")
SCRIPTS = ("install.sh", "install.ps1")
INSTALL = re.compile(r"/i/([^/]+)/(install\.sh|install\.ps1)")
KEY_CHARS = re.compile(r"[A-Za-z0-9_-]{1,128}")      # what `keygen` makes (urlsafe); only such a key can be written in
KEY_MARK = b"@@HUB_DELIVER_KEY@@"
SCRIPT_TEXT = re.compile(rb"[\t\r\n\x20-\x7e]+")      # printable ASCII (tools/package/deliver_send.py holds the same)


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(BLOCK), b""):
            h.update(block)
    return h.hexdigest()


def write_atomic(path, data):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def forget(*paths):
    for p in paths:
        with contextlib.suppress(OSError):
            os.unlink(p)


class Refused(Exception):
    def __init__(self, status, code, headers=None):
        super().__init__(code)
        self.status, self.code, self.headers = status, code, headers or {}


def min_doc(doc):
    """min.json's form, or Refused."""
    if not isinstance(doc, dict) or not doc or set(doc) - set(FLAVORS):
        raise Refused(400, "form")
    for entry in doc.values():
        if not isinstance(entry, dict) or set(entry) - {"version", "reason", "since", "stops"}:
            raise Refused(400, "form")
        if not isinstance(entry.get("version"), str) or not VERSION.fullmatch(entry["version"]):
            raise Refused(400, "version")
        reason = entry.get("reason", {})
        if not isinstance(reason, dict) or not all(isinstance(k, str) and isinstance(v, str) and len(k) <= 16
                                                   and len(v) <= 2000 for k, v in reason.items()):
            raise Refused(400, "reason")
        since = entry.get("since")
        if since is not None:
            if not isinstance(since, str) or not DAY.fullmatch(since):
                raise Refused(400, "since")
            try:
                datetime.date.fromisoformat(since)
            except ValueError:
                raise Refused(400, "since") from None
        stops = entry.get("stops", [])
        if not isinstance(stops, list) or len(stops) > 16 or \
                not all(isinstance(s, str) and WORD.fullmatch(s) for s in stops):
            raise Refused(400, "stops")
    return doc


class Stands:
    """Which release each hub stands on: sha256(hub id) -> [release, when]; see the module's text."""
    TTL = 30 * 86400         # a word older than this counts for nothing
    REFRESH = 86400          # the same word again within this is not written
    LIMIT = 5000

    def __init__(self, data, clock=time.time):
        self.path = os.path.join(data, "stands.json")
        self.clock, self.limit = clock, self.LIMIT
        self.lock = threading.Lock()
        try:
            with open(self.path, encoding="utf-8") as f:
                got = json.load(f)
        except (OSError, ValueError):
            got = {}
        self.items = {k: v for k, v in got.items() if SHA.fullmatch(str(k)) and isinstance(v, list) and len(v) == 2
                      and isinstance(v[0], str) and isinstance(v[1], (int, float))} if isinstance(got, dict) else {}

    def save(self, items):
        write_atomic(self.path, json.dumps(items, separators=(",", ":")).encode())

    def say(self, hub_id, version):
        """'stored' | 'same' | 'removed'; Refused(507) — the list is full and this hub is not on it. The list changes on a
        copy and is kept only once it is on the disk: a write that fails (OSError, to the caller) changes nothing."""
        key = hashlib.sha256(hub_id.encode()).hexdigest()
        with self.lock:
            now = self.clock()
            items = {k: v for k, v in self.items.items() if v[1] >= now - self.TTL}
            changed = len(items) != len(self.items)
            had = items.get(key)
            if version is None:
                answer = "removed"
                if had is not None:
                    del items[key]
                    changed = True
            elif had is not None and had[0] == version and now - had[1] < self.REFRESH:
                answer = "same"
            else:
                if had is None and len(items) >= self.limit:
                    raise Refused(507, "full")
                items[key] = [version, round(now)]
                answer, changed = "stored", True
            if changed:
                self.save(items)
            self.items = items
            return answer

    def count(self, version):
        with self.lock:
            since = self.clock() - self.TTL
            return sum(1 for v in self.items.values() if v[0] == version and v[1] >= since)


class Store:
    """The files on the volume: files/ (published), meta/ (sum and size of each), uploads/ (parts), log.jsonl."""

    def __init__(self, data):
        self.data = data
        for d in ("files", "meta", "uploads", *(os.path.join(x, k) for x in ("files", "meta") for k in KINDS)):
            os.makedirs(os.path.join(data, d), exist_ok=True)
        self.lock = threading.Lock()
        self.locks = {}          # one per name; only the release key's writes reach here
        self.log_lock = threading.Lock()
        self.stands = Stands(data)

    def lock_of(self, key):
        with self.lock:
            return self.locks.setdefault(key, threading.Lock())

    def path(self, kind, name):
        return os.path.join(self.data, "files", kind, name)

    def read_meta(self, kind, name):
        try:
            with open(os.path.join(self.data, "meta", kind, name + ".json")) as f:
                m = json.load(f)
            if isinstance(m, dict) and SHA.fullmatch(str(m.get("sha256"))) and isinstance(m.get("size"), int):
                return m
        except (OSError, ValueError):
            pass
        return None

    def meta(self, kind, name, locked=False):
        """{sha256, size} of a published file, None when there is none. A file published without its meta (the
        process stopped between the two) gets it now, from its bytes, once (under the name's lock); a meta that
        cannot be written is still answered."""
        if not os.path.isfile(self.path(kind, name)):
            return None
        m = self.read_meta(kind, name)
        if m is not None:
            return m
        with contextlib.nullcontext() if locked else self.lock_of((kind, name)):
            m = self.read_meta(kind, name)
            if m is None:
                path = self.path(kind, name)
                m = {"sha256": file_sha(path), "size": os.path.getsize(path), "at": now(), "healed": True}
                with contextlib.suppress(OSError):
                    write_atomic(os.path.join(self.data, "meta", kind, name + ".json"), json.dumps(m).encode())
            return m

    def log(self, **fields):
        line = json.dumps({"at": now(), **fields}, ensure_ascii=False) + "\n"
        with self.log_lock, open(os.path.join(self.data, "log.jsonl"), "a") as f:
            f.write(line)

    # --- immutable files

    def put_part(self, kind, name, offset, size, digest, length, read):
        """Takes `length` bytes at `offset` from read(n); (status, answer). Refused on a conflict."""
        with self.lock_of((kind, name)):
            had = self.meta(kind, name, locked=True)
            if had is not None:
                if had["sha256"] == digest and had["size"] == size:
                    return 200, {"state": "same"}
                raise Refused(409, "published")
            if offset + length > size:
                raise Refused(400, "past-size")
            part = os.path.join(self.data, "uploads", f"{kind}--{name}.part")
            want_file = part[:-5] + ".want"
            try:
                with open(want_file) as f:
                    want = json.load(f)
            except (OSError, ValueError):
                want = None
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if offset == 0:
                if os.path.exists(part):
                    os.truncate(part, 0)
                have = 0
                want = {"size": size, "sha256": digest}
                write_atomic(want_file, json.dumps(want).encode())
            elif want != {"size": size, "sha256": digest}:
                raise Refused(409, "other-upload", {"X-Offset": "0"})
            if offset != have:
                raise Refused(409, "offset", {"X-Offset": str(have)})
            if shutil.disk_usage(os.path.join(self.data, "uploads")).free < size - have + SPARE:
                raise Refused(507, "no-room")
            got = 0
            with open(part, "ab") as f:
                try:
                    while got < length:
                        block = read(min(BLOCK, length - got))
                        if not block:
                            break
                        f.write(block)
                        got += len(block)
                    if got < length:
                        raise Refused(400, "short-body")
                except BaseException:
                    f.truncate(offset)       # a broken part leaves nothing: the next one goes from `offset` again
                    raise
                f.flush()
                os.fsync(f.fileno())
            if offset + length < size:
                return 202, {"state": "partial", "offset": offset + length}
            if file_sha(part) != digest:
                forget(part, want_file)
                raise Refused(422, "sum")
            if kind == "script":
                with open(part, "rb") as f:
                    if not script_form(f.read(MAX_SCRIPT + 1)):
                        forget(part, want_file)
                        raise Refused(422, "script")
            final = self.path(kind, name)
            os.link(part, final)     # never over an existing name; a missing meta is healed by meta()
            write_atomic(os.path.join(self.data, "meta", kind, name + ".json"),
                         json.dumps({"sha256": digest, "size": size, "at": now()}).encode())
            for p in (part, want_file):
                with contextlib.suppress(OSError):
                    os.unlink(p)
            os.chmod(final, 0o444)   # after the part is gone: Windows does not delete a read-only file
            return 201, {"state": "stored", "sha256": digest, "size": size}

    # --- the two documents

    def doc_path(self, name):
        return os.path.join(self.data, "files", name)

    def doc(self, name):
        try:
            with open(self.doc_path(name), "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def check_refs(self, node):
        """Every {url, sha256, size} names a stored file with that sum and size."""
        if isinstance(node, list):
            for x in node:
                self.check_refs(x)
        elif isinstance(node, dict):
            if "url" in node:
                m = re.fullmatch(r"/v1/(layer|app|script)/([^/]+)", str(node["url"]))
                if not m or not NAME.fullmatch(m.group(2)) or \
                        (m.group(1) == "script" and not SCRIPT_NAME.fullmatch(m.group(2))):
                    raise Refused(400, "url")
                had = self.meta(m.group(1), m.group(2))
                if not had or had["sha256"] != node.get("sha256") or had["size"] != node.get("size"):
                    raise Refused(400, "not-stored")
            for x in node.values():
                self.check_refs(x)

    def with_stands(self, body):
        """releases.json for a reader: each channel's release carries "stands": the number of hubs on it."""
        try:
            doc = json.loads(body)
            for entry in doc.get("channels", {}).values():
                if isinstance(entry, dict):
                    entry["stands"] = self.stands.count(entry.get("version"))
            return json.dumps(doc, ensure_ascii=False).encode()
        except (ValueError, AttributeError, TypeError):
            return body   # a document that is not releases.json's form is served as it is stored

    def script(self, name):
        """The text of the script releases.json names as current ("install.sh" | "install.ps1"), None when it names none."""
        try:
            ref = json.loads(self.doc("releases.json") or b"")["scripts"][name]
            kind, file = re.fullmatch(r"/v1/(script)/([^/]+)", ref["url"]).groups()
            with open(self.path(kind, file), "rb") as f:
                data = f.read(MAX_SCRIPT + 1)
        except (ValueError, KeyError, TypeError, AttributeError, OSError):
            return None
        return data if hashlib.sha256(data).hexdigest() == ref.get("sha256") and script_form(data) else None

    def put_releases(self, body, if_match, if_none):
        try:
            doc = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise Refused(400, "json") from None
        if not isinstance(doc, dict) or not isinstance(doc.get("channels", {}), dict):
            raise Refused(400, "form")
        for ch, entry in doc.get("channels", {}).items():
            if ch not in ("stable", "nightly") or not isinstance(entry, dict) or \
                    not isinstance(entry.get("layer"), dict) or not VERSION.fullmatch(str(entry.get("version"))):
                raise Refused(400, "form")
        release_shape(doc)
        self.check_refs(doc)
        return self.put_doc("releases.json", body, if_match, if_none)

    def put_min(self, body, if_match, if_none):
        try:
            doc = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise Refused(400, "json") from None
        min_doc(doc)
        return self.put_doc("min.json", body, if_match, if_none)

    def put_doc(self, name, body, if_match, if_none):
        """Written only over the version the writer read: If-Match with its sum, or If-None-Match: * for the first."""
        if if_match is None and if_none != "*":
            raise Refused(428, "if-match")
        with self.lock_of(name):
            old = self.doc(name)
            had = hashlib.sha256(old).hexdigest() if old is not None else None
            if (if_match is None and old is not None) or (if_match is not None and not matches(if_match, had)):
                raise Refused(412, "changed", {"ETag": etag(old), "X-Sha256": had} if had else {})
            write_atomic(self.doc_path(name), body)
            return etag(body)


def release_shape(doc):
    """The parts of releases.json the one-line install reads: "scripts" and "runtime"."shells"; Refused(400) otherwise."""
    scripts = doc.get("scripts", {})
    if not isinstance(scripts, dict) or set(scripts) - set(SCRIPTS):
        raise Refused(400, "scripts")
    for name, ref in scripts.items():
        version = ref.get("version") if isinstance(ref, dict) else None
        if not isinstance(version, str) or not VERSION.fullmatch(version) or \
                ref.get("url") != f"/v1/script/install-{version}.{name.split('.', 1)[1]}":
            raise Refused(400, "scripts")
    runtime = doc.get("runtime", {})
    if not isinstance(runtime, dict):
        raise Refused(400, "runtime")
    shells = runtime.get("shells", {})
    if not isinstance(shells, dict) or set(shells) - set(PLATFORMS):
        raise Refused(400, "shells")
    for platform, ref in shells.items():
        number = ref.get("runtime") if isinstance(ref, dict) else None
        url = str(ref.get("url")) if isinstance(ref, dict) else ""
        if type(number) is not int or number < 1 or not url.startswith("/v1/app/") or not url.endswith(f"-{platform}.tar.gz"):
            raise Refused(400, "shells")


def etag(body):
    return '"' + hashlib.sha256(body).hexdigest() + '"'


def bare(tag):
    """The sum in an entity tag as a proxy may hand it on: "<sum>", W/"<sum>" or the bare sum."""
    tag = tag.strip()
    return (tag[2:] if tag.startswith("W/") else tag).strip('"').lower()


def matches(if_match, had):
    """If-Match against the current sum: * — any existing version; a list — any of its tags."""
    if had is None:
        return False
    return any(t.strip() == "*" or bare(t) == had for t in if_match.split(","))


def keyset(value):
    return [x.strip().lower() for x in (value or "").split(",") if SHA.fullmatch(x.strip().lower())]


def key_in(key, sums):
    if not key:
        return False
    digest = hashlib.sha256(key.encode()).hexdigest()
    found = False
    for s in sums:
        found |= hmac.compare_digest(digest, s)
    return found


def script_form(data):
    """An install script is text of printable ASCII: PowerShell 5.1 reads a script it fetches in the system's code page."""
    return len(data) <= MAX_SCRIPT and SCRIPT_TEXT.fullmatch(data) is not None


def with_key(name, body, key):
    """The script as it is handed out: the key stands where the script marks its place, else on a line put first (after
    a #! line, so the file still runs by itself)."""
    k = key.encode()
    if KEY_MARK in body:
        return body.replace(KEY_MARK, k)
    line = (b"$HUB_DELIVER_KEY = '" + k + b"'\n") if name.endswith(".ps1") else (b"HUB_DELIVER_KEY=" + k + b"\n")
    if body.startswith(b"#!"):
        head, nl, rest = body.partition(b"\n")
        return head + b"\n" + line + rest
    return line + body


def target_path(target):
    """The path of a request target as it was sent (urlsplit would take a leading // for an address)."""
    return re.split(r"[?#]", target, maxsplit=1)[0]


def asks_install(path):
    """Does the path read as /i/<key>/… — unquoted, its slashes gathered, its case dropped, with whatever before it?"""
    return "/i/" in re.sub(r"/+", "/", urllib.parse.unquote(path)).lower() + "/"


def logged(path):
    """The path as the log gets it: nothing of a key. Whatever reads as /i/<key>/… is /i/-."""
    return "/i/-" if asks_install(path) else path


def doc_headers(body):
    # no-transform: a proxy in front that compresses JSON would weaken or drop the ETag the writer sends back;
    # X-Sha256 carries the same sum where a proxy leaves custom headers alone
    return {"ETag": etag(body), "X-Sha256": hashlib.sha256(body).hexdigest(),
            "Cache-Control": "no-store, no-transform"}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "hub-deliver"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 120          # seconds of silence on a connection before it is dropped

    def log_message(self, fmt, *args):
        pass        # requests are logged by log_request below, without the query (a key may be there)

    def log_request(self, code="-", size="-"):
        self.server.say(f"{self.command} {logged(target_path(self.path))} {code}")

    # --- answers

    def answer(self, status, body=b"", ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        if self.command in ("PUT", "POST"):
            self.drain()
        else:
            self.close_connection = True     # a reader's connection does not idle in a slot
        self.send_response(status)
        if self.close_connection:
            self.send_header("Connection", "close")     # said aloud: a proxy in front must not reuse the connection
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        headers = {"Cache-Control": "no-store", **(headers or {})}
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def refuse(self, e):
        self.answer(e.status, {"code": e.code}, headers=e.headers)

    def drain(self):
        """Before any answer to a PUT: a body left unread within the limit is read to its end, so the writer gets the
        answer and not a broken pipe; a bigger one is not read at all and the connection closes — unread bytes are
        never taken for the next request."""
        left = getattr(self, "unread", 0)
        if getattr(self, "writer", False) and 0 < left <= getattr(self, "limit", 0):
            while left > 0:
                block = self.rfile.read(min(BLOCK, left))
                if not block:
                    break
                left -= len(block)
            self.unread = left
        if self.unread or not getattr(self, "writer", False):
            self.close_connection = True     # a refusal before the key was known: its body was not read at all

    def read_body(self, n):
        block = self.rfile.read(min(n, self.unread))
        self.unread -= len(block)
        return block

    # --- who

    def key(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        return (q.get("key") or [""])[0]

    def may_read(self):
        k = self.key()
        return key_in(k, self.server.read) or key_in(k, self.server.write)

    def who(self):
        if self.server.trust_proxy:
            for h in ("CF-Connecting-IP", "X-Forwarded-For"):
                if self.headers.get(h):
                    return self.headers[h].split(",")[0].strip()
        return self.client_address[0]

    def route(self):
        """(kind, name) of a file, or the name of a document, or Refused."""
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/healthz", "/min.json", "/v1/releases.json", "/v1/stand"):
            return path
        parts = [urllib.parse.unquote(x) for x in path.split("/")]
        if len(parts) == 4 and parts[1] == "v1" and parts[2] in KINDS and NAME.fullmatch(parts[3]) \
                and not parts[3].endswith((".part", ".want")) and (parts[2] != "script" or SCRIPT_NAME.fullmatch(parts[3])):
            return parts[2], parts[3]
        raise Refused(404, "not-found")

    # --- reading

    def do_HEAD(self):
        self.do_GET()

    def install(self, key, name):
        """/i/<key>/install.sh|ps1: the current script with the key in it; anything else is a bare 404."""
        server = self.server
        ok = KEY_CHARS.fullmatch(key) and key_in(key, server.read) and not key_in(key, server.write)
        body = server.store.script(name) if ok else None
        if body is None:
            if ok:       # only a holder of a real key gets here: the one line that tells "nothing published" from a wrong key
                server.say(f"install {name}: nothing published")
            return self.answer(404, b"", "text/plain")
        self.answer(200, with_key(name, body, key), "text/plain; charset=utf-8", {"Cache-Control": "no-store, private"})

    def do_GET(self):
        path = target_path(self.path)
        if asks_install(path):
            m = INSTALL.fullmatch(path)
            return self.install(*m.groups()) if m else self.answer(404, b"", "text/plain")
        try:
            where = self.route()
            if where == "/healthz":
                return self.answer(200, b"ok", "text/plain")
            if where == "/min.json":
                body = self.server.store.doc("min.json")
                if body is None:
                    raise Refused(404, "not-found")
                return self.answer(200, body, headers=doc_headers(body))
            if not self.may_read():
                raise Refused(401, "key")
            if where == "/v1/stand":
                raise Refused(405, "method")
            if where == "/v1/releases.json":
                body = self.server.store.doc("releases.json")
                if body is None:
                    raise Refused(404, "not-found")
                if not key_in(self.key(), self.server.write):   # the writer gets what it stored: it writes back from it
                    body = self.server.store.with_stands(body)
                return self.answer(200, body, headers=doc_headers(body))
            self.send_file(*where)
        except Refused as e:
            self.refuse(e)

    def send_file(self, kind, name):
        store = self.server.store
        meta = store.meta(kind, name)
        try:
            f = open(store.path(kind, name), "rb")
        except FileNotFoundError:
            raise Refused(404, "not-found") from None
        with f:
            size = os.fstat(f.fileno()).st_size
            start, status, headers = 0, 200, {}
            rng = self.headers.get("Range", "")
            m = re.fullmatch(r"bytes=(\d+)-(\d*)", rng.strip())
            if rng and m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
                if start >= size or end < start:
                    raise Refused(416, "range", {"Content-Range": f"bytes */{size}"})
                end = min(end, size - 1)
                status, length = 206, end - start + 1
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            else:
                length = size
            self.send_response(status)
            ctype = "application/gzip" if name.endswith(".gz") else "application/octet-stream"
            self.close_connection = True
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Connection", "close")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "private, max-age=31536000, immutable, no-transform")
            if meta:
                self.send_header("ETag", f'"{meta["sha256"]}"')
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command == "HEAD":
                return
            f.seek(start)
            left = length
            while left:
                block = f.read(min(BLOCK, left))
                if not block:
                    break
                self.wfile.write(block)
                left -= len(block)

    # --- writing

    def do_PUT(self):
        path = urllib.parse.urlsplit(self.path).path
        size = sha = None
        self.unread, self.writer, self.limit = 0, False, 0
        try:
            where = self.route()
            if where == "/healthz":
                raise Refused(405, "method")
            k = self.key()
            if not key_in(k, self.server.write):
                raise Refused(403 if key_in(k, self.server.read) else 401, "key")
            self.writer = True       # only the release key's bodies are read to their end before a refusal
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self.close_connection = True      # no length: the body (maybe chunked) is never read
                raise Refused(411, "length") from None
            if length < 0:
                raise Refused(400, "length")
            self.unread = length
            self.limit = self.server.max_body if isinstance(where, tuple) else \
                MAX_MIN if where == "/min.json" else MAX_JSON
            if isinstance(where, tuple):
                if length > self.server.max_body:
                    raise Refused(413, "too-big")
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                try:
                    offset, size = int(q["offset"][0]), int(q["size"][0])
                    sha = q["sha256"][0].lower()
                except (KeyError, ValueError, IndexError):
                    raise Refused(400, "query") from None
                if not SHA.fullmatch(sha) or offset < 0 or not 0 < size <= MAX_SIZE:
                    raise Refused(400, "query")
                if where[0] == "script" and size > MAX_SCRIPT:
                    raise Refused(413, "too-big")
                status, ans = self.server.store.put_part(*where, offset, size, sha, length, self.read_body)
                if status != 202:
                    self.server.store.log(path=path, size=size, sha256=sha, result=ans["state"], by=self.who())
                return self.answer(status, ans)
            limit = MAX_MIN if where == "/min.json" else MAX_JSON
            if length > limit:
                raise Refused(413, "too-big")
            body = self.read_body(length)
            sha = hashlib.sha256(body).hexdigest()
            size = len(body)
            put = self.server.store.put_min if where == "/min.json" else self.server.store.put_releases
            tag = put(body, self.headers.get("If-Match"), self.headers.get("If-None-Match"))
            self.server.store.log(path=path, size=size, sha256=sha, result="stored", by=self.who())
            return self.answer(200, {"state": "stored"}, headers={"ETag": tag, "X-Sha256": sha})
        except Refused as e:
            result = {"published": "clash"}.get(e.code, "refused:" + e.code)
            if e.code not in ("offset", "not-found"):
                self.server.store.log(path=path, size=size, sha256=sha, result=result, by=self.who())
            self.refuse(e)

    def do_POST(self):
        """POST /v1/stand: "this hub stands on <release>" (the module's text). Only the build or the release key."""
        self.unread, self.writer, self.limit = 0, False, 0
        try:
            if self.route() != "/v1/stand":
                raise Refused(405, "method")
            if not self.may_read():
                raise Refused(401, "key")
            self.writer = True
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self.close_connection = True
                raise Refused(411, "length") from None
            if not 0 <= length <= MAX_STAND:
                self.unread, self.limit = max(length, 0), MAX_STAND
                raise Refused(413, "too-big")
            self.unread, self.limit = length, MAX_STAND
            try:
                doc = json.loads(self.read_body(length))
            except (ValueError, UnicodeDecodeError):
                raise Refused(400, "json") from None
            hub, version = (doc.get("id"), doc.get("version")) if isinstance(doc, dict) and set(doc) == {"id", "version"} \
                else (None, 0)
            if not (isinstance(hub, str) and STAND_ID.fullmatch(hub)) or not (
                    version is None or (isinstance(version, str) and VERSION.fullmatch(version))):
                raise Refused(400, "form")
            try:
                state = self.server.store.stands.say(hub, version)
            except OSError:
                raise Refused(503, "disk") from None
            return self.answer(200, {"state": state})
        except Refused as e:
            self.refuse(e)

    def do_DELETE(self):
        self.answer(405, {"code": "method"})


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64
    connections = 128      # at once; one more is closed at the door

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.slots = threading.BoundedSemaphore(self.connections)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def make(data, host, port, read, write, max_body=MAX_BODY, trust_proxy=False, say=None):
    srv = Server((host, port), Handler)
    srv.store = Store(data)
    srv.read, srv.write = list(read), list(write)
    srv.max_body = max_body
    srv.trust_proxy = trust_proxy
    srv.say = say or (lambda line: print(line, file=sys.stderr, flush=True))
    return srv


def serve():
    read, write = keyset(os.environ.get("HUB_DELIVER_READ_KEYS")), keyset(os.environ.get("HUB_DELIVER_WRITE_KEYS"))
    if not read or not write:
        sys.exit("server.py: HUB_DELIVER_READ_KEYS and HUB_DELIVER_WRITE_KEYS — the sha256 of the build key and of "
                 "the release key (`server.py keygen`)")
    srv = make(os.environ.get("HUB_DELIVER_DATA", "/data"), os.environ.get("HUB_DELIVER_HOST", "0.0.0.0"),
               int(os.environ.get("HUB_DELIVER_PORT", "8080")), read, write,
               trust_proxy=os.environ.get("HUB_DELIVER_TRUST_PROXY") == "1")
    host, port = srv.server_address[:2]
    print(f"hub-deliver listening on {host}:{port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def du():
    data = os.environ.get("HUB_DELIVER_DATA", "/data")
    total = 0
    for kind in KINDS:
        d = os.path.join(data, "files", kind)
        for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            size = os.path.getsize(os.path.join(d, name))
            total += size
            print(f"{kind}/{name}\t{size}")
    print(f"total\t{total}\tfree\t{shutil.disk_usage(data).free}")


def main(argv):
    cmd = argv[0] if argv else ""
    if cmd == "serve":
        serve()
    elif cmd == "keygen":
        key = secrets.token_urlsafe(32)
        print(key)
        print(f"sha256: {hashlib.sha256(key.encode()).hexdigest()}", file=sys.stderr)
    elif cmd == "check":
        data = os.environ.get("HUB_DELIVER_DATA", "/data")
        probe = os.path.join(data, f".check-{os.getpid()}-{int(time.time())}")
        with open(probe, "w") as f:
            f.write("ok")
        os.unlink(probe)
    elif cmd == "du":
        du()
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
