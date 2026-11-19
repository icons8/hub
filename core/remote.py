"""Remote access: who may open the hub page and Files from elsewhere than this machine — the contract between the
core and the providers that bring the page to other devices.

Two steps. First who came: a request is local — the machine itself: the peer is 127.0.0.1 or ::1, Host names
localhost (not another site's page through DNS rebinding), and no proxy or tunnel passed it on
(modules/tailnet/access.py, local); otherwise a provider must name the person behind it; no identity, no entry. Then,
when the hub has a password (core/pagepass.py, off by default), the cookie its sign-in form gives — from anywhere. The
core decides; a provider only reports what it can prove.

The providers are built into the hub, in this order (a list in the code, not a manifest field: whoever decides "who
came" runs inside the page and Files with the owner's rights, so only code shipped with the hub may be one):
- tailnet — the request came to the tailnet address: its Host is in tailnet.json and `tailscale whois` of the peer is
  one of the owners (modules/tailnet/access.py; the behavior is the one before this contract).
- cloudflare — the request came from cloudflared on this machine with a signed Cloudflare Access token
  (modules/cloudflare/remote.py); off by default.

Each has:
- status(state_dir) -> {ok, why, hosts: [Host names]}: read a file its own process keeps, never call out — it runs on
  every request.
- claims(state_dir, req) -> bool: the request came through this provider (its Host, its headers).
- identify(state_dir, req) -> the verified identity (a login, an e-mail) or None; asked only while status is ok.
req: {host, peer, headers (lower-case names; a header sent twice is empty), whois (tests)}.
A provider that fails closes only its own door: an error in it is no identity.
"""
import sys

from modules.tailnet.access import LOCAL as LOCAL_NAMES, PEERS as LOCAL_PEERS, _name as hostname  # noqa: F401 — one list

_said = set()   # (provider, error) already logged: each distinct error once, at most SAID of them
SAID = 50


def _log(name, err):
    seen = (name, type(err).__name__, str(err)[:200])
    if seen not in _said and len(_said) < SAID:
        _said.add(seen)
        print(f"remote access: {name}: check failed: {err!r}", file=sys.stderr, flush=True)


def request(host, peer, headers=None, whois=None):
    """A request as providers see it. `headers`: a dict or the server's message object. `memo`: what a provider read
    for this request (its files are read once per request, not per call)."""
    seen = {}
    for k, v in (headers.items() if headers is not None else ()):
        k = str(k).lower()
        seen[k] = "" if k in seen else str(v)   # sent twice: which one is meant is unknown
    return {"host": (host or "").strip(), "peer": peer, "headers": seen, "whois": whois, "memo": {}}


class Tailnet:
    """The tailnet module as a provider: an owner's device by its tailnet address."""
    name = "tailnet"
    worded = False    # its refusal is worded by the page as before (not "through a proxy")

    def status(self, state_dir, req=None):
        from modules.tailnet import access
        if req is None:
            return access.status(state_dir)
        if "tailnet" not in req["memo"]:
            req["memo"]["tailnet"] = access.status(state_dir)
        return req["memo"]["tailnet"]

    def claims(self, state_dir, req):
        return req["peer"] not in LOCAL_PEERS

    def identify(self, state_dir, req):
        from modules.tailnet import access
        st = self.status(state_dir, req)
        if req["host"] not in (st.get("hosts") or []):
            return None
        logins = access.owners(st)
        if not logins:
            return None
        login = (req["whois"] or access.tailscale_whois)(state_dir, req["peer"])
        return login if login in logins else None


class Cloudflare:
    name = "cloudflare"
    worded = True     # a refused request it claimed hears "Cloudflare Access did not confirm who you are"

    def status(self, state_dir, req=None):
        from modules.cloudflare import remote
        return remote.status(state_dir, req=req)

    def claims(self, state_dir, req):
        from modules.cloudflare import remote
        return remote.claims(state_dir, req)

    def identify(self, state_dir, req):
        from modules.cloudflare import remote
        return remote.identify(state_dir, req)


PROVIDERS = (Tailnet(), Cloudflare())


def admit(state_dir, host, peer, headers=None, whois=None, providers=PROVIDERS):
    """{"local": True}, {"local": False, "provider", "who"}, or None — not let in. Without `headers` (the call of an
    earlier release's server still in memory) a provider that needs them finds nothing."""
    got = identify(state_dir, host, peer, headers, whois, providers)
    return got if got is not None and _signed_in(headers, got) else None


def identify(state_dir, host, peer, headers=None, whois=None, providers=PROVIDERS):
    """The first step: who came — {"local": True}, {"local": False, "provider", "who"}, or None (refused before any
    sign-in form)."""
    from modules.tailnet import access
    if access.local(host, peer, headers=headers):
        return {"local": True}
    req = request(host, peer, headers, whois)
    for p in providers:
        try:
            if not p.claims(state_dir, req) or not p.status(state_dir, req).get("ok"):
                continue
            who = p.identify(state_dir, req)
        except Exception as e:  # noqa: BLE001 — a provider's bug is its own closed door
            _log(p.name, e)
            continue
        if isinstance(who, str) and who:
            return {"local": False, "provider": p.name, "who": who}
    return None


def _signed_in(headers, who):
    """The second step (core/pagepass.let_in): no password, or this browser signed in; a failure to read it is a closed
    door."""
    try:
        from core import pagepass
        return pagepass.let_in(headers, remote=not who.get("local"))
    except Exception as e:  # noqa: BLE001
        _log("page password", e)
        return False


def needs_signin(state_dir, host, peer, headers, whois=None):
    """A request turned away only at the second step — who came is known (this machine, the owner by a provider), the
    sign-in is missing: it hears 401 and the sign-in form, not "localhost only"."""
    got = identify(state_dir, host, peer, headers, whois)
    return got is not None and not _signed_in(headers, got)


def needs_key(host, peer, headers):
    """needs_signin for a request of this machine (the name a page server of an earlier release still in memory calls)."""
    from modules.tailnet import access
    return access.local(host, peer, headers=headers) and not _signed_in(headers, {"local": True})


def refused_by(state_dir, host, peer, headers):
    """For a request admit() did not let in (the caller knows): the provider that is on and claimed it, when it words
    its own refusal, else None."""
    req = request(host, peer, headers)
    for p in PROVIDERS:
        if not p.worded:
            continue
        try:
            if p.claims(state_dir, req) and p.status(state_dir, req).get("ok"):
                return p.name
        except Exception as e:  # noqa: BLE001
            _log(p.name, e)
    return None


SERVER_ADDRESS = "<server address>"   # the tunnel's host when this shell does not know the one the person reaches


def public(address):
    import ipaddress
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


def tunnel(web, files, user, env=None, is_public=public):
    """The one command that brings this machine's page and Files to the person's computer when the machine has no screen
    (a server: install.sh --server, `hub open`): both ports on 127.0.0.1 there; here the request then comes from
    127.0.0.1 with Host localhost — local, as from the keyboard; nothing of the hub listens outside. The host is the
    address this shell's SSH client reached (SSH_CONNECTION) when it is a public one; a private one (a cloud's NAT
    behind its public address) or none — a placeholder the person replaces with the address they connect to."""
    seen = ((env if env is not None else {}).get("SSH_CONNECTION") or "").split()
    host = seen[2] if len(seen) == 4 and is_public(seen[2]) else SERVER_ADDRESS
    return f"ssh -N -L {web}:127.0.0.1:{web} -L {files}:127.0.0.1:{files} {user}@{host}"
