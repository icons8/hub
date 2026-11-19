"""Link two hubs in one action (spec hubbus/ux §3.5, §4.2, §4.4, §10, §11): the owner's path as functions any process of
the owner calls — the terminal (`hub bus link`, `hub bus join -`, core/buscli.py), the page server and the Telegram bot.

`Flow.ensure` makes the bus work whole: OpenSSH, the cryptography library in `<state>/pylib`, its self-check, the keys
(the core's `bus.on_requested`), the card, the relay — and reports every step as the owner's fact `bus.setup_step` (the
core keeps the last run in `bus.setup`: the page shows the progress, `hub doctor` the last failure); a failed step
names its reason and a ready command. `link` makes a link on A, `join` takes another hub's link on B — or the relay
operator's invitation (the first hub of a relay, `hub-relay-invite-1:`); `reset` drops the hub's identity.

The core decides everything as before (core/busstate.py, core/busd.py): these functions put the owner's requests and read
the core's answers — the decision facts in the journal, the progress in the view the core writes (`bus/view.json`). The
slow part (pip) runs here, in the owner's process, never in the core's tick. The secrets stay where they are: a pasted
link goes to the core as a file 0600, never into an argument, the journal or a log.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time

import platforms
from core import bus, busd, busrelay, busstate, catalog, coretext, i18n, journal

WAIT = 20              # seconds for the core's decision on a request
STEPS = busstate.SETUP_STEPS
INSTALL_TIMEOUT = 900  # pip on a slow line
POSTED_WAIT = 30       # seconds A waits for its link's permit to be recorded on the relay
JOIN_WAIT = 120        # seconds a caller watches B's join before it says «it finishes by itself»
JOIN_END = ("linked", "refused", "expired", "cancelled", "cancelling", "forgotten")
# what pip or uv print when the network, a proxy or PyPI is the cause (not the library or the interpreter)
NETWORK = re.compile(r"(?i)failed to establish a new connection|temporary failure in name resolution|name or service "
                     r"not known|nodename nor servname|getaddrinfo failed|network is unreachable|connection (refused|"
                     r"reset|timed out|aborted)|proxyerror|tunnel connection failed|read timed out|sslerror|"
                     r"certificate verify failed|could not fetch url|dns error|error sending request|tcp connect error|"
                     r"failed to fetch")
NO_PIP = re.compile(r"(?i)no module named pip|ensurepip is disabled")
# the three kinds of text the one paste box takes (spec §3.3): a link, a relay invitation, a card (the advanced way)
KINDS = (("link", bus.LINK_PREFIX, r"[A-Za-z0-9_-]"), ("invite", busrelay.INVITE_PREFIX, r"[A-Za-z0-9_.-]"),
         ("card", bus.CARD_PREFIX, r"[A-Za-z0-9+/=]"))


def invite_like(text):
    """Does a text carry the start of a link, an invitation or a card: such a paste is no name of a person — the name field
    of the page cuts it at 100 characters, and the cut piece is no longer a link the paste box would know."""
    low = str(text).casefold()
    return any(prefix.casefold() in low for _, prefix, _ in KINDS)


def people():
    """The records of the owner's contacts book for the page's «Whose hub»: [{name, kinds}] — the name and the kinds of
    their paths (Telegram, Teams, e-mail), never a value."""
    from modules.contacts import contacts as book
    try:
        rows = book.people(book.default_path())
    except (OSError, ValueError):
        return []
    out = []
    for r in rows:
        kinds = [k for k, has in (("Telegram", r.get("telegram")), ("Teams", r.get("teams") or r.get("teams_chat")),
                                  ("email", r.get("email"))) if has]
        out.append({"name": r["name"], "kinds": kinds})
    return sorted(out, key=lambda p: p["name"].casefold())


def person_for(spec):
    """«Whose hub» from the page or the terminal: {"name": <a record of the book>} or {"new": "<name>"} (a record made
    now, confirmed by the owner's pick) → ({id, name}, None) or (None, code). The record gets its id under the book's
    lock, as `hub contacts link` does; a book is the owner's — the core is only told the id and the name."""
    from modules.contacts import contacts as book
    if not isinstance(spec, dict) or (("name" in spec) == ("new" in spec)):
        return None, "bad_person"
    path = book.default_path()
    if "new" in spec:
        name = " ".join(str(spec["new"] or "").split())
        if not name or not busstate.alias_ok(name) or invite_like(name):
            return None, "bad_person"

        def make(rows):
            q = name.casefold()
            hits = [r for r in rows if isinstance(r, dict) and isinstance(r.get("name"), str) and r["name"].casefold() == q]
            if len(hits) > 1:
                return None
            if hits:
                r = hits[0]
            else:
                r = {"name": name, "aliases": [], "source": "owner, hub on the bus",
                     "confirmed": time.strftime("%Y-%m-%d")}
                rows.append(r)
            if not r.get("id") or sum(1 for x in rows if isinstance(x, dict) and x.get("id") == r["id"]) > 1:
                r["id"] = book.new_id(rows)
            return {"id": r["id"], "name": r["name"]}
        person = book.update(path, make)
        return (person, None) if person else (None, "person_many")
    q = str(spec["name"] or "").strip().casefold()

    def pick(rows):
        hits = [r for r in rows if isinstance(r, dict) and isinstance(r.get("name"), str)
                and any(isinstance(k, str) and k.casefold() == q
                        for k in [r["name"], *(r.get("aliases") if isinstance(r.get("aliases"), list) else [])])]
        if len(hits) != 1:
            return len(hits)
        r = hits[0]
        if not r.get("id") or sum(1 for x in rows if isinstance(x, dict) and x.get("id") == r["id"]) > 1:
            r["id"] = book.new_id(rows)
        return {"id": r["id"], "name": r["name"]}
    got = book.update(path, pick)
    if isinstance(got, dict):
        return got, None
    return None, "person_none" if got == 0 else "person_many"


PUBLIC_FILE = "/v1/relay.json"   # the open build's pages: the public relay's line (tools/package/pages.py --relay)
PUBLIC_KEEP = (3600, 300)        # seconds a line read is kept, and a miss (no file, no network)
PUBLIC_TIMEOUT = 10
_public = {}                     # state dir -> (when, the line or None): monotonic seconds
_public_lock = threading.Lock()
_public_reading = set()          # state dirs whose line is being read in the background (the page's view)


def public_line(state_dir, clock=time.monotonic):
    """The public relay's line the hub's own install names (spec public relay §3.1): the open build's pages — the hub's
    install source, its updates come from there too — carry `v1/relay.json` {"invite": <an operator's line>}. None:
    the team build (its source has a key: its hubs stay on the team's relay), a hub from git (no source), no such file,
    no answer, a wrong one. Kept an hour (a miss — five minutes); a line the relay refused is dropped (forget_public)."""
    got = _public.get(state_dir)
    if got and 0 <= clock() - got[0] < PUBLIC_KEEP[got[1] is None]:
        return got[1]
    line = _read_public(state_dir)
    _public[state_dir] = (clock(), line)
    return line


def forget_public(state_dir):
    _public.pop(state_dir, None)


def public_relay_now(state_dir, clock=time.monotonic):
    """(the public relay's address or None, whether it is being read) without waiting for the network: the page's view
    answers at once and asks again while it is read (a thread, one at a time)."""
    got = _public.get(state_dir)
    if got and 0 <= clock() - got[0] < PUBLIC_KEEP[got[1] is None]:
        return (busrelay.read_invite(got[1])[0]["relay"] if got[1] else None), False
    if _source(state_dir) is None:   # the team build, a hub from git: decided here, no network, no thread
        _public[state_dir] = (clock(), None)
        return None, False
    with _public_lock:
        if state_dir in _public_reading:
            return None, True
        _public_reading.add(state_dir)

    def read():
        try:
            public_line(state_dir, clock)
        except Exception:  # noqa: BLE001 — whatever it was, a miss: the page stops asking until the miss is old
            _public[state_dir] = (clock(), None)
        finally:
            with _public_lock:
                _public_reading.discard(state_dir)
    threading.Thread(target=read, name="public-relay", daemon=True).start()
    return None, True


def _source(state_dir):
    """The open build's pages this hub was installed from (no key), or None."""
    from modules.update import layer
    src = layer.source(state_dir, "open")
    return src["url"] if src and not src.get("key") else None


def _read_public(state_dir):
    from core import fetch
    base = _source(state_dir)
    if base is None:
        return None
    try:   # a redirect only to the pages' own host or the release's (fetch.asset_ok), as the update's own reads
        status, body = fetch.small(base + PUBLIC_FILE, allow=lambda u: fetch.asset_ok(u, base), timeout=PUBLIC_TIMEOUT)
        text = json.loads(body.decode("utf-8")).get("invite") if status == 200 else None
        busrelay.read_invite(text)   # the form and an address a hub takes (https; http only to this machine)
    except (fetch.Failed, ValueError, AttributeError, RecursionError, busrelay.Error):
        return None
    return text


def public_relay(state_dir):
    """The address of the public relay the hub's install names, or None (the page shows it before «Link»)."""
    line = public_line(state_dir)
    return busrelay.read_invite(line)[0]["relay"] if line else None


def no_name():
    """The refusal of a link or a join while the hub has no name of its own for the other hubs (the owner's setting)."""
    return Done("no_name", why("no_name"), command='hub bus name "<name>"')


class Done:
    """What a step of the path came to: `code` "ok" or a refusal code; `text` — the words for the owner; `command` — a
    ready command the owner may run himself (a failed install); `value` — the result (the link's text); `data` — the
    rest (the link's id and term, the join's step, the pair code)."""

    def __init__(self, code="ok", text="", command=None, value=None, step=None, **data):
        self.code, self.text, self.command, self.value, self.step, self.data = code, text, command, value, step, data

    @property
    def ok(self):
        return self.code == "ok"

    def __repr__(self):
        return f"Done({self.code!r}, {self.text!r}, step={self.step!r}, data={self.data!r})"


def invite_why(reason, t):
    """The words for a refused relay invitation by the reason: ours (the line is not one — read_invite) or the relay's
    (core/busrelay.Error text of its `bad_invite`). Only «used» may say it was intercepted; an unknown reason is a plain
    refusal, never that."""
    reason = str(reason or "")
    if reason in ("not an invitation", "base64url", "the invitation's fields", "the relay's key"):
        return t("bus.flow.invite.form")
    if reason == "the invitation was used":
        return t("bus.flow.code.used")
    if reason == "the invitation expired":
        return t("bus.flow.code.expired")
    if reason == "the invitation was withdrawn":
        return t("bus.flow.invite.withdrawn")
    if reason == "no such invitation":
        return t("bus.flow.invite.unknown")
    if reason == "the invitation is for another relay":
        return t("bus.flow.invite.other_relay")
    if reason == "the hub that made the invitation is not here":
        return t("bus.flow.invite.maker_gone")
    if reason == "the invitation's signature does not check":
        return t("bus.flow.invite.signature")
    return t("bus.flow.invite.refused")


def why(code, reason="", lang=None):
    """The owner's words for a refusal code of the path: the library, the relay, a link or a join (literal keys: the
    i18n checks find them by text). Unknown codes — the generic text and the code. `lang`: the language of the state
    for a text the fold writes (the process's own language otherwise)."""
    t = i18n.t if lang is None else (lambda key, **params: i18n.tr(lang, key, **params))
    texts = {
        "no_ssh": t("bus.flow.code.no_ssh"), "no_lib": t("bus.flow.code.no_lib"), "old_lib": t("bus.flow.code.no_lib"),
        "no_hpke": t("bus.flow.code.no_lib"), "no_network": t("bus.flow.code.no_network"),
        "no_pip": t("bus.flow.code.no_pip"), "install_failed": t("bus.flow.code.install_failed"),
        "selfcheck": t("bus.flow.code.selfcheck"), "keys_broken": t("bus.flow.code.keys_broken"),
        "keys_exist": t("bus.flow.code.keys_broken"), "keygen": t("bus.flow.code.keygen"),
        "key_changed": t("bus.flow.code.key_changed"), "no_relay": t("bus.flow.code.no_relay"),
        "relay_other": t("bus.flow.code.relay_other"), "other_relay": t("bus.flow.code.relay_other"),
        "old_relay": t("bus.flow.code.old_relay"), "network": t("bus.flow.code.network"),
        "joins_day": t("bus.flow.code.relay_limit"), "joins_open": t("bus.flow.code.relay_limit"),
        "joins_addr": t("bus.flow.code.relay_limit"), "full": t("bus.flow.code.relay_limit"),
        "unavailable": t("bus.flow.code.network"), "bad_link": t("bus.flow.code.bad_link"),
        "bad_invite": invite_why(reason, t), "used": t("bus.flow.code.used"), "revoked": t("bus.flow.code.used"),
        "expired": t("bus.flow.code.expired"), "stale": t("bus.flow.code.expired"), "own_link": t("bus.flow.code.own_link"),
        "own_card": t("bus.flow.code.own_link"), "old_hub": t("bus.flow.code.old_hub"),
        "blocked": t("bus.flow.code.blocked"), "pasted": t("bus.flow.code.pasted"), "lost": t("bus.flow.code.lost"),
        "bad_signature": t("bus.flow.code.bad_signature"), "not_a_link": t("bus.flow.code.not_a_link"),
        "card": t("bus.flow.code.card"), "no_name": t("bus.flow.code.no_name"),
        "bad_person": t("bus.flow.code.bad_person"), "person_none": t("bus.flow.code.person_none"),
        "person_many": t("bus.flow.code.person_many"), "bad_label": t("bus.flow.code.bad_label"), "no_answer": t("bus.flow.code.no_answer"), "reader_busy": t("bus.flow.code.reader_busy"),
        "queued": t("bus.flow.code.queued"), "owner_said_no": t("bus.flow.code.owner_said_no"),
        "offers_full": t("bus.flow.code.offers_full"), "no_card": t("bus.flow.code.no_card"), "declined": t("bus.flow.code.declined"), "no_tty": t("bus.flow.code.no_tty"),
        "rejected": t("bus.flow.code.rejected"), "secret": t("bus.flow.code.secret"),
        "pair_code_unknown": t("bus.flow.code.pair_code_unknown"), "code_mismatch": t("bus.flow.code.code_mismatch"),
        "code_needed": t("bus.flow.code.code_needed"),
        "not_verified": t("bus.flow.code.not_verified"), "no_contact": t("bus.flow.code.no_contact"),
        "error": t("bus.flow.code.error")}
    text = texts.get(code) or t("bus.flow.code.other", code=code)
    # the core's phrases of a refusal in the language of the words around them
    return f"{text} ({coretext.human(reason, lang)})" if reason and code in ("install_failed", "selfcheck", "keys_broken", "keygen", "error",
                                                       "rejected", "card", "key_changed", "network", "other_relay",
                                                       "relay_other") else text


def waiting_request(state_dir, type_, digest):
    """True while a request of this type for this very link (its digest) lies in the core's mailbox, not yet recorded: a
    repeat of the same paste (or of the same offer) must not make a second one."""
    return (type_, digest) in busd.queued_digests(state_dir)


def find(text):
    """(kind, the token) of the first link, relay invitation or card in a pasted text — a message may carry words around
    it and a messenger may break a long line (a continuation counts only if it is a run of 16 or more of the token's
    characters, so a word after it is not glued on); (None, None) if there is none."""
    text = str(text or "")
    best = None
    for kind, prefix, chars in KINDS:
        m = re.search(re.escape(prefix) + r"\s*(" + chars + r"+(?:[ \t]*\r?\n[ \t]*" + chars + r"{16,})*)", text)
        if m and (best is None or m.start() < best[0]):
            token = "".join(m.group(1).split())
            # an invitation's signature never ends with «.»: that is the full stop of the sentence it was pasted in
            best = (m.start(), kind, prefix + (token.rstrip(".") if kind == "invite" else token))
    return (best[1], best[2]) if best else (None, None)


def kind_of(text):
    return find(text)[0]


def preview(text, bus_state, now):
    """What B's owner sees before «Link» (spec §3.3): A's name, its hub code, the relay of the link, the term and what
    this hub still needs. The card's signature and the term are checked (ssh-keygen); bus.Error with a code otherwise —
    a link of this very hub is `own_link`, a link to another relay than this hub's is `other_relay`."""
    got = bus.read_link(text, now)
    card, me = got["card"], (bus_state.get("me") or {})
    if card["fp"] == me.get("fp"):
        raise bus.Error("own_link", "")
    relay = got["invite"]["relay"] if got["invite"] else None
    mine = bus_state.get("relay") or {}
    if got["invite"] and mine and mine.get("key") != got["invite"]["key"]:
        raise bus.Error("other_relay", i18n.t("bus.flow.other_relay", url=got["invite"]["relay"], mine=mine.get("url")))
    needs = []
    if not bus_state.get("enabled"):
        needs.append("enable")
    if not mine and relay:
        needs.append("relay")
    return {"id": got["id"], "label": card["label"], "fp": card["fp"], "code": card["code"], "relay": relay,
            "exp": got["exp"], "needs": needs}


def _run(cmd):
    """pip or uv: (exit code, what it printed — the tail)."""
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                           timeout=INSTALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError as e:
        return 127, str(e)
    return r.returncode, r.stdout.decode("utf-8", "replace")[-4000:]


def ask(put, state_dir, rejected, type_, data, wait=WAIT):
    """The owner's request and the core's decision on it: (code, reason, the decision fact) — "ok", a refusal code,
    "rejected" (the fold refused it) or None (no answer in time). The decision is the core's `core.bus_*` fact with `of` =
    the request's number, read from what the journal appends (not the state, which is big)."""
    path = os.path.join(state_dir, "journal.jsonl")
    start = journal.mark(path)
    seq = put(type_, None, data, say=False)
    if seq is None:
        return None, "", None
    if seq in rejected:
        return "rejected", rejected[seq], None
    if busstate.folded_at_once(type_, data):   # folded from the request itself
        return "ok", "", None
    hit = journal.waited(path, start, lambda e: str(e.get("type", "")).startswith("core.bus_")
                         and (e.get("data") or {}).get("of") == seq, wait)
    if hit is None:
        return None, "", None
    # the core writes a fact its fold refuses together with the refusal (daemon.fold_write): one pass over the same
    refusal = journal.waited(path, start, lambda e: e.get("type") == "core.rejected"
                             and (e.get("data") or {}).get("of") == hit.get("seq"), 0)
    if refusal is not None:
        return "rejected", coretext.why_of(refusal["data"]), hit   # with the marks of its core phrases
    if hit["type"] in ("core.bus_request_failed", "core.bus_send_refused"):   # the chat's refusal: a key-like text, …
        return hit["data"].get("code") or "error", hit["data"].get("reason") or "", hit
    return "ok", "", hit


class Flow:
    """The owner's path from one process (the terminal, the page server, the bot).

    put(type, entity, data, say=False) → seq: the owner's fact (its channel — the caller's); current() → the state (read
    once per call, not polled); note(type, data): a progress fact that is not waited for (default: put); say(step,
    state, text): the caller's live display; run(cmd) → (code, output): pip; idle(): between two looks at the core's
    view (default half a second; the tests' stand runs the cores there)."""

    def __init__(self, put, current, state_dir, rejected=None, note=None, say=None, run=None, idle=None,
                 config_dir=None, clock=time.time, wait=WAIT):
        self.put, self.current, self.state_dir = put, current, state_dir
        self.rejected = rejected if rejected is not None else {}
        self.note = note or (lambda type_, data: self.put(type_, None, data, say=False))
        self.say = say or (lambda step, state, text: None)
        self.run = run or _run
        self.idle = idle or (lambda: time.sleep(0.5))
        self.config_dir, self.clock, self.wait = config_dir, clock, wait

    # --- the core's answers and view ---------------------------------------------------------------------------------

    def ask(self, type_, data):
        code, reason, hit = ask(self.put, self.state_dir, self.rejected, type_, data, self.wait)
        if code is None:
            return Done("no_answer", why("no_answer")), None
        if code != "ok":
            return Done(code, why(code, reason), reason=reason), hit
        return Done(), hit

    def view(self):
        return busd.read_view(self.state_dir) or {}

    def watch(self, pick, until, seconds):
        """pick(view) each look until until(value) or the time is over: the last value."""
        end = self.clock() + seconds
        value = pick(self.view())
        while not until(value) and self.clock() < end:
            self.idle()
            value = pick(self.view())
        return value

    def state(self):
        return self.current().get("bus") or {}

    # --- progress -----------------------------------------------------------------------------------------------------

    def step(self, name, state, code=None, reason="", command=None):
        """One step's state: to the caller's display and, as a fact, to the core (best effort: an older core refuses an
        unknown fact — the path goes on)."""
        text = why(code, reason) if code else ""
        self.say(name, state, text)
        reason, command = _printable(reason)[:300], _printable(command)[:500]   # ssh-keygen's or pip's lines
        data = {"step": name, "state": state, **({"code": code} if code else {}),
                **({"reason": reason} if reason else {}), **({"command": command} if command else {})}
        try:
            self.note("bus.setup_step", data)
        except Exception:  # noqa: BLE001 — the progress is a display, never a reason to stop
            pass

    def failed(self, name, code, reason="", command=None):
        self.step(name, "fail", code, reason, command)
        return Done(code, why(code, reason), command=command, step=name)

    # --- ensure -------------------------------------------------------------------------------------------------------

    def ensure(self, install=True, label=None, invite=None, need_relay=True):
        """Make the bus work whole (spec §11): ssh-keygen → the library (installed into `<state>/pylib` with consent:
        install) → the self-check → the keys (the core) → the card → the relay (an invitation joins it; need_relay —
        the hub must be on one: A's link carries its permit). Done ok, or the failed step with the reason and a
        command."""
        if not shutil.which("ssh-keygen"):
            return self.failed("ssh", "no_ssh", command=_ssh_command())
        self.step("ssh", "ok")
        b = self.state()
        if b.get("enabled") and b.get("crypto_ready") and not invite and not self.has_library():
            # the core's process has the library and its keys work; this process only puts requests (the link, the
            # join) — no install for it; the key files are still the core's (their signing key, read without it)
            for name in ("lib", "selfcheck", "keys"):
                self.step(name, "skip")
            if bus._sign_pub(bus.key_dir(self.config_dir)) != (b.get("me") or {}).get("sign_pub"):
                return self.failed("card", "key_changed")
            self.step("card", "ok")
            return self.relay_step(b, None, None, need_relay, Done(fp=(b.get("me") or {}).get("fp")))
        lib = self.library(install)
        if isinstance(lib, Done):
            return lib
        problem = bus.selfcheck(lib)
        if problem:
            return self.failed("selfcheck", "selfcheck", problem)
        self.step("selfcheck", "ok")
        b = self.state()   # again: an install takes minutes, the core may have moved meanwhile
        if not b.get("enabled") or b.get("crypto_ready") is False:
            self.step("keys", "run")
            done, _ = self.ask("bus.on_requested", {"label": label} if label else {})
            if not done.ok:
                return self.failed("keys", done.code, done.data.get("reason") or "")
            b = self.state()
            if b.get("crypto_ready") is False:
                f = b.get("failed") or {}
                return self.failed("keys", f.get("code") or "error", f.get("reason") or "")
        self.step("keys", "ok")
        try:
            keys = bus.load(bus.key_dir(self.config_dir), self.state_dir)
            if keys.fp != (b.get("me") or {}).get("fp"):
                raise bus.Error("key_changed", keys.fp)
            bus.make_card(keys, bus.hub_label(b.get("me"), catalog.config_or_last(self.state_dir)), self.clock())
        except bus.Error as e:
            return self.failed("card", e.code if e.code in ("key_changed", "keys_broken", "no_ssh") else "card", e)
        self.step("card", "ok")
        return self.relay_step(b, invite, keys, need_relay, Done(fp=keys.fp))

    def relay_step(self, b, invite, keys, need_relay, done):
        """The relay: an invitation is always taken (another relay's is refused; the same relay's spends nothing);
        without one — the hub's relay, or a refusal that names the invitation (need_relay), or nothing to do."""
        relay = b.get("relay")
        if invite and relay:   # already on a relay: the same one spends nothing, another one is refused
            try:
                inv = busrelay.read_invite(invite)[0]
            except busrelay.Error as e:
                return self.failed("relay", "bad_invite", str(e))
            if inv["key"] != relay.get("key"):
                return self.failed("relay", "relay_other", relay.get("url"))
            self.step("relay", "ok")
            done.data.update(url=relay.get("url"), by=relay.get("by") or "")
        elif invite:
            joined = self.relay_join(invite, b, keys)
            if not joined.ok:
                return joined
            done.data.update(joined.data)
        elif relay:
            self.step("relay", "ok")
        elif need_relay and keys is not None and (line := public_line(self.state_dir)):
            # a hub on no relay that needs one (A's «Link»): the public relay its install names, as if pasted (spec
            # public relay §3.1); b is the state read after the install, so a relay joined meanwhile is the one above.
            # keys None — this process has no library (the core's has): no install here, the paste as before
            joined = self.relay_join(line, b, keys)
            if not joined.ok:
                if joined.code == "bad_invite":   # the line used up or withdrawn: the pages may name a new one
                    forget_public(self.state_dir)
                return joined
            done.data.update(joined.data)
        elif need_relay:
            return self.failed("relay", "no_relay")
        else:
            self.step("relay", "skip")
        return done

    def has_library(self):
        try:
            bus.crypto(self.state_dir)
            return True
        except bus.Unavailable:
            return False

    def library(self, install):
        """The library object, or the failed step: missing and install — pip (or uv) puts it into `<state>/pylib` with
        the interpreter of this process (bus.install_command); the network, no pip, a failed install — each its words."""
        try:
            lib = bus.crypto(self.state_dir)
            self.step("lib", "ok")
            return lib
        except bus.Unavailable as e:
            first = e
        cmd = bus.install_command(self.state_dir)
        line = platforms.current.join_command(cmd)
        if not install:
            return self.failed("lib", first.code, str(first), command=line)
        self.step("lib", "run", reason=line)
        rc, out = self.run(cmd)
        if rc != 0:
            code = "no_network" if NETWORK.search(out) else "no_pip" if NO_PIP.search(out) else "install_failed"
            tail = " ".join(out.strip().splitlines()[-3:])[:300]
            return self.failed("lib", code, tail if code == "install_failed" else "", command=line)
        bus._LIB = None
        try:
            lib = bus.crypto(self.state_dir)
        except bus.Unavailable as e:
            return self.failed("lib", e.code, str(e), command=line)
        self.step("lib", "ok")
        return lib

    def relay_join(self, text, b=None, keys=None):
        """Register on the relay an invitation names (the operator's or another hub's): the registration first (the
        invitation is spent on the relay; a repeat with the same keys spends nothing), then the core records the relay —
        a cut between them is mended by repeating."""
        b = b or self.state()
        try:
            inv = busrelay.read_invite(text)[0]
        except busrelay.Error as e:
            return self.failed("relay", "bad_invite", str(e))
        old = b.get("relay")
        if old and old.get("key") != inv["key"]:
            return self.failed("relay", "relay_other", old.get("url"))
        self.step("relay", "run", reason=inv["relay"])
        try:
            keys = keys or bus.load(bus.key_dir(self.config_dir), self.state_dir)
            key_file, _ = busrelay.transport_key(self.config_dir, make=True)
            got = busrelay.Client(inv["relay"], inv["key"], keys.fp, key_file).join(keys, text, (b.get("me") or {})
                                                                                   .get("label"))
        except busrelay.Error as e:
            return self.failed("relay", e.code or "network", str(e))
        except bus.Error as e:
            return self.failed("relay", e.code, str(e))
        done, _ = self.ask("bus.relay_requested", {"op": "join", "invite": text})
        if not done.ok:
            return self.failed("relay", done.code, done.data.get("reason") or "")
        self.step("relay", "ok")
        return Done(url=inv["relay"], by=got.get("invited_by") or "")

    # --- A: a link --------------------------------------------------------------------------------------------------

    def link(self, ttl=bus.LINK_TTL, install=True, label=None, posted_wait=None, person=None):
        """«Link with a hub» on A: ensure, then the core makes the link (its text — a file 0600 the core wrote, read
        here and returned, never logged); then a short wait until A's transport records the link's permit on the relay
        (B registers with it). Done(value=<the link>, id, exp, posted). `label` — the hub's name for this link alone; the
        hub's own name (the owner's setting) is required: without it, nothing is made."""
        name = self.state().get("name")
        if not name:
            return no_name()
        label = (label or "").strip() or None
        if label and not busstate.alias_ok(label):
            return Done("bad_label", why("bad_label"))
        done = self.ensure(install=install, label=name)
        if not done.ok:
            return done
        self.say("link", "run", "")
        done, hit = self.ask("bus.link_requested", {"ttl": ttl, **({"label": label} if label and label != name else {}),
                                                    **({"person": person} if person else {})})
        if not done.ok:
            self.say("link", "fail", done.text)
            return done
        lid = (hit.get("data") or {}).get("id")
        try:
            with open(busd.link_path(self.state_dir, lid), encoding="ascii") as f:
                text = f.read().strip()
        except (OSError, TypeError):
            return Done("lost", why("lost"))
        # the view the core writes at its tick may not have the link yet: an empty record is «not known», never «done»
        rec = self.watch(lambda v: (v.get("links") or {}).get(lid) or {},
                         lambda r: bool(r) and (r.get("posted") or not r.get("invite") or r.get("state") != "open"),
                         POSTED_WAIT if posted_wait is None else posted_wait)
        if rec.get("state") == "failed":   # the relay refused the permit for good (a quota): the link is useless
            code = rec.get("code") or "error"
            self.say("link", "fail", why(code))
            return Done(code, why(code), id=lid)
        self.say("link", "ok", "")
        return Done(value=text, id=lid, exp=hit["data"].get("exp"),
                    posted=bool(rec.get("posted")) or (bool(rec) and not rec.get("invite")))

    def revoke(self, lid):
        """«Revoke»: the link closes on A and its permit leaves the relay (the core's job)."""
        done, _ = self.ask("bus.link_revoke_requested", {"id": lid})
        return done

    # --- B: a pasted text ---------------------------------------------------------------------------------------------

    def join(self, text, install=True, wait=JOIN_WAIT, person=None):
        """What B's owner pasted, by its kind: a relay invitation — ensure with it (the first hub of a relay); a link —
        ensure without a relay of its own (the link's permit registers B), the link to the core as a file 0600, then
        B's steps until A answers (or `wait` is over: the core finishes by itself). A card is the advanced way
        (`hub bus contact add`): refused with the command. The hub's own name (the owner's setting) is required."""
        kind, token = find(text)
        name = self.state().get("name")
        if kind in ("link", "invite") and not name:
            return no_name()
        if kind == "invite":
            return self.ensure(install=install, label=name, invite=token)
        if kind == "card":
            return Done("card", why("card"), command="hub bus contact add <card> --as <name>")
        if kind != "link":
            return Done("not_a_link", why("not_a_link"))
        b = self.state()
        try:
            info = preview(token, b, self.clock())
        except bus.Error as e:
            return Done(e.code, why(e.code, str(e) if e.code == "other_relay" else ""))
        raw = token.encode("ascii")
        digest = hashlib.sha256(raw).hexdigest()
        if waiting_request(self.state_dir, "bus.join_requested", digest):   # the first press is still in the mailbox: the core will carry it out
            return Done("queued", why("queued"), id=info["id"], label=info["label"])
        done = self.ensure(install=install, label=name, need_relay=False)
        if not done.ok:
            return done
        folder = busd.join_dir(self.state_dir)
        platforms.current.makedirs_private(folder)
        path = os.path.join(folder, digest + ".link")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        self.say("join", "run", info["label"])
        done, hit = self.ask("bus.join_requested", {"path": path, "sha256": digest,
                                                    **({"person": person} if person else {})})
        if done.code == "no_answer":   # the request is in the mailbox or recorded: the core reads the file when it gets to
            # it (and takes it away or removes it itself; a file nobody took within an hour goes with its tick)
            return Done("queued", why("queued"), id=info["id"], label=info["label"])
        if not done.ok:
            try:
                os.unlink(path)   # the core takes the file away when it reads it; refused before that — ours to remove
            except OSError:
                pass
            return Done(done.code, why(done.code, done.data.get("reason") or ""))
        jid = (hit.get("data") or {}).get("id") or info["id"]
        seen = [None]

        def pick(v):
            j = (v.get("joins") or {}).get(jid) or {}
            mark = (j.get("step"), j.get("state"), j.get("intro"))
            if mark != seen[0]:
                seen[0] = mark
                if j:
                    self.say("join", j.get("step") or "", j.get("state") or "")
            return j
        j = self.watch(pick, lambda r: r.get("state") in JOIN_END, wait)
        # the pair code exists once the link is done on this side (the commitment's code, core/bus.contact_code)
        pair = bus.contact_code((self.state().get("contacts") or {}).get(j.get("name") or "")) \
            if j.get("state") == "linked" else None
        common = {"id": jid, "label": info["label"], "pair_code": pair, "step": j.get("step"), "state": j.get("state"),
                  "name": j.get("name")}
        if j.get("state") == "linked":
            return Done(**common)
        if j.get("state") in ("refused", "cancelled", "forgotten", "cancelling"):
            code = j.get("code") or j.get("state")
            return Done(code, why(code), **common)
        if j.get("state") == "expired":
            return Done("expired", why("expired"), **common)
        return Done("waiting", i18n.t("bus.flow.waiting_a", label=info["label"]), **common)

    # --- reset --------------------------------------------------------------------------------------------------------

    def reset(self):
        """`hub bus reset` (spec §10): the hub leaves its relay (best effort — only the operator removes a hub that
        cannot), then the core drops the identity: the bus goes off, the contacts, links and joins go, the keys are
        removed (the core's action; the next start makes new ones — another fingerprint)."""
        b = self.state()
        relay, me = b.get("relay"), (b.get("me") or {}).get("fp")
        done, _ = self.ask("bus.reset_requested", {})
        if not done.ok:
            return done
        # only after the core dropped the identity: a refused reset keeps the hub on its relay. The request is signed
        # by the transport key, which the reset keeps
        left = None
        if relay and me:
            try:
                found = busrelay.transport_key(self.config_dir)
                if found is not None:
                    busrelay.Client(relay["url"], relay["key"], me, found[0]).leave()
                    left = True
            except (busrelay.Error, bus.Error, OSError):
                left = False
        return Done(left=left, fp=me)

    # --- the owners' chat and the check ------------------------------------------------------------------------------

    def chat(self, contact, text, confirm=False):
        """«Tell the hub»: the owner's words to that hub's liaison — an ordinary message, from him, without a card (his press
        is the word); a key-like text is refused (`secret`) until he confirms. The answer in its thread is for him: the
        page and Telegram, no turn of this hub's liaison."""
        done, _ = self.ask("bus.chat_requested", {"contact": contact, "text": text, "to_hub": True,
                                                  **({"confirm": True} if confirm else {})})
        return done

    def verify(self, contact, code=None):
        """«Matches»: the owner compared the pair code on both screens (or typed it)."""
        done, _ = self.ask("bus.contact_requested", {"op": "verify", "name": contact, **({"code": code} if code else {})})
        return done


def _ssh_command():
    return platforms.current.ssh_install_hint()


# --- hub doctor ------------------------------------------------------------------------------------------------------

def doctor_rows(state_dir, st, config_dir=None, now=None):
    """[(ok|warn|fail, what, details)] of the hub bus for `hub doctor`: off — one line; on — the keys and the library
    as the core sees them, OpenSSH, the relay and its transport, the links and joins in progress, the last failed setup
    step with its command."""
    now = now or time.time()
    b = (st or {}).get("bus") or {}
    what = i18n.t("bus.doctor.what")
    rows = []
    setup = b.get("setup") or {}
    failed = next((s for s in (setup.get("steps") or {}).values() if s.get("state") == "fail"), None)
    last = [("warn", what, i18n.t("bus.doctor.setup_failed", step=failed.get("step"),
                                  reason=why(failed.get("code") or "error", failed.get("reason") or ""))
             + (f" — {failed['command']}" if failed.get("command") else ""))] if failed else []
    if not b.get("enabled"):
        return [("ok", what, i18n.t("bus.doctor.off"))] + last
    me = b.get("me") or {}
    if b.get("crypto_ready") is False:
        f = b.get("failed") or {}
        rows.append(("fail", what, i18n.t("bus.doctor.broken", reason=why(f.get("code") or "error", f.get("reason") or ""))))
    else:
        rows.append(("ok", what, i18n.t("bus.doctor.on", label=me.get("label") or "-", fp=me.get("fp") or "-",
                                        n=len(b.get("contacts") or {}))))
    if not shutil.which("ssh-keygen"):
        cmd = _ssh_command()
        rows.append(("fail", what, why("no_ssh") + (f" — {cmd}" if cmd else "")))
    try:
        bus.crypto(state_dir)
    except bus.Unavailable as e:   # this terminal's Python; the core's may have it — still the way to install it
        rows.append(("warn", what, i18n.t("bus.doctor.no_lib_here", why=str(e),
                                          command=platforms.current.join_command(bus.install_command(state_dir)))))
    relay = b.get("relay")
    if not relay:
        rows.append(("warn", what, why("no_relay")))
    else:
        part = busd.read_relay_status(state_dir) or {}
        err = part.get("error") or {}
        if relay.get("bus_v") is not None and relay["bus_v"] < 2:
            rows.append(("warn", what, why("old_relay") + f" ({relay.get('url')})"))
        if err and (err.get("ts") or 0) >= (part.get("ok") or 0):
            rows.append(("warn", what, i18n.t("bus.doctor.relay_error", url=relay.get("url"), code=err.get("code"),
                                              text=str(err.get("text") or "")[:200])))
        else:
            rows.append(("ok", what, i18n.t("bus.doctor.relay_ok", url=relay.get("url"),
                                            when=_when(part.get("ok")) if part.get("ok") else "-")))
    links = [r for r in (b.get("links") or {}).values() if r.get("state") == "open" and (r.get("exp") or 0) > now]
    joins = [j for j in (b.get("joins") or {}).values() if j.get("state") in ("active", "expired", "cancelling")]
    if links or joins:
        rows.append(("ok", what, i18n.t("bus.doctor.pending", links=len(links), joins=len(joins))))
    return rows + last


# --- the page (spec hubbus/ux §3.2, §3.3, §3.6, §7, §8) --------------------------------------------------------------

# an outgoing message's state in the core, as the owner reads it under his words (spec §8)
OUT_SHOWN = {"requested": "queued", "signed": "queued", "held": "held", "handed": "sent", "delivered": "delivered",
             "expired": "expired", "refused": "refused"}
CHAT_ROWS = 200   # the last rows of one contact's feed on the page


def page_view(st, now=None):
    """What the page's «Hubs» tab and a contact's page read: whether the bus is on and works, this hub, its relay, the
    last setup run, the open links and the joins in progress, the contacts with their pair code, the check and the
    rights. No keys, no envelopes, no link texts (they are secrets: the link comes only in the answer to its owner)."""
    now = now or time.time()
    b = (st or {}).get("bus") or {}
    me, relay = b.get("me") or {}, b.get("relay") or None
    sessions = (st or {}).get("sessions") or {}
    contacts, came = [], _owner_words(b)
    for name, c in sorted((b.get("contacts") or {}).items()):
        chat = came.get(name) or []
        contacts.append({
            "name": name, "label": busstate.shown(c) or name, "alias": c.get("alias"), "stated": c.get("label"),
            "status": c.get("status"), "verified": bool(c.get("verified")), "via": c.get("via"), "code": bus.contact_code(c), "linked_at": c.get("linked_at"),
            # our introduction went and the other hub's answer (it carries the pair code) has not come back yet
            "code_wait": bus.contact_code(c) is None and not c.get("verified") and busstate.waiting_code(b, c),
            "person": (c.get("person") or {}).get("name") if isinstance(c.get("person"), dict) else None,
            "agents": dict(c.get("agents") or {}),
            "v2": busstate.effective_v(b, c) >= 2, "quarantine": bool(c.get("quarantine")),
            "chat_in": len(chat), "last_in": max(chat, default=0)})
    links = [{"id": k, "state": r.get("state"), "exp": r.get("exp"), "ts": r.get("ts"), "posted": bool(r.get("posted")),
              "invite": bool(r.get("invite")), "code": r.get("code"), "label": r.get("label"),
              "person": (r.get("person") or {}).get("name") if isinstance(r.get("person"), dict) else None}
             for k, r in sorted((b.get("links") or {}).items(), key=lambda kv: kv[1].get("ts") or 0)
             if r.get("state") == "open" and (r.get("exp") or 0) > now]
    joins = [{"id": k, "state": j.get("state"), "step": j.get("step"), "label": j.get("label"), "name": j.get("name"),
              "code": j.get("code"), "exp": j.get("exp"), "ts": j.get("ts")}
             for k, j in sorted((b.get("joins") or {}).items(), key=lambda kv: kv[1].get("ts") or 0)
             if j.get("state") in ("active", "cancelling") or now - (j.get("done") or 0) < 3600]
    hub = next((n for n, s in sessions.items() if s.get("kind") == "hub" and s.get("state") != "removed"), None)
    return {"enabled": bool(b.get("enabled")), "ready": b.get("crypto_ready") is not False,
            "failed": b.get("failed") if b.get("crypto_ready") is False else None,
            "me": {"fp": me.get("fp"), "label": me.get("label"), "code": bus.code(me["fp"])} if me.get("fp") else None,
            "name": b.get("name"),   # the hub's own name for the others (the owner's setting; none — no invitation)
            "mode": b.get("mode") or "secretary",   # remote: nobody at this page, the linked hubs run it (spec d2505378, part D, remote)
            "relay": {"url": relay.get("url"), "bus_v": relay.get("bus_v")} if relay else None,
            "setup": b.get("setup") or None, "links": links, "joins": joins, "contacts": contacts,
            "hub_session": hub}


def list_view(st):
    """The «Hubs» group of the page's session list (snapshot key `bus`): only once the bus has contacts — the name, the
    label, the state of the link, how many words of the other owner came, when the hub last wrote anything and when it
    last answered the owner (the page counts the new ones itself)."""
    b = (st or {}).get("bus") or {}
    contacts = b.get("contacts") or {}
    if not contacts:
        return None
    out, came, on_way, heard, answered = [], _owner_words(b), {}, {}, {}
    for o in (b.get("out") or {}).values():   # one pass over the table (a snapshot builds this on every change)
        if o.get("status") in ("requested", "signed", "handed", "held"):
            on_way[o.get("contact")] = on_way.get(o.get("contact"), 0) + 1
    # when the hub last wrote anything — its owner or its sessions (a thread, an answer to ours): "Now" lists a hub with
    # something new by it (the page remembers what it showed); a muted word is not news
    # and when it last answered him (owner decision 18.11, "Now" by the rules of sessions): an answer in the thread of
    # his own words that came to him, or one the core gave to the session that asked it in his turn (busstate._deliver)
    # — "Now" keeps such a hub until he opens it (`seen`, the core's), as "Answered you"; any other word, as "Sent on
    # their own", for a day
    for x in (b.get("inbox") or {}).values():
        if x.get("status") != "muted":
            c, ts = x.get("contact"), x.get("ts") or 0
            heard[c] = max(heard.get(c, 0), ts)
            # (an owners' chat word of the first releases has `for: owner` with no `reply`: not an answer to him)
            if x.get("answered_owner") or (x.get("reply") and x.get("for") == "owner"):
                answered[c] = max(answered.get(c, 0), ts)
    seen = ((st or {}).get("owner") or {}).get("seen") or {}
    for name, c in sorted(contacts.items()):
        chat, waiting = came.get(name) or [], on_way.get(name, 0)
        out.append({"name": name, "label": busstate.shown(c) or name, "alias": c.get("alias"), "stated": c.get("label"),
                    "person": (c.get("person") or {}).get("name") if isinstance(c.get("person"), dict) else None,
                    "status": c.get("status"),
                    "verified": bool(c.get("verified")), "code": bus.contact_code(c), "chat_in": len(chat),
                    "last_in": max(chat, default=0), "heard": heard.get(name, 0),
                    "answered": answered.get(name, 0), "seen": seen.get(":hub:" + name, 0), "waiting": waiting})
    return {"enabled": bool(b.get("enabled")), "contacts": out}


def _owner_words(b):
    """{contact: [ts of each word of the other owner]} in one pass over the inbox."""
    out = {}
    for x in (b.get("inbox") or {}).values():
        if x.get("for") == "owner" or x.get("chat"):
            out.setdefault(x.get("contact"), []).append(x.get("ts") or 0)
    return out


def chat_rows(st, name, journal_path=None, most=CHAT_ROWS, said=None):
    """One contact's feed (spec §8): the owner's words to the hub and its answers (a bubble each, ours with the state:
    queued, sent, delivered, out of time, refused), and the liaison's threads with this hub — what came from it, what the
    liaison did with it (passed, declined, waits for a card), what our sessions asked it to send, by thread.
    A delivered message keeps no text in the state: its text is the owner's request in the journal."""
    b = (st or {}).get("bus") or {}
    if said is None:
        said = said_in_journal(journal_path) if journal_path else {}
    rows = []
    for oid, o in (b.get("out") or {}).items():
        if o.get("contact") != name or o.get("kind") != "message" or o.get("code") == "secret":
            continue   # a key-like text the core held back never went
        owner = o.get("for") == "owner" or bool(o.get("press"))
        rows.append({"id": oid, "ts": o.get("ts") or 0, "way": "out", "kind": "chat" if owner else "thread",
                     "thread": o.get("thread"),
                     # None: the words are only in an older journal (the state keeps no text of a delivered one)
                     "text": o.get("text") or said.get(o.get("of")), "status": OUT_SHOWN.get(o.get("status"),
                                                                                                 o.get("status")),
                     "code": o.get("code"), "source": None if owner else o.get("source"),
                     "agent": o.get("agent_to"), "of": o.get("of"), "basis": o.get("basis")})
    for bid, x in (b.get("inbox") or {}).items():
        if x.get("contact") != name:
            continue
        owner = x.get("for") == "owner" or bool(x.get("chat"))
        rows.append({"id": bid, "ts": x.get("ts") or 0, "way": "in", "kind": "chat" if owner else "thread",
                     "text": x.get("text") or "", "status": x.get("status"), "outcome": x.get("outcome"),
                     "passed_to": list(x.get("passed_to") or []), "agent": x.get("agent_from"),
                     "finding": bool(x.get("finding")), "thread": x.get("thread"), "reply": bool(x.get("reply")),
                     "why": x.get("why"), "note": x.get("note")})
    rows.sort(key=lambda r: (r["ts"], r["id"]))
    return rows[-most:]


def said_in_journal(path, most=16 << 20):
    """{seq: text} of the owner's chat requests in the journal's tail — only those lines are parsed."""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, f.seek(0, 2) - most))
            chunk = f.read()
    except OSError:
        return {}
    return said_lines(chunk, {})


def said_lines(chunk, out):
    """The owner's chat requests among whole journal lines (bytes) into out {seq: text}."""
    for line in chunk.split(b"\n"):
        if b'"type":"bus.chat_requested"' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "bus.chat_requested":
            out[e.get("seq")] = (e.get("data") or {}).get("text")
    return out


def _printable(text):
    return "".join(c if c.isprintable() else " " for c in str(text or ""))


def _when(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if isinstance(ts, (int, float)) and ts else "-"
