"""The Cloudflare API v4 for `hub remote cloudflare setup|remove|rotate` (modules/cloudflare/setup.py): the owner's API
token goes only to api.cloudflare.com, held in memory for one command, never written, never followed through a
redirect. Through the owner's proxy (core/net). The stdlib only.

HUB_CLOUDFLARE_API replaces the address for tests — only with an address of this machine (a fake server), so that a
changed environment cannot send the token elsewhere.
"""
import json
import os
import urllib.parse

from core import net, nethttp

BASE = "https://api.cloudflare.com/client/v4"
TIMEOUT = 30
MAX_ANSWER = 4 << 20


class Failed(Exception):
    """Cloudflare refused or could not be reached: `status` (0 — no answer), `errors` [{code, message}] as it said."""

    def __init__(self, method, path, status, errors):
        self.method, self.path, self.status, self.errors = method, path, status, errors
        said = "; ".join(f"{e.get('code', '')} {e.get('message', '')}".strip() for e in errors if isinstance(e, dict))
        super().__init__(f"{method} {path}: HTTP {status or '—'} {said}".strip())

    @property
    def gone(self):
        """The thing is not there (already deleted): 404, or Cloudflare's «not found» codes under another status."""
        return self.status == 404 or any(isinstance(e, dict) and "not found" in str(e.get("message", "")).lower()
                                         for e in self.errors)


def base():
    own = os.environ.get("HUB_CLOUDFLARE_API", "")
    if own and net.is_local(urllib.parse.urlsplit(own).netloc):
        return own.rstrip("/")
    return BASE


class Api:
    def __init__(self, token):
        self._token = token
        self.base = base()

    def __repr__(self):
        return f"Api({self.base})"   # never the token

    def call(self, method, path, body=None, query=None):
        """The `result` of Cloudflare's answer; Failed on a refusal or no answer."""
        url = self.base + path + (f"?{urllib.parse.urlencode(query)}" if query else "")
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json",
                   "User-Agent": "agents-hub-cloudflare/1"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            status, _, raw = nethttp.request(method, url, data, headers, TIMEOUT, MAX_ANSWER)
        except OSError as e:
            raise Failed(method, path, 0, [{"message": str(e) or type(e).__name__}]) from None
        try:
            answer = json.loads(raw) if raw else {}
        except ValueError:
            answer = {}
        answer = answer if isinstance(answer, dict) else {}
        if status >= 300 or answer.get("success") is False:   # a redirect is not followed: the token stays here
            errors = answer.get("errors") if isinstance(answer.get("errors"), list) else []
            raise Failed(method, path, status, errors)
        return answer.get("result")

    def pages(self, path, query=None):
        """All items of a listed resource (50 a page, 20 pages at most)."""
        out = []
        for page in range(1, 21):
            got = self.call("GET", path, query={**(query or {}), "page": page, "per_page": 50})
            got = got if isinstance(got, list) else []
            out += got
            if len(got) < 50:
                break
        return out
