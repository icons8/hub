"""Downloading one big file by its pinned sha256 and size (installer spec §4, §8): in chunks through the owner's proxy
(core/net); a broken download continues where it stopped (Range) — within the run and on the next run, from the same
`<file>.part`; a file that does not match is deleted and never handed over. Claude Code (core/claudeinstall) and the
parts the app fetches on first use (Chrome for Testing: URL, sha256 and size pinned in tools/package/versions.json)
go through here. The stdlib only."""
import contextlib
import hashlib
import os
import re
import time
import urllib.parse

from core import i18n, nethttp

TIMEOUT = 60          # seconds of silence on a socket before the attempt counts as broken
SMALL = 1 << 20       # a pointer, a manifest, a signature, a key — far below a megabyte
CHUNK = 1 << 20
PROGRESS_STEP = 1 << 18   # bytes between two calls of progress()
TRIES = 4             # attempts in a row that bring not one byte, within one run; the next run continues the same .part
RUN_SECONDS = 600     # past this a run starts no new attempt (the bytes so far stay): a link that gives a byte per try is not waited on
ATTEMPTS_MAX = 16     # attempts within one run, with bytes or without: a link that gives a little each time is not for ever
UA = "agents-hub (fetch)"
# where the open build's environments are (the assets of its GitHub releases): an address its releases.json names off
# its own pages, and every redirect of that download, is https to one of these exactly — no port, no login
# (deploy/oneline/install.sh and install.ps1 carry the same list: tests/test_oneline.py holds the three equal)
ASSET_HOSTS = ("github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com")
LOOPBACK = ("127.0.0.1", "localhost", "[::1]")
ADDRESS = re.compile(r"[A-Za-z0-9._~:/?&=%+\[\]-]+\Z")   # what such an address is made of (no quote, space, # or @)


class Failed(Exception):
    """A step did not work out: `why` — a short code, the text — for a person."""

    def __init__(self, why, text):
        super().__init__(text)
        self.why = why


def _number(v):
    """v is a whole number int() reads: ASCII digits only ("²" is a digit to str.isdigit and not a number to int())."""
    return v.isascii() and v.isdigit() and len(v) < 20


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def asset_ok(url, server):
    """url is an address the open build downloads from, off its pages at server: https to the pages' own host or to one
    of ASSET_HOSTS, the host as it is; a stand (pages over http to this machine) — http to this machine as well."""
    if not ADDRESS.match(url):
        return False
    got, pages = urllib.parse.urlsplit(url), urllib.parse.urlsplit(server)
    if got.scheme == "https":
        return got.netloc in ASSET_HOSTS or got.netloc == pages.netloc
    def loopback(netloc):
        return netloc.rsplit(":", 1)[0] if netloc.rsplit(":", 1)[-1].isdigit() else netloc
    return got.scheme == "http" == pages.scheme and loopback(got.netloc) in LOOPBACK and loopback(pages.netloc) in LOOPBACK


def open_url(url, headers=None, private=None, allow=None, timeout=TIMEOUT):
    """(response, connection) after redirects (https never goes down to http); OSError on network trouble. private —
    headers that go only to the host of url (a key of the distribution server): a redirect to another host goes without
    them. allow(address) — every address asked, the first and each redirect, or Failed(refused) before it is
    asked."""
    host = urllib.parse.urlsplit(url).netloc
    for _ in range(8):
        if allow and not allow(url):   # a refusal: not tried again (an OSError would be, as a network's)
            raise Failed("refused", f"not an address to download from: {url[:200]}")
        own = private if private and urllib.parse.urlsplit(url).netloc == host else {}
        conn, resp = nethttp.send("GET", url, headers={"User-Agent": UA, **(headers or {}), **(own or {})},
                                  timeout=timeout)
        where = resp.getheader("Location")
        if resp.status in (301, 302, 303, 307, 308) and where:
            resp.read(SMALL)
            conn.close()
            nxt = urllib.parse.urljoin(url, where)
            if urllib.parse.urlsplit(url).scheme == "https" and urllib.parse.urlsplit(nxt).scheme != "https":
                raise OSError(f"redirect from https to {nxt}")
            url = nxt
            continue
        return resp, conn
    raise OSError(f"too many redirects: {url}")


def small(url, private=None, allow=None, timeout=TIMEOUT):
    """(status, body) of a small file; Failed(network) when the server cannot be reached, Failed(http) past 1 MB.
    private, allow, timeout — as open_url."""
    import http.client
    try:
        resp, conn = open_url(url, private=private, allow=allow, timeout=timeout)
        try:
            body = resp.read(SMALL + 1)
        finally:
            conn.close()
    except (OSError, http.client.HTTPException) as e:
        raise Failed("network", i18n.t("cli.fetch.network", host=urllib.parse.urlsplit(url).hostname,
                                       error=str(e) or type(e).__name__)) from e
    if len(body) > SMALL:
        raise Failed("http", i18n.t("cli.fetch.http", url=url, status="> 1 MB"))
    return resp.status, body


def post(url, data, private=None, timeout=15):
    """(status, body) of a small JSON POST (a dict, sent as JSON); Failed(network) when the server cannot be reached. A
    redirect is an answer, not followed: the key in `private` goes only to the address named, never further."""
    import http.client
    import json
    try:
        conn, resp = nethttp.send("POST", url, body=json.dumps(data).encode(), timeout=timeout,
                                  headers={"User-Agent": UA, "Content-Type": "application/json", **(private or {})})
        try:
            body = resp.read(SMALL + 1)
        finally:
            conn.close()
    except (OSError, http.client.HTTPException) as e:
        raise Failed("network", i18n.t("cli.fetch.network", host=urllib.parse.urlsplit(url).hostname,
                                       error=str(e) or type(e).__name__)) from e
    return resp.status, body[:SMALL]


def download(url, dest, sha, size, what, again, progress=None, log=None, say=None, pause=2.0, private=None, allow=None):
    """`url` into `dest` when its bytes have this sha256 and size; Failed: broken (the .part stays for the next run),
    checksum (deleted), http (a refusal, not a passing failure). `what` names the file and `again` the command that
    continues, for the texts; progress(done, total), log(text) and say(text) — callbacks; private, allow — as
    open_url."""
    progress = progress or (lambda done, total: None)
    log = log or (lambda text: None)
    part = dest + ".part"
    have = os.path.getsize(part) if os.path.isfile(part) else 0
    if have > size:
        os.remove(part)
        have = 0
    if have and say:
        say(i18n.t("cli.fetch.resumed", what=what, mb=f"{have / 1e6:.1f}"))
    last, fruitless, began = None, 0, time.monotonic()
    for attempt in range(ATTEMPTS_MAX):
        if have >= size or fruitless >= TRIES or (attempt and time.monotonic() - began > RUN_SECONDS):
            break
        if attempt:
            time.sleep(pause * (fruitless or 1))
        before = have
        try:
            have = _attempt(url, part, have, size, what, again, progress, pause, private, allow)
            last = None
        except OSError as e:
            last = e
            have = os.path.getsize(part) if os.path.isfile(part) else 0
            log(f"attempt {attempt + 1}: {e} (have {have} of {size})")
        fruitless = 0 if have > before else fruitless + 1
    if have < size:
        raise Failed("broken", i18n.t("cli.fetch.broken", what=what, error=str(last or "?"), mb=f"{have / 1e6:.1f}",
                                      again=again))
    if have != size or sha256(part) != sha:
        os.remove(part)
        raise Failed("checksum", i18n.t("cli.fetch.checksum", what=what, size=have, want_size=size, again=again))
    os.replace(part, dest)
    return dest


def _attempt(url, part, have, size, what, again, progress, pause, private=None, allow=None):
    """One request: continue `part` from `have` (Range) or start over when the server sends the whole file; the bytes
    in `part`. OSError — broken or a passing failure (the bytes so far stay)."""
    import http.client
    resp, conn = open_url(url, {"Range": f"bytes={have}-"} if have else None, private=private, allow=allow)
    try:
        got_range = resp.getheader("Content-Range") or ""
        skip = 0   # bytes at the start of the answer that the part already holds
        if resp.status == 206 and got_range.startswith(f"bytes {have}-"):
            mode = "ab" if have else "wb"
        elif resp.status == 200:
            # the whole file, a Range not honoured (a proxy in front, now and then): the part's bytes are read past, not
            # written again and not thrown away — an answer that breaks off before them costs the part nothing; an answer
            # that is not what the part holds starts it over
            mode, skip = ("ab" if have else "wb"), have
        elif resp.status in (206, 416):
            # a range from another place, or past the end: what we have is no good, the next attempt starts over
            # without Range
            if os.path.exists(part):
                os.remove(part)
            raise OSError(f"HTTP {resp.status} {got_range}".strip())
        elif resp.status >= 500 or resp.status in (408, 429):   # a passing failure of the CDN: the next attempt
            wait = resp.getheader("Retry-After") or ""
            if pause and _number(wait):
                time.sleep(min(int(wait), 60))
            raise OSError(f"HTTP {resp.status}")
        else:
            raise Failed("http", i18n.t("cli.fetch.http", url=url, status=resp.status))
        body, other, shown = 0, False, have   # bytes of the answer so far; the answer is not the file the part holds; progress
        with open(part, mode) as f, (open(part, "rb") if skip else contextlib.nullcontext()) as held:
            progress(have, size)
            while True:
                try:
                    block = resp.read1(CHUNK)   # what has come, not a whole block: a stall loses nothing of it
                except (OSError, http.client.HTTPException) as e:
                    raise OSError(str(e) or type(e).__name__) from e
                if not block:
                    break
                body += len(block)
                if skip:
                    cut = min(skip, len(block))
                    if held.read(cut) != block[:cut]:
                        other = True
                        break
                    block, skip = block[cut:], skip - cut
                    if not block:
                        continue
                if have + len(block) > size:
                    have = size + 1   # more than pinned: not this file
                    break
                f.write(block)
                have += len(block)
                if have - shown >= PROGRESS_STEP or have >= size:   # a read is what the socket had: often 16 KB
                    shown = have
                    progress(have, size)
        if other:   # the next attempt asks without a Range
            os.remove(part)
            raise OSError("the whole answer is not the file the part holds")
        if have > size:
            os.remove(part)
            raise Failed("checksum", i18n.t("cli.fetch.checksum", what=what, size=f"> {size}", want_size=size,
                                            again=again))
        length = resp.getheader("Content-Length")
        if have < size and length and _number(length) and body == int(length):
            # the server sent the whole answer, and it is shorter than pinned: not this file
            os.remove(part)
            raise Failed("checksum", i18n.t("cli.fetch.checksum", what=what, size=have, want_size=size, again=again))
        if have < size:
            raise OSError(f"the connection closed at {have} of {size} bytes")
        return have
    finally:
        conn.close()
