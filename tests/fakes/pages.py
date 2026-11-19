"""The open build's publishing places on 127.0.0.1 (tools/package/pages.py writes what they serve): "pages" — a folder as
static files to anyone, no key (GitHub Pages); "github" — a release asset's address that answers with a redirect to
another server, as github.com does to its file storage; that one serves the bytes and continues a download (Range). Every
request is recorded: (server name, path, its Authorization header) — a key must never reach any of them."""
import http.server
import os
import threading


class Place:
    """A server on a free port of 127.0.0.1: files of a folder (root) and redirects ({path: address}); asked — the log
    of all places of the test (a list they share)."""

    def __init__(self, name, asked, root=None, redirects=None):
        self.name, self.asked, self.root, self.redirects = name, asked, root, dict(redirects or {})
        place = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                place.asked.append((place.name, self.path, self.headers.get("Authorization")))
                path = self.path.split("?", 1)[0]
                if path in place.redirects:
                    self.send_response(302)
                    self.send_header("Location", place.redirects[path])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                data = place.read(path)
                if data is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start = 0
                rng = self.headers.get("Range") or ""
                if rng.startswith("bytes=") and rng[6:].split("-")[0].isdigit():
                    start = int(rng[6:].split("-")[0])
                    end = rng[6:].split("-", 1)[1]
                    last = min(int(end), len(data) - 1) if end.isdigit() else len(data) - 1
                    if start >= len(data):
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{len(data)}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{last}/{len(data)}")
                    data = data[start:last + 1]
                else:
                    self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def read(self, path):
        if self.root is None:
            return None
        rel = [p for p in path.split("/") if p]
        if not rel or any(p in (".", "..") for p in rel):
            return None
        file = os.path.join(self.root, *rel)
        if not os.path.isfile(file):
            return None
        with open(file, "rb") as f:
            return f.read()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def places(test, pages_root, assets_root, base="/agents-hub"):
    """The three places of a test: pages (pages_root under base, as https://<account>.github.io/<repo>), files
    (assets_root: the storage the redirects lead to) and github (its redirects, added by the test); closed with the
    test. -> (pages, github, files, asked)."""
    asked = []
    files = Place("files", asked, assets_root)
    pages = Place("pages", asked)
    pages.read = lambda path: Place.read(pages, path[len(base):]) if path.startswith(base + "/") else None
    pages.root = pages_root
    github = Place("github", asked)
    for p in (pages, github, files):
        test.addCleanup(p.close)
    return pages, github, files, asked


S = "https://icons8.example.test/agents-hub"   # an open build's pages
STAND = "http://127.0.0.1:8"                    # a stand's
# (address, the pages it came from, may the open build download from it) — core/fetch.asset_ok, install.sh asset_ok and
# install.ps1 Asset-Ok answer the same
ASSET_CASES = (
    ("https://github.com/acme/agents-hub/releases/download/v1.9.0/agents-hub-v1.9.0-open-linux-x64.tar.gz", S, True),
    ("https://objects.githubusercontent.com/github-production-release-asset-2e65be/1/2?X-Amz-Algorithm=AWS4-HMAC-SHA256"
     "&X-Amz-Signature=0a1b&response-content-disposition=attachment%3B%20filename%3Dx.tar.gz", S, True),
    ("https://release-assets.githubusercontent.com/github-production-release-asset/1/2?sp=r&sv=2018-11-09&sr=b"
     "&se=2026-11-11T10%3A00%3A00Z&sig=a%2Bb%3D&jwt=eyJ0.eyJ1.c2ln", S, True),
    ("https://icons8.example.test/agents-hub/v1/app/agents-hub-v1.9.0-open-linux-x64.tar.gz", S, True),
    ("http://github.com/acme/x.tar.gz", S, False),
    ("https://github.com.example.test/x.tar.gz", S, False),
    ("https://example.test/github.com/x.tar.gz", S, False),
    ("https://user@github.com/x.tar.gz", S, False),
    ("https://github.com:8443/x.tar.gz", S, False),
    ("https://GitHub.com/x.tar.gz", S, False),
    ("ftp://github.com/x.tar.gz", S, False),
    ("https://github.com/x y.tar.gz", S, False),
    ("https://github.com/x\".tar.gz", S, False),
    ("https://github.com\\@example.test/x.tar.gz", S, False),
    ("https://github.com/x.tar.gz#y", S, False),
    ("http://127.0.0.1:9/x.tar.gz", STAND, True),
    ("http://localhost:9/x.tar.gz", STAND, True),
    ("http://localhost:x/x.tar.gz", STAND, False),
    ("http://127.0.0.1:/x.tar.gz", STAND, False),
    ("http://203.0.113.5/x.tar.gz", STAND, False),
    ("https://203.0.113.5/x.tar.gz", STAND, False),
    ("http://127.0.0.1:9/x.tar.gz", S, False),
)
