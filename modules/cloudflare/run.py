"""The cloudflare provider's process (hub-up part `cloudflare`): keeps the Cloudflare Access keys of the owner's team and
the state of their tunnel in cloudflare-status.json for the page and Files (remote.py), which never call out.

Two ways to the same settings. The owner's own tunnel (`hub remote cloudflare set`): the hub neither starts nor
configures their cloudflared, as the tailnet module never touches Tailscale's settings. The tunnel the hub made
(`hub remote cloudflare setup`, setup.py): this process runs cloudflared as its child with the tunnel's token
(cloudflared.py) while the provider is on, and stops it when it turns off or the process ends. While the provider is
off (`hub remote cloudflare on` not given) the process only says so: nothing goes to the network.
"""
import http.client
import json
import os
import signal
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
import core.net  # noqa: E402,F401 — the hub reaches itself without the machine's proxy (tests/test_net.py)
from core import catalog, lock  # noqa: E402
from core.daemon import default_state_dir  # noqa: E402
from modules.cloudflare import cloudflared, remote, setup  # noqa: E402

MAX_KEYS = 1 << 20   # bytes of the keys document read at most


def fetch(url):
    """The team's keys document (JSON) through the machine's proxy."""
    req = urllib.request.Request(url, headers={"User-Agent": "agents-hub-cloudflare/1", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        if r.status != 200:
            raise OSError(f"{url}: HTTP {r.status}")
        return json.loads(r.read(MAX_KEYS))


def ready(addr):
    """Is the cloudflared whose metrics listen on `addr` (this machine) connected to Cloudflare? /ready answers 200."""
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(f"http://{addr}/ready", timeout=2) as r:
            return r.status == 200
    except (OSError, ValueError, http.client.HTTPException):
        return False


class Cloudflare:
    def __init__(self, state_dir, fetch=fetch, ready=ready, clock=time.time, connector=None):
        self.state_dir, self.fetch, self.ready, self.clock = state_dir, fetch, ready, clock
        self.connector = connector
        self.last = remote.process_status(state_dir)
        self.tried = float("-inf")
        self.held = None   # why the hub's tunnel is not run (a copy of the hub, no binary), None while it runs or is not ours

    def keep(self):
        """The tunnel the hub made runs while the provider is on: cloudflared with this hub's tunnel token; anything
        else stops it. Called every few seconds (an exit is noticed and the child started again with a backoff)."""
        if self.connector is None:
            return
        s, missing = remote.normalized(remote.load_settings(self.state_dir))
        m = s["managed"]
        token = setup.load_secrets(self.state_dir).get("tunnel_token") if m else None
        binary = m.get("binary") if m else None
        self.held = None
        if m and s["enabled"] and not missing:
            if not isinstance(token, str) or not token:
                self.held = "cloudflare.why_copy" if setup.copied(self.state_dir) else "cloudflare.why_no_token"
            elif not isinstance(binary, str) or not os.path.isfile(binary):
                self.held = "cloudflare.why_no_binary"
            else:
                self.connector.want(binary, token)
                return
        self.connector.stop()

    def tick(self):
        now = self.clock()
        s, missing = remote.normalized(remote.load_settings(self.state_dir))
        if not s["enabled"] or missing:
            self.unask()   # nothing to fetch for: a request left behind would wake the loop every 5 s
        port = remote.page_port()
        if not s["enabled"]:
            return self.save({"ok": False, "why": {"key": "cloudflare.why_off"}, "page_port": port})
        if missing:
            return self.save({"ok": False, "why": {"key": "cloudflare.why_settings", "missing": ", ".join(missing)},
                              "page_port": port})
        same = self.last.get("team") == s["team"]
        keys, at = (self.last.get("keys") or {}, self.last.get("keys_at") or 0) if same else ({}, 0)
        if not same:
            self.tried = float("-inf")
        asked = os.path.exists(os.path.join(self.state_dir, remote.REFRESH))
        error = None
        if (now - at >= remote.KEYS_EVERY or not keys or asked) and now - self.tried >= remote.REFETCH_AFTER:
            self.tried = now
            if asked:
                self.unask()
            try:
                got = self.fetch(f"https://{s['team']}/cdn-cgi/access/certs")
                fresh = {k["kid"]: k for k in got.get("keys") or []
                         if isinstance(k, dict) and k.get("kty") == "RSA" and isinstance(k.get("kid"), str)}
                if not fresh:
                    raise ValueError("no RSA keys in the answer")
                keys, at = fresh, now
            except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException) as e:
                error = str(e) or type(e).__name__
        ok = bool(keys) and now - at <= remote.KEYS_MAX_AGE
        why = None if ok else {"key": "cloudflare.why_stale" if keys else "cloudflare.why_keys", "error": error or "…"}
        if s["managed"] and self.connector is not None:   # the hub's own cloudflared: its own metrics address
            addr = self.connector.metrics if self.connector.running() else None
            tunnel = "up" if addr and self.ready(addr) else "down"
        else:
            tunnel = ("up" if self.ready(s["metrics"]) else "down") if s["metrics"] else "unknown"
        held = {"key": self.held} if self.held else None
        moved = s["managed"].get("port") not in (None, port) if s["managed"] else False   # HUB_WEB_PORT changed since
        return self.save({"ok": ok, "why": why, "team": s["team"], "keys": keys, "keys_at": at, "error": error,
                          "tunnel": tunnel, "held": held, "page_port": port,
                          **({"moved": {"key": "cloudflare.why_port", "was": s["managed"].get("port"), "now": port}}
                             if moved else {})})

    def asked(self):
        """A request for keys is waiting and may be served now: not sooner than REFETCH_AFTER after the last fetch for
        the same team (a stream of tokens with a made-up key id must not spin the loop), at once for a new team."""
        if not os.path.exists(os.path.join(self.state_dir, remote.REFRESH)):
            return False
        team = remote.normalized(remote.load_settings(self.state_dir))[0]["team"]
        return team != self.last.get("team") or self.clock() - self.tried >= remote.REFETCH_AFTER

    def unask(self):
        try:
            os.unlink(os.path.join(self.state_dir, remote.REFRESH))
        except OSError:
            pass

    def save(self, data):
        data = {**data, "ts": self.clock()}
        self.last = data
        catalog.write_private(os.path.join(self.state_dir, remote.STATUS), data)
        return data


def main():
    state_dir = default_state_dir()
    if not lock.hold(os.path.join(state_dir, "cloudflare.lock")):
        sys.exit("cloudflare: already running (one process per hub)")
    cf = Cloudflare(state_dir, connector=cloudflared.Connector(state_dir))

    def leave(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, leave)   # hub-up stops the part: the tunnel goes with it
    try:
        loop(cf)
    finally:
        cf.connector.stop()


def loop(cf):
    last, said = 0, None
    while True:
        try:
            cf.keep()
        except Exception as e:  # noqa: BLE001 — as a tick: one failure must not stop the part
            print(f"cloudflare: cloudflared: {e!r}", file=sys.stderr, flush=True)
        if time.time() - last >= remote.EVERY or cf.asked():
            last = time.time()
            try:
                st = cf.tick()
            except Exception as e:  # noqa: BLE001 — one bad answer must not stop the part (hub-up would restart it into the same)
                cf.unask()
                st = {"why": {"key": "tick failed", "error": repr(e)}}
            # no texts from the catalogs here: the first i18n.t of a process reads the whole state for the language,
            # and this process runs on every hub, the provider on or off (hub doctor and the command translate)
            why = st.get("why") or ({"key": "tunnel down"} if st.get("tunnel") == "down" else {"key": "ready"})
            if why != said:
                print("cloudflare: " + " ".join(str(v) for v in why.values()), file=sys.stderr, flush=True)
                said = why
        time.sleep(5)


if __name__ == "__main__":
    main()
