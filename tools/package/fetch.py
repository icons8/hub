"""Download what the hub packages pin (versions.json) for one platform into a cache and verify sha256 and size.

  python3 tools/package/fetch.py <platform> <cache>   → KEY=file_name lines (python, node, git, gh, ffmpeg) for a build

Platforms: linux-x64, linux-arm64, mac-arm64, mac-x64, windows-x64, windows-arm64 (tools/package/build.py PLATFORMS).
Only what goes into the package: parts with "bundle": false (Chrome for Testing — the hub downloads it at the first
start) are not fetched.
A file already in the cache with a matching sha256 and size is not downloaded again. A mismatch is an error and the
download is deleted: nothing unverified goes into a build. The stdlib only — runs on any python3."""
import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
VERSIONS = os.path.join(HERE, "versions.json")


class Mismatch(Exception):
    pass


def load(path=VERSIONS):
    with open(path) as f:
        return json.load(f)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parts(versions, platform, bundled=True, tools=False):
    """[{key, url, sha256, size, name}] of the platform: one file per part that has a pin for it. Build tools ("tool":
    true — Inno Setup) only with tools=True, and then only they."""
    out = []
    for key, item in versions.items():
        if not isinstance(item, dict) or (bundled and item.get("bundle") is False) or bool(item.get("tool")) != tools:
            continue
        pin = (item.get("files") or {}).get(platform)
        if pin:
            name = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(pin["url"]).path))
            out.append({"key": key, "url": pin["url"], "sha256": pin["sha256"], "size": pin["size"], "name": name})
    return out


def fetch(part, cache):
    """The path of the verified file in the cache; Mismatch when what came is not what is pinned."""
    os.makedirs(cache, exist_ok=True)
    path = os.path.join(cache, part["name"])
    if os.path.exists(path) and os.path.getsize(path) == part["size"] and sha256(path) == part["sha256"]:
        return path
    tmp = path + ".part"
    try:
        with urllib.request.urlopen(part["url"], timeout=60) as r, open(tmp, "wb") as f:
            got = 0
            while chunk := r.read(1 << 20):
                got += len(chunk)
                if got > part["size"]:
                    break
                f.write(chunk)
        size, digest = os.path.getsize(tmp), sha256(tmp)
        if (size, digest) != (part["size"], part["sha256"]):
            raise Mismatch(f"{part['name']}: {'more than ' if got > part['size'] else ''}{size} bytes, sha256 {digest}, "
                           f"and versions.json has {part['size']} and {part['sha256']} — not building")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def main(platform, cache):
    for part in parts(load(), platform):
        try:
            fetch(part, cache)
        except Mismatch as e:
            sys.exit(str(e))
        print(f"{part['key'].upper()}={part['name']}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
