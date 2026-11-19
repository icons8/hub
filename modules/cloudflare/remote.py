"""The cloudflare provider of remote access (core/remote.py): the hub page through the owner's own Cloudflare Tunnel,
behind Cloudflare Access. Off by default.

cloudflared — the owner's own connector from their Cloudflare account, as Tailscale is for the tailnet — reaches
127.0.0.1 with the public Host (hub.example.test; a tunnel that rewrites it to localhost works too). Who came is taken
only from the JWT Cloudflare Access puts into `Cf-Access-Jwt-Assertion`: RS256 under one of the team's keys, issued by
the team domain, for one of the Application Audience tags, not expired, and either an `email` from the owner's list or,
for a service token (a native client: no e-mail), its Client ID (`common_name`) from the list of service tokens.
Unsigned headers (Cf-Access-Authenticated-User-Email) are never read. The token counts only on a connection from this
machine: the same JWT from a tailnet or LAN address is nobody (a stolen one must not open the hub there). A request
that carries Cloudflare's headers is claimed whatever its Host says: a tunnel that rewrites Host to localhost is not
the person at the keyboard.

Two files:
- settings — `cloudflare.json` in the settings folder (next to hub.json, 0600), written by `hub remote cloudflare
  set|on|off` (modules/cloudflare/cli.py): enabled, team, aud, emails, service_tokens, url, metrics; by `setup` also
  `managed` (the ids of what it made in Cloudflare, the page port, cloudflared's path) and `orphans` (what an undo
  could not delete; the tokens themselves are in cloudflare-secrets.json, modules/cloudflare/setup.py). Read on every
  request (re-read only when the file changed): turning it off or removing a person works at once.
- status — `cloudflare-status.json` in the state folder (0600), written by the provider's process (hub-up part
  `cloudflare`, modules/cloudflare/run.py): the team's keys and when they were fetched, the tunnel's state. The page
  and Files never call out: keys older than KEYS_MAX_AGE are not used.
"""
import json
import os
import re
import threading
import time
from urllib.parse import urlsplit

from core import catalog, jwt, remote

SETTINGS = "cloudflare.json"            # in the settings folder
STATUS = "cloudflare-status.json"       # in the state folder
REFRESH = "cloudflare.refresh"          # left by a server that met an unknown key id, or by `on`: the process wakes up
SKEW = 60                               # seconds of clock difference with Cloudflare
MAX_TOKEN = 16 * 1024
EVERY = 60                              # the process writes the status this often
KEYS_EVERY = 3600                       # keys are fetched hourly
KEYS_MAX_AGE = 7 * 24 * 3600            # Access keeps the previous key valid a week after a rotation: older keys are not used
REFETCH_AFTER = 60                      # an asked-for or failed fetch is not repeated sooner
HEADERS = ("cf-ray", "cf-connecting-ip", "cf-access-jwt-assertion")
TEAM = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\.cloudflareaccess\.com")
AUD = re.compile(r"[A-Za-z0-9_-]{16,256}")
CLIENT_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,199}")
EMAIL = re.compile(r"[^@\s]+@[^@\s]+")
METRICS = re.compile(r"(127\.0\.0\.1|localhost|\[::1\]):\d{1,5}")   # cloudflared's metrics on this machine only
SERVICE = "service:"                    # the identity of a service token: "service:<client id>"

_lock = threading.Lock()
_read = {}   # path -> (stat key, parsed)


def settings_path(state_dir):
    """cloudflare.json next to hub.json (a second hub has its own settings folder)."""
    return os.path.join(os.path.dirname(catalog.config_path(state_dir)), SETTINGS)


def _json(path):
    """The file's object, re-parsed only when it changed; {} when missing or broken."""
    try:
        f = os.stat(path)
        key = (f.st_ino, f.st_mtime_ns, f.st_size)
    except OSError:
        return {}
    with _lock:
        hit = _read.get(path)
        if hit and hit[0] == key:
            return hit[1]
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    with _lock:
        if len(_read) > 16:
            _read.clear()
        _read[path] = (key, data)
    return data


def load_settings(state_dir):
    """The settings as written (a copy: callers edit it)."""
    return dict(_json(settings_path(state_dir)))


def save_settings(state_dir, data):
    path = settings_path(state_dir)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    catalog.write_private(path, data)


def _list(value):
    items = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    return [x.strip() for x in items if isinstance(x, str) and x.strip()]


def team_domain(value):
    """`myteam` or `myteam.cloudflareaccess.com` -> the team domain, or None."""
    team = str(value or "").strip().lower() if isinstance(value, str) else ""
    team = team if "." in team else f"{team}.cloudflareaccess.com" if team else ""
    return team if TEAM.fullmatch(team) else None


def https_url(value):
    """The page's public address: https, a host, no path, query or user; -> `https://host[:port]` or None."""
    try:
        u = urlsplit(value) if isinstance(value, str) else None
        port = u.port if u else None
    except ValueError:
        return None
    if not u or u.scheme != "https" or not u.hostname or u.path not in ("", "/") or u.query or u.fragment \
            or u.username or u.password:
        return None
    return f"https://{u.hostname.lower()}" + (f":{port}" if port else "")


def normalized(raw):
    """(settings, the names that are missing or wrong). settings: enabled, team, aud, emails, service_tokens, url,
    names (the public host names), metrics."""
    raw = raw if isinstance(raw, dict) else {}
    url = https_url(raw.get("url"))
    metrics = raw.get("metrics") if isinstance(raw.get("metrics"), str) and METRICS.fullmatch(raw["metrics"]) else None
    s = {"enabled": raw.get("enabled") is True, "team": team_domain(raw.get("team")),
         "aud": [a for a in _list(raw.get("aud")) if AUD.fullmatch(a)],
         "emails": [e.lower() for e in _list(raw.get("emails")) if EMAIL.fullmatch(e)],
         "service_tokens": [c.lower() for c in _list(raw.get("service_tokens")) if CLIENT_ID.fullmatch(c.lower())],
         "url": url, "names": [urlsplit(url).hostname] if url else [], "metrics": metrics,
         "managed": raw["managed"] if isinstance(raw.get("managed"), dict) and raw["managed"].get("tunnel") else None}
    missing = [n for n in ("team", "aud", "url") if not s[n]]
    if not (s["emails"] or s["service_tokens"]):
        missing.append("emails")
    if raw.get("metrics") and not metrics:
        missing.append("metrics")
    return s, missing


def process_status(state_dir):
    return dict(_json(os.path.join(state_dir, STATUS)))


def page_port(state_dir=None):
    """This hub's page port: as the cloudflare part (a child of hub-up, with the hub's HUB_WEB_PORT) wrote it, else
    HUB_WEB_PORT of this process, else the anchor of an instance (core/instance.py), else 8787."""
    got = process_status(state_dir).get("page_port") if state_dir else None
    if isinstance(got, int) and not isinstance(got, bool) and 0 < got < 65536:
        return got
    from core import instance
    return instance.port("web", state_dir)


def current(state_dir, req=None):
    """(settings, the process's status) as a request is judged against them; with `req`, read once for it."""
    if req is None:
        return normalized(load_settings(state_dir))[0], process_status(state_dir)
    if "cloudflare" not in req["memo"]:
        req["memo"]["cloudflare"] = current(state_dir)
    return req["memo"]["cloudflare"]


def wake(state_dir):
    """Ask the process for fresh keys now (an unknown key id, the provider just turned on)."""
    try:
        with open(os.path.join(state_dir, REFRESH), "a"):
            pass
    except OSError:
        pass


def _number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def usable(s, st, now):
    """The keys may be used: on, complete, the keys of this team and not older than KEYS_MAX_AGE."""
    at = st.get("keys_at")
    return bool(s["enabled"] and s["team"] and s["aud"] and s["url"] and (s["emails"] or s["service_tokens"])
                and st.get("team") == s["team"] and isinstance(st.get("keys"), dict) and st["keys"]
                and _number(at) and now - at <= KEYS_MAX_AGE)


def why_text(st):
    """The reason in the process's status (a catalog key and its values) as text in this process's language."""
    from core import i18n
    why = st.get("why")
    if isinstance(why, dict) and str(why.get("key") or "").startswith("cloudflare."):
        return i18n.t(why["key"], **{k: str(v) for k, v in why.items() if k != "key"})
    return why if isinstance(why, str) else ""


def status(state_dir, now=None, req=None):
    """The provider's status for core/remote.py and hub doctor: ok, why, hosts, keys, keys_at, tunnel."""
    s, st = current(state_dir, req)
    ok = usable(s, st, time.time() if now is None else now)
    return {**st, "ok": ok, "hosts": s["names"], "tunnel": st.get("tunnel") or "unknown", "why": None if ok else st.get("why")}


def claims(state_dir, req):
    h = req["headers"]
    if any(k in h for k in HEADERS) or "cloudflare" in h.get("cdn-loop", "").lower():
        return True
    return remote.hostname(req["host"]).lower() in current(state_dir, req)[0]["names"]


def identify(state_dir, req):
    """The e-mail of a listed person or `service:<client id>` of a listed service token, else None."""
    if req["peer"] not in remote.LOCAL_PEERS:
        return None   # cloudflared lives on this machine: the same token by a tailnet or LAN address is nobody
    s, st = current(state_dir, req)
    name = remote.hostname(req["host"]).lower()
    if name not in s["names"] and name not in remote.LOCAL_NAMES:
        return None   # a Host neither ours nor localhost (a tunnel that rewrites it to localhost is still ours)
    who, why = verify(req["headers"].get("cf-access-jwt-assertion"), s, st, time.time())
    if why == "unknown signing key":
        wake(state_dir)
    return who


def verify(token, s, st, now):
    """(who, None) for a token of this team's Access application naming a listed person or service token, else
    (None, why not)."""
    if not usable(s, st, now):
        return None, "not set up"
    if not token:
        return None, "no token"
    parts = jwt.parse(token) if len(token) <= MAX_TOKEN else None
    if not parts:
        return None, "not a JWT"
    head, body, signed, sig = parts
    if head.get("alg") != "RS256":
        return None, "not RS256"
    kid = head.get("kid")
    key = st["keys"].get(kid) if isinstance(kid, str) else None
    if not isinstance(key, dict):
        return None, "unknown signing key"
    numbers = jwt.key_numbers(key)
    if not numbers or not jwt.rsa_ok(*numbers, signed, sig):
        return None, "bad signature"
    if body.get("iss") != f"https://{s['team']}":
        return None, "wrong issuer"
    aud = body.get("aud")
    aud = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
    if not {a for a in aud if isinstance(a, str)} & set(s["aud"]):
        return None, "not for this application"
    exp, nbf = body.get("exp"), body.get("nbf")
    if not _number(exp) or exp < now - SKEW:
        return None, "expired"
    if _number(nbf) and nbf > now + SKEW:
        return None, "not valid yet"
    if "email" in body:   # a person
        email = body["email"]
        if not isinstance(email, str) or not EMAIL.fullmatch(email.strip()):
            return None, "no identity"
        email = email.strip().lower()
        return (email, None) if email in s["emails"] else (None, "not listed")
    client = body.get("common_name")   # a service token: its Client ID
    if not isinstance(client, str) or not client.strip():
        return None, "no identity"
    client = client.strip().lower()
    return (SERVICE + client, None) if client in s["service_tokens"] else (None, "not listed")
