"""upload.py <server> …: sending a release to the delivery server of the team build (deliver/server.py, app spec
§4.1). The commands and codes are in upload.py's help; this file is its half that talks to the server.
Codes: 0 — done / would be done, 2 — wrong input, 3 — conflict (other bytes under a published name), 4 — the server."""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch import sha256  # noqa: E402
import authenticode  # noqa: E402

CHUNK = 64 << 20             # one PUT body: under the 100 MB a proxy in front takes
MAX_SIZE = 4 << 30
TRIES = 3
UA = "agents-hub-upload"     # a proxy in front (Cloudflare) refuses a request without its own User-Agent
PLATFORMS = ("mac-arm64", "windows-x64", "linux-x64", "linux-arm64")
SHELL_PLATFORMS = ("mac-arm64", "mac-x64", "linux-x64", "linux-arm64", "windows-x64", "windows-arm64")   # the server's
SCRIPTS = ("install.sh", "install.ps1")
MAX_SCRIPT = 256 << 10       # the server's
SCRIPT_TEXT = re.compile(rb"[\t\r\n\x20-\x7e]+")      # the server's (a test holds them equal)
URL_MARK = b"@@HUB_DELIVER_URL@@"   # a script names its server here: the one it is put on is written in as it is sent
FLAVOR = "team"
KEY = os.path.join("~", ".config", "hub-core", "deliver-release.key")
# the same as the server's NAME and VERSION (deliver/server.py; a test holds them equal)
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,199}")
VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}")
AGAIN = (502, 503, 504)      # a proxy or a restarting server; any other answer is the server's word


class Stop(Exception):
    def __init__(self, code, text):
        super().__init__(text)
        self.code = code


def read_key(path):
    path = os.path.expanduser(path)
    try:
        st = os.stat(path)
        with open(path) as f:
            key = f.read().strip()
    except OSError as e:
        raise Stop(2, f"no release key: {path} ({e.strerror})") from None
    if os.name == "posix" and st.st_mode & 0o077:
        raise Stop(2, f"{path} is readable by others — chmod 600 it")
    if not key or len(key.split()) != 1:
        raise Stop(2, f"{path}: one line, the release key")
    return key


class _Stay(urllib.request.HTTPRedirectHandler):
    """The delivery server never redirects: a 30x comes back as itself, not followed with the release key (urllib copies
    Authorization to whatever host and scheme the redirect names — release review 2026-11-09)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPEN = urllib.request.build_opener(_Stay()).open


class Client:
    def __init__(self, server, key):
        self.server = server.rstrip("/")
        self.key = key

    def call(self, method, path, body=None, headers=None):
        """(status, headers, body); Stop(4) when the server cannot be reached."""
        h = {"User-Agent": UA, "Authorization": f"Bearer {self.key}", **(headers or {})}
        if body is not None:
            h["Content-Length"] = str(len(body))
        req = urllib.request.Request(self.server + path, data=body, method=method, headers=h)
        last = None
        for i in range(TRIES):
            try:
                with OPEN(req, timeout=300) as r:
                    return r.status, r.headers, r.read()
            except urllib.error.HTTPError as e:
                with e:
                    if e.code not in AGAIN:
                        return e.code, e.headers, e.read()
                    last = f"{e.code} {e.reason}"
            except (OSError, ValueError) as e:
                last = str(e)
            time.sleep(min(2 ** i, 10))
        raise Stop(4, f"{method} {path}: {last}")

    def doc(self, path):
        """(the document, the sum of its bytes); (None, None) when there is none yet."""
        st, h, body = self.call("GET", path)
        if st == 404:
            return None, None
        if st != 200:
            raise Stop(4, f"GET {path}: {st} {body[:200]!r}")
        try:
            doc = json.loads(body)
        except ValueError:
            raise Stop(4, f"GET {path}: not JSON") from None
        # the server's sum of its bytes (a proxy may weaken or drop the ETag), else the sum of the bytes read
        return doc, (h.get("X-Sha256") or hashlib.sha256(body).hexdigest()).lower()

    def stored(self, kind, name):
        """The sum of a published file, or None when there is none."""
        st, h, _ = self.call("HEAD", f"/v1/{kind}/{name}")
        if st == 404:
            return None
        if st != 200:
            raise Stop(4, f"HEAD /v1/{kind}/{name}: {st}")
        tag = (h.get("ETag") or "").strip()
        return (tag[2:] if tag.startswith("W/") else tag).strip('"')

    def send(self, kind, path, name, size, digest):
        """The file in parts; 'stored' or 'same'."""
        offset, rounds = 0, 0
        with open(path, "rb") as f:
            while True:
                rounds += 1
                if rounds > 3 * (size // CHUNK + 1) + 5:
                    raise Stop(4, f"{name}: the server does not take the parts")
                f.seek(offset)
                part = f.read(CHUNK)
                q = urllib.parse.urlencode({"offset": offset, "size": size, "sha256": digest})
                st, h, body = self.call("PUT", f"/v1/{kind}/{urllib.parse.quote(name)}?{q}", part,
                                        {"Content-Type": "application/octet-stream"})
                try:
                    ans = json.loads(body)
                except ValueError:
                    ans = {}
                if st in (200, 201):
                    return ans.get("state", "stored")
                if st == 202:
                    offset = int(ans["offset"])
                elif st == 409 and h.get("X-Offset") is not None:
                    offset = int(h["X-Offset"])
                elif st == 409:
                    raise Stop(3, f"REFUSED: {name} is already published with other content")
                else:
                    raise Stop(4, f"PUT {name}: {st} {ans.get('code') or body[:200]!r}")

    def change(self, path, edit, first=None):
        """Read the document (or take `first` — a (doc, sum) just read), change it with edit(doc) → new doc, write it
        from that version; again if someone else wrote in between."""
        for _ in range(TRIES):
            doc, tag = first or self.doc(path)
            first = None
            new = edit(json.loads(json.dumps(doc)) if doc is not None else None)
            body = json.dumps(new, ensure_ascii=False, indent=1, sort_keys=True).encode()
            st, _, ans = self.call("PUT", path, body, {"Content-Type": "application/json",
                                                       **({"If-Match": f'"{tag}"'} if tag else {"If-None-Match": "*"})})
            if st == 200:
                return new
            if st != 412:
                raise Stop(2 if st == 400 else 4, f"PUT {path}: {st} {ans[:200]!r}")
        raise Stop(4, f"PUT {path}: changed under us {TRIES} times")


def check_file(path, kind):
    if not os.path.isfile(path):
        raise Stop(2, f"no file {path}")
    size = os.path.getsize(path)
    if not 0 < size <= MAX_SIZE:
        raise Stop(2, f"{path}: {size} bytes — the server takes 1 byte … 4 GiB")
    name = os.path.basename(path)
    if not NAME.fullmatch(name):
        raise Stop(2, f"{name}: a file name of letters, digits and ._+-")
    return name, size, sha256(path)


def put_file(c, kind, path, name, size, digest, send):
    """'same' / 'stored' / 'todo' (a look); Stop(3) when other bytes are there."""
    had = c.stored(kind, name)
    print(f"  {kind}/{name}  {size / 1e6:.1f} MB  sha256 {digest}")
    if had == digest:
        print("  already on the server, the same")
        return "same"
    if had:
        raise Stop(3, f"REFUSED: {name} is already on the server with other content (sha256 {had})")
    if not send:
        return "todo"
    return c.send(kind, path, name, size, digest)


def ref(kind, name, size, digest):
    return {"url": f"/v1/{kind}/{name}", "sha256": digest, "size": size}


def layer(c, a):
    if not VERSION.fullmatch(a.version):
        raise Stop(2, f"version {a.version!r}: letters, digits and .+-")
    if not re.fullmatch(r"[0-9a-f]{7,40}", a.commit):
        raise Stop(2, "--commit: the sha of the published snapshot")
    if a.runtime_min < 0:
        raise Stop(2, "--runtime-min: a number of the environment")
    name, size, digest = check_file(a.file, "layer")
    if name != f"code-{a.version}-{FLAVOR}.tar.gz":
        raise Stop(2, f"{name}: a layer of {a.version} is named code-{a.version}-{FLAVOR}.tar.gz")
    notes = ""
    if a.notes:
        with open(a.notes, encoding="utf-8") as f:
            notes = f.read().strip()
    entry = {"version": a.version, "commit": a.commit, "runtime_min": a.runtime_min, "notes": notes,
             "published_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "layer": ref("layer", name, size, digest)}
    read = c.doc("/v1/releases.json")
    was = ((read[0] or {}).get("channels") or {}).get(a.channel, {}).get("version")
    print(f"{a.channel}: {was or '—'} → {a.version}")
    if put_file(c, "layer", a.file, name, size, digest, a.send) == "todo":
        return look()

    def edit(d):
        d = d or {}
        d.setdefault("channels", {})[a.channel] = entry
        d.setdefault("runtime", {"current": 0, "apps": {}})
        return d
    c.change("/v1/releases.json", edit, read)
    print(f"releases.json: {a.channel} is {a.version}")
    return 0


def app(c, a):
    if a.platform not in PLATFORMS:
        raise Stop(2, f"--platform: one of {', '.join(PLATFORMS)}")
    if a.runtime < 1:
        raise Stop(2, "--runtime: the number of the environment the app carries")
    name, size, digest = check_file(a.file, "app")
    if a.platform not in name:
        raise Stop(2, f"{name}: the name of an app for {a.platform} names it")
    if a.platform.startswith("windows-") and not a.unsigned:   # the team's installer goes out signed (app spec §6)
        try:
            algo, claimed, _ = authenticode.signature(a.file)
            if authenticode.digest(a.file, algo) != claimed:
                raise authenticode.Bad(f"{name} changed after it was signed (its signature claims another digest)")
        except authenticode.Bad as e:
            raise Stop(2, f"{e} — a Windows app is sent signed (tools/package/sign.py); --unsigned to send it as is")
    if put_file(c, "app", a.file, name, size, digest, a.send) == "todo":
        return look()

    def edit(d):
        d = d or {"channels": {}}
        rt = d.setdefault("runtime", {"current": 0, "apps": {}})
        rt.setdefault("apps", {})[a.platform] = {**ref("app", name, size, digest), "runtime": a.runtime}
        rt["current"] = max([x.get("runtime", 0) for x in rt["apps"].values()] + [rt.get("current", 0)])
        return d
    c.change("/v1/releases.json", edit)
    print(f"releases.json: the app for {a.platform} is {name} (environment {a.runtime})")
    return 0


def shell(c, a):
    """The environment of a platform (the archive the factory makes: agents-hub-<tag>-<platform>.tar.gz) — a file of the
    app kind, named in releases.json under runtime.shells."""
    if a.platform not in SHELL_PLATFORMS:
        raise Stop(2, f"--platform: one of {', '.join(SHELL_PLATFORMS)}")
    if a.runtime < 1:
        raise Stop(2, "--runtime: the number of the environment the archive carries")
    name, size, digest = check_file(a.file, "app")
    if not name.endswith(f"-{a.platform}.tar.gz"):
        raise Stop(2, f"{name}: the archive of {a.platform} is named ...-{a.platform}.tar.gz")
    if put_file(c, "app", a.file, name, size, digest, a.send) == "todo":
        return look()

    def edit(d):
        d = d or {"channels": {}}
        rt = d.setdefault("runtime", {"current": 0, "apps": {}})
        rt.setdefault("shells", {})[a.platform] = {**ref("app", name, size, digest), "runtime": a.runtime}
        return d             # "current" is the apps' number: an environment carries its own
    c.change("/v1/releases.json", edit)
    print(f"releases.json: the environment for {a.platform} is {name} (number {a.runtime})")
    return 0


def script(c, a):
    """install.sh | install.ps1 of the core's deploy/oneline: stored as install-<version>.<ext> (the name carries the
    version, a published name is never replaced) and made the current one in releases.json under "scripts"."""
    base = os.path.basename(a.file)
    if base not in SCRIPTS:
        raise Stop(2, f"{base}: the file is install.sh or install.ps1")
    if not VERSION.fullmatch(a.version):
        raise Stop(2, f"version {a.version!r}: letters, digits and .+-")
    if os.path.isfile(a.file) and os.path.getsize(a.file) > MAX_SCRIPT:
        raise Stop(2, f"{base}: more than {MAX_SCRIPT} bytes, the server takes no more")
    check_file(a.file, "script")
    with open(a.file, "rb") as f:
        data = f.read()
    if not SCRIPT_TEXT.fullmatch(data):
        raise Stop(2, f"{base}: a script is printable ASCII only (PowerShell 5.1 reads what it fetches in the system's "
                      "code page)")
    # the script runs as `curl <server>/i/<key>/install.sh | bash`: it cannot tell where it came from, so the server's
    # address is written in here (the core names no server of its own); the server writes the key in later
    address = (getattr(a, "address", None) or c.server).rstrip("/")
    host = urllib.parse.urlsplit(address).hostname
    if not re.fullmatch(r"https?://[A-Za-z0-9.:\[\]_-]+", address) or \
            (not address.startswith("https://") and host not in ("127.0.0.1", "localhost", "::1")):
        raise Stop(2, f"{address}: the address a script names is https:// (http only on this machine), scheme and host")
    if URL_MARK in data:
        print(f"  the script names the server {address}" + ("  — this machine only: a stand" if
                                                             not address.startswith("https://") else ""))
    data = data.replace(URL_MARK, address.encode("ascii"))
    if len(data) > MAX_SCRIPT:
        raise Stop(2, f"{base}: more than {MAX_SCRIPT} bytes with the address written in, the server takes no more")
    with tempfile.TemporaryDirectory(prefix="deliver-script-") as tmp:
        path = os.path.join(tmp, base)
        with open(path, "wb") as f:
            f.write(data)
        _, size, digest = check_file(path, "script")
        name = f"install-{a.version}.{base.split('.', 1)[1]}"
        if put_file(c, "script", path, name, size, digest, a.send) == "todo":
            return look()

    def edit(d):
        d = d or {}
        d.setdefault("scripts", {})[base] = {**ref("script", name, size, digest), "version": a.version}
        return d
    c.change("/v1/releases.json", edit)
    print(f"releases.json: {base} is {name}")
    return 0


def minimum(c, a):
    entry = {"version": a.version}
    if a.reason:
        with open(a.reason, encoding="utf-8") as f:
            try:
                entry["reason"] = json.load(f)
            except ValueError:
                raise Stop(2, f"{a.reason}: JSON {{language: text}}") from None
    if a.since:
        entry["since"] = a.since
    if a.stops:
        entry["stops"] = [s.strip() for s in a.stops.split(",") if s.strip()]
    read = c.doc("/min.json")
    was = ((read[0] or {}).get(a.flavor) or {}).get("version")
    print(f"min.json {a.flavor}: {was or '—'} → {a.version}  {json.dumps(entry, ensure_ascii=False)}")
    if not a.send:
        return look()

    def edit(d):
        d = d or {}
        d[a.flavor] = entry
        return d
    c.change("/min.json", edit, read)
    print("min.json written")
    return 0


def show(c, a):
    doc, _ = c.doc("/v1/releases.json")
    print(json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) if doc is not None else "no releases.json")
    doc, _ = c.doc("/min.json")
    print(json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) if doc is not None else "no min.json")
    return 0


def look():
    print("a dry run: nothing sent (sending is --send, on the owner's word)")
    return 0


def main(argv):
    ap = argparse.ArgumentParser(prog="upload.py <server>")
    ap.add_argument("server")
    ap.add_argument("--key-file", default=KEY)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("layer")
    p.add_argument("file")
    p.add_argument("--channel", required=True, choices=("stable", "nightly"))
    p.add_argument("--version", required=True)
    p.add_argument("--commit", required=True)
    p.add_argument("--runtime-min", required=True, type=int)
    p.add_argument("--notes")
    p = sub.add_parser("app")
    p.add_argument("file")
    p.add_argument("--platform", required=True)
    p.add_argument("--runtime", required=True, type=int)
    p.add_argument("--unsigned", action="store_true", help="a Windows app without a signature (a trial)")
    p = sub.add_parser("shell")
    p.add_argument("file")
    p.add_argument("--platform", required=True)
    p.add_argument("--runtime", required=True, type=int)
    p = sub.add_parser("script")
    p.add_argument("file")
    p.add_argument("--version", required=True)
    p.add_argument("--address", help="the server's address the script names (by default the one it is sent to: "
                                     "give the public one when sending through a tunnel)")
    p = sub.add_parser("min")
    p.add_argument("flavor", choices=("team", "open"))
    p.add_argument("--version", required=True)
    p.add_argument("--reason")
    p.add_argument("--since")
    p.add_argument("--stops")
    sub.add_parser("show")
    for name in ("layer", "app", "shell", "script", "min"):
        sub.choices[name].add_argument("--send", action="store_true", help="for real (on the owner's word)")
        sub.choices[name].add_argument("--key-file", default=argparse.SUPPRESS)
    sub.choices["show"].add_argument("--key-file", default=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if not re.match(r"https://", a.server) and urllib.parse.urlsplit(a.server).hostname not in ("127.0.0.1", "localhost"):
        print("upload.py: the server is https:// (http only on this machine)", file=sys.stderr)
        return 2
    try:
        c = Client(a.server, read_key(a.key_file))
        print(f"server {c.server}")
        return {"layer": layer, "app": app, "shell": shell, "script": script, "min": minimum,
                "show": show}[a.cmd](c, a)
    except Stop as e:
        print(f"upload.py: {e}", file=sys.stderr)
        return e.code
