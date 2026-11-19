#!/usr/bin/env python3
"""tools/publish.sh pushes the whole tree into a repository of the publishing hub; the open repositories get only the open
snapshot, and only from tools/oss_open.py (its list OPEN_TARGETS) and tools/publish_pair.py, which also never puts
the embed there. Exit 0 — the address is not one of them, 17 — it is one
of them (the name is printed); any other code is a failure of the check itself.

  publish_guard.py <git-url>

The address is compared by its owner/name: https, ssh, scp-like (git@host:owner/name), a trailing .git or slash, a sign-in
in front of the host, the case — all the same. A folder on this machine is never an open repository (a rehearsal)."""
import os
import re
import sys
import urllib.parse

HIT = 17
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def slug_of(url):
    """owner/name of a remote address (lowercase), "" for a folder on this machine or what has no owner/name."""
    url = url.strip()
    if not url or url.startswith(("/", ".", "~", "file:")) or re.match(r"^[A-Za-z]:[\\/]", url):
        return ""
    if "://" in url:
        path = urllib.parse.urlsplit(url).path
    else:   # scp-like: [user@]host:owner/name
        path = url.split(":", 1)[1] if ":" in url else url
    parts = [p for p in re.split(r"[\\/]+", urllib.parse.unquote(path)) if p]
    if len(parts) < 2:
        return ""
    name = re.sub(r"\.git$", "", parts[-1], flags=re.I)
    return f"{parts[-2]}/{name}".lower()


def open_targets():
    if ROOT not in sys.path:   # tools/ as a package: the module of a caller that imported it already, no second copy
        sys.path.insert(0, ROOT)
    from tools import oss_open
    return tuple(t.lower() for t in oss_open.OPEN_TARGETS)


def main(argv):
    if len(argv) != 1:
        sys.exit(__doc__)
    slug = slug_of(argv[0])
    if not slug:
        return 0
    if slug in open_targets():
        print(slug)
        return HIT
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
