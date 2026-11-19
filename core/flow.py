"""The "who wrote to whom" flow from the journal for hours and days - for replaying the animation on the page (owner
request 2026-10-02: an animation of who wrote to whom from the journal, as animated dots and signals between them).

The live flow is built by views.Reader (from the messages in state). Here is a light reading of the journals, the archive
(journal-archive/) included, without folding: only the facts that show a sender and a recipient. An archive's events do
not change (journal.compact keeps its time) - its signals for the last week (WINDOW) are computed once and remembered.
"""
import copy
import json
import os
import re
import threading
import time

from core import corewrite, forms, journal, langdata, tools
# the label at line start (forms.LINE_HEAD). getattr: a process of v1.4.0 ("Files" during the update's self-check)
# imports this file with its own core.forms in memory, without LINE_HEAD: the same pattern from HEAD
# (tests/test_old_page_server)
LINE_HEAD = getattr(forms, "LINE_HEAD", re.compile(rf"(?m)^{forms.HEAD.pattern}"))

KINDS = ("core.snapshot", "message.received", "delegation.created", "turn.started", "turn.result", "session.requested",
         "schedule.set", "schedule.due", "session.remove_requested", "core.rejected", "session.started",
         "session.exited")

# the message signature the core prints to a session (reducer.MARKER): `[hub:mN]` (the id may have a tail: forms.MSG_TAIL),
# `[hub:mN <ru from> X, ...]` or the future `[hub:mN from X, ...]` (core/forms.py)
HEAD = forms.HEAD
OLD_WORDS = dict(langdata.legacy("flow_words"))   # the signature of earlier core versions
OLD_FROM = langdata.legacy("from")   # their label's preposition (forms.WORDS); not forms': a v1.4.0 process lacks it
HUB_WORDS = ("hub", *langdata.words("words", "hub"))   # the hub as an address, in any language (identity.HUB_WORDS)

def _origins(data):
    """Who wrote what the turn took: (the author of the turn's label, whether the lines glued to it (also) contain the
    owner's word). The author - owner, schedule, core, hub or a session name - by the label signature at the start of the
    turn (prompt_head, 200 characters; the signature counts whole, up to "," or "]"; words - core/forms.WORDS by
    preposition); without the Russian "from" - the owner (his message and task, as views._sender); a module and a
    plugin - core; no label or no signature at the start (a command has the label at the end, an also line is past 200
    characters) - None, "none"."""
    signed = {}
    # the signature counts only on a label at line start: that is how the core puts it (foreign text mid-line or quoted is text)
    for found in LINE_HEAD.finditer(data.get("prompt_head") or ""):
        prep, who = found.group(2), (found.group(3) or "").strip()
        if not prep or not who:
            kind = "owner"
        elif prep == OLD_FROM and who in OLD_WORDS:
            kind = OLD_WORDS[who]
        else:
            kind, name = forms.who_kind(prep, who)
            kind = name if kind == "session" else "core" if kind in ("module", "plugin") else kind
        signed.setdefault(found.group(1), kind)
    return signed.get(data.get("marker") or ""), any(signed.get(m) == "owner" for m in data.get("also") or [])


def _outside(d):
    """The node of a module or a plugin process - the same as in the live "Flow" (views._SENDER): not the owner."""
    if d.get("channel") != "plugin" or not d.get("sender"):
        return None
    from core.views import _SENDER   # views calls flow: nodes come from one table
    who = forms.outside_id(d["sender"])
    return _SENDER["module" if who.startswith("mod:") else "plugin"].format(name=who[4:])


class Turns:
    """Who a turn's result goes to (owner decision 2026-10-16: "-> You" only for what is addressed to him). The hub talks
    to the owner - its results go to him. For the others - to whoever started the turn: the owner's word (a message, an
    answer to a card, his task) - to him; a session's message or task - to it; the schedule and the core - to their
    nodes. The owner's word taken by the same turn (inserted mid-turn, glued lines) makes the turn his - like "new" in
    the fold (turn_owner); a turn without a label (Claude Code woke itself: the background finished) continues the
    previous one. A turn ends with a result or with the process (start, exit). One rule for the live "Flow"
    (views.Reader) and for history from the journal."""

    def __init__(self):
        self.to, self.busy = {}, set()

    def feed(self, e, hub):
        """A journal event in order; for a turn result with an answer - the addressee (or None: we do not know who started the turn)."""
        kind, name, d = e["type"], e.get("entity"), e.get("data") or {}
        if not name:
            return None
        if kind == "turn.started":
            who, owner = _origins(d)
            if name not in self.busy:
                self.busy.add(name)
                if who or d.get("marker"):   # a turn without a label - the same chain as the previous one
                    self.to[name] = "owner" if owner else who
            elif who == "owner" or owner:
                self.to[name] = "owner"
        elif kind in ("session.started", "session.exited"):
            self.busy.discard(name)
        elif kind == "turn.result":
            self.busy.discard(name)
            if tools.answered(d):
                to = "owner" if name == hub else self.to.get(name)
                return hub if to == "hub" else to
        return None


def signals(lines, ctx=None, snapshots=True):
    """Signals {seq, ts, from, to, kind} from journal lines (bytes) - in order. ctx - what is known from earlier journals
    (the hub session's name, schedule jobs, open turns, the last event read - an event up to it is a copy rotation
    carried and is skipped; a snapshot is read anyway); it is extended. snapshots=False - without the state snapshots
    (only what goes on to the next journal is needed: its own snapshot gives the hub and the jobs anew). kind "gone"
    (from = to = the session) - the session was removed: from that moment it is not on the "Flow"; a rejected removal
    is not "gone"."""
    ctx = ctx if ctx is not None else {}
    hub, jobs, out = ctx.get("hub", "hub"), ctx.setdefault("jobs", {}), []
    turns = ctx.setdefault("turns", Turns())
    for line in lines:
        if not any(f'"type":"{k}"'.encode() in line for k in KINDS):
            continue
        if b'"type":"core.snapshot"' in line[:200]:
            if not snapshots:
                continue
        elif (at := journal._SEQ.match(line)) and int(at[1]) <= ctx.get("seq", 0):
            continue
        elif at:
            ctx["seq"] = int(at[1])
        try:
            e = json.loads(line)
        except ValueError:
            continue
        kind, d, who = e["type"], e.get("data") or {}, e.get("entity")
        frm = to = what = None
        if kind == "core.snapshot" and d.get("state"):  # a new journal starts with a snapshot: who the hub is, whose jobs
            st = d["state"]
            hub = next((n for n, x in (st.get("sessions") or {}).items() if x.get("kind") == "hub"), hub)
            jobs.update({j: x["session"] for j, x in (st.get("schedule") or {}).items()
                         if x.get("session") and not x.get("command")})   # a command does not wake a session
        elif kind == "session.requested" and d.get("kind") == "hub":
            hub = who
        elif kind == "message.received":
            frm = d.get("sender") if d.get("channel") == "session" else _outside(d) or "owner"
            to = d.get("to") or hub
            to = hub if to in HUB_WORDS else to
            what = "message"
        elif kind == "delegation.created":
            frm, to, what = _outside(d) or d.get("sender") or ("owner" if d.get("owner") else hub), d.get("session"), "task"
        elif kind in ("turn.started", "turn.result", "session.started", "session.exited"):
            frm, to, what = who, turns.feed(e, hub), "result"
        elif kind == "schedule.set" and d.get("command"):
            jobs.pop(who, None)   # was a session's job, became a command - it wakes nobody
        elif kind == "schedule.set" and d.get("session"):
            jobs[who] = d["session"]
        elif kind == "schedule.due" and who in jobs and d.get("exit") != 0:   # 0 - the pre-check: nothing new, nobody was woken
            frm, to, what = "schedule", jobs[who], "message"
        elif kind == "session.remove_requested" and who:
            out.append({"seq": e["seq"], "ts": e["ts"], "from": who, "to": who, "kind": "gone"})
        elif kind == "core.rejected" and d.get("type") in ("session.remove_requested", "message.received"):
            # what was rejected did not happen: neither "gone" nor an arrow for a repeat the fold did not accept (client_id, dedup_key)
            out = [s for s in out if s["seq"] != d.get("of")]
            ctx.setdefault("rejected", set()).add(d.get("of"))   # for a reader that continues in parts: from earlier chunks too
        if frm and to and frm != to:
            mod = corewrite.author(e)   # a module's core.write line - the page draws the signal dashed
            out.append({"seq": e["seq"], "ts": e["ts"], "from": frm, "to": to, "kind": what, **({"mod": mod} if mod else {})})
    ctx["hub"] = hub
    return out


WINDOW = 7 * 86400   # the replay on the page goes back no further than a week: older ones are not kept in memory
LEAD = 2   # archives read before the window, for what goes on into it (a turn, the copies rotation carried)
# archive path -> (key: its mtime and the mtime of the archive before, the time from which the signals are kept, signals,
# ctx: what is known after it). The archives are read one after another, each from what the one before it left
# (_signals_of): a turn started archives earlier and ended in this one goes to whoever started it; the copies rotation
# carried are read once (the replay showed them several times). An archive earlier than the window keeps no
# signals, only its ctx; an archive more than LEAD before the window is not read at all
_archive = {}
# the current journal: {"key" - path and file, "seed" - the key of the last archive, its ctx is where reading starts,
# "offset" - how far it is read in whole lines, "at" and "head" - where the last read line starts and its first bytes
# (event number and time), "ctx", "signals"} - a request reads only the tail (a journal of a hundred MB was parsed anew
# on every "Flow" request: tens of seconds under load)
_current = {}
PROBE = 64   # bytes of the last line's start for checking: the snapshot line is tens of MB, we neither hold it whole nor reread it
_current_lock = threading.Lock()
_archive_lock = threading.Lock()


def history(state_dir, since_ts, now=None):
    """Signals since since_ts: archive journals (cached by mtime) and the current one, each read on from what the earlier
    ones left; the hub and the jobs are taken from the snapshot at a journal's start. An archive without a snapshot (not
    from rotation) gives the hub and the jobs from the archives before it."""
    now = time.time() if now is None else now
    keep = min(since_ts, now - WINDOW)
    with _archive_lock:   # the page's threads: one reads what is not read yet, the others wait for it
        out, ctx, prev = _archives(state_dir, since_ts, keep)
    current = _current_signals(os.path.join(state_dir, "journal.jsonl"), ctx, prev)
    return out + [s for s in current if s["ts"] >= since_ts]


def _archives(state_dir, since_ts, keep):
    """The archives' signals since since_ts, what the last one left and its key. Reading starts LEAD archives before the
    first one in the window, from an empty ctx: the archives of months (tens of GB) are not read for a week."""
    listed = []
    for p in journal.archives(state_dir):
        try:
            listed.append((p, os.path.getmtime(p)))
        except OSError:   # removed while we were looking
            continue
    first = next((i for i, (_, mtime) in enumerate(listed) if mtime >= keep), len(listed))
    out, ctx, prev, seen = [], {}, None, set()
    for i, (p, mtime) in enumerate(listed):
        if i < first - LEAD:
            continue
        # its time and the time of the archive before it: flat, not the chain of all keys before (hundreds of nested
        # tuples compared on every request)
        key, got = (mtime, listed[i - 1][1] if i else None), _archive.get(p)
        if not got or got[0] != key or got[1] > keep:
            try:
                got = _signals_of(p, ctx, key, keep, full=mtime >= keep)
            except FileNotFoundError:
                continue
        elif got[1] < keep:
            got = (key, keep, [s for s in got[2] if s["ts"] >= keep], got[3])
        _archive[p] = got   # the dict is shared by the page's threads
        seen.add(p)
        ctx, prev = got[3], key
        out += [s for s in got[2] if s["ts"] >= since_ts]
    for gone in [k for k in list(_archive) if k not in seen]:   # removed or before the reading starts
        _archive.pop(gone, None)
    return out, ctx, prev


def _signals_of(path, ctx, key, keep, full=True):
    """The cache entry of an archive read on from ctx (what the archives before it left; not changed): its signals from
    keep on and what is known after it. full=False - the archive is earlier than the window: no signals are needed, the
    state snapshots are not read."""
    ctx = copy.deepcopy(ctx)
    with open(path, "rb") as f:
        got = signals(f, ctx, snapshots=full)
    ctx.pop("rejected", None)   # a rejection follows its fact in the same journal
    return key, keep, [s for s in got if s["ts"] >= keep] if full else [], ctx


def _current_signals(path, seed=None, seed_key=None):
    """Signals of the current journal - the same as reading it whole from seed (what the archives left; seed_key - the
    last archive's key): what was read is remembered, new whole lines are read on (a half-written one - next time). A
    different file (rotation), another seed or a file shorter than what was read - from the start."""
    with _current_lock:   # the cache is shared by the page's threads
        try:
            f = open(path, "rb")
        except FileNotFoundError:
            _current.clear()
            return []
        with f:
            st = os.fstat(f.fileno())
            c, head = _current, _current.get("head") or b""
            key = (path, st.st_dev, st.st_ino)
            if c.get("key") != key or c.get("seed") != seed_key or st.st_size < c.get("offset", 0) or (
                    head and (f.seek(c["at"]), f.read(len(head)))[1] != head):
                c.clear()   # a different file, or what was read is no longer the same (the file was shortened and appended to)
                c.update(key=key, seed=seed_key, offset=0, at=0, head=b"", ctx=copy.deepcopy(seed or {}), signals=[])
            f.seek(c["offset"])
            read = [0, c["at"], c["head"]]

            def whole():
                for line in f:
                    if not line.endswith(b"\n"):
                        return   # a half-written line
                    read[1:] = c["offset"] + read[0], line[:PROBE]
                    read[0] += len(line)
                    yield line
            try:
                new = signals(whole(), c["ctx"])
            except BaseException:
                c.clear()   # parsing stopped on a line mid-chunk: ctx is already touched - read from the start, not on top of it
                raise
            gone = c["ctx"].pop("rejected", None)
            old = [s for s in c["signals"] if s["seq"] not in gone] if gone else c["signals"]
            c["signals"] = old + new
            c["offset"] += read[0]
            c["at"], c["head"] = read[1:]
            return c["signals"]
