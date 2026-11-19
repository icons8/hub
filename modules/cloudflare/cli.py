"""`hub remote cloudflare …` and the hub doctor rows of the cloudflare provider (modules/cloudflare/remote.py).

  hub remote cloudflare [status]   settings, then keys, tunnel and reach apart
  hub remote cloudflare set [--team T] [--aud A …] [--url https://…] [--email E …] [--token-id ID …]
                            [--no-token-ids] [--metrics 127.0.0.1:N]   a named field is replaced whole, the rest stay
  hub remote cloudflare on|off
  hub remote cloudflare setup [--domain D] [--email E …] [--service-token]   the hub makes the tunnel, DNS name and
                            Access application in the owner's account by their API token (setup.py)
  hub remote cloudflare remove | rotate [--service-token]   delete what setup made / a new tunnel token (the API token
                            is asked again)

The settings live in cloudflare.json next to hub.json (0600). `set` never turns the provider on; `on` refuses
incomplete settings and wakes the process at once (it fetches the team's keys within seconds).
"""
import argparse
import http.client
import os
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import core.net  # noqa: F401 — the hub reaches itself without the machine's proxy (tests/test_net.py)
from core import i18n
from modules.cloudflare import remote

STALE = 3 * remote.EVERY   # a status older than this: the process is not running


class Quiet(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parser():
    p = Quiet(prog="hub remote cloudflare set", add_help=False)
    p.add_argument("--team")
    p.add_argument("--aud", action="append")
    p.add_argument("--url")
    p.add_argument("--email", action="append")
    p.add_argument("--token-id", action="append", dest="tokens")
    p.add_argument("--no-token-ids", action="store_true")
    p.add_argument("--metrics")
    return p


def set_fields(state_dir, argv):
    """-> (code, text). A value that does not pass the same check a request is judged by is refused, nothing written."""
    try:
        a = parser().parse_args(argv)
    except ValueError as e:
        return 2, str(e)
    bad = []
    if a.team is not None and not remote.team_domain(a.team):
        bad.append("--team")
    if a.aud is not None and not all(remote.AUD.fullmatch(x.strip()) for x in a.aud):
        bad.append("--aud")
    if a.url is not None and not remote.https_url(a.url):
        bad.append("--url")
    if a.email is not None and not all(remote.EMAIL.fullmatch(x.strip()) for x in a.email):
        bad.append("--email")
    if a.tokens is not None and not all(remote.CLIENT_ID.fullmatch(x.strip().lower()) for x in a.tokens):
        bad.append("--token-id")
    if a.metrics is not None and a.metrics and not remote.METRICS.fullmatch(a.metrics):
        bad.append("--metrics")
    if bad:
        return 2, i18n.t("cloudflare.cli.bad", names=", ".join(bad))
    raw = remote.load_settings(state_dir)
    for key, value in (("team", a.team), ("aud", a.aud), ("url", a.url), ("emails", a.email), ("service_tokens", a.tokens),
                       ("metrics", a.metrics)):
        if value is not None:
            raw[key] = [x.strip() for x in value] if isinstance(value, list) else value.strip()
    if a.no_token_ids:
        raw["service_tokens"] = []
    if not raw.get("metrics"):
        raw.pop("metrics", None)
    remote.save_settings(state_dir, raw)
    return 0, i18n.t("cloudflare.cli.saved", path=remote.settings_path(state_dir))


def switch(state_dir, on):
    raw = remote.load_settings(state_dir)
    if on:
        missing = remote.normalized({**raw, "enabled": True})[1]
        if missing:
            return 1, i18n.t("cloudflare.cli.incomplete", missing=", ".join(missing))
    raw["enabled"] = on
    remote.save_settings(state_dir, raw)
    if on:
        remote.wake(state_dir)
    return 0, i18n.t("cloudflare.cli.on" if on else "cloudflare.cli.off")


def judge(code, headers, body):
    """(level, detail) of the public address's answer to a request without credentials."""
    where = urlsplit(headers.get("Location") or "").hostname or ""
    if 300 <= code < 400 and where.endswith(".cloudflareaccess.com"):
        return "ok", i18n.t("cloudflare.reach_access")
    if code in (401, 403) and not body.lstrip().startswith(b'{"error"'):
        return "ok", i18n.t("cloudflare.reach_access")
    if code == 403:
        return "warn", i18n.t("cloudflare.reach_hub")   # the hub refused it itself: Access is not in front
    if code == 200:
        return "fail", i18n.t("cloudflare.reach_open")
    return "warn", i18n.t("cloudflare.reach_other", code=code)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def reach(url):
    """Ask the public address as a stranger would (no cookie, no token) -> (level, detail)."""
    req = urllib.request.Request(url.rstrip("/") + "/api/snapshot", headers={"User-Agent": "agents-hub-doctor/1"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=10) as r:
            return judge(r.status, r.headers, r.read(4096))
    except urllib.error.HTTPError as e:
        return judge(e.code, e.headers, e.read(4096) if e.fp else b"")
    except (OSError, ValueError, http.client.HTTPException) as e:
        return "warn", i18n.t("cloudflare.reach_failed", error=str(e) or type(e).__name__)


def doctor_rows(state_dir, reach=None, now=None):
    """[(ok|warn|fail, what, details)]: nothing while it was never set up, one row while off, else keys, tunnel and
    reach apart: only the keys decide who is let in, the other two say whether the address works."""
    path = remote.settings_path(state_dir)
    if not os.path.exists(path):
        return []
    s, missing = remote.normalized(remote.load_settings(state_dir))
    if not s["enabled"]:
        return [("ok", "cloudflare", i18n.t("cloudflare.why_off"))]
    if missing:
        return [("fail", "cloudflare", i18n.t("cloudflare.why_settings", missing=", ".join(missing)))]
    now = time.time() if now is None else now
    st = remote.status(state_dir, now=now)
    ts = st.get("ts")
    if not isinstance(ts, (int, float)) or now - ts > STALE:
        keys = ("fail", i18n.t("cloudflare.process_down"))
    elif st["ok"]:
        keys = ("ok", i18n.t("cloudflare.keys_ok", count=len(st.get("keys") or {}),
                             age=int((now - st["keys_at"]) // 60)))
    else:
        keys = ("fail", remote.why_text(st) or i18n.t("cloudflare.why_keys", error="…"))
    tunnel = {"up": ("ok", i18n.t("cloudflare.tunnel_up")), "down": ("fail", i18n.t("cloudflare.tunnel_down"))}.get(
        st.get("tunnel"), ("warn", i18n.t("cloudflare.tunnel_unknown")))
    for extra in ("held", "moved"):   # the hub's own cloudflared is not run / the page moved to another port since setup
        if isinstance(st.get(extra), dict):
            tunnel = ("fail", remote.why_text({"why": st[extra]}))
    got = (reach or globals()["reach"])(s["url"])
    return [(keys[0], "cloudflare · keys", keys[1]), (tunnel[0], "cloudflare · tunnel", tunnel[1]),
            (got[0], "cloudflare · reach", got[1])]


def describe(state_dir):
    s, missing = remote.normalized(remote.load_settings(state_dir))
    lines = [i18n.t("cloudflare.cli.state_on" if s["enabled"] else "cloudflare.cli.state_off"),
             f"  team: {s['team'] or '—'}", f"  aud: {', '.join(s['aud']) or '—'}", f"  url: {s['url'] or '—'}",
             f"  emails: {', '.join(s['emails']) or '—'}", f"  service tokens: {', '.join(s['service_tokens']) or '—'}"]
    if s["metrics"]:
        lines.append(f"  metrics: {s['metrics']}")
    if s["managed"]:
        m = s["managed"]
        lines.append(i18n.t("cloudflare.cli.managed", name=m.get("name") or "?", domain=m.get("domain") or "?"))
        from modules.cloudflare import setup
        if setup.load_secrets(state_dir).get("service_secret"):
            lines.append(i18n.t("cloudflare.setup.secret_in", path=setup.secrets_path(state_dir)))
    orphans = remote.load_settings(state_dir).get("orphans")
    if isinstance(orphans, list) and orphans:
        lines.append(i18n.t("cloudflare.setup.orphans", count=len(orphans)))
    if missing:
        lines.append(i18n.t("cloudflare.why_settings", missing=", ".join(missing)))
    for level, what, detail in doctor_rows(state_dir) if s["enabled"] and not missing else ():
        lines.append(f"{level:<5} {what.split(' · ')[-1]:<7} {detail}")
    return "\n".join(lines)


def main(argv, state_dir):
    if not argv or argv == ["status"]:
        print(describe(state_dir))
        return 0
    if argv[0] == "set":
        code, text = set_fields(state_dir, argv[1:])
    elif argv in (["on"], ["off"]):
        code, text = switch(state_dir, argv == ["on"])
    elif argv[0] in ("setup", "remove", "rotate"):
        from modules.cloudflare import setup
        return setup.main(argv[0], argv[1:], state_dir)
    else:
        code, text = 2, i18n.t("cloudflare.cli.usage")
    print(text)
    return code
