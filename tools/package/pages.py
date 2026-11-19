"""What one release of the open build puts where its hubs take it without a key:
the static pages (GitHub Pages of the open repository) and the files of its GitHub release. This writes the two
folders; it publishes nothing — the pages go out as one commit of their branch, the files with
`tools/package/upload.py <account>/<repo> <tag> <files>`, on the owner's word.

  python3 tools/package/pages.py <the factory's out of an open tag> --out DIR --base URL --assets URL
        --relay FILE|none [--notes FILE] [--min FILE]

--base — the pages' address (https://<account>.github.io/<repo>); --assets — where the release's files are downloaded
from (https://github.com/<account>/<repo>/releases/download/<tag>). In the factory's out (`factory.py <tag> --flavor
open`): release.json, every platform's environment archive and the layer code-<version>-open.tar.gz.

DIR/pages/:
  relay.sh                  one's own bus relay in one command: `curl -fsSL <base>/relay.sh | sh -s <name>` — the
                            release's relay/install.sh with its repository and tag written in (from --assets): Docker
                            builds the relay of this very release
  install.sh, install.ps1   the line `curl -fsSL <base>/install.sh | bash` (Windows: `irm <base>/install.ps1 | iex`):
                            the release's own scripts (from its layer) with <base> written in at @@HUB_DELIVER_URL@@ and
                            the open flavor at their flavor line — such a script asks for no key and sends none
  v1/releases.json          as the delivery server's (deliver/README.md), the stable channel alone: its layer on these
                            pages (/v1/layer/…), runtime.shells — each platform's environment at its absolute address
                            under --assets, with its sha256, size and runtime number (an environment is 190–285 MB:
                            GitHub Pages takes no file over 100 MB)
  v1/layer/code-<version>-open.tar.gz
  v1/relay.json             {"invite": <the public relay's line>} (--relay: a file of one hub-relay-invite-1 line, an
                            operator's open line, `relay/server.py open`): a hub of this build that links with no relay
                            takes it (core/busflow.public_line). Every release names it or says `none`: the pages are
                            written whole, a release without it would drop the file the hubs read
  min.json                  the open build's minimum (--min: a file of {"open": {...}}, as upload.py min writes it), else {}
  .nojekyll                 the pages are served as they are
DIR/release/<the environment archives>   the files of the GitHub release of the tag (hard links where it can)

Only this release's layer is on the pages: a hub takes the newest one, its repairs come from its own archive.
Codes: 0 — written, 1 — refused (the reason is printed). The stdlib only."""
import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import time
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = ("install.sh", "install.ps1")
RELAY_SCRIPT = "relay.sh"   # relay/install.sh of the layer, on the pages
RELAY_MARKS = (b"@@HUB_RELAY_REPO@@", b"@@HUB_RELAY_TAG@@")
RELEASE_ASSETS = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/releases/download/([A-Za-z0-9_.+-]+)")
URL_MARK = b"@@HUB_DELIVER_URL@@"
# the flavor line of each script, as it is in the core and as the open build's copy has it (tests/test_pages.py)
FLAVOR_LINE = {"install.sh": (b"\nHUB_INSTALL_FLAVOR=team\n", b"\nHUB_INSTALL_FLAVOR=open\n"),
               "install.ps1": (b"\n  $Flavor = 'team'\n", b"\n  $Flavor = 'open'\n")}
SCRIPT_TEXT = re.compile(rb"[\t\r\n\x20-\x7e]+")   # printable ASCII: PowerShell 5.1 reads what it fetches so
PAGES_MAX = 100 * 1000 * 1000                      # GitHub Pages takes no bigger file
ASSET_MAX = 2 * 1024 ** 3 - 1                      # nor a release a bigger asset
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,150}\Z")


class Refused(Exception):
    """Not written: what is wrong, for a person."""


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def address(url, what):
    """An https address of scheme and host (a stand: http to this machine), without its last slash."""
    url = (url or "").rstrip("/")
    got = urllib.parse.urlsplit(url)
    local = got.scheme == "http" and got.hostname in ("127.0.0.1", "localhost", "::1")
    if not ((got.scheme == "https" or local) and got.hostname and not got.query and not got.fragment
            and got.username is None and re.fullmatch(r"[A-Za-z0-9._~:/\[\]-]+", url)):
        raise Refused(f"{what} {url!r}: an https address (http only on this machine)")
    return url


def scripts(layer_archive):
    """{name: bytes} of the release's own install.sh and install.ps1, from its layer, as they are in the core."""
    out = {}
    with tarfile.open(layer_archive) as t:
        for m in t:
            parts = m.name.split("/")
            if len(parts) == 4 and parts[1:3] == ["deploy", "oneline"] and parts[3] in SCRIPTS and m.isreg():
                out[parts[3]] = t.extractfile(m).read()
            if len(parts) == 3 and parts[1:] == ["relay", "install.sh"] and m.isreg():
                out[RELAY_SCRIPT] = t.extractfile(m).read()
    missing = [f"deploy/oneline/{n}" for n in SCRIPTS if n not in out] + \
        (["relay/install.sh"] if RELAY_SCRIPT not in out else [])
    if missing:
        raise Refused(f"{os.path.basename(layer_archive)} has no {', '.join(missing)}")
    return out


def relay_script(data, assets):
    """relay/install.sh as the pages hand it out: the release's repository and tag (from --assets, its GitHub
    release) at its marks, each there exactly once — Docker builds the relay of this release from them."""
    got = RELEASE_ASSETS.fullmatch(assets)
    if not got:
        raise Refused(f"--assets {assets}: not a GitHub release's address (relay.sh builds from its repository)")
    data = data.replace(b"\r\n", b"\n")
    if any(data.count(m) != 1 for m in RELAY_MARKS):
        raise Refused("relay/install.sh: not one repository mark and one tag mark — a script of another core")
    data = data.replace(RELAY_MARKS[0], f"https://github.com/{got[1]}/{got[2]}.git".encode("ascii")) \
        .replace(RELAY_MARKS[1], got[3].encode("ascii"))
    if not SCRIPT_TEXT.fullmatch(data):
        raise Refused("relay/install.sh: not printable ASCII")
    return data


def open_script(name, data, base):
    """A script of the core as the open build hands it out: base at its server mark, the open flavor at its flavor
    line (each there exactly once), printable ASCII."""
    old, new = FLAVOR_LINE[name]
    data = data.replace(b"\r\n", b"\n")
    if data.count(URL_MARK) != 1 or data.count(old) != 1:
        raise Refused(f"{name}: not one server mark and one flavor line ({old.strip().decode()}) — a script of "
                      "another core")
    data = data.replace(URL_MARK, base.encode("ascii")).replace(old, new)
    if not SCRIPT_TEXT.fullmatch(data):
        raise Refused(f"{name}: not printable ASCII")
    return data


def releases(layer, shells, assets, notes="", now=None):
    """releases.json of the open build: layer — the layer's sheet (tools/package/layer.py: version, commit,
    runtime_min, archive {name, sha256, size}), shells — {platform: {"archive": {name, sha256, size}, "runtime": N}},
    assets — the address the release's files are downloaded from."""
    when = datetime.datetime.fromtimestamp(now, datetime.timezone.utc) if now is not None else \
        datetime.datetime.now(datetime.timezone.utc)
    arc = layer["archive"]
    entry = {"version": layer["version"], "commit": layer["commit"], "runtime_min": layer["runtime_min"],
             "notes": notes, "published_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "layer": {"url": f"/v1/layer/{arc['name']}", "sha256": arc["sha256"], "size": arc["size"]}}
    rows = {plat: {"url": f"{assets}/{s['archive']['name']}", "sha256": s["archive"]["sha256"],
                   "size": s["archive"]["size"], "runtime": s["runtime"]} for plat, s in sorted(shells.items())}
    current = max([r["runtime"] for r in rows.values()] or [layer["runtime_min"]])
    return {"channels": {"stable": entry}, "runtime": {"current": current, "shells": rows}}


def _same(path, sheet):
    return (type(sheet.get("size")) is int and isinstance(sheet.get("sha256"), str) and os.path.isfile(path)
            and os.path.getsize(path) == sheet["size"] and sha256(path) == sheet["sha256"])


def _place(src, dst):
    """src at dst: a hard link where the disk allows, else a copy."""
    if os.path.lexists(dst):
        os.remove(dst)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".part", "wb") as f:
        f.write(data)
    os.replace(path + ".part", path)


INVITE_PREFIX = "hub-relay-invite-1:"
OPEN_LEFT = 30 * 86400   # the open line goes out with at least this much of its term left


def relay_doc(path, now=None):
    """v1/relay.json of --relay: {"invite": <line>} from a file of one public relay's line, or None for `none`. The
    line is read as every hub reads it (core/busrelay.read_invite: the prefix, the fields, the address, the signature's
    form), by an operator, and with the open line's term: a one-time invitation (`server.py invite`, seven days) would
    let one hub in and refuse the next."""
    if path == "none":
        return None
    try:
        with open(path, encoding="utf-8") as f:
            text = "".join(f.read().split())
    except OSError as e:
        raise Refused(f"--relay {path}: {e}") from None
    sys.path.insert(0, ROOT)
    from core import busrelay   # the core's own reader: what passes here, a hub takes
    try:
        fields = busrelay.read_invite(text)[0]
    except busrelay.Error as e:
        raise Refused(f"--relay {path}: not a relay line a hub takes ({e})") from None
    if fields["by"] != "operator" or not fields["relay"].startswith("https://"):
        raise Refused(f"--relay {path}: the line must be an operator's (relay/server.py open) with an https address")
    if fields["exp"] < (time.time() if now is None else now) + OPEN_LEFT:
        raise Refused(f"--relay {path}: the line is good for less than {OPEN_LEFT // 86400} days — the open line of "
                      "relay/server.py open lasts a year; a one-time invitation lets one hub in")
    return {"invite": text}


def write(release_dir, out, base, assets, notes="", minimum=None, now=None, relay=None):
    """The pages and the release's files of the factory's out of an open tag into out/pages and out/release (relay —
    relay_doc's {"invite"} for v1/relay.json, None — none). -> the summary: {version, tag, pages: [relative paths],
    release: [names]}."""
    base, assets = address(base, "--base"), address(assets, "--assets")
    try:
        with open(os.path.join(release_dir, "release.json"), encoding="utf-8") as f:
            summary = json.load(f)
    except (OSError, ValueError) as e:
        raise Refused(f"{release_dir}: no release.json of the factory ({e})") from None
    layer = summary.get("layer") if isinstance(summary, dict) else None
    platforms = summary.get("platforms") if isinstance(summary, dict) else None
    if not (isinstance(layer, dict) and isinstance(platforms, dict) and platforms):
        raise Refused("release.json: no layer and platforms (the factory's out of a tag, not of a nightly)")
    if layer.get("flavor") != "open":
        raise Refused(f"release.json: the layer is of the {layer.get('flavor')} flavor — the pages carry the open one "
                      "(factory.py <tag> --flavor open)")
    arc = layer.get("archive") or {}
    layer_file = os.path.join(release_dir, str(arc.get("name")))
    if not (NAME.match(str(arc.get("name"))) and _same(layer_file, arc)):
        raise Refused(f"{arc.get('name')}: not the layer release.json names (size or sha256)")
    if arc["size"] > PAGES_MAX:
        raise Refused(f"{arc['name']}: {arc['size']} bytes, the pages take a file of at most {PAGES_MAX}")
    shells = {}
    for plat, p in sorted(platforms.items()):
        a = (p or {}).get("archive") or {}
        path = os.path.join(release_dir, str(a.get("name")))
        if not (NAME.match(str(a.get("name"))) and str(a.get("name")).endswith(f"-open-{plat}.tar.gz")):
            raise Refused(f"{plat}: {a.get('name')} is no environment of the open build (agents-hub-<tag>-open-{plat}.tar.gz)")
        if not _same(path, a):
            raise Refused(f"{plat}: {a.get('name')} is not the archive release.json names (size or sha256)")
        if a["size"] > ASSET_MAX:
            raise Refused(f"{plat}: {a['name']} is {a['size']} bytes, a release takes less than 2 GiB")
        shells[plat] = {"archive": a, "runtime": summary.get("runtime"), "path": path}
    need = layer.get("runtime_min")
    if not (type(need) is int and isinstance(layer.get("version"), str) and isinstance(layer.get("commit"), str)):
        raise Refused("release.json: the layer has no version, commit or runtime_min")
    if not (type(summary.get("runtime")) is int and summary["runtime"] >= need):
        raise Refused(f"release.json: the environments' runtime {summary.get('runtime')!r} is below the layer's "
                      f"{need}")
    texts = scripts(layer_file)
    if minimum is None:
        min_doc = {}
    else:
        try:
            with open(minimum, encoding="utf-8") as f:
                min_doc = json.load(f)
        except (OSError, ValueError) as e:
            raise Refused(f"--min {minimum}: {e}") from None
        if not (isinstance(min_doc, dict) and set(min_doc) <= {"open"}):
            raise Refused("--min: {\"open\": {...}} — the pages are the open build's only")
    pages, release = os.path.join(out, "pages"), os.path.join(out, "release")
    for folder in (pages, release):
        if os.path.isdir(folder):
            shutil.rmtree(folder)
        os.makedirs(folder)
    for name, data in texts.items():
        if name == RELAY_SCRIPT and not RELEASE_ASSETS.fullmatch(assets):
            continue   # files not in a GitHub release (a stand): relay.sh has no repository to build from
        _write(os.path.join(pages, name), relay_script(data, assets) if name == RELAY_SCRIPT
               else open_script(name, data, base))
    doc = releases(layer, shells, assets, notes, now)
    _write(os.path.join(pages, "v1", "releases.json"), json.dumps(doc, indent=1).encode() + b"\n")
    if relay:
        _write(os.path.join(pages, "v1", "relay.json"), json.dumps(relay, indent=1).encode() + b"\n")
    os.makedirs(os.path.join(pages, "v1", "layer"))
    _place(layer_file, os.path.join(pages, "v1", "layer", arc["name"]))
    _write(os.path.join(pages, "min.json"), json.dumps(min_doc, ensure_ascii=False, indent=1).encode() + b"\n")
    _write(os.path.join(pages, ".nojekyll"), b"")
    for s in shells.values():
        _place(s["path"], os.path.join(release, s["archive"]["name"]))
    listed = sorted(os.path.relpath(os.path.join(d, n), pages).replace(os.sep, "/")
                    for d, _, names in os.walk(pages) for n in names)
    return {"version": layer["version"], "tag": summary.get("tag"), "pages": listed,
            "release": sorted(s["archive"]["name"] for s in shells.values())}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pages.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("release", help="the factory's out of an open tag (release.json, the archives, the layer)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--base", required=True, help="the pages' address: https://<account>.github.io/<repo>")
    ap.add_argument("--assets", required=True,
                    help="where the release's files are: https://github.com/<account>/<repo>/releases/download/<tag>")
    ap.add_argument("--notes", help="a file with the release's notes (its CHANGES.md entries)")
    ap.add_argument("--min", help="a file with the open build's minimum: {\"open\": {...}}")
    ap.add_argument("--relay", required=True, help="a file with the public relay's line (relay/server.py open), or none")
    a = ap.parse_args(argv)
    try:
        notes = ""
        if a.notes:
            with open(a.notes, encoding="utf-8") as f:
                notes = f.read().strip()
        got = write(a.release, a.out, a.base, a.assets, notes, a.min, relay=relay_doc(a.relay))
    except (Refused, OSError) as e:
        print(f"pages.py: {e}", file=sys.stderr)
        return 1
    print(f"{got['version']}: the pages in {os.path.join(a.out, 'pages')} ({len(got['pages'])} files), the release's "
          f"files in {os.path.join(a.out, 'release')}:")
    for name in got["release"]:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
