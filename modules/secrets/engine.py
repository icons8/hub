"""Putting a secret into a form field (spec d2345345 §3.4): place -> fact -> the hub's vault -> one call in the tab that
checks the field and writes the value into exactly that element -> fact.

Runs in the long-lived page process, not in the session process: the value never reaches the session. The session is a
thin client (`hub secret fill`). There is no owner confirmation: a card would not be a barrier (a session is the same OS
user and can press it through the page's API); the limits are the ones the owner set when he put the secret in — its
sites and the type of field. Every hand-out is a fact before the value leaves (`secret.requested`) and after it.

The vault and Chrome are injected from outside in tests (`store`, `targets`, `connect`)."""
import json
import re
import threading
import urllib.parse

from core import i18n
from modules.browser import browsers
from modules.proof import cdp
from modules.secrets import vault

FIELDS = vault.FIELDS
FIELD_TYPES = {"password": ("password",), "username": ("text", "email")}
NAME = vault.NAME
PROBE = ("JSON.stringify((()=>{const e=document.activeElement;return {o:location.origin,t:e&&e.tagName,"
         "ty:e&&e.type,f:document.hasFocus()}})())")
WORLD = "hub-secrets"
# One synchronous piece of JS in the tab's isolated world: the page's scripts do not see our variables. It checks the
# origin, the focus and the focused element, then writes the value into that element by reference (the native setter —
# not "type where the focus is"): no handler of the site runs between the check and the write. `input` and `change`
# then let frameworks see it (React's value tracker sees a new value). The answer is a word, never the value.
INSERT = """(() => {
  const v = %(value)s, o = %(origin)s, types = %(types)s, e = document.activeElement;
  if (location.origin !== o || !document.hasFocus() || !e || e.tagName !== "INPUT" || !types.includes(e.type))
    return "moved";
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(e, v);
  e.dispatchEvent(new Event("input", {bubbles: true}));
  e.dispatchEvent(new Event("change", {bubbles: true}));
  return e.value === v ? "ok" : "changed";
})()"""


class Reason(Exception):
    """A refusal or a failure with its reason: a catalog key and its parameters."""

    def __init__(self, key, **params):
        super().__init__(key)
        self.key, self.params = key, params

    def __str__(self):
        return i18n.tr(i18n.SOURCE, self.key, **self.params)


class Refused(Reason):
    """Not allowed: no such secret or field, wrong site or field."""


class Unavailable(Reason):
    """The vault or Chrome did not answer, or the key is lost."""


def answer(key, **params):
    """The reply's text in the page process's language, plus the key and its parameters: `hub secret` prints it in its
    own language (a session's step reads English)."""
    return {"error": i18n.t(key, **params), "key": key, **({"params": params} if params else {})}


def deny_all(request):   # an earlier release's page server still in memory passes it; nothing calls it now
    raise Refused("cli.secrets.not_listed")


def origin_of(url):
    """A tab's origin in the form the secret's sites are kept in (vault.origin_of: punycode, [IPv6], no default port)."""
    u = urllib.parse.urlparse(url or "")
    return vault.origin_of(url) if u.scheme in ("http", "https") and u.netloc else ""


def settings(cfg):
    s = (cfg or {}).get("secrets")
    return s if isinstance(s, dict) else {}


def enabled(cfg):
    """On unless the owner wrote `secrets.enabled: false` (an empty vault does nothing)."""
    return settings(cfg).get("enabled") is not False


def items(cfg):
    """The allowlist of an earlier release (`secrets.items` in hub.json, references to outside vaults): no longer
    read. Kept for an earlier page server that calls it from a file read from the disk (modules/web/modinfo.py)."""
    return {}


def browser_url(cfg):
    """CDP address of the agents' browser: `secrets.browser` is a name from `browsers` in hub.json; without a name, "agents", then the first one."""
    found = {n: b for n, b in browsers.configured(cfg).items() if not b.get("problem")}
    name = settings(cfg).get("browser")
    pick = found.get(name) if name else (found.get("agents") or next(iter(found.values()), None))
    return pick["url"] if pick else None


class Vault:
    """The hub's vault (modules/secrets/vault.py) as the engine sees it: what is shown, and one value."""

    def __init__(self, state_dir):
        self.state_dir = state_dir

    def meta(self, name):
        from modules.secrets import vault
        try:
            return vault.items(self.state_dir).get(name)
        except vault.Error as e:
            raise Unavailable(e.key, **e.params) from None

    def value(self, name, field):
        from modules.secrets import vault
        try:
            return vault.value(self.state_dir, name, field)
        except vault.Error as e:
            raise Unavailable(e.key, **e.params) from None


class Session:
    """One Chrome tab over CDP: call a method, evaluate an expression."""

    def __init__(self, ws_url, timeout=30):
        self.ws = cdp.Socket(ws_url, timeout=timeout)
        self.n = 0

    def call(self, method, params=None):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise cdp.Error(f"{method}: {msg['error'].get('message')}")
                return msg.get("result") or {}

    def js(self, expr, context=None):
        out = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                             **({"contextId": context} if context else {})})
        if out.get("exceptionDetails"):
            raise cdp.Error("the expression in the tab failed")
        return (out.get("result") or {}).get("value")

    def insert(self, value, origin, types):
        """INSERT in an isolated world of the tab's main frame: "ok", "changed" (the site changed the value) or "moved"."""
        frame = self.call("Page.getFrameTree")["frameTree"]["frame"]["id"]
        ctx = self.call("Page.createIsolatedWorld", {"frameId": frame, "worldName": WORLD})["executionContextId"]
        # json.dumps with ensure_ascii: a JS string literal with no line separators in it
        return self.js(INSERT % {"value": json.dumps(value), "origin": json.dumps(origin), "types": json.dumps(list(types))},
                       ctx)

    def close(self):
        self.ws.close()


class Engine:
    """`backends` and `approve` are what an earlier page server passes: ignored — the store is the hub's vault."""

    def __init__(self, load_cfg, put, backends=None, approve=None, targets=None, connect=Session, state_dir=None,
                 store=None):
        if store is None:
            if state_dir is None:
                from core.daemon import default_state_dir
                state_dir = default_state_dir()
            store = Vault(state_dir)
        self.load_cfg, self.put, self.store = load_cfg, put, store
        self.targets = targets or (lambda url: browsers.tabs(url))
        self.connect = connect
        self.waiting, self.lock = set(), threading.Lock()

    def fact(self, kind, who, item, field, origin=None, why=None, **extra):
        self.put(kind, who, {"item": item, "field": field, **({"origin": origin} if origin else {}),
                             **({"why": why} if why else {}), **extra})

    @staticmethod
    def why(e):
        """The reason in a fact: the owner reads it (the page, Telegram) — in his language."""
        return i18n.tr(i18n.owner(), e.key, **e.params)

    def place(self, cfg, sites, field):
        """A tab with the cursor in a field of the right type on one of the secret's sites: (tab session, origin)."""
        url = browser_url(cfg)
        if not url:
            raise Refused("cli.secrets.no_browser")
        try:
            pages = [t for t in self.targets(url) if t.get("type") == "page" and origin_of(t.get("url")) in sites
                     and t.get("webSocketDebuggerUrl")]
        except (OSError, ValueError):
            raise Unavailable("cli.secrets.browser_down")
        if not pages:
            raise Refused("cli.secrets.no_tab", origins=", ".join(sites))
        for t in pages:
            page = self.connect(t["webSocketDebuggerUrl"])
            try:
                try:
                    now = json.loads(page.js(PROBE) or "{}")
                except ValueError:
                    now = {}
                origin = origin_of(t["url"])
                if (now.get("o") == origin and now.get("f") and now.get("t") == "INPUT"
                        and now.get("ty") in FIELD_TYPES[field]):
                    return page, origin
            except Exception:
                page.close()
                raise
            page.close()
        raise Refused("cli.secrets.cursor_password" if field == "password" else "cli.secrets.cursor_text")

    def fill(self, who, item, field):
        """(code, answer). 200 put in; 403 not allowed; 409 already busy; 502 the vault or Chrome did not answer."""
        cfg = self.load_cfg()
        if not enabled(cfg):
            return 403, answer("cli.secrets.off")
        try:
            meta = self.store.meta(item)
        except Unavailable as e:
            return 502, answer(e.key, **e.params)
        refusal = None
        if not meta or not meta.get("value") or field not in FIELDS:
            refusal = Refused("cli.secrets.not_listed")
        elif field == "username" and not meta.get("login"):
            refusal = Refused("cli.secrets.no_login", name=item)
        elif not meta.get("sites"):   # a login too goes only onto the secret's sites
            refusal = Refused("cli.secrets.no_sites", name=item)
        if refusal:
            self.fact("secret.denied", who, item, field, why=self.why(refusal))
            return 403, answer(refusal.key, **refusal.params)
        with self.lock:
            if item in self.waiting:
                return 409, answer("cli.secrets.waiting")
            self.waiting.add(item)
        page = None
        try:
            try:
                page, origin = self.place(cfg, meta["sites"], field)
            except Refused as e:
                self.fact("secret.denied", who, item, field, why=self.why(e))
                return 403, answer(e.key, **e.params)
            except Unavailable as e:
                self.fact("secret.failed", who, item, field, why=self.why(e))
                return 502, answer(e.key, **e.params)
            # before the value leaves: a crash after this leaves a hand-out without an outcome, not none at all
            self.fact("secret.requested", who, item, field, origin)
            try:
                got = page.insert(self.store.value(item, field), origin, FIELD_TYPES[field])
            except Reason as e:
                self.fact("secret.failed", who, item, field, origin, why=self.why(e))
                return 502, answer(e.key, **e.params)
            except (OSError, ValueError, KeyError, TypeError, cdp.Error) as e:
                self.fact("secret.failed", who, item, field, origin, why=type(e).__name__)
                return 502, answer("cli.secrets.chrome_no_answer", error=type(e).__name__)
            if got == "moved":
                e = Refused("cli.secrets.moved")
                self.fact("secret.denied", who, item, field, origin, why=self.why(e))
                return 403, answer(e.key)
            if got not in ("ok", "changed"):
                e = Unavailable("cli.secrets.not_typed")
                self.fact("secret.failed", who, item, field, origin, why=self.why(e))
                return 502, answer(e.key)
            self.fact("secret.filled", who, item, field, origin, **({"changed": True} if got == "changed" else {}))
            key = "cli.secrets.filled" if got == "ok" else "cli.secrets.filled_changed"
            params = {"field": field, "item": item, "origin": origin}
            return 200, {"ok": True, "text": i18n.t(key, **params), "key": key, "params": params}
        finally:
            if page:
                page.close()
            with self.lock:
                self.waiting.discard(item)
