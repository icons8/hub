"""The tailnet module: hub pages on the owner's devices through Tailscale.

Once a minute it looks at `tailscale status` and writes tailnet.json: the Mac's tailnet IP (the hub page :8787 and "Files"
:8788 listen on it too, see modules/tailnet/access.py), addresses for links (urls, by IP: a phone with private DNS does not find the *.ts.net
name; urls_dns is the same by the name, for a client that keeps the name and survives the IP changing, see access.addresses), which Hosts to let in (hosts), who the owner is (this Mac's Tailscale user) and the path to the CLI for whois.
It removes a `tailscale serve` setup that leads only to disabled services (:8790, :8792). Settings are `tailnet`
in hub.json: enabled (off until it is `true`: the page opens to other devices only when the owner asked for it), cli,
expose ({name: port}), owners (logins to let in). Earlier releases turned it on by themselves; a hub where it worked
then keeps it: the first tick of this code writes `enabled: true` there (Tailnet.keep_on), once (CHOSEN).

Owners are this node's Tailscale user or the logins from `tailnet.owners`. A tagged node is nobody's (Tailscale gives it
the shared user `tagged-devices`): the page does not open from the tailnet on it until `tailnet.owners` names someone.
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
from core import catalog, i18n, lock  # noqa: E402
from core.daemon import default_state_dir  # noqa: E402
from modules.tailnet import access  # noqa: E402
import platforms  # noqa: E402

EXPOSE = {"web": 8787, "files": 8788}
STALE_PORTS = ("8790", "8792")  # disabled services: the reports gateway and fileview
EVERY = 60
TIMEOUT = 30   # one call of the tailscale CLI
CALLS = 3   # the calls of one look at most: status, serve status, serve reset
PAUSE = 5   # the loop's step: boards, and a hub.json just changed
STALE_AFTER = 300  # how long to keep the previous working record when tailscale did not answer within the timeout
CHOSEN = "tailnet.chosen"   # in the state folder: this code has decided whether an earlier release's tailnet stays on


def cli_env():
    """The CLI from Tailscale.app without SHLVL decides it was opened as an app and tries to start the GUI ("The Tailscale GUI
    failed to start"), as under launchd (hub-up), a live case of 30.09. From a shell SHLVL is always there."""
    return {**os.environ, "SHLVL": os.environ.get("SHLVL") or "1"}


def owners_of(cfg, st):
    """Tailscale logins let in from the tailnet: `tailnet.owners` in hub.json, otherwise this node's user. A tagged
    node is nobody's (its user `tagged-devices` is shared by all tagged nodes), so then only the
    setting names the owners."""
    listed = cfg.get("owners")
    if listed:
        return [str(x) for x in ([listed] if isinstance(listed, str) else listed) if x and x != access.TAGGED]
    me = st["Self"]
    login = (st.get("User") or {}).get(str(me.get("UserID")), {}).get("LoginName")
    return [] if me.get("Tags") or not login or login == access.TAGGED else [login]


def _stale_target(handler):
    if not isinstance(handler, dict) or set(handler) != {"Proxy"}:
        return False
    try:
        u = urllib.parse.urlsplit(str(handler["Proxy"]))
        port = u.port
    except ValueError:
        return False
    return (u.scheme in ("http", "https") and u.hostname in ("127.0.0.1", "localhost", "::1", "0.0.0.0")
            and str(port) in STALE_PORTS and u.path in ("", "/") and not u.query)


def stale_only(serve):
    """The `tailscale serve` setup is nothing but web proxies to the disabled services (:8790, :8792) on this machine."""
    if not isinstance(serve, dict) or set(k for k, v in serve.items() if v) - {"TCP", "Web"}:
        return False
    tcp, web = serve.get("TCP") or {}, serve.get("Web") or {}
    if not isinstance(tcp, dict) or not isinstance(web, dict) or not web:
        return False
    if any(not isinstance(t, dict) or set(k for k, v in t.items() if v) - {"HTTPS", "HTTP"} for t in tcp.values()):
        return False
    if any(not isinstance(site, dict) or not isinstance(site.get("Handlers"), dict) for site in web.values()):
        return False
    handlers = [h for site in web.values() for h in site["Handlers"].values()]
    return bool(handlers) and all(_stale_target(h) for h in handlers)


class Chosen(Exception):
    """keep_on found the choice already in hub.json: the edit is dropped, the file is not rewritten."""


class Tailnet:
    def __init__(self, state_dir, config=None, cli=None, settings=None):
        self.state_dir = state_dir
        self.config = config or (lambda: catalog.load_config(state_dir))
        self.cli = cli
        self.settings = settings   # the hub.json keep_on writes to (None: catalog.config_path)
        self.told = set()   # lines already in the log (tell)

    def expose(self):
        """The ports of this hub's page and "Files" without tailnet.expose: an instance's own (its anchor,
        core/instance.py), the main hub's 8787/8788 (EXPOSE)."""
        from core import instance
        return {kind: instance.port(kind, self.state_dir) for kind in EXPOSE}

    def command(self):
        cfg = self.config().get("tailnet") or {}
        return self.cli or [cfg.get("cli") or platforms.current.tailscale_cli()]

    def ts(self, *args):
        r = subprocess.run([*self.command(), *args], capture_output=True, text=True, timeout=TIMEOUT, env=cli_env())
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[-300:] or f"tailscale {' '.join(args)}: {r.returncode}")
        return r.stdout

    def drop_stale_serve(self):
        """`serve reset` drops the whole setup: only when all of it is proxies to the disabled services on this machine
        (and the ports they are served on) — a TCP forward, a funnel, another target next to them keep it all."""
        serve = json.loads(self.ts("serve", "status", "--json") or "{}")
        if stale_only(serve):
            self.ts("serve", "reset")

    def keep(self, prev, cfg):
        """Is the previous record still good to serve while `tailscale` does not answer: it was working less than STALE_AFTER
        ago (it is not rewritten, so the age holds across timeouts) and it still agrees with the settings that can change
        meanwhile: the owners named in hub.json and the ports exposed."""
        if not isinstance(prev, dict) or not prev.get("ok") or not access.fresh(prev, STALE_AFTER, skew=0):
            return False
        if cfg.get("owners") and sorted(owners_of(cfg, None)) != sorted(prev.get("owners") or ()):
            return False
        ports = {str(p) for p in (cfg.get("expose") or self.expose()).values()}
        return ports == {h.rsplit(":", 1)[-1] for h in prev.get("hosts") or () if isinstance(h, str)}

    def tick(self):
        prev = access.record(self.state_dir)   # as written: an earlier release's working one counts however old
        prev = prev if isinstance(prev, dict) else {}
        try:
            cfg = self.config().get("tailnet")
        except OSError:   # a read that failed just now (Windows: hub.json being replaced): the next tick reads again
            return prev
        except ValueError as e:   # hub.json is not JSON: not what the owner chose — no page on a guess
            if not os.path.exists(os.path.join(self.state_dir, "tailnet.json")):
                self.chosen()   # a new hub: its record below must not pass for an earlier release's later
            return self.save({"ok": False, "why": f"hub.json: {e}"})
        cfg = {} if cfg is None else cfg
        if not isinstance(cfg, dict):   # a section of another shape: off, saying so (the switch on the card fixes it)
            self.chosen()
            return self.save({"ok": False, "why": "hub.json: tailnet is not an object"})
        # the first ticks after an earlier release (its record, nothing chosen in hub.json): carry over what worked there
        carry = "enabled" not in cfg and not os.path.exists(os.path.join(self.state_dir, CHOSEN))
        if carry and (prev.get("off") or not os.path.exists(os.path.join(self.state_dir, "tailnet.json"))):
            self.chosen()   # a new hub (the module never ran here), or off already
            carry = False
        if cfg.get("enabled") is not True and not carry:
            return self.off(prev)
        rec = self.look(cfg)
        if carry:
            if prev.get("ok") is True or (rec or {}).get("ok") is True:
                kept = self.keep_on()
                if kept is None:   # hub.json is not writable: on as before, and the next tick tries again
                    return prev if rec is None else self.save(rec)
            elif (rec or {}).get("down"):   # Tailscale is here but not up now (a laptop away): decide once it is
                return self.save(rec)
            else:   # no Tailscale program, or nobody to let in: it did not work
                kept = False
            self.chosen()
            if not kept:
                return self.off(prev)
        return prev if rec is None else self.save(rec)

    def chosen(self):
        """Remember that this code has decided (the carry-over is done once): a rollback's earlier release turns the
        tailnet on by itself again, and its record must not count as "it worked" on the way forward."""
        try:
            open(os.path.join(self.state_dir, CHOSEN), "a").close()
        except OSError as e:
            self.tell(f"tailnet: {CHOSEN} is not written ({e}): the earlier release's tailnet is looked at again")

    def tell(self, line):
        """A line to the log once per process, not every minute (13.10: half of hub-up.log)."""
        if line not in self.told:
            self.told.add(line)
            print(line, file=sys.stderr, flush=True)

    def off(self, prev):
        """The record of the tailnet that is off; the same one is not rewritten every minute."""
        why = i18n.tr(i18n.owner(), "cli.tailnet.off")
        if prev.get("off") is True and prev.get("why") == why and set(prev) == {"ok", "off", "why", "ts"}:
            return prev
        return self.save({"ok": False, "off": True, "why": why})

    def keep_on(self):
        """`tailnet.enabled: true` into hub.json, once: the tailnet worked under the earlier release, which turned it on
        by itself, and the owner's phone must not lose the page with the update. Under the settings lock and only when
        the key is still absent (the owner may have chosen meanwhile). -> True (on), False (the owner chose otherwise
        meanwhile), None (not written)."""
        try:
            with catalog.edit_settings(self.settings or catalog.config_path(self.state_dir)) as raw:
                if not isinstance(raw, dict):
                    raise ValueError("hub.json is not an object")
                if raw.get("tailnet") is None:
                    raw["tailnet"] = {}
                section = raw["tailnet"]
                if not isinstance(section, dict) or "enabled" in section:   # chosen meanwhile: left as it is
                    raise Chosen(isinstance(section, dict) and section["enabled"] is True)
                section["enabled"] = True
        except Chosen as e:
            return e.args[0]
        except (OSError, ValueError) as e:
            self.tell(f"tailnet: it worked before the update, but tailnet.enabled: true is not written ({e})")
            return None
        print("tailnet: it worked before the update — hub.json now has tailnet.enabled: true", file=sys.stderr, flush=True)
        return True

    def look(self, cfg):
        """What Tailscale says now -> the record for tailnet.json; None — keep the previous record as it is (a timeout
        while it is fresh: it is not rewritten, so its age holds)."""
        try:
            st = json.loads(self.ts("status", "--json"))
            if st.get("BackendState") != "Running":   # down: Tailscale is here, not connected now
                return {"ok": False, "down": True,
                        "why": i18n.tr(i18n.owner(), "cli.tailnet.not_connected", state=st.get("BackendState"))}
            me = st["Self"]
            host = me["DNSName"].rstrip(".")
            ip = next((a for a in me.get("TailscaleIPs") or [] if "." in a), None)
            owners = owners_of(cfg, st)
            try:
                self.drop_stale_serve()
            except subprocess.TimeoutExpired:
                pass   # `status` just answered: its fresh record stands, the cleanup of the old export waits for the next tick
        except subprocess.TimeoutExpired as e:
            # a hung machine: do not kill a working record while it is fresh (the page would answer 403 until the next tick)
            if self.keep(access.record(self.state_dir), cfg):
                return None
            return {"ok": False, "down": True, "why": f"tailscale: {e}"}
        except FileNotFoundError as e:   # no Tailscale on this machine
            return {"ok": False, "why": f"tailscale: {e}"}
        except (RuntimeError, ValueError, KeyError, OSError) as e:   # it is here, but failed now (tailscaled down)
            return {"ok": False, "down": True, "why": f"tailscale: {e}"}
        if not owners:
            return {"ok": False, "owners": [], "why": i18n.tr(i18n.owner(), "cli.tailnet.no_owner")}
        expose = cfg.get("expose") or self.expose()
        return {"ok": bool(ip), **({} if ip else {"why": "tailscale: no IPv4 address"}), "owners": owners,
                "owner": owners[0], "ip": ip, "name": host,
                "cli": self.command()[-1],
                "urls": {n: f"http://{ip}:{p}" for n, p in expose.items()},
                "urls_dns": {n: f"http://{host}:{p}" for n, p in expose.items()} if host else {},
                "hosts": sorted({f"{h}:{p}" for p in expose.values() for h in (ip, host, host.split(".")[0]) if h})}

    def save(self, data):
        data = {**data, "ts": time.time()}
        path = os.path.join(self.state_dir, "tailnet.json")
        with open(path + ".tmp", "w") as f:
            json.dump(data, f, ensure_ascii=False)
        platforms.current.replace(path + ".tmp", path)   # Windows: while the page reads the file, a bare os.replace fails
        return data


class Forwards:
    """Forwarding of running direction boards (boards.json, modules/directions/boards.py) on the Mac's tailnet IP:
    a board listens only on 127.0.0.1, so a phone could not open it otherwise. Lets in only the owner (whois)."""

    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.active = {}  # port -> (ip, stop event)

    def reconcile(self):
        from modules.tailnet.forward import Forward
        st = access.status(self.state_dir)
        try:
            with open(os.path.join(self.state_dir, "boards.json")) as f:
                wanted = {int(p) for p in json.load(f)}
        except (OSError, ValueError):
            wanted = set()
        ip = st.get("ip") if st.get("ok") else None
        for port, (bound, stop) in list(self.active.items()):
            if port not in wanted or bound != ip:
                stop.set()
                del self.active[port]
        for port in wanted - set(self.active) if ip else ():
            stop = threading.Event()
            name = st.get("name") or ""
            names = {f"{h}:{port}" for h in (ip, name, name.split(".")[0]) if h}   # Hosts to let in under
            fw = Forward(ip, port, port, allow=lambda peer: access.owner_peer(self.state_dir, peer), stop=stop,
                         hosts=lambda names=names: names)
            threading.Thread(target=self.run, args=(fw, port), daemon=True).start()
            self.active[port] = (ip, stop)

    def run(self, fw, port):
        try:
            fw.serve()
        except OSError as e:
            print(f"tailnet: forward :{port} did not come up: {e}", file=sys.stderr, flush=True)
            self.active.pop(port, None)


def report(st, last):
    """The failure reason goes to the log only when it changes (13.10: without Tailscale, a line a minute was half of hub-up.log);
    back in service, one line. Returns what is now in the log."""
    now = "working" if st["ok"] else st.get("why", "no IP or owner")
    if now != last and (last is not None or not st["ok"]):
        print(f"tailnet: {now}", file=sys.stderr, flush=True)
    return now


def settings_stamp(state_dir):
    """Which hub.json is there now: its time, size and file (a replace makes a new one); None — no file."""
    try:
        f = os.stat(catalog.config_path(state_dir))
    except OSError:
        return None
    return f.st_mtime_ns, f.st_size, f.st_ino


def main():
    state_dir = default_state_dir()
    if not lock.hold(os.path.join(state_dir, "tailnet.lock")):
        sys.exit("tailnet is already running: one process per machine")
    t, fwd, last, said, seen = Tailnet(state_dir), Forwards(state_dir), 0, None, None
    while True:
        edited = settings_stamp(state_dir)   # the switch on the page: turned on or off now, not within a minute
        since = time.time() - last   # negative: the clock was set back — look now, not when it catches up
        if not 0 <= since < EVERY or edited != seen:
            last, seen = time.time(), edited
            said = report(t.tick(), said)
        fwd.reconcile()  # boards are started on demand: pick them up within seconds, not a minute
        time.sleep(PAUSE)


if __name__ == "__main__":
    main()
