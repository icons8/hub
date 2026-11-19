"""The sign-in of the page by its password (core/pagepass.py): the form a browser sees without the cookie, POST /signin
(a plain HTML form, no script: a foreign site cannot send it — access.cross_site refuses every cross-site POST) and
POST /api/signout. The form carries nothing of the hub's data: its title (two hubs on one machine are told apart), the
field, and how to take the password off in the terminal when it is forgotten."""
import html
import json
from urllib.parse import parse_qs

from core import pagepass

FORM_MAX = 4096   # the body of the form: the password is at most pagepass.MAX_LEN characters


def _command():
    from core import instance
    return instance.command_name(instance.name())


def secure(web, h):
    """The request came over https (Cloudflare Access): its cookie is Secure."""
    from core import remote
    got = remote.identify(web.state_dir, h.headers.get("Host"), h.client_address[0], h.headers)
    return bool(got) and got.get("provider") == "cloudflare"


def page(web, h, code=200, said=None):
    """The form (state "password"), or what to do in the terminal (states "key" and "broken")."""
    st = pagepass.state()
    try:
        cfg = web.config() or {}
    except (OSError, ValueError):
        cfg = {}
    title = str(cfg.get("title") or web.instance_name() or h.t("web.hub.default_title"))[:100]
    cmd = _command()
    lines = []
    if st == "key":
        from core import pagekey
        # the old key's file damaged: its link cannot help, only the terminal
        lines = [h.t("web.pass.key_moved", cmd=cmd) if pagekey.read() else h.t("web.pass.broken", cmd=cmd)]
    elif st == "broken":
        lines = [h.t("web.pass.broken", cmd=cmd)]
    form = ""
    if st == "password":
        warn = f'<p class="warn" role="alert">{html.escape(said)}</p>' if said else ""
        form = (f'{warn}<form method="post" action="/signin"><label for="p">{html.escape(h.t("web.pass.field"))}</label>'
                '<input id="p" name="password" type="password" autocomplete="current-password" autofocus required '
                f'maxlength="{pagepass.MAX_LEN}"><button type="submit">{html.escape(h.t("web.pass.sign_in"))}</button>'
                f'</form><p class="hint">{html.escape(h.t("web.pass.not_os"))}</p>'
                f'<p class="hint">{html.escape(h.t("web.pass.forgot", cmd=cmd))}</p>')
    body = "".join(f"<p>{html.escape(x)}</p>" for x in lines)
    doc = (f'<!doctype html><html lang="{html.escape(h.lang())}"><head><meta charset="utf-8">'
           '<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer">'
           f"<title>{html.escape(title)}</title><style>"
           "body{font:16px/1.5 system-ui,sans-serif;max-width:26em;margin:15vh auto;padding:0 1em;color-scheme:light dark}"
           "form{display:flex;flex-direction:column;gap:.5em;margin:1em 0}input,button{font:inherit;padding:.5em}"
           ".hint{opacity:.7;font-size:.9em}.warn{color:#c33}</style></head>"
           f"<body><h1>{html.escape(title)}</h1>{body}{form}</body></html>").encode()
    h.send_response(code)
    h.send_header("Content-Type", "text/html; charset=utf-8")
    h.send_header("Content-Length", str(len(doc)))
    h.send_header("Cache-Control", "no-store")
    h.send_header("Referrer-Policy", "no-referrer")
    h.end_headers()
    h.wfile.write(doc)


def post(web, h):
    """POST /signin password=…: right — the cookie and a redirect to the page; wrong — the form again with how many
    minutes are left when it is locked. The body is never logged."""
    try:
        length = int(h.headers.get("Content-Length") or 0)
    except ValueError:
        length = -1
    if not 0 <= length <= FORM_MAX:
        h.close_connection = True
        return page(web, h, 413)
    raw = h.rfile.read(length).decode("utf-8", "replace")
    got = parse_qs(raw, keep_blank_values=True).get("password") or [""]
    password = got[0] if len(got) == 1 else ""
    verdict, wait = pagepass.attempt(password)
    if verdict == "ok":
        h.send_response(303)
        h.send_header("Location", "/")
        h.send_header("Cache-Control", "no-store")
        h.send_header("Content-Length", "0")
        h.send_header("Set-Cookie", pagepass.set_cookie(pagepass.read(), secure=secure(web, h)))
        return h.end_headers()
    if verdict == "none":   # no password (any more): the page itself
        h.send_response(303)
        h.send_header("Location", "/")
        h.send_header("Content-Length", "0")
        return h.end_headers()
    key, params = pagepass.said(verdict, wait)
    return page(web, h, 429 if verdict == "locked" else 401, h.t(key, **params))


def signout(web, h):
    """POST /api/signout: this browser's cookie is gone (a copy taken elsewhere lives until a password change)."""
    data = json.dumps({"ok": True}).encode()
    h.send_response(200)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "no-store")
    h.send_header("Set-Cookie", pagepass.clear_cookie(secure=secure(web, h)))
    # the owner's files this browser kept to revalidate (/api/file is private, no-cache) leave its disk cache; browsers
    # obey it only on https or localhost, over plain http they keep the copies (each still needs the cookie to be shown)
    h.send_header("Clear-Site-Data", '"cache"')
    h.end_headers()
    h.wfile.write(data)


NOTICE = "page-key-moved"
TOLD = "page-key-moved.json"           # in the state folder: the notice is out (one per stay in the state "key")
OLD_NOTICE, OLD_TOLD = "page-key-users", "page-key-hint.json"   # the former hint about other accounts: withdrawn
NOTICE_EVERY = 300


def notice(web):
    """A hub with the old link with a key and no password: one notice to the owner (no model turn) — the sign-in by a
    key is replaced by the password, set it; withdrawn once the state is left (a password set, the protection taken
    off — from the page or the terminal). Returns True while the notice stands."""
    import os
    from core import catalog
    told = os.path.join(web.state_dir, TOLD)
    old = os.path.join(web.state_dir, OLD_TOLD)
    if os.path.exists(old):   # the hint about other accounts of the machine is gone with this release
        web.put("owner.notify_resolved", None, {"id": OLD_NOTICE, "source": "web"})
        try:
            os.unlink(old)
        except OSError:
            pass
    if pagepass.state() == "key":
        if not os.path.exists(told):
            web.put("owner.notify", None, {"id": NOTICE, "source": "web", "push": False,
                                           "title": web.t("web.pass.notice_title"), "text": web.t("web.pass.notice_text")})
            catalog.write_private(told, {"told": True})
        return True
    if os.path.exists(told):
        web.put("owner.notify_resolved", None, {"id": NOTICE, "source": "web"})
        try:
            os.unlink(told)
        except OSError:
            pass
    return False


def notice_loop(web):
    while True:
        try:
            notice(web)
        except Exception as e:  # noqa: BLE001 — a notice must not take the page down
            print(f"web: sign-in notice: {e!r}", flush=True)
        if web.stop.wait(NOTICE_EVERY):
            return
