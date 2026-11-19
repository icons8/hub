"""One session under different addresses: name, sessionId, pid, tmux pane, socket."""
from core import langdata

KINDS = ("sid", "pid", "pane", "sock")
FIELDS = {"session_id": "sid", "pid": "pid", "pane": "pane", "socket": "sock"}


def remember(state, name, data):
    """Remember the session's addresses from the event data; the session's older addresses of the same kind are forgotten."""
    aliases = state["aliases"]
    for field, kind in FIELDS.items():
        value = data.get(field)
        if value in (None, ""):
            continue
        for key in [k for k, v in aliases.items() if v == name and k.startswith(kind + ":")]:
            del aliases[key]
        aliases[f"{kind}:{value}"] = name


def resolve(state, address):
    """Session name by any address, or None."""
    address = str(address).strip()
    if address in state["sessions"] and state["sessions"][address].get("state") != "removed":
        return address
    if f"was:{address}" in state["aliases"]:  # the former name of a renamed session
        return state["aliases"][f"was:{address}"]
    gone = state["sessions"].get(address)
    up = gone and gone.get("raised_by")
    if up and resolve(state, up):  # a departed executor — to whoever raised it (release 2)
        return resolve(state, up)
    if address in state["sessions"]:
        return address
    if address.startswith("uds:"):
        address = "sock:" + address[4:]
    if address.startswith("name:"):
        name = address[5:]
        return name if name in state["sessions"] else None
    if address.split(":", 1)[0] in KINDS:
        return state["aliases"].get(address)
    for kind in KINDS:
        name = state["aliases"].get(f"{kind}:{address}")
        if name:
            return name
    return None


HUB_WORDS = ("hub", *langdata.words("words", "hub"))   # the hub's name in any language with data


def address(state, to):
    """Address → session name: like resolve, plus "hub" (and its Russian form) — the hub session, whatever it is called:
    this is what the skills teach (`hub send hub`, `hub tools grant hub …`). No such session — None."""
    to = str(to or "").strip()
    name = resolve(state, to) if to else None
    if name is None and to.casefold() in HUB_WORDS:
        name = next((n for n, s in state["sessions"].items() if s.get("kind") == "hub" and s.get("state") != "removed"), None)
    return name
