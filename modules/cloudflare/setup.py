"""`hub remote cloudflare setup|remove|rotate` — the hub sets up its own address in the owner's Cloudflare account by
their API token (path B of the remote access spec): a named tunnel, the DNS name `hub-<random>.<their domain>`, an
Access application with a policy for the owner's e-mails and, when asked, a service token for a native client with a
Service Auth policy; the team and the application's aud go into cloudflare.json, the provider turns on, and the
cloudflare part runs cloudflared with the tunnel's token (cloudflared.py).

The API token is asked for every time (setup, remove, rotate), kept in memory for that one command and never written:
the hub keeps only the tunnel's token (the right to run this tunnel) and the service token's secret, in
cloudflare-secrets.json next to hub.json (0600), bound to this hub's state folder — a copy of the hub (a lab, a stand)
does not run the tunnel. A step that fails undoes what this run created, in the same run (the token is still in
memory); what could not be undone is kept by id in cloudflare.json (`orphans`), and `remove` deletes it with the token
asked again. Only what the hub created, by the ids it wrote, is ever deleted.
"""
import argparse
import base64
import os
import re
import secrets
import sys
import urllib.parse

from core import catalog, fetch, i18n
from modules.cloudflare import api as cfapi, cloudflared, remote

SECRETS = "cloudflare-secrets.json"     # next to hub.json, 0600
ID = re.compile(r"[A-Za-z0-9-]{1,64}")
ZONE = re.compile(r"[a-z0-9][a-z0-9.-]{0,252}")
SESSION = "24h"                          # the Access session: how long a sign-in (and a stolen cookie) lasts
SERVICE_DURATION = "8760h"               # a service token for a year
# What the API token needs, by step (Cloudflare's names in the dashboard). The token is the owner's: as narrow as
# this, on the one account and the one zone.
PERMISSIONS = {
    "zones": "Zone · Zone · Read",
    "team": "Account · Access: Organizations, Identity Providers, and Groups · Read",
    "dns": "Zone · DNS · Edit",
    "access": "Account · Access: Apps and Policies · Edit",
    "service_token": "Account · Access: Service Tokens · Edit",
    "tunnel": "Account · Cloudflare Tunnel · Edit",
}
# The dashboard's token form, filled in as far as its short keys go (it drops a key it does not know, silently: the
# list above is what to check)
_KEYS = [{"key": "zone", "type": "read"}, {"key": "dns", "type": "edit"}, {"key": "access", "type": "edit"},
         {"key": "access_acct", "type": "read"}, {"key": "argotunnel", "type": "edit"}]
TOKEN_FORM = ("https://dash.cloudflare.com/profile/api-tokens?" + urllib.parse.urlencode(
    {"permissionGroupKeys": str(_KEYS).replace("'", '"').replace(" ", ""), "accountId": "*", "zoneId": "all",
     "name": "agents-hub"}))
ZERO_TRUST = "https://one.dash.cloudflare.com/"


class Stop(Exception):
    """The command stops with this text for the owner."""


class Quiet(argparse.ArgumentParser):
    def error(self, message):
        raise Stop(message)


class Terminal:
    """The owner at their terminal: questions with input(), the token without echo; without a terminal the token is one
    line of stdin and there are no questions (the answers come as options)."""

    def __init__(self):
        self.tty = sys.stdin.isatty()

    def say(self, text):
        print(text, flush=True)

    def ask(self, prompt):
        if not self.tty:
            return None
        try:
            return input(prompt + " ").strip()
        except EOFError:
            return None

    def secret(self, prompt):
        if self.tty:
            import getpass
            try:
                return getpass.getpass(prompt + " ").strip()
            except EOFError:
                return ""
        return (sys.stdin.readline() or "").strip()


def secrets_path(state_dir):
    return os.path.join(os.path.dirname(remote.settings_path(state_dir)), SECRETS)


def load_secrets(state_dir):
    """The tunnel's token and the service token's secret, when they belong to this hub (its state folder), else {}."""
    got = remote._json(secrets_path(state_dir))
    return dict(got) if got.get("state_dir") == os.path.realpath(state_dir) else {}


def copied(state_dir):
    """This hub's settings describe a tunnel set up by another hub (its state folder): a copy of the hub — the lab, a
    stand, the folders copied by hand. Such a copy neither runs that tunnel nor changes or deletes it in Cloudflare."""
    m = remote.load_settings(state_dir).get("managed")
    if not (isinstance(m, dict) and m.get("tunnel")):
        return False
    if m.get("state_dir"):
        return m["state_dir"] != os.path.realpath(state_dir)
    got = remote._json(secrets_path(state_dir))
    return bool(got) and got.get("state_dir") != os.path.realpath(state_dir)


def save_secrets(state_dir, data):
    catalog.write_private(secrets_path(state_dir), {**data, "state_dir": os.path.realpath(state_dir)})


def forget_secrets(state_dir):
    try:
        os.unlink(secrets_path(state_dir))
    except OSError:
        pass


def _id(result, *keys):
    """result[key] for the first key, checked to look like an id (it goes into the API's paths)."""
    value = result.get(keys[0]) if isinstance(result, dict) else None
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise Stop(i18n.t("cloudflare.setup.odd_answer", what=keys[0]))
    return value


def failed(step, e):
    """The owner's text for a refusal at a step: what Cloudflare said and which permission the step needs."""
    said = "; ".join(str(x.get("message") or x.get("code") or "") for x in e.errors if isinstance(x, dict)) \
        or f"HTTP {e.status}"
    if e.status in (401, 403) or "auth" in said.lower():
        return i18n.t("cloudflare.setup.refused_perm", said=said, permission=PERMISSIONS.get(step, "?"))
    return i18n.t("cloudflare.setup.refused", said=said)


# --- deleting what the hub made, by its ids -------------------------------------------------------------------------

ORDER = ("dns", "tunnel", "app", "policy", "service_token")   # an application holds its policies: it goes first


def _paths(item):
    owner, rid = item["in"], item["id"]
    if not (ID.fullmatch(str(owner)) and ID.fullmatch(str(rid))):
        return []
    return {"dns": [f"/zones/{owner}/dns_records/{rid}"],
            "tunnel": [f"/accounts/{owner}/cfd_tunnel/{rid}/connections", f"/accounts/{owner}/cfd_tunnel/{rid}"],
            "app": [f"/accounts/{owner}/access/apps/{rid}"],
            "policy": [f"/accounts/{owner}/access/policies/{rid}"],
            "service_token": [f"/accounts/{owner}/access/service_tokens/{rid}"]}.get(item["kind"], [])


def undo(api, items, say):
    """Delete the items (kinds in ORDER, within a kind in the order they were made); -> the ones that stay (a refusal
    or no answer). Already gone counts as deleted: a second remove is safe."""
    left = []
    for item in sorted(items, key=lambda x: ORDER.index(x["kind"]) if x.get("kind") in ORDER else len(ORDER)):
        paths = _paths(item)
        if not paths:
            continue
        try:
            for path in paths:
                try:
                    api.call("DELETE", path)
                except cfapi.Failed as e:
                    if not e.gone:
                        raise
            say(i18n.t("cloudflare.setup.deleted", what=item.get("name") or item["kind"], kind=item["kind"]))
        except cfapi.Failed as e:
            left.append({**item, "error": str(e)[:300]})
            say(i18n.t("cloudflare.setup.not_deleted", what=item.get("name") or item["kind"], kind=item["kind"],
                       error=str(e)[:300]))
    return left


def managed_items(m):
    """The resources of a finished setup as items for undo."""
    if not isinstance(m, dict):
        return []
    out = []
    if m.get("dns"):
        out.append({"kind": "dns", "in": m.get("zone"), "id": m["dns"], "name": m.get("host")})
    if m.get("tunnel"):
        out.append({"kind": "tunnel", "in": m.get("account"), "id": m["tunnel"], "name": m.get("name")})
    if m.get("app"):
        out.append({"kind": "app", "in": m.get("account"), "id": m["app"], "name": m.get("name")})
    for pid in m.get("policies") or []:
        out.append({"kind": "policy", "in": m.get("account"), "id": pid, "name": m.get("name")})
    if m.get("service_token"):
        out.append({"kind": "service_token", "in": m.get("account"), "id": m["service_token"], "name": m.get("name")})
    return [x for x in out if isinstance(x.get("in"), str) and isinstance(x.get("id"), str)]


# --- the commands ---------------------------------------------------------------------------------------------------

def parser():
    p = Quiet(prog="hub remote cloudflare setup", add_help=False)
    p.add_argument("--domain")
    p.add_argument("--email", action="append")
    p.add_argument("--service-token", action="store_true")
    return p


def ask_token(ui):
    ui.say(i18n.t("cloudflare.setup.token_intro", url=TOKEN_FORM,
                  permissions="\n".join(f"  · {p}" for p in PERMISSIONS.values())))
    token = ui.secret(i18n.t("cloudflare.setup.token_prompt"))
    if not token or len(token) > 512 or any(c.isspace() for c in token):
        raise Stop(i18n.t("cloudflare.setup.no_token"))
    return token


def choose_zone(zones, domain, ui):
    names = sorted({z["name"] for z in zones})
    if domain:
        domain = domain.strip().lower().rstrip(".")
        hit = [z for z in zones if z["name"] == domain]
        if not hit:
            raise Stop(i18n.t("cloudflare.setup.no_such_domain", domain=domain, domains=", ".join(names) or "—"))
        return hit[0]
    if not zones:
        raise Stop(i18n.t("cloudflare.setup.no_domains"))
    if len(zones) == 1:
        return zones[0]
    ui.say("\n".join(f"  {n}. {name}" for n, name in enumerate(names, 1)))
    got = ui.ask(i18n.t("cloudflare.setup.pick_domain", count=len(names)))
    if got is None:
        raise Stop(i18n.t("cloudflare.setup.need_domain", domains=", ".join(names)))
    pick = names[int(got) - 1] if got.isdigit() and 1 <= int(got) <= len(names) else got.lower().rstrip(".")
    hit = [z for z in zones if z["name"] == pick]
    if not hit:
        raise Stop(i18n.t("cloudflare.setup.no_such_domain", domain=pick, domains=", ".join(names)))
    return hit[0]


def _orphans(raw):
    return [x for x in raw.get("orphans") or [] if isinstance(x, dict) and x.get("kind") in ORDER]


def _orphaned(raw, items):
    """The settings `raw` with the orphans list `items` (saved by the caller, in one write with the rest)."""
    raw["orphans"] = [{k: v for k, v in x.items() if k != "error"} for x in items]
    if not items:
        raw.pop("orphans")
    return raw


def _note(state_dir, items):
    """Setup writes down what it makes before the next step: a run killed in the middle (Ctrl+C twice, the machine
    went down) leaves a record `remove` deletes by, not a stray tunnel nobody knows of."""
    remote.save_settings(state_dir, _orphaned(remote.load_settings(state_dir), items))


KEPT = ("enabled", "team", "aud", "url", "emails", "service_tokens", "metrics")   # the owner's own tunnel, set by `set`


def run_setup(state_dir, argv, ui, api_for=cfapi.Api, ensure=cloudflared.ensure, rand=secrets.choice):
    """-> exit code. Everything the owner is asked comes before the token, the token comes last and is dropped when
    the command ends."""
    a = parser().parse_args(argv)
    raw = remote.load_settings(state_dir)
    if isinstance(raw.get("managed"), dict) and raw["managed"].get("tunnel"):
        if copied(state_dir):
            raise Stop(i18n.t("cloudflare.setup.copy", name=raw["managed"].get("name") or "?"))
        raise Stop(i18n.t("cloudflare.setup.already", url=raw.get("url") or raw["managed"].get("host") or "?"))
    emails = [e.strip() for e in a.email or [] if e.strip()]
    if not emails:
        got = ui.ask(i18n.t("cloudflare.setup.email_prompt"))
        emails = [e.strip() for e in (got or "").replace(",", " ").split() if e.strip()]
    if not emails:
        raise Stop(i18n.t("cloudflare.setup.need_email"))
    bad = [e for e in emails if not remote.EMAIL.fullmatch(e)]
    if bad:
        raise Stop(i18n.t("cloudflare.cli.bad", names=", ".join(bad)))
    try:
        binary = ensure(state_dir, ui.say)
    except (fetch.Failed, cloudflared.Missing, OSError) as e:
        raise Stop(i18n.t("cloudflare.setup.binary_failed", error=str(e) or type(e).__name__)) from None
    port = remote.page_port(state_dir)
    api = api_for(ask_token(ui))
    step = "zones"
    made, before = [], _orphans(raw)
    try:
        zones = [z for z in api.pages("/zones", {"status": "active"})
                 if isinstance(z, dict) and isinstance(z.get("name"), str) and ZONE.fullmatch(z["name"].lower())
                 and isinstance(z.get("id"), str) and ID.fullmatch(z["id"])
                 and isinstance(z.get("account"), dict) and ID.fullmatch(str(z["account"].get("id") or ""))]
        for z in zones:
            z["name"] = z["name"].lower()
        zone = choose_zone(zones, a.domain, ui)
        account, zone_id = zone["account"]["id"], zone["id"]
        step = "team"
        try:
            org = api.call("GET", f"/accounts/{account}/access/organizations")
        except cfapi.Failed as e:
            if e.status != 404:
                raise
            org = None
        team = remote.team_domain((org or {}).get("auth_domain")) if isinstance(org, dict) else None
        if not team:
            raise Stop(i18n.t("cloudflare.setup.no_team", url=ZERO_TRUST))
        step = "dns"
        for _ in range(5):
            name = "hub-" + "".join(rand("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(8))
            host = f"{name}.{zone['name']}"
            if not api.call("GET", f"/zones/{zone_id}/dns_records", query={"name": host}):
                break
        else:
            raise Stop(i18n.t("cloudflare.setup.no_name"))
        ui.say(i18n.t("cloudflare.setup.making", host=host, team=team))

        def made_one(kind, owner, result):
            rid = _id(result, "id")
            made.append({"kind": kind, "in": owner, "id": rid, "name": name})
            _note(state_dir, before + made)
            return rid

        step = "access"
        policies = [made_one("policy", account, api.call("POST", f"/accounts/{account}/access/policies", {
            "name": f"{name} owner", "decision": "allow", "session_duration": SESSION,
            "include": [{"email": {"email": e}} for e in emails]}))]
        client = secret = token_id = None
        if a.service_token:
            step = "service_token"
            got = api.call("POST", f"/accounts/{account}/access/service_tokens",
                           {"name": f"{name} client", "duration": SERVICE_DURATION})
            token_id = made_one("service_token", account, got)
            client, secret = got.get("client_id"), got.get("client_secret")
            if not (isinstance(client, str) and remote.CLIENT_ID.fullmatch(client.lower()) and isinstance(secret, str)):
                raise Stop(i18n.t("cloudflare.setup.odd_answer", what="client_id"))
            step = "access"
            policies.append(made_one("policy", account, api.call("POST", f"/accounts/{account}/access/policies", {
                "name": f"{name} client", "decision": "non_identity",
                "include": [{"service_token": {"token_id": token_id}}]})))
        app = api.call("POST", f"/accounts/{account}/access/apps", {
            "name": f"Hub by Icons8 {name}", "domain": host, "type": "self_hosted", "session_duration": SESSION,
            "app_launcher_visible": False, "http_only_cookie_attribute": True,
            "policies": [{"id": pid, "precedence": n} for n, pid in enumerate(policies, 1)]})
        app_id = made_one("app", account, app)
        aud = app.get("aud")
        if not (isinstance(aud, str) and remote.AUD.fullmatch(aud)):
            raise Stop(i18n.t("cloudflare.setup.odd_answer", what="aud"))
        step = "tunnel"
        tunnel = api.call("POST", f"/accounts/{account}/cfd_tunnel", {"name": name, "config_src": "cloudflare"})
        tunnel_id = made_one("tunnel", account, tunnel)
        api.call("PUT", f"/accounts/{account}/cfd_tunnel/{tunnel_id}/configurations", {"config": {"ingress": [
            {"hostname": host, "service": f"http://127.0.0.1:{port}",
             "originRequest": {"access": {"required": True, "teamName": team.split(".")[0], "audTag": [aud]}}},
            {"service": "http_status:404"}]}})
        tunnel_token = tunnel.get("token")
        if not isinstance(tunnel_token, str) or not tunnel_token:
            tunnel_token = api.call("GET", f"/accounts/{account}/cfd_tunnel/{tunnel_id}/token")
        if not isinstance(tunnel_token, str) or not tunnel_token or len(tunnel_token) > 4096:
            raise Stop(i18n.t("cloudflare.setup.odd_answer", what="token"))
        step = "dns"
        dns_id = made_one("dns", zone_id, api.call("POST", f"/zones/{zone_id}/dns_records", {
            "type": "CNAME", "name": host, "content": f"{tunnel_id}.cfargotunnel.com", "proxied": True,
            "comment": "agents hub"}))
        save_secrets(state_dir, {"tunnel_token": tunnel_token, **({"service_secret": secret} if secret else {})})
        raw = remote.load_settings(state_dir)
        previous = {k: raw[k] for k in KEPT if k in raw} if raw.get("team") or raw.get("url") else None
        raw.update({"enabled": True, "team": team, "aud": [aud], "emails": emails, "url": f"https://{host}",
                    "service_tokens": [client.lower()] if client else [],
                    "managed": {"account": account, "zone": zone_id, "domain": zone["name"], "host": host,
                                "name": name, "tunnel": tunnel_id, "dns": dns_id, "app": app_id, "policies": policies,
                                "service_token": token_id, "port": port, "binary": binary,
                                "state_dir": os.path.realpath(state_dir),
                                **({"previous": previous} if previous else {})}})
        raw.pop("metrics", None)
        remote.save_settings(state_dir, _orphaned(raw, before))
    except BaseException as e:   # Ctrl+C too: what this run made is deleted, whatever stopped it
        text = failed(step, e) if isinstance(e, cfapi.Failed) else str(e) if isinstance(e, Stop) \
            else i18n.t("cloudflare.setup.refused", said=str(e) or type(e).__name__)
        if made or not isinstance(e, Exception):
            ui.say(text if isinstance(e, Exception) else i18n.t("cloudflare.setup.interrupted"))
        if made:
            forget_secrets(state_dir)   # saved only after the last step: a run that failed leaves no token behind
            ui.say(i18n.t("cloudflare.setup.undoing"))
            left = undo(api, made, ui.say)
            _note(state_dir, before + left)
            text = i18n.t("cloudflare.setup.orphans", count=len(left)) if left else i18n.t("cloudflare.setup.undone")
        if not isinstance(e, Exception):
            if made:
                ui.say(text)
            raise
        raise Stop(text) from None
    finally:
        del api
    remote.wake(state_dir)
    rows = [(i18n.t("cloudflare.setup.row_address"), f"https://{host}"),
            (i18n.t("cloudflare.setup.row_who"), ", ".join(emails)),
            (i18n.t("cloudflare.setup.row_session"), SESSION)]
    if client:
        rows.append((i18n.t("cloudflare.setup.row_client"), client))
        rows.append((i18n.t("cloudflare.setup.row_secret"),
                     secret if getattr(ui, "tty", False) else i18n.t("cloudflare.setup.secret_in", path=secrets_path(state_dir))))
    width = max(len(k) for k, _ in rows)
    ui.say(i18n.t("cloudflare.setup.done") + "\n" + "\n".join(f"  {k.ljust(width)}  {v}" for k, v in rows)
           + "\n" + i18n.t("cloudflare.setup.next"))
    if previous:
        ui.say(i18n.t("cloudflare.setup.kept_previous", url=previous.get("url") or previous.get("team") or "?"))
    return 0


def _drop_managed(raw):
    """The settings without what setup put there; the owner's own tunnel it replaced comes back, turned off."""
    previous = raw["managed"].get("previous") if isinstance(raw.get("managed"), dict) else None
    for key in ("managed",) + KEPT:
        if key != "emails" or isinstance(previous, dict):
            raw.pop(key, None)
    if isinstance(previous, dict):
        raw.update({k: v for k, v in previous.items() if k in KEPT})
    raw["enabled"] = False


def run_remove(state_dir, ui, api_for=cfapi.Api, wait=cloudflared.wait_stopped):
    raw = remote.load_settings(state_dir)
    if copied(state_dir):   # the other hub's tunnel: forget it here, touch nothing in Cloudflare
        if raw.get("managed"):
            _drop_managed(raw)
            raw.pop("orphans", None)
            remote.save_settings(state_dir, raw)
        ui.say(i18n.t("cloudflare.setup.forgot_copy"))
        return 0
    items = managed_items(raw.get("managed")) + _orphans(raw)
    if not items:
        forget_secrets(state_dir)
        ui.say(i18n.t("cloudflare.setup.nothing"))
        return 0
    api = api_for(ask_token(ui))
    try:
        if raw.get("managed"):
            raw["enabled"] = False   # the part stops cloudflared within seconds: a tunnel with a connection is not deleted
            remote.save_settings(state_dir, raw)
            remote.wake(state_dir)
            if os.path.exists(os.path.join(state_dir, cloudflared.PIDFILE)):
                ui.say(i18n.t("cloudflare.setup.stopping"))
                wait(state_dir)
        left = undo(api, items, ui.say)
    finally:
        del api
    if len(left) == len(items):   # nothing went (a wrong token, no network): everything stays as it was, off
        ui.say(i18n.t("cloudflare.setup.orphans", count=len(left)))
        return 1
    raw = remote.load_settings(state_dir)
    if raw.get("managed"):
        _drop_managed(raw)
        forget_secrets(state_dir)
    remote.save_settings(state_dir, _orphaned(raw, left))
    ui.say(i18n.t("cloudflare.setup.removed") if not left else i18n.t("cloudflare.setup.orphans", count=len(left)))
    return 0 if not left else 1


def run_rotate(state_dir, argv, ui, api_for=cfapi.Api):
    if [x for x in argv if x != "--service-token"]:
        raise Stop(i18n.t("cloudflare.cli.usage"))
    raw = remote.load_settings(state_dir)
    m = raw.get("managed") if isinstance(raw.get("managed"), dict) else {}
    account, tunnel = m.get("account"), m.get("tunnel")
    if not (isinstance(account, str) and ID.fullmatch(account) and isinstance(tunnel, str) and ID.fullmatch(tunnel)):
        raise Stop(i18n.t("cloudflare.setup.not_managed"))
    if copied(state_dir):
        raise Stop(i18n.t("cloudflare.setup.copy", name=m.get("name") or "?"))
    service = "--service-token" in argv
    if service and not (isinstance(m.get("service_token"), str) and ID.fullmatch(m["service_token"])):
        raise Stop(i18n.t("cloudflare.setup.no_service_token"))
    api = api_for(ask_token(ui))
    step, patched = "tunnel", False
    try:
        api.call("PATCH", f"/accounts/{account}/cfd_tunnel/{tunnel}",
                 {"tunnel_secret": base64.b64encode(os.urandom(32)).decode()})
        patched = True   # from here the kept token no longer works until the new one is saved
        token = api.call("GET", f"/accounts/{account}/cfd_tunnel/{tunnel}/token")
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise Stop(i18n.t("cloudflare.setup.odd_answer", what="token"))
        kept = load_secrets(state_dir)
        kept["tunnel_token"] = token
        save_secrets(state_dir, kept)
        patched, secret = False, None
        remote.wake(state_dir)
        ui.say(i18n.t("cloudflare.setup.rotated"))
        if service:
            step = "service_token"
            got = api.call("POST", f"/accounts/{account}/access/service_tokens/{m['service_token']}/rotate")
            secret = got.get("client_secret") if isinstance(got, dict) else None
            if not isinstance(secret, str) or not secret:
                raise Stop(i18n.t("cloudflare.setup.odd_answer", what="client_secret"))
            save_secrets(state_dir, {**kept, "service_secret": secret})
    except (cfapi.Failed, Stop, OSError) as e:
        text = failed(step, e) if isinstance(e, cfapi.Failed) else str(e)
        if patched:
            text = i18n.t("cloudflare.setup.rotate_half", why=text)
        elif step == "service_token":
            text = i18n.t("cloudflare.setup.rotate_service_half", why=text)
        raise Stop(text) from None
    finally:
        del api
    if secret:
        ui.say(i18n.t("cloudflare.setup.row_secret") + ": " + (
            secret if getattr(ui, "tty", False) else i18n.t("cloudflare.setup.secret_in", path=secrets_path(state_dir))))
    return 0


def main(sub, argv, state_dir, ui=None):
    ui = ui or Terminal()
    try:
        if sub == "setup":
            return run_setup(state_dir, argv, ui)
        if sub == "remove":
            if argv:
                raise Stop(i18n.t("cloudflare.cli.usage"))
            return run_remove(state_dir, ui)
        return run_rotate(state_dir, argv, ui)
    except Stop as e:
        ui.say(str(e))
        return 1
