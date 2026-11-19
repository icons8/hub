"""Send the built files of a release where the hubs take them. Sending is publishing: run it with --send only on the
owner's word; without --send it only looks and prints what it would do.

To the delivery server of the team build (app spec §4.1, deliver/server.py) — the first argument is its address:

  upload.py <server> layer <code-V-team.tar.gz> --channel stable|nightly --version V --commit SHA --runtime-min N
            [--notes FILE] [--send] [--key-file F]
  upload.py <server> app <file> --platform <mac-arm64|windows-x64|linux-x64|linux-arm64> --runtime N [--send]
            [--unsigned]   (a Windows app is refused without a signature: tools/package/sign.py)
  upload.py <server> shell <agents-hub-TAG-PLATFORM.tar.gz> --platform P --runtime N [--send]
            (the environment a one-line install downloads; P is mac-arm64, mac-x64, linux-x64, linux-arm64,
            windows-x64 or windows-arm64)
  upload.py <server> script <install.sh|install.ps1> --version V [--address URL] [--send]
            (the install script, deploy/oneline/; stored as install-V.sh|ps1 with <server> — or --address, the
            public one when <server> is a tunnel — written in at its @@HUB_DELIVER_URL@@, ASCII only, made current
            in releases.json)
  upload.py <server> min team|open --version V [--reason FILE] [--since YYYY-MM-DD] [--stops a,b] [--send]
  upload.py <server> show

- the release key is in a file only its owner reads (0600; default ~/.config/hub-core/deliver-release.key);
- a file goes in parts of 64 MiB (a proxy in front takes at most 100 MB a body); a file already there with the same
  bytes is left as is, with other bytes — refused (code 3);
- then releases.json (or min.json) changes in one place — the channel, the platform's app, the build's minimum — from
  the version just read: someone else's change in between is read again, never written over.

To the release of a tag on GitHub (the open build; installer spec S4, §18.2):

  python3 tools/package/upload.py <account>/<repo> <tag> <file>… [--send] [--notes FILE] [--remote URL]

- the tag must already be on the repo (tags are made by the release, not here) — the release is made on it
  (--verify-tag), as a regular one, not a draft;
- every file is named for the tag and gets a `<file>.sha256` next to it (per file: platforms are built on different
  machines and attached by separate runs);
- a file already attached with the same bytes is left as is; with other bytes — refused (code 3): a published file is
  never replaced in place, delete it on GitHub first if that is really meant;
- through gh (its login on this machine). Codes: 0 — done / would be done, 2 — wrong input, 3 — conflict, 4 — gh."""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch import sha256  # noqa: E402

LIMIT = 2 * 1024 ** 3 - 1   # GitHub: a release asset is under 2 GiB


def of_release(name, tag):
    """The file names this release: its version stands apart (v1.2.3 is not v1.2.30; .deb names it without v)."""
    ver = re.escape(tag.removeprefix("v"))
    return bool(re.search(rf"(?<![0-9A-Za-z.])v?{ver}(?![0-9A-Za-z~]|\.[0-9]|[-.](?i:rc|alpha|beta|pre|dev))", name))


def gh(*args, check=True):
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=3600, stdin=subprocess.DEVNULL)
    if check and r.returncode:
        print(f"gh {' '.join(args[:2])}: {(r.stderr or r.stdout).strip()[-500:]}", file=sys.stderr)
        sys.exit(4)
    return r


def draft(repo, tag):
    """A draft release of the tag: `release view` does not see drafts, `release list` does. Not known — code 4."""
    r = gh("release", "list", "-R", repo, "--json", "tagName,isDraft", "--limit", "1000")
    try:
        return any(x.get("tagName") == tag and x.get("isDraft") for x in json.loads(r.stdout))
    except (ValueError, AttributeError, TypeError):
        print("upload.py: gh release list — an answer it does not understand", file=sys.stderr)
        sys.exit(4)


def tag_on_remote(remote, tag):
    r = subprocess.run(["git", "ls-remote", "--tags", remote, f"refs/tags/{tag}"], capture_output=True, text=True,
                       timeout=120, stdin=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    return r.returncode == 0 and bool(r.stdout.strip())


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and re.match(r"https?://", argv[0]):
        import deliver_send
        return deliver_send.main(argv)
    ap = argparse.ArgumentParser(prog="upload.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("repo")
    ap.add_argument("tag")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--send", action="store_true", help="attach for real (on the owner's word to publish)")
    ap.add_argument("--notes", help="a file with the release text (when the release is made here)")
    ap.add_argument("--remote", help="where to look for the tag (default: the repo on GitHub)")
    a = ap.parse_args(argv)
    if a.repo.count("/") != 1:
        sys.exit("upload.py: the repo is <account>/<name>")
    files = []
    for path in a.files:
        name = os.path.basename(path)
        if not os.path.isfile(path):
            print(f"upload.py: no file {path}", file=sys.stderr)
            return 2
        if not of_release(name, a.tag):
            print(f"upload.py: {name} is not a file of the release {a.tag} (the name has no tag)", file=sys.stderr)
            return 2
        size = os.path.getsize(path)
        if size > LIMIT:
            print(f"upload.py: {name} is {size} bytes, GitHub takes less than 2 GiB", file=sys.stderr)
            return 2
        files.append((path, name, size, sha256(path)))
    if len({f[1] for f in files}) != len(files):
        print("upload.py: two files with one name", file=sys.stderr)
        return 2
    remote = a.remote or f"https://github.com/{a.repo}.git"
    if not tag_on_remote(remote, a.tag):
        print(f"upload.py: no tag {a.tag} in {remote} — a release with the tag first", file=sys.stderr)
        return 2
    view = gh("release", "view", a.tag, "-R", a.repo, "--json", "assets", check=False)
    exists = view.returncode == 0
    attached = {}
    if exists:
        try:
            attached = {x["name"]: str(x.get("digest") or "") for x in json.loads(view.stdout or "{}")["assets"]}
        except (ValueError, KeyError, TypeError):
            print("upload.py: gh release view — an answer it does not understand", file=sys.stderr)
            return 4
    elif "not found" not in (view.stderr or "").lower():
        print(f"upload.py: gh release view: {(view.stderr or '').strip()[-300:]}", file=sys.stderr)
        return 4
    if exists is False and draft(a.repo, a.tag):
        print(f"upload.py: {a.tag} has a draft release (an interrupted send?) — publish or delete it on GitHub",
              file=sys.stderr)
        return 3
    # every file and its <file>.sha256 are compared one by one: an interrupted send leaves either of them alone
    todo, same, clash = [], [], []
    for path, name, size, digest in files:
        line = f"{digest}  {name}\n"
        for item, want in ((path, f"sha256:{digest}"), (line, "sha256:" + hashlib.sha256(line.encode()).hexdigest())):
            n = name if item is path else name + ".sha256"
            had = attached.get(n)
            (todo if had is None else same if had == want else clash).append((item, n))
    print(f"release {a.repo} {a.tag}: {'exists' if exists else 'to be created on the tag'}")
    for _, name, size, digest in files:
        print(f"  {name}  {size / 1e6:.1f} MB  sha256 {digest}")
    for _, name in same:
        print(f"  already attached, the same: {name}")
    for _, name in clash:
        print(f"  REFUSED: {name} is already attached with other content ({attached[name]})")
    if clash:
        return 3
    if not a.send:
        print("a dry run: nothing sent (sending is --send, on the owner's word)")
        return 0
    with tempfile.TemporaryDirectory(prefix="hub-upload-") as d:
        send = []
        for item, name in todo:
            if name.endswith(".sha256") and not os.path.isfile(item):
                with open(os.path.join(d, name), "w") as f:
                    f.write(item)
                item = os.path.join(d, name)
            send.append(item)
        if not exists:   # with files gh makes a draft, attaches them all and only then publishes: no empty "latest"
            notes = ["--notes-file", a.notes] if a.notes else ["--notes", a.tag]
            gh("release", "create", a.tag, *send, "-R", a.repo, "--verify-tag", "--title", a.tag, *notes)
        elif send:
            gh("release", "upload", a.tag, "-R", a.repo, *send)
    print(f"attached: {', '.join(n for _, n in todo) or 'nothing new'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
