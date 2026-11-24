"""A core module's own state (Module API, spec section 2.3): JSON keys and values in the journal, folded by common core
code; the module's code does not take part in it.

    mod.<name>.set {key, value, if_rev?, launch?}   key := value; launch - the process launch that wrote it
    mod.<name>.del {key, if_rev?}         delete the key
    mod.<name>.<what> {...}               other module facts - only a journal record (history), they do not change state

state["mods"][<name>][key] = {value, rev, launch?}: rev is the number of the write event (seq), continuous - a deleted and
recreated key gets a new number, the old rev does not fit it. if_rev - write only if the key is still at this revision (0 - the
key does not exist), otherwise a refusal with a reason: protection against lost updates of one key between a handler, a command
and the module process.
Only a module that has answered hello at least once (module.ready) may write: any process on the machine can put an event
in the mailbox, and without this a foreign name would take up the shared limit. Access questions are not decided by mods.

The limits keep the page snapshot light: the stream (/api/stream) resends all of mods on any write. Frequent and large data
go through a handler or the module's stream, not here. The mods key appears in state only with the first write.
"""
import json
import os
import re

from core import forms, modpages, modrun, modules
from core.coretext import T
from core.tools import Rejected

KEY = re.compile(r"[a-z0-9_.-]{1,64}")
MAX_KEYS = 256              # keys per module
MAX_MODULE = 256 * 1024     # bytes per module: keys + values in JSON
MAX_ALL = 512 * 1024        # bytes for all modules: the page stream sends them whole on every write (measured - decisions, S11)


def size(key, value):
    """How much a key with its value weighs against the limit: the bytes of the key and of the value in compact JSON (as
    in the journal). A value that is not JSON (NaN, infinity - the browser cannot parse them) - ValueError."""
    return len(key) + len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def _used(mods):
    return sum(size(k, r["value"]) for k, r in mods.items())


def known(st, name):
    """The module has answered hello at least once in this journal (module.ready recorded the version)."""
    return bool(((st.get("modules") or {}).get(name) or {}).get("version"))


def apply(st, ev, data):
    """Part of the fold: mod.<name>.* events."""
    parts = ev["type"].split(".", 2)
    if len(parts) < 3 or not parts[2]:
        raise Rejected("module event — mod.<name>.<what>")
    name, what = parts[1], parts[2]
    if not modules.NAME.fullmatch(name) or ev.get("entity") not in (None, name):
        raise Rejected("module event: mod.<name>.<what>, entity is its name or empty")
    if not known(st, name):
        raise Rejected(f"module {name} is not loaded in this hub (it did not answer hello) — its state is not written")
    if what not in ("set", "del"):
        return   # the module's own fact: history in the journal
    key = data.get("key")
    if not (isinstance(key, str) and KEY.fullmatch(key)):
        raise Rejected("key: lowercase Latin letters, digits and \"_.-\", up to 64 characters")
    mine = (st.get("mods") or {}).get(name) or {}
    have = (mine.get(key) or {}).get("rev", 0)
    if "if_rev" in data:
        want = data["if_rev"]
        if not (isinstance(want, int) and not isinstance(want, bool) and want >= 0):
            raise Rejected("if_rev: a key revision number (0 — no such key)")
        if want != have:
            raise Rejected(f"{name}.{key}: the key is at revision {have}, not {want}" if have
                           else f"{name}.{key}: there is no key, but if_rev is {want}")
    if what == "del":
        if key in mine:
            del st["mods"][name][key]
            if not st["mods"][name]:
                del st["mods"][name]
        return
    if "value" not in data:
        raise Rejected("value: a JSON value (null is a value too)")
    launch = data.get("launch")
    if "launch" in data and not (isinstance(launch, str) and 0 < len(launch) <= 40):
        raise Rejected("launch: the module process launch mark (HUB_MODULE_LAUNCH), up to 40 characters")
    try:
        new = size(key, data["value"])
    except (ValueError, RecursionError):
        raise Rejected("value: not JSON (a browser cannot parse NaN and infinity)")
    if key not in mine and len(mine) >= MAX_KEYS:
        raise Rejected(f"module {name} already has {MAX_KEYS} keys — no more; keep big and frequent data behind a module handle")
    old = size(key, mine[key]["value"]) if key in mine else 0
    used = _used(mine)
    if used - old + new > MAX_MODULE:
        raise Rejected(f"the state of module {name} would be {used - old + new} B — the limit is {MAX_MODULE} B; keep big data "
                       "behind a module handle")
    total = sum(_used(m) for n, m in (st.get("mods") or {}).items() if n != name) + used - old + new
    if total > MAX_ALL:
        raise Rejected(f"the state of all modules would be {total} B — the limit is {MAX_ALL} B")
    st.setdefault("mods", {}).setdefault(name, {})[key] = {"value": data["value"], "rev": ev["seq"],
                                                           **({"launch": launch} if launch else {})}


# rights that have their own part in a module's slice (Module API 2.4: sessions; 2.6: accounts, messages)
VIEW_RIGHTS = ("sessions", "accounts", "messages")


def views(st, sets):
    """Slice parts for the modules' right sets sets, each once per event: {"sessions": session rows and renamed,
    "sessions+accounts": the same with the session's account (if someone has both rights), "accounts": limits, "messages":
    owner_messages}. Parts nobody has the right for are not built - a hub without such modules does not pay for them."""
    sets = [set(x) for x in sets]
    rights = set().union(*sets)
    out = {}
    if "accounts" in rights:
        out["accounts"] = account_view(st)
    if not rights & {"sessions", "messages"}:
        return out
    index, last = owner_index(st)
    if "messages" in rights:
        out["messages"] = index
    if "sessions" in rights:
        rows = session_rows(st, last)
        out["sessions"] = rows
        if any({"sessions", "accounts"} <= x for x in sets):   # a module with both also gets the account of the session and of the cut
            sess = st.get("sessions") or {}
            out["sessions+accounts"] = {**rows, "sessions": {
                n: {**r, "account": sess[n].get("account") if isinstance(sess[n].get("account"), str) else None,
                    "limit_cut": r["limit_cut"] and {**r["limit_cut"], "account": _cut_account(sess[n])}}
                for n, r in rows["sessions"].items()}}
    return out


# right -> the top-level keys its part puts into the slice
PART_OF = {"sessions": ("sessions", "renamed"), "accounts": ("accounts",), "messages": ("owner_messages",)}
PART_KEYS = tuple(k for keys in PART_OF.values() for k in keys)


def part_keys(caps):
    """The part keys in the slice of a module with rights caps."""
    return tuple(k for r, keys in PART_OF.items() if r in set(caps) for k in keys)


def part(views_, caps):
    """What of the parts views_ goes into the slice of a module with rights caps - as top-level keys (part_keys)."""
    caps = set(caps)
    out = {}
    if "sessions" in caps:
        out.update(views_["sessions+accounts" if "accounts" in caps else "sessions"])
    if "accounts" in caps:
        out["accounts"] = views_["accounts"]
    if "messages" in caps:
        out["owner_messages"] = views_["messages"]
    return out


def slices(st, names, rights=None, built=None):
    """{name: slice} - what of the hub state a module's process needs (hubmod.state, @on_ready): its own records, the weight
    of others' in the shared limit, the readiness of this launch; modules with rights from VIEW_RIGHTS (rights: {name: its
    rights}) also get their parts (views). hubd writes this as a file (modrun.Supervisor.publish) - a module process does
    not read the whole state.json (on a big hub that is +100 MB of peak per process). built - parts already built for this
    event (views)."""
    rights = {n: set(rights.get(n) or ()) & set(VIEW_RIGHTS) for n in names} if rights else {}
    mods, recs = st.get("mods") or {}, st.get("modules") or {}
    used = {n: _used(m) for n, m in mods.items()}
    total = sum(used.values())
    if built is None:
        built = views(st, rights.values()) if rights else {}
    return {n: {"ready": (recs.get(n) or {}).get("ready"),
                "mods": {k: dict(r) for k, r in (mods.get(n) or {}).items()},
                "others": total - used.get(n, 0),
                **part(built, rights.get(n) or ())} for n in names}


def slice(st, name, rights=()):
    return slices(st, [name], {name: rights})[name]


def session_rows(st, last=None):
    """The hub's sessions for a module with the sessions right: {"sessions": {name: row}, "renamed": {old name: current}}.
    A row: state - as in the core (idle, busy, asleep...); parked - parked (views: no process, a message will raise it -
    free for the owner); asleep - the core will accept wake (the SESSION machine; for a parked one too); busy - a turn is
    running (chain.BUSY) or the process is starting; model - the id of the model it was started with (info.model). Since
    2.6: retired - handed over and asleep, messages do not wake it; owner_msg - {id, ts} of the owner's last message to
    it (last - from owner_index; among those lying in state) or None; asks - cards to the owner [{id, since, until}] (until
    is since + ASK_TTL; expired ones are not filtered by the clock - the slice is written per event); forms - open forms
    and permissions [{id: request_id, tool, since}]; reacts - {line: emoji} of pending owner reactions; hub_wait -
    whether there is an active `hub wait` check; limit_cut - {ts, resets}: the turn was cut off by a limit and the core has not
    continued yet (resets - when the window resets, or None), or None. Since 2.10: raised_by - the session that raised it
    (an executor) or None; since - when its current state began (for a handed-in one - when it handed in) or None; worktree -
    {branch, base} of its working copy (each a name or None) or None. The fields are always present. No removed sessions;
    no texts, paths, conversation ids, keys."""
    from core import chain, limitresume, machines, owner, schedule
    wakes = machines.SESSION["transitions"]["session.wake_requested"]
    if last is None:
        last = owner_index(st)[1]
    waits = {j.get("session") for jid, j in (st.get("schedule") or {}).items()
             if isinstance(j, dict) and j.get("state") == "active" and schedule.WAIT_ID.match(str(j.get("id") or jid))}
    out = {}
    for n, s in (st.get("sessions") or {}).items():
        state = s.get("state")
        if state == "removed":
            continue
        info = s.get("info")
        model = info.get("model") if isinstance(info, dict) else None
        cut, wt = s.get("limit_cut"), s.get("worktree")
        # state is the old vocabulary (Module API 2.4): parked and handed-over are asleep, with separate fields parked, retired
        out[n] = {"state": "asleep" if state in machines.SLEEP else state, "parked": state == "parked",
                  "asleep": state in wakes,
                  "busy": state in chain.BUSY or state == "starting",
                  "model": model if isinstance(model, str) else None,
                  "retired": state == "retired", "owner_msg": last.get(n),
                  "asks": [{"id": a.get("id"), "since": a.get("since"),
                            "until": owner.ask_since(a) + owner.ASK_TTL
                            if _num(a.get("since")) and _num(owner.ask_since(a)) and not owner.waits_return(a) else None}
                           for a in _list(s.get("asks")) if isinstance(a, dict)],
                  "forms": [{"id": p.get("request_id"), "tool": p.get("tool"), "since": p.get("since")}
                            for p in _list(s.get("pending")) if isinstance(p, dict)],
                  "reacts": {str(k): v.get("emoji") for k, v in (s.get("reacts") if isinstance(s.get("reacts"), dict)
                                                                 else {}).items() if isinstance(v, dict)},
                  "hub_wait": n in waits,
                  "limit_cut": limitresume.view(cut),
                  "raised_by": _name(s.get("raised_by")), "since": s.get("since") if _num(s.get("since")) else None,
                  "worktree": {"branch": _name(wt.get("branch")), "base": _name(wt.get("base"))}
                  if isinstance(wt, dict) else None}
    renamed = {k[4:]: v for k, v in (st.get("aliases") or {}).items()
               if k.startswith("was:") and v in out and k[4:] not in out}   # the name is taken again - then it is that one
    return {"sessions": out, "renamed": renamed}


def _name(x):
    return x if isinstance(x, str) else None


def _list(x):
    return x if isinstance(x, list) else []


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _cut_account(sess):
    a = (sess.get("limit_cut") or {}).get("account")
    return a if isinstance(a, str) else None


def owner_index(st):
    """The owner's messages that lie in state (delivered and failed ones older than a day are removed by the core -
    core.pruned): ({mN: {to, ts, answers, form}}, {session: {id, ts} of the last one to it}). to - the addressee as the core
    understood it (session; the core carries a rename over); answers - the card qN the core counted it as an answer to; form
    - the request_id of the form he answered. As in the core (tools.card_answer): "Deny" is his message but not an answer; the
    core's note about closing a card with the x is not his message. No text."""
    from core import tools
    index, last = {}, {}
    for mid, m in (st.get("messages") or {}).items():
        if not isinstance(m, dict) or m.get("from_owner") is not True or tools.dismissal(m):
            continue
        to, ts = m.get("session"), m.get("created")
        index[mid] = {"to": to, "ts": ts,
                      "answers": None if m.get("card_declined") else m.get("answers_question") or None,
                      "form": m.get("answered_form") or None}
        if isinstance(to, str) and _num(ts) and ts >= (last.get(to) or {}).get("ts", float("-inf")):
            last[to] = {"id": mid, "ts": ts}
    return index, last


def account_view(st):
    """The hub's Claude account limits for a module with the accounts right - what the page shows in "Accounts":
    {active, blocked: {name: until}, exhausted: {until} - all are busy until that time, or None, seen: {name: {status,
    type, windows: {window: {used, resets}}, ts}}}. There are no tokens in
    the core state (they are in accounts.json of the settings folder); token fingerprints and the like do not belong here."""
    a = st.get("account") if isinstance(st.get("account"), dict) else {}
    seen = {}
    for k, v in (a.get("seen") if isinstance(a.get("seen"), dict) else {}).items():
        if not isinstance(v, dict):
            continue
        windows = v.get("windows") if isinstance(v.get("windows"), dict) else {}
        seen[k] = {"status": v.get("status"), "type": v.get("type"), "ts": v.get("ts"),
                   "windows": {w: {"used": x.get("used"), "resets": x.get("resets")}
                               for w, x in windows.items() if isinstance(x, dict)}}
    blocked = a.get("blocked") if isinstance(a.get("blocked"), dict) else {}
    out = a.get("exhausted")
    return {"active": a.get("active"), "blocked": dict(blocked),
            "exhausted": {"until": out.get("until")} if isinstance(out, dict) else None, "seen": seen}


def changes_slices(ev_type):
    """A fact after which the module slices may differ: a key write (its own or a foreign one - the weight) and readiness
    (module.error clears it). A type that is not a string is a mailbox fact the fold will reject: the slices are the same."""
    return isinstance(ev_type, str) and (ev_type in ("module.ready", "module.error")
                                         or (ev_type.startswith("mod.") and ev_type.endswith((".set", ".del"))))


# --- a repeat without a duplicate: dedup_key (spec section 2.2) and the page's client_id --------------------------
#
# A module fact (mod.<name>.* or with channel "mod:<name>" - the owner's word from a module handler) may carry a dedup_key.
# Only an accepted fact occupies the key; a repeat with the same key from the same module, before DEDUP_WINDOW has passed by
# the journal's ts (not by the reader's clock - a replay gives the same), is rejected by the fold, and the rejection names
# the accepted one: mN if the fact created a message, otherwise the event number. So a client that repeated a request after a
# break gets one fact, without a "wrote the key but did not send" gap. state["mod_dedup"][<name>][key] = {seq, ts, id} -
# appears only with the first key.
# The same goes for the client_id of an owner message from the page and its clients (/api/send): its own root
# state["web_dedup"][<addressee, as named>][key] (a module may be named web - the keys are not shared); other channels
# ignore client_id.

DEDUP_WINDOW = 86400
DEDUP_MAX = 4096        # keys per module (for the page - per addressee): beyond that old ones leave before the window ends
DEDUP_KEY_MAX = 128


def origin(ev):
    """The name of the module the fact is from, or None: type mod.<name>.* or channel mod:<name>."""
    kind, data = str(ev.get("type") or ""), ev.get("data") or {}
    if kind.startswith("mod.") and kind.count(".") >= 2:
        return kind.split(".", 2)[1]
    ch = data.get("channel") if isinstance(data, dict) else None
    return ch[4:] if isinstance(ch, str) and ch.startswith("mod:") and ch[4:] else None


def _scope(ev):
    """Whose repeat key a fact has: (key field, state root, whose keys: module or addressee, key) or None - no key. The
    event's dedup field (Module API 2.6, hubmod.fact) is written by hubd only for a module fact - from mod:<name>; the keys
    are shared with its mod.* and the owner's word. Old journals do not carry it: the fold did not look at dedup_key in the
    data of an ordinary module fact and does not now."""
    data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
    if "dedup" in ev:
        sender = data.get("sender")
        name = sender[4:] if isinstance(sender, str) and sender.startswith("mod:") else ""
        return ("dedup_key", "mod_dedup", name, ev["dedup"]) if modules.NAME.fullmatch(name) else None
    name = origin(ev)
    if name and "dedup_key" in data:
        return "dedup_key", "mod_dedup", name, data["dedup_key"]
    if ev.get("type") == "message.received" and data.get("channel") == "web" and data.get("owner") is True \
            and "client_id" in data:
        # the same id for another session is another message
        return "client_id", "web_dedup", str(data.get("to") or ""), data["client_id"]
    return None


def _keys(st, root, name, make=False):
    box = st.setdefault(root, {}) if make else st.get(root) or {}
    return box.setdefault(name, {}) if make else box.get(name) or {}


def dedup_check(st, ev):
    """Before the fold: a repeat of an accepted fact - Rejected with the accepted one's number. Without a repeat key - nothing."""
    scope = _scope(ev)
    if not scope:
        return
    field, root, name, key = scope
    if not (isinstance(key, str) and 0 < len(key) <= DEDUP_KEY_MAX):
        raise Rejected(f"{field}: a string of up to {DEDUP_KEY_MAX} characters")
    seen = _keys(st, root, name).get(key)
    if seen and ev["ts"] - seen["ts"] < DEDUP_WINDOW:
        # the accepted one: its message mN, otherwise its event (a phrase, also for an id an earlier core wrote)
        mid = seen.get("id") if re.fullmatch(r"m\d+", str(seen.get("id") or "")) else \
            T("core.modstate.event_n", "event {seq}").format(seq=seen.get("seq"))
        raise Rejected(forms.Why(T("core.modstate.repeat", "repeat of {field} {key}: already accepted — {id}").format(
            field=field, key=key, id=mid), forms.DUPLICATE))


def dedup_take(st, ev):
    """After the fold of an accepted fact: the key is taken by it; old keys outside the window are dropped, and so is a
    foreign box of this root where even the freshest key is outside the window (keys go in time order): an addressee that
    is no longer written to would otherwise keep them forever."""
    scope = _scope(ev)
    if not scope:
        return
    field, root, name, key = scope
    mid = f"m{ev['seq']}"
    mine = _keys(st, root, name, make=True)
    for k in [k for k, v in mine.items() if ev["ts"] - v["ts"] >= DEDUP_WINDOW]:
        del mine[k]
    for who in [w for w, box in st[root].items()
                if w != name and (not box or ev["ts"] - next(reversed(box.values()))["ts"] >= DEDUP_WINDOW)]:
        del st[root][who]
    mine.pop(key, None)
    mine[key] = {"seq": ev["seq"], "ts": ev["ts"], "id": mid if mid in st["messages"] else f"event {ev['seq']}"}
    while len(mine) > DEDUP_MAX:
        del mine[next(iter(mine))]


# --- the snapshot of the page and clients ------------------------------------------------------------------------------

_web = {}   # (key want, folder) -> (plugin folder, main file, css) or None: the manifest is read once per its version


def web_of(want):
    """A module's page plugin (the web field in module.json): (the folder of its files, the main file relative to it, its
    stylesheets relative to it) or None. The css field (2.11) names files relative to the module folder, as web does; a
    file outside the plugin's folder could not be served at /mod/<name>/web/ and is dropped, as a bad entry is (the field
    is cosmetic: it costs the module that file, not its load)."""
    k = (want.get("key"), want.get("cwd"))
    if k not in _web:
        from core import pluginstyle
        man = modules.read(want.get("cwd") or "", "want")["manifest"]
        web = man.get("web")
        if isinstance(web, str) and not modules.web_error(web):
            folder = os.path.dirname(web)   # web_error: no backslash, no leading /
            pre = folder + "/" if folder else ""
            css = [f[len(pre):] for f in pluginstyle.css_files(man.get("css")) if f.startswith(pre)]
            _web[k] = (os.path.join(want["cwd"], folder), os.path.basename(web), css)
        else:
            _web[k] = None
    return _web[k]


def view(mods, state_dir, records=None, now=None):
    """What to add to the snapshot of the page and clients: mods and mod_versions of loaded modules (hubd holds them - the
    want files), plugins - their page plugins, claims - {keys: {key: value}, features: [...]} of the modules that are ready
    now (core/modclaims.py). records - state["modules"] of the same snapshot. No modules - {}: the snapshot as without the Module API."""
    want = pages_in_order(state_dir)
    if not want:
        return {}
    plugins, keys, features = [], {}, []
    for n, w in want:
        p = web_of(w)
        if p:
            plugins.append({"name": f"mod:{n}", "main": p[1], "base": f"/mod/{n}/web/", **({"css": p[2]} if p[2] else {})})
        claims = w.get("claims") if isinstance(w.get("claims"), dict) else {}
        launch = ready_launch(state_dir, n, (records or {}).get(n), now) if claims else None
        if not launch:
            continue
        mine = (mods or {}).get(n) or {}
        for k in claims.get("keys") or []:
            if k not in keys and (mine.get(k) or {}).get("launch") == launch:   # written by this launch
                keys[k] = mine[k]["value"]
        features += [f for f in claims.get("features") or [] if f not in features]
    return {"mods": {n: {k: {"value": r["value"], "rev": r["rev"]} for k, r in mods[n].items()}
                     for n, _ in want if n in (mods or {})},
            "mod_versions": {n: str(w.get("version") or "") for n, w in want}, "plugins": plugins,
            "claims": {"keys": keys, "features": features}}


def ready_launch(state_dir, name, rec, now=None):
    """The module's launch, if it is ready - it answered hello with this process launch and is neither paused nor stopped
    (as Modules.ready, but by the record from the same snapshot); otherwise None."""
    import time
    rec = rec or {}
    run = modrun.run_of(state_dir, name) or {}
    if not rec.get("ready") or rec["ready"] != run.get("launch"):
        return None
    return rec["ready"] if modrun.status(rec, time.time() if now is None else now) == "ok" else None


def web_styles(state_dir):
    """[(plugin name, address)] of the loaded modules' stylesheets (the css field), in the snapshot's order: the page puts
    them into its <head> (modules/web/server.py ext_style_links) at the same address hub.addStyle builds."""
    return [(f"mod:{n}", f"/mod/{n}/web/{f}") for n, w in pages_in_order(state_dir) for f in (web_of(w) or (None, None, []))[2]]


def pages_in_order(state_dir):
    """[(name, want)] of the loaded modules in the snapshot's order (order, then name): view and web_styles walk the same."""
    return sorted(modpages.pages(state_dir).items(), key=lambda x: (x[1].get("order", 0), x[0]))


def web_file(state_dir, name, rel):
    """A loaded module's plugin file by a path inside its folder, or None (foreign, escaping the folder, no such file)."""
    w = modpages.one(state_dir, name)
    p = web_of(w) if w else None
    if not p or not rel:
        return None
    root = os.path.realpath(p[0])
    real = os.path.realpath(os.path.join(root, rel))
    return real if real.startswith(root + os.sep) and os.path.isfile(real) else None
