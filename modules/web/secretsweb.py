"""Settings -> Secrets (spec d2345345 §3.3): the owner puts secrets into the hub's vault (modules/secrets/vault.py),
changes, removes and copies them. The page passes the gates of every page action first (localhost, the tailnet owner,
Cloudflare Access; X-Hub on POST) — the vault has no gate of its own.

- A value never comes to the page except as the answer of "copy": not in the list, not in the edit form.
- Values travel only from this machine or over the tailnet (WireGuard end to end); through Cloudflare Access or any
  other way they go neither way: TLS ends elsewhere, and secrets are passwords of other services. The list, sites, notes and removal work there; the page hides the value field and "Copy" (`values: false`),
  and the server refuses them all the same.
- The facts name the secret and what changed, never the value; the bodies of these requests are not logged."""
import json

from modules.secrets import vault

VALUES_VIA = ("local", "tailnet")   # an allowlist: a provider added later carries no values until it is named here



def via(web, handler):
    """How this request came: "local", or the remote provider's name (tailnet, cloudflare)."""
    from core import remote
    from modules.tailnet import access
    host, peer = handler.headers.get("Host"), handler.client_address[0]
    if access.local(host, peer, headers=handler.headers):
        return "local"
    got = remote.identify(web.state_dir, host, peer, handler.headers)   # the way only: the page's gate let it in
    return (got or {}).get("provider") or "remote"


def last_uses(web):
    """{name: the last hand-out} from the fold (core/reducer.py secrets_last); {} while the journal is being read."""
    got = web.tail.peek(lambda r: dict(r.state.get("secrets_last") or {}), web.READ_WAIT)
    return got if isinstance(got, dict) else {}


def status(web, handler):
    """GET /api/secrets: the vault's state, its secrets without values, and whether values may travel this way."""
    from modules.secrets import engine
    path = via(web, handler)
    state = vault.status(web.state_dir)
    try:
        shown = vault.items(web.state_dir) if state != "broken" else {}
    except vault.Error:
        state, shown = "broken", {}
    last = last_uses(web)
    out = {"state": state, "via": path, "values": path in VALUES_VIA, "on": engine.enabled(web.config()),
           "items": [{**m, **({"last": last[n]} if n in last else {})} for n, m in sorted(shown.items())]}
    try:   # the hubs of this account without a page password (spec d2344898 §2.0): the tab names them
        from core import pagepass
        from modules.web import instancesweb
        names = pagepass.unprotected(knows=instancesweb.knows_password, state_dir=web.state_dir)
        out["unlocked"] = list(dict.fromkeys(n or web.t("web.secrets.main_hub") for n in names))   # "" is the main hub
    except (ImportError, AttributeError):   # a core without the hub password yet: no field, no line
        pass
    return out


def _err(web, e):
    """A refusal: the page's text, and the key with its parameters for `hub secret` to say it in its own language."""
    return {"error": web.t(e.key, **e.params), "key": e.key, "params": e.params}


def act(web, handler, body):
    """POST /api/secrets/<save|add|restore|remove|copy|start-over|install>: (code, body as a JSON string)."""
    what = handler.path.rsplit("/", 1)[-1]
    name = body.get("name")
    path = via(web, handler)
    remote_values = path not in VALUES_VIA
    try:
        if what == "save":
            password, login = body.get("password"), body.get("login")
            if remote_values and (password is not None or login not in (None, "")):
                return 403, json.dumps({"error": web.t("web.secrets.no_values_remote")}, ensure_ascii=False)
            if body.get("new") and isinstance(name, str) and name in vault.items(web.state_dir):
                return 409, json.dumps({"error": web.t("web.secrets.exists", name=name)}, ensure_ascii=False)
            changed = vault.save(web.state_dir, name, password=password, login=login,
                                 sites=body.get("sites"), commands=body.get("commands"), note=body.get("note"))
            if changed:
                web.put("secret.saved", None, {"item": name, "changed": changed})
            return 200, json.dumps({"ok": True, "changed": changed}, ensure_ascii=False)
        if what == "add":
            if remote_values:   # the hub's own command comes from this machine; anything else carries no value
                return 403, json.dumps({"error": web.t("web.secrets.no_values_remote"), "key": "web.secrets.no_values_remote"},
                                       ensure_ascii=False)
            return add(web, body)
        if what == "restore":   # the boxes move sealed: no value travels, so it works through Cloudflare too
            if not (isinstance(name, str) and isinstance(body.get("id"), str)
                    and vault.restore(web.state_dir, name, body["id"])):
                return 404, json.dumps({"error": web.t("web.secrets.no_version")}, ensure_ascii=False)
            web.put("secret.saved", None, {"item": name, "changed": ["value"], "restored": body["id"]})
            return 200, json.dumps({"ok": True})
        if what == "remove":
            if not (isinstance(name, str) and vault.remove(web.state_dir, name)):
                return 404, json.dumps({"error": web.t("cli.secrets.not_listed")}, ensure_ascii=False)
            web.put("secret.removed", None, {"item": name})
            return 200, json.dumps({"ok": True})
        if what == "copy":
            field = body.get("field") if body.get("field") in vault.FIELDS else "password"
            if remote_values:
                return 403, json.dumps({"error": web.t("web.secrets.no_values_remote")}, ensure_ascii=False)
            if not (isinstance(name, str) and vault.NAME.match(name)):
                return 400, json.dumps({"error": web.t("cli.secrets.not_listed")}, ensure_ascii=False)
            value = vault.value(web.state_dir, name, field)
            web.put("secret.copied", None, {"item": name, "field": field, "via": path})
            return 200, json.dumps({"ok": True, "value": value}, ensure_ascii=False)
        if what == "start-over":
            if vault.status(web.state_dir) != "lost":   # only a vault whose key is lost goes aside
                return 409, json.dumps({"error": web.t("cli.secrets.not_lost")}, ensure_ascii=False)
            vault.start_over(web.state_dir)
            return 200, json.dumps({"ok": True})
        if what == "install":
            return install(web)
    except vault.Error as e:
        if e.key == "cli.secrets.add_owner_parts":
            return 409, json.dumps(_err(web, e), ensure_ascii=False)
        return (502 if e.key in ("cli.secrets.lost", "cli.secrets.key_store", "cli.secrets.no_lib",
                                 "cli.secrets.vault_broken", "cli.secrets.key_inside") else 400), \
            json.dumps(_err(web, e), ensure_ascii=False)
    except OSError as e:   # the disk (full, rights): the reason's kind, never a value
        return 502, json.dumps({"error": web.t("cli.secrets.disk", error=type(e).__name__)}, ensure_ascii=False)
    return 404, json.dumps({"error": web.t("web.srv.no_action")}, ensure_ascii=False)


def add(web, body):
    """`hub secret add` (spec d2441132): a session puts a value it read from a file into the vault. A new secret takes
    its sites and "into commands" from the command; an existing one only a new value, login or note — its sites and
    switch stay the owner's. The previous value goes into the history; the fact names the session and the file."""
    from core import spool
    from modules.secrets import engine
    who, name, password, source = body.get("session"), body.get("name"), body.get("password"), body.get("source")
    key = body.get("key")   # the .env line or JSON field, apart from the path: a "#" in a path is a path
    if not engine.enabled(web.config()):
        return 403, json.dumps({"error": web.t("cli.secrets.off"), "key": "cli.secrets.off"}, ensure_ascii=False)
    if not (isinstance(who, str) and (who == "" or vault.NAME.match(who)) and isinstance(name, str)
            and isinstance(password, str) and isinstance(source, str) and len(source) <= 4096
            and (key is None or isinstance(key, str) and vault.KEY.match(key))):
        return 400, json.dumps({"error": web.t("web.srv.secret_add_args"), "key": "web.srv.secret_add_args"},
                               ensure_ascii=False)
    was = {}   # the version this one replaced: whose it was and when (for the session's answer), under the vault's lock
    changed = vault.save(web.state_dir, name, password=password, login=body.get("login"), sites=body.get("sites"),
                         commands=body.get("commands"), note=body.get("note"), owners_parts=True, replaced=was,
                         by={"who": who or ":terminal", "source": source, **({"key": key} if key else {})})
    out = {"ok": True, "new": "new" in changed, "changed": [c for c in changed if c != "new"], **({"was": was} if was else {})}
    if changed:
        spool.put(web.spool, "secret.saved", who or None,
                  {"item": name, "changed": changed, "by": "session" if who else "terminal", "source": vault._cut(source, vault.SOURCE_MAX),
                   **({"key": key} if key else {}), "channel": "web", "owner": False}, src="web")
    return 200, json.dumps(out, ensure_ascii=False)


def install(web):
    """"Install": cryptography into `<state>/pylib` with the hub's interpreter (a hub from git; the package carries it)."""
    import platforms
    from core import cryptolib
    cmd = cryptolib.install_command(web.state_dir, minimum=cryptolib.AEAD_MIN)
    try:
        done = platforms.current.run(cmd, 600, capture_output=True, text=True)
    except (OSError, ValueError) as e:
        return 502, json.dumps({"error": web.t("web.secrets.install_failed", error=type(e).__name__)}, ensure_ascii=False)
    if done.returncode != 0:
        tail = ((done.stderr or done.stdout or "").strip().splitlines() or [""])[-1][:200]
        return 502, json.dumps({"error": web.t("web.secrets.install_failed", error=tail)}, ensure_ascii=False)
    return 200, json.dumps({"ok": True, "state": vault.status(web.state_dir)})
