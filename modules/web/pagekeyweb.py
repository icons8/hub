"""What is left of the sign-in by a link with a key (core/pagekey.py) since the page has a password (core/pagepass.py):
the state "key" of a hub that had it on and has no password yet — its link still gives the cookie, until a password is
set or the protection is taken off — and the short /api/about the update's watchdog reads without signing in."""
from core import pagekey, pagepass
from modules.tailnet import access


def _local(h):
    return access.local(h.headers.get("Host"), h.client_address[0], headers=h.headers)


def from_link(web, h):
    """GET /?key=…: in the state "key", the key of this hub from a request of this machine — the cookie and a redirect
    to the address without the key (it does not stay in the address bar or the history); a stale key on this machine —
    the page that says what to do. Any other state: the key means nothing, the redirect drops it from the address (an
    app shell of an earlier build still opens the link). True if answered."""
    path, _, query = h.path.partition("?")
    if path not in ("/", "/index.html") or not query:
        return False
    key = pagekey.asked(query)
    if key is None:
        return False
    cookie = None
    if pagepass.state() == "key" and _local(h):
        mine = pagekey.read()
        if not (mine and pagekey.same(key, mine)):
            from modules.web import signin
            signin.page(web, h)
            return True
        cookie = pagekey.set_cookie(mine)
    from core import remote
    if remote.identify(web.state_dir, h.headers.get("Host"), h.client_address[0], h.headers) is None:
        return False   # who came is unknown: the usual refusal
    h.send_response(303)
    h.send_header("Location", "/")
    h.send_header("Cache-Control", "no-store")
    h.send_header("Referrer-Policy", "no-referrer")
    h.send_header("Content-Length", "0")
    if cookie:
        h.send_header("Set-Cookie", cookie)
    h.end_headers()
    return True


def about_alive(web):
    """/api/about for a request of this machine without the sign-in: only whether the hub is up, on what code, and how
    it is signed in to — what the update's watchdog (modules/update/trial.py, the previous release's own copy, which
    knows no password) waits for, and what a neighbouring hub's page asks before it offers a password for this one.
    Without it, a password would roll every update back. No paths, no sessions, no addresses; the code card is the
    page's cached one (git at most every 30 s)."""
    code = web.code()
    return {"core_started": web.tail.core_started(), "web_started": getattr(web, "started", None),
            "code": {k: code[k] for k in ("running", "layer") if k in code}, "signin": pagepass.state()}
