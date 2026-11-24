"""The past of a session feed, on scrolling up (owner decision 2026-10-02: load journal records from the archive as the
user scrolls).

First what is older than what is shown, from the core's live feed (views.Reader keeps more than goes to the page), then
from archive journals (journal.rotate): each archive is parsed once by the same Reader without a length limit, the
parse goes into a file next to it (<archive>.items.json). An archive's events do not change (journal.compact drops only
what it repeats), so a parse does not go stale.
Archives go from new to old and are merged with the live feed by event number; copies of events carried by rotation
into the new journal are not repeated.

The page keeps only pointers in memory (<archive>.index.json: a session -> its first event and where its feed lies in
the parse) and reads the feed of the one needed session from disk. A whole archive parse is tens of MB of JSON,
hundreds of MB in memory: on 11.10 scrolling held the parse of all archives (the page at 1.2 GB), on 15.10 of the last
three (up to 900 MB). A new archive is parsed by a separate process: otherwise its memory peak (the whole archive)
would stay the page's memory after the parse.

Service rows of the feed are in the hub language (core/i18n.py): a parse remembers its language (the lang field of the
pointer; older parses are Russian), and in another language the archive is parsed again.

A parse also remembers its version (PARSE, the parse field of the pointer). A parse of an earlier version is read as is
until the background parse replaces it: a new version re-parses every archive (hundreds on a big hub, hours), and the
feed past must not close for that time; the rows of an earlier parse differ only in a few places (02.11, PARSE 2: a call
closed in the next archive was "stopped"; 11.11, PARSE 3: the copies of words in another feed — to an executor in the
feed of whoever gave the task, the bus's words in the executor's feed — were dropped by a core snapshot, views._drop_strays;
20.11, PARSE 4: the core's lines on a task — "REPLIED d8: …", "STOPPED d9: …", "TIMEOUT d7: …" — got the label the owner
reads, without the tag and the numbers, in English hubs too: a parse of an earlier version has the tag in the label or,
in an English hub, no label).
"""
import bisect
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time

import platforms
from core import forms, i18n, journal, reducer, tools, views

CACHE = ".items.json"
INDEX = ".index.json"
OLD_INDEX = ".sessions.json"   # pointer of older versions ({session: first event}): they do not read the parse by it
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # the hub code; the package has no core/*.py, only .so
# A parse of an archive that hung stops; those parsed before stay. Hung by its work, not by the clock: it got no CPU for
# TIMEOUT seconds (blocked), or worked more than TIMEOUT seconds of CPU (a loop). A parse yields to the hub
# (platforms.give_way): on a loaded machine it is slow, not hung (04.11: at load ~200 a 110 MB archive took 18 minutes by
# the clock and 4 of CPU; killed after 10 minutes, it started from scratch again and again, the feed past stayed empty).
TIMEOUT = 600
WATCH = 5         # how often the parse process looks at its work (seconds), and less than this CPU per look is no work
MOVED = 0.01
GIVE_UP = 4 * 3600   # by the clock — only when the process could not stop itself (stopped, its watch did not run)
RETRY = 600       # a parse failed — do not repeat for this many seconds: what is unparsed is missing from the feed past
# search in the feed past (/api/history/search): a process per request, at most SEARCH_AT_ONCE at once per server; the
# answer is a portion: archives go until SEARCH_BUDGET seconds have passed (an archive is whole, so a portion may run
# one archive longer), SEARCH_LIMIT matches collected — stop
SEARCH_AT_ONCE, SEARCH_BUDGET, SEARCH_TIMEOUT, SEARCH_LIMIT = 2, 0.3, 10, 1000
GREP_BUDGET = 20   # the terminal's search (grep): seconds, and an archive is whole
SEARCHES = threading.BoundedSemaphore(SEARCH_AT_ONCE)
_RUNNING, _RUNNING_LOCK = {}, threading.Lock()   # tab -> its running search ({proc, cancelled})


class History:
    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.dir = os.path.join(state_dir, journal.ARCHIVE)
        self.lock = threading.Lock()
        # archive -> ({session: [its first event number, start and end of its feed in the parse]}, the parse version)
        self.index = {}
        self.building = None
        self.running, self.rerun, self.spawn = False, False, threading.Lock()   # the background parse; spawn guards both flags
        self.failed = 0.0     # when a parse by a separate process failed
        self.lang = None      # the language the pointers were read in (changed — read again)
        self.swap = threading.Lock()   # language change and pointer reading — request and background parse threads

    def archives(self):
        """Archive names (journal.archives) from new to old."""
        return [os.path.basename(p) for p in reversed(journal.archives(self.state_dir))]

    def indexed(self, name, current=False):
        """The archive pointer: {session: [first event, start, end]}; no parse yet — None. A parse of an earlier version
        (PARSE) is given too, it is read until it is parsed again; current — only a parse of this version."""
        with self.swap:
            if self.lang != i18n.current():
                self.index, self.lang = {}, i18n.current()
            lang, got = self.lang, self.index.get(name)
        if got is None or (current and got[1] != PARSE):   # an earlier one may have been parsed again since
            got = _read_index(os.path.join(self.dir, name), lang)   # from disk — without the lock
            if got is None:
                return None
            with self.swap:
                if self.lang == lang:
                    self.index[name] = got
        return got[0] if not current or got[1] == PARSE else None

    def forget(self, name):
        """Parsed again: the pointer read before points into the former parse file."""
        with self.swap:
            self.index.pop(name, None)

    def renew(self, names):
        """Archives names (their pointers read) hold a parse of an earlier version: it is read as is, the parse of this
        version is made in the background — not sooner than RETRY after a failed one."""
        with self.swap:
            earlier = any(got[1] != PARSE for got in (self.index.get(n) for n in names) if got)
        if earlier and time.time() - self.failed >= RETRY:
            self.build_in_background()

    def sessions(self, name):
        return self.indexed(name) or {}

    def items(self, name, session, sessions=None):
        """A session feed from an archive parse — only its piece of the file; sessions is the pointer used for the
        decision (older: a language change in the middle of a request does not reset it). The parse was rewritten after
        the pointer was read (another language, the background parse of an earlier version) — None; broken under the
        same pointer — nothing from it."""
        pointer = self.sessions(name) if sessions is None else sessions
        at = pointer.get(session)
        if not at:
            return []
        path = os.path.join(self.dir, name)
        try:
            with open(path + CACHE, "rb") as f:
                f.seek(at[1])
                got = json.loads(f.read(at[2] - at[1]))
        except (OSError, ValueError):
            got = None
        if isinstance(got, list):
            return views.sender_now(got)   # a parse of an earlier core names the senders in Russian words
        self.forget(name)
        now = _read_index(path, self.lang)
        return [] if now and now[0] == pointer else None

    def ready(self):
        return all(self.indexed(n, current=True) is not None for n in self.archives())

    def build_all(self):
        """Parse what is not parsed yet, in this process; do not read the ready ones (11.10: the page start read all
        archives — 1.4 GB)."""
        with self.lock:
            for name in self.archives():
                if self.indexed(name, current=True) is None:
                    build(os.path.join(self.dir, name), self.lang)
                    self.forget(name)

    def build_in_background(self, appeared=False):
        """appeared — an archive has just come (the page saw the journal rotate): a parse that is running listed the
        archives before it, so it makes one more pass when it finishes. Other callers (scrolling, search) leave a running
        parse alone."""
        with self.spawn:
            if self.running:
                self.rerun = self.rerun or appeared
                return
            self.running, self.rerun = True, False
            self.building = threading.Thread(target=self._build_loop, daemon=True)
            self.building.start()

    def _build_loop(self):
        tried = set()   # parsed by a separate process and failed in this run: not again in the next pass
        try:
            while True:
                self._build_outside(tried)
                with self.spawn:
                    if not self.rerun:
                        self.running = False
                        return
                    self.rerun = False
        except BaseException:
            with self.spawn:
                self.running = False
            raise

    def _build_outside(self, tried=None):
        """A parse by a separate process per archive (the memory peak goes away with it, the timeout is per archive: a
        long tail of unparsed ones after an update is not killed whole); if it did not start — here. If it crashed or
        hung — not here (the same peak would go into the page): the cause goes to the log, parsing goes on, the failed
        one again no earlier than RETRY."""
        folder = os.path.abspath(self.dir)   # the parse process runs in the code folder
        env = _child_env()
        tried = set() if tried is None else tried
        for name in self.archives():
            if name in tried or self.indexed(name, current=True) is not None:
                continue
            code = f"import platforms; platforms.current.give_way(); from core import history; " \
                   f"history.main({folder!r}, {name!r})"   # after a PARSE bump — hours of CPU: it yields to the hub
            try:
                r = subprocess.run(platforms.current.below_hub([sys.executable, "-c", code]), cwd=ROOT, env=env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=GIVE_UP,
                                   **platforms.current.background())
                why = r.returncode and f"exit code {r.returncode}: {r.stderr.decode('utf-8', 'replace').strip()[-500:]}"
            except subprocess.TimeoutExpired:
                why = f"did not finish in {GIVE_UP} s"
            except OSError:
                self.build_all()
                return
            self.forget(name)
            if why:
                self.failed = time.time()
                tried.add(name)
                print(f"feed history: parsing archive {name} failed — {why}", file=sys.stderr, flush=True)

    def older(self, session, before, n, live, build=True, keep=None, names=None):
        """Up to n feed records of the session older than before (event number): {"items": in order, "more",
        "building"}; keep — which records to return (to the page — without service ones, views.shown_line).
        The live feed and the archives are merged by event number, repeats (the rotation carry copies events) once. Not
        "only older than the oldest found": a rotation carry may hold old rows, and everything between them and the
        rest got lost. names — also the session's former names: in archives from before the rename the feed is under
        them. Archives go from new to old; on reaching an unparsed one (the parse failed) which may hold something older
        than what is shown, we stop: return what was collected and more (older than it the feed would have a gap; once
        it is parsed, we read on)."""
        keep = keep or (lambda it: True)
        page = [it for it in live if it.get("seq", 0) < before and keep(it)]
        archives = self.archives()
        index = {name: self.indexed(name) for name in archives}   # unparsed — None; from disk, once per request
        if None in index.values():
            if build:
                self.build_all()
                index = {name: self.indexed(name) for name in archives}
            elif time.time() - self.failed >= RETRY:
                self.build_in_background()
                return {"items": page[-n:], "more": True, "building": True}
        if not build:
            self.renew(archives)
        own = names or [session]

        def first(name):   # the session's first event (any of its names) in the archive; unparsed — by archive name
            got = index[name]
            if got is None:
                return journal.archive_first(name)
            return min([got[who][0] for who in own if who in got] or [before])
        seen, more = {_key(it) for it in page}, False
        for i, name in enumerate(archives):
            last = journal.archive_last(name)
            if sum(1 for it in page if it.get("seq", 0) > last) >= n:   # this one and older ones are earlier
                more = any(first(older) < before for older in archives[i:])
                break
            if first(name) >= before:
                continue
            if index[name] is None:
                return {"items": page[-n:], "more": True, "building": False}
            got = []
            for who in own:
                if (items := self.items(name, who, index[name])) is None:
                    # parsed again after the pointer was read: the page asks again with a fresh one
                    return {"items": [], "more": True, "building": True}
                got += items
            for it in got:
                if it.get("seq", 0) < before and keep(it) and _key(it) not in seen:
                    seen.add(_key(it))
                    page.append(it)
            page.sort(key=lambda it: it.get("seq", 0))
        return {"items": page[-n:], "more": more or len(page) > n, "building": False}

    def newer(self, session, after, n, live, keep=None, names=None):
        """Up to n feed records of the session newer than after (event number), in order: a window around what search
        found (together with older). Archives go from old to new, while the next one may give a row earlier than the
        collected n-th; then the live feed. {"items", "building"}: an unparsed archive on the way — return what was
        collected and building (without it the window would silently have a gap)."""
        keep = keep or (lambda it: True)
        own, page, seen = names or [session], [], set()

        def add(items):
            for it in items:
                if it.get("seq", 0) > after and keep(it) and _key(it) not in seen:
                    seen.add(_key(it))
                    page.append(it)
            page.sort(key=lambda it: it.get("seq", 0))
        archives = self.archives()
        for name in reversed(archives):   # from old to new
            if journal.archive_last(name) <= after:
                continue
            index = self.indexed(name)
            if index is None:   # not parsed: parse in the background (like older), the window ends before it
                if time.time() - self.failed >= RETRY:
                    self.build_in_background()
                return {"items": page[:n], "building": True}
            firsts = [index[who][0] for who in own if who in index]
            if not firsts or (len(page) >= n and min(firsts) > page[n - 1].get("seq", 0)):
                continue
            got = []
            for who in own:
                if (items := self.items(name, who, index)) is None:
                    # parsed again after the pointer was read: the window ends before it, as unparsed
                    return {"items": page[:n], "building": True}
                got += items
            add(got)
        add(live)
        self.renew(archives)
        return {"items": page[:n], "building": False}

    def search(self, names, q, before, cursor=None, tab=None):
        """Search for text q in the feed past from archives (like older: from new to old, only older than before) by a
        separate process (search_main): its memory peak (a piece of a big session, JSON parsing) goes away with it, the
        page does not grow. At most SEARCH_AT_ONCE processes at once, the rest wait; a new search of the same tab (tab)
        cancels its previous one — the user typed further, the old answer is not needed. The answer is as in search(),
        plus building: an archive on the way is still being parsed (repeat with the same cursor), unparsed: the parse
        failed — nothing past this event number was searched, failed: the process crashed or hung, unavailable: it did
        not start, cancelled: cancelled by the tab's new search."""
        args = {"state_dir": os.path.abspath(self.state_dir), "names": names, "q": q, "before": before, "cursor": cursor,
                "lang": i18n.current(), "budget": SEARCH_BUDGET}
        env = _child_env()
        code = "from core import history; history.search_main()"
        mine, empty = {"proc": None, "cancelled": False}, {"hits": [], "cursor": cursor, "done": False}
        with _RUNNING_LOCK:
            old = _RUNNING.pop(tab, None) if tab else None
            if tab:
                _RUNNING[tab] = mine
            if old:
                old["cancelled"] = True
                if old["proc"]:
                    old["proc"].kill()
        held = False
        try:
            while not (held := SEARCHES.acquire(timeout=0.1)):   # wait for a slot; a cancelled one does not wait
                if mine["cancelled"]:
                    return {**empty, "cancelled": True}
            with _RUNNING_LOCK:
                if mine["cancelled"]:   # while waiting for a slot, the tab asked something else
                    return {**empty, "cancelled": True}
                try:
                    p = mine["proc"] = subprocess.Popen(platforms.current.below_hub([sys.executable, "-c", code]),
                                                        cwd=ROOT, env=env,
                                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                        stderr=subprocess.PIPE)
                except OSError as e:
                    print(f"feed history: the search did not start — {e}", file=sys.stderr, flush=True)
                    return {**empty, "failed": True, "unavailable": True}
            try:
                out, err = p.communicate(json.dumps(args).encode(), timeout=SEARCH_TIMEOUT)
            except subprocess.TimeoutExpired:
                p.kill()
                p.communicate()
                print(f"feed history: the search did not finish within {SEARCH_TIMEOUT} s", file=sys.stderr, flush=True)
                return {**empty, "failed": True}
        finally:
            if held:
                SEARCHES.release()
            with _RUNNING_LOCK:
                if tab and _RUNNING.get(tab) is mine:
                    del _RUNNING[tab]
        if mine["cancelled"]:
            return {**empty, "cancelled": True}
        try:
            got = json.loads(out) if p.returncode == 0 else None
        except ValueError:
            got = None
        if not isinstance(got, dict):
            print(f"feed history: the search failed — code {p.returncode}: "
                  f"{err.decode('utf-8', 'replace').strip()[-500:]}", file=sys.stderr, flush=True)
            return {**empty, "failed": True}
        if got.pop("unparsed", False):   # reached an unparsed archive: the parse is running — repeat; failed — stop
            if time.time() - self.failed >= RETRY:
                self.build_in_background()
                got["building"] = True
            else:
                got["unparsed"] = journal.archive_first(got["cursor"])
        return got


def _child_env():
    """The environment of the parse and search process: the hub code in PYTHONPATH, the page language (without it the
    process looks for the language itself — seconds, core/i18n.py)."""
    return dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in (ROOT, os.environ.get("PYTHONPATH")) if p),
                HUB_LANG=i18n.current())


def names(state, session):
    """The session and its former names (session.renamed, aliases "was:"), except those another session bears now."""
    out = [session]
    for name in out:
        out += [k[4:] for k, v in (state.get("aliases") or {}).items()
                if k.startswith("was:") and v == name and k[4:] not in out and k[4:] not in state.get("sessions", {})]
    return out


def _key(item):
    """The same feed row from different parses: the key k and the edit rev are each reader's own, and the text of a
    core message may depend on the state at parse time (an account switch: "sessions will move…" vs "already on the
    gateway") — a row with an id is recognized by the id."""
    return item.get("seq"), item.get("kind"), item.get("id"), None if item.get("id") else item.get("text")


def _load_index(path, lang=None, earlier=False):
    """The pointer of the parse of archive path (_read_index); of an earlier parse version (PARSE) — only with
    earlier."""
    got = _read_index(path, lang)
    return got[0] if got and (earlier or got[1] == PARSE) else None


def _read_index(path, lang=None):
    """The pointer of the parse of archive path and the version of the parse; none, broken, from another parse (another
    size) or in another language (lang; older parses are Russian; "*" — any) — None."""
    try:
        with open(path + INDEX, encoding="utf-8") as f:
            got = json.load(f)
        if got["size"] != os.path.getsize(path + CACHE):
            return None
        if lang != "*" and got.get("lang", "ru") != (lang or i18n.current()):
            return None
        return got["sessions"], got.get("parse", 1)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def build(path, lang=None):
    """The parse of archive path in language lang (without it — the process language) and the pointer to it. A parse in
    the same language and of this parse version (PARSE) — the pointer is built from it, the archive is not read again;
    a parse without a pointer (an older version, or an interrupted write) — again."""
    lang = lang or i18n.current()
    try:
        with open(path + INDEX, encoding="utf-8") as f:
            index = json.load(f) or {}
        had = index.get("lang", "ru") if index.get("parse", 1) == PARSE else None
    except (OSError, ValueError, AttributeError):
        had = None
    try:
        if had != lang:
            raise ValueError("parsed in another language")
        with open(path + CACHE, encoding="utf-8") as f:
            timelines = json.load(f)
        if not isinstance(timelines, dict):
            raise ValueError("not a parse")
    except (OSError, ValueError):
        timelines = parse(path)
    _write(path, timelines, lang)


def parse(path):
    """Session feeds of archive path. An archive without the rotation carry (journal.compact) starts right at its own
    events and keeps no later snapshots: the reader first reads the archives before it (_lead_in) — a turn or a tool call
    started there gets its result here, as when the carry was in the archive — and of those earlier rows keeps only the
    ones this archive changed. The messages the core sent in an archive come from the facts of sending (_sent): compact
    drops the later snapshots of any archive, the first one too."""
    r = views.Reader(limit=10 ** 9)
    events = journal.read(path)
    if not events:
        return r.timelines
    lead = _lead_in(path, events)
    for earlier in lead:
        r.feed_all(earlier)
    mark, start = r.rev, events[0]["seq"]
    r.feed_all(events)
    _sent(r, path, events, _end_state(path), lead[0][0]["seq"] if lead else _own(path, events), lead)
    if not lead:
        return r.timelines
    return {name: kept for name, line in r.timelines.items()
            if (kept := [it for it in line if it.get("seq", 0) >= start or it.get("rev", 0) > mark])}


PARSE = 4   # the parse version (the pointer's parse field): a parse of an earlier one is made again
LEAD_MAX = 30   # archives read back for a turn still open: one open longer (days) is not followed further


def _lead_in(path, events):
    """The events the reader reads before archive path (events), oldest archive first: the archive before it whole (a
    call started there may end in path), and further back, while a session in a turn when path begins (its first line is
    the core's state then) did not start that turn yet, the snapshot and that session's events (a command for hours: its
    result came six archives later)."""
    out, at, waiting = [], path, _in_turn(events)
    while len(out) < LEAD_MAX and (previous := journal.before(at)):
        got = journal.read(previous)
        if not got:   # an empty archive (truncated, a torn first line): nothing to read back from it or past it
            break
        if out:
            got = got[:1] + [e for e in got[1:] if journal.session_of(e) in waiting]
        out.insert(0, got)
        waiting = (waiting - {e.get("entity") for e in got if e["type"] == "turn.started"}) & _in_turn(got)
        if not waiting:
            break
        at = previous
    return out


def _in_turn(events):
    """The sessions in a turn when a journal begins: by its first line, the core's state then."""
    state = (events[0].get("data") or {}).get("state") or {} if events and events[0]["type"] == "core.snapshot" else {}
    return {name for name, s in (state.get("sessions") or {}).items() if s.get("state") in views.MIDTURN}


def _end_state(path):
    """The core's state when archive path ends: the rotation snapshot that names it — the first line of the archive after
    it, past its carry while that one is whole, or in the current journal; None if it is nowhere."""
    state_dir = os.path.dirname(os.path.dirname(path))
    later = [p for p in journal.archives(state_dir) if journal.archive_last(p) > journal.archive_last(path)]
    name = b'"why":"rotate","archive":"' + os.path.basename(path).encode() + b'"'
    try:
        with open(later[0] if later else os.path.join(state_dir, "journal.jsonl"), "rb") as f:
            for line in f:
                if b'"type":"core.snapshot"' in line[:200] and name in line[:300]:
                    return (json.loads(line).get("data") or {}).get("state")
    except (OSError, ValueError):
        pass
    return None


def _sent(r, path, events, end, first, lead=()):
    """The messages the core typed to a session in this archive (host.done "type" sent, a turn with the message's mark)
    are in the feed, even where the reader's fold did not create them: it folds these old events by the current code from
    the archive's first snapshot, while the core decided by its rules then and by a state it no longer has (the snapshots
    compact dropped). The row is built from the message as it was in the core's state at the archive's end (end) — the
    text the session got; a message already gone from it — from the start of the turn it began (200 characters). A
    message created before the first event the reader read (first) has its row in an earlier archive's parse. A row of a
    message that state holds gets that text, unless the core changed the message after the row (handed it to another
    session, added to it)."""
    messages = (end or {}).get("messages") or {}
    sent, heads, start = {}, {}, _own(path, events)
    made = {e["seq"]: e for earlier in lead for e in earlier}   # a message may come in an archive before
    for e in events:
        d = e.get("data") or {}
        if e["seq"] < start:   # the carry of a whole archive: sent in the archive before it
            continue
        made[e["seq"]] = e
        if e["type"] == "host.done" and d.get("do") == "type" and d.get("result") == "sent" and d.get("message"):
            sent.setdefault(d["message"], e)
        elif e["type"] == "turn.started" and d.get("marker"):
            sent.setdefault(d["marker"], e)
            heads.setdefault(d["marker"], d.get("prompt_head") or "")
    shown = set()
    for name, line in r.timelines.items():
        for it in line:
            if it.get("kind") != "in" or not it.get("id"):
                continue
            shown.add(it["id"])
            msg = messages.get(it["id"])
            if msg and msg.get("session") == name and isinstance(msg.get("text"), str) and it.get("seq", 0) >= start \
                    and not it.get("relayed") and not msg["text"].startswith(it.get("text") or ""):
                it["text"] = msg["text"]
                it.pop("label", None)   # the label of the new text, by the live feed's rule
                came = made.get(it.get("seq")) or {}   # the event that created it: the pressed button's caption
                data = came.get("data") if came.get("type") == "message.received" else None
                it.update({"label": lbl} if (lbl := views.row_label(msg, data)) else {})
                r.touch(name, it)
    for mid, e in sent.items():
        at = MESSAGE_ID.match(mid)
        if not at or mid in shown or int(at[1]) < first:
            continue
        msg = messages.get(mid) or _from_head(mid, e.get("entity"), heads.get(mid))
        if not msg or not msg.get("session") or not isinstance(msg.get("text"), str):
            continue
        origin = made.get(int(at[1]))   # the event that created it
        row = views.core_row(msg, int(at[1]), origin["ts"] if origin else msg.get("created") or e["ts"], end or {})
        if origin and origin["type"] == "message.received" and mid == f"m{origin['seq']}":
            # a message that came in: its row as views._items makes it — the words, the channel, the author
            d = origin.get("data") or {}
            row.pop("on_behalf", None)   # words that came in are their author's own, not the core's note in his name
            row.update(text=d.get("text") if isinstance(d.get("text"), str) else row["text"],
                       channel=d.get("channel") or origin.get("src"), sender=views._sender_of(d),
                       **views._member_of(d), attachments=msg.get("attachments") or reducer.clean_attachments(d.get("attachments")))
            row.pop("label", None)   # the pressed button's caption is in the event's data
            row.update({"label": lbl} if (lbl := views.row_label(msg, d)) else {})
            if mid in messages:   # the author as the core decided it
                row["from"] = views._from_field(msg) or (forms.origin_from("owner") if msg.get("from_owner") else None)
            else:   # by the rule it decides by: the owner's word comes only from his channels
                frm = reducer._origin(d)[1]
                row["from"] = forms.origin_from("owner") if tools.from_owner(d) else \
                    None if (frm or {}).get("kind") == "owner" else frm
        elif msg.get("from_owner") and not (msg.get("on_behalf") or msg.get("answers_question")
                                             or origin and origin["type"] != "message.received"):
            # the owner's words: his row. The core's note in his name about his action on a card stays the core's row,
            # views.core_row: marked, or one that answers a card (his own words answering it are the message.received's
            # m<seq> above; a typed answer's retelling is m<seq>x1), or made by another event. Else — his words, as before
            row.update(channel=msg.get("channel") or "web", sender=None, attachments=msg.get("attachments") or [],
                       **{"from": forms.origin_from("owner")})
        line = r.timelines.setdefault(msg["session"], [])
        r.keys += 1
        row["k"] = r.keys
        r.touch(msg["session"], row)
        line.insert(bisect.bisect_right([it.get("seq", 0) for it in line], row["seq"]), row)


def _own(path, events):
    """The number archive path's own events (events) start from: past the rotation carry of a whole archive (its
    rotate-start snapshot first, the rotate one after the carry), if the archive the carry came from is in place — as
    journal.compact keeps a carry whose archive is gone (it may lie nowhere else)."""
    if not ((events[0].get("data") or {}).get("why") == "rotate-start" and events[0]["type"] == "core.snapshot"):
        return events[0]["seq"]
    rotate = next((e for e in events if e["type"] == "core.snapshot" and (e.get("data") or {}).get("why") == "rotate"),
                  None)
    came = os.path.basename(str(((rotate or {}).get("data") or {}).get("archive") or ""))
    if not came:
        return events[0]["seq"]
    try:
        os.stat(os.path.join(os.path.dirname(path), came))
    except OSError:
        return events[0]["seq"]
    return rotate["seq"]


MESSAGE_ID = re.compile(r"m([0-9]+)")   # a state message: m<the event that created it> and a tail (x2, s, r1, …)


def _from_head(mid, session, head):
    """A message that left the core's state, from the start of its turn: the label gives who it is from (no preposition —
    the owner, as flow._origins reads it), the rest — the text (cut at 200 characters)."""
    label = f"[hub:{mid}"
    close = (head or "").find("]")
    if not session or not (head or "").startswith(label) or close < 0:
        return None
    origin = head[len(label):close]
    return {"id": mid, "session": session, "origin": origin, "from_owner": not origin.strip(),
            "text": head[close + 1:].lstrip() + ("…" if len(head) >= 200 else "")}


def _write(path, timelines, lang=None):
    """The parse is the same JSON {session: [records]} as before (an older version after a rollback reads it), and where
    in it each session's feed lies. lang is the language of the parse (without it — the process language)."""
    lang = lang or i18n.current()
    index, tmp = {}, f"{path}{CACHE}.{os.getpid()}.tmp"
    for old in (INDEX, OLD_INDEX):   # pointers go first: an interrupted write will not pass it off as another language
        try:
            os.remove(path + old)
        except FileNotFoundError:
            pass
    with open(tmp, "wb") as f:
        f.write(b"{")
        for i, (name, items) in enumerate(timelines.items()):
            f.write((b"," if i else b"") + json.dumps(name, ensure_ascii=False).encode("utf-8") + b":")
            start = f.tell()
            f.write(json.dumps(items, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            if items:
                index[name] = [items[0].get("seq", 0), start, f.tell()]
        f.write(b"}")
        size = f.tell()
    platforms.current.replace(tmp, path + CACHE)
    # the pointer with the language goes first: an interrupted write will not leave a parse with only .sessions.json
    # (it means "Russian")
    _save(path + INDEX, {"size": size, "sessions": index, "lang": lang, "parse": PARSE})
    _save(path + OLD_INDEX, {k: v[0] for k, v in index.items()})   # for an older version after a rollback


def _save(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    platforms.current.replace(tmp, path)


def main(folder, name):
    """Parse the unparsed archive name by a separate process (History._build_outside). First — unfinished parse files
    older than TIMEOUT: their process stopped itself as hung (_watch), was killed or crashed (up to hundreds of MB);
    younger ones may still be being written."""
    path = os.path.join(folder, name)
    if _load_index(path, i18n.current()) is None:
        _watch()
        started = time.time()
        _sweep(path, started - TIMEOUT)
        build(path)
        _sweep(path, started)   # parsed: unfinished files from before our start (page restarted mid-parse) go too


def _watch():
    """The parse process stops itself when it hung (TIMEOUT): no CPU work for TIMEOUT seconds, or more than TIMEOUT
    seconds of it. Starved by a loaded machine it keeps going, however long by the clock."""
    def watch():
        start = cpu = time.process_time()
        moved = time.monotonic()
        while True:
            time.sleep(min(WATCH, TIMEOUT / 4))
            now, used = time.monotonic(), time.process_time()
            if used - start > TIMEOUT:
                why = f"used more than {TIMEOUT} s of CPU"
            elif used - cpu >= MOVED:
                cpu, moved = used, now
                continue
            elif now - moved > TIMEOUT:
                why = f"{TIMEOUT} s without work"
            else:
                continue
            print(why, file=sys.stderr, flush=True)
            os._exit(3)
    threading.Thread(target=watch, daemon=True).start()


def _sweep(path, before):
    """Remove unfinished parse files of archive path touched before `before`."""
    for tmp in glob.glob(glob.escape(path) + ".*.tmp"):
        try:
            if os.path.getmtime(tmp) < before:
                os.remove(tmp)
        except OSError:   # another process removed it
            pass



def search(state_dir, names, q, before, cursor=None, lang=None, budget=SEARCH_BUDGET, limit=SEARCH_LIMIT, terminal=False, until=None):
    """Search for text q (views.found: the SEARCH_FIELDS fields, case-insensitive, the Russian letters yo and ye
    treated as equal) in the feed of the session under names across archive parses from new to old, rows older than
    before; cursor — the archive to continue from. A session piece in a parse is first checked as text — whether it has
    q in the form the rows lie in JSON (the parse is written with ensure_ascii=False: letters as is, quote and newline
    escaped); if not, the JSON is not parsed: this way a big session is passed in fractions of a second. A row carried
    by rotation into several archives is one match.
    {"hits": [{key, seq, ts, kind, id, snippet}], "cursor": the next archive | None, "done", "capped", "left": archives
    remaining, "unparsed": ran into the unparsed archive cursor}.
    The terminal (grep) asks across all sessions — names None; lang "*" takes a parse in any language; terminal adds to a
    hit its "session" and the whole row ("row") to print it as the session view does, and skips an unparsed archive
    (counted in "skipped") instead of stopping at it; until(hits) true after an archive ends the search there."""
    needle = views.fold(q)
    raw = views.fold(json.dumps(q, ensure_ascii=False)[1:-1])
    folder = os.path.join(state_dir, journal.ARCHIVE)
    archives = [os.path.basename(p) for p in reversed(journal.archives(state_dir))]
    if cursor and cursor not in archives:   # the archive vanished (removed by hand) — nothing to search from
        return {"hits": [], "cursor": None, "done": True, "capped": False, "left": 0}
    start = archives.index(cursor) if cursor else 0
    hits, seen, began = [], set(), time.monotonic()

    skipped = 0

    def out(i, **more):
        return {"hits": hits, "cursor": archives[i] if i < len(archives) else None, "done": i >= len(archives),
                "capped": False, "left": len(archives) - i, **({"skipped": skipped} if terminal else {}), **more}
    for i in range(start, len(archives)):
        name = archives[i]
        if i > start and time.monotonic() - began > budget:
            return out(i)
        if journal.archive_first(name) >= before:   # the whole archive is not older than what is shown
            continue
        index = _load_index(os.path.join(folder, name), lang, earlier=True)   # an earlier version — until parsed again
        if index is None and terminal:
            skipped += 1
            continue
        if index is None:
            return out(i, unparsed=True)
        for who, at in ((who, index[who]) for who in (index if names is None else names) if who in index):
            if at[0] >= before:
                continue
            with open(os.path.join(folder, name + CACHE), "rb") as f:
                f.seek(at[1])
                chunk = f.read(at[2] - at[1])
            if raw not in views.fold(chunk.decode("utf-8", "replace")):
                continue
            try:
                items = views.sender_now(json.loads(chunk))
            except ValueError:   # the parse was rewritten (language) mid-search — skip this piece
                continue
            for it in items:
                # a row carried into several archives is one match; the terminal's search across sessions keeps a
                # row of two feeds under each (the page's names are one session under its former names: one)
                if not isinstance(it, dict):
                    continue
                mark = (who, _key(it)) if terminal else _key(it)
                if it.get("seq", 0) >= before or mark in seen or not views.shown_line(it):
                    continue
                text = views.found(it, needle)
                if text is None:
                    continue
                seen.add(mark)
                hits.append(hit(it, text, needle))
                if terminal:
                    hits[-1].update(session=who, row=it)
                if len(hits) >= limit:
                    return {**out(len(archives)), "capped": True}
        if until and until(hits):
            return out(i + 1)
    return out(len(archives))


def grep(state_dir, q, names=None, need=30, live=None, budget=GREP_BUDGET, renamed=None):
    """The terminal's search (hub show --grep) in the archived feed: [(session, row)] of the sessions under names (None —
    all), from new to old, archive by archive, until `need` rows are found or `budget` seconds have passed (an archive
    is whole). live — {session: rows} the caller already has: the rows carried into the live journal are not given
    twice. Only parsed archives are searched (the parse is the page's background work; a terminal does not wait minutes
    for it): info says how many were skipped and where it stopped short: {"unparsed": archives skipped, "stopped": the
    first archive not searched, or None}. renamed — {former name: the session's current one}: an archive keeps a session
    under the name it had then, the live window and the answer use the current one."""
    renamed = renamed or {}
    seen = {(who, _key(it)) for who, line in (live or {}).items() for it in line}
    fresh = {}

    def enough(hits):
        for h in hits:
            who = renamed.get(h["session"], h["session"])
            if (who, _key(h["row"])) not in seen:
                fresh[(who, _key(h["row"]))] = h
        return len(fresh) >= need
    got = search(state_dir, names, q, 10 ** 18, None, "*", budget, terminal=True, until=enough)
    enough(got["hits"])
    stopped = None if got["done"] or len(fresh) >= need else got["cursor"]
    return [(who, h["row"]) for (who, _), h in fresh.items()], {"unparsed": got["skipped"], "stopped": stopped}


def hit(it, text, needle):
    """A match for the page: the row key (like the feed's data-key), where it is, and a piece of text around what was
    searched."""
    low = views.fold(text)
    j = max(0, low.find(needle))
    if len(low) != len(text):   # rare letters change length under fold: the place in the text is found per letter
        at = [k for k, ch in enumerate(text) for _ in views.fold(ch)]
        j = at[min(j, len(at) - 1)] if at else 0
    a = max(0, j - 40)
    return {"key": f"{it.get('seq')}{it.get('kind')}{it.get('id') or ''}", "seq": it.get("seq"), "ts": it.get("ts"),
            "kind": it.get("kind"), "id": it.get("id"),
            "snippet": ("…" if a else "") + text[a:j + len(needle) + 80].replace("\n", " ")}


def search_main():
    """A search by a separate process (History.search): the question is JSON on stdin, the answer is JSON on stdout."""
    a = json.loads(sys.stdin.buffer.read())
    got = search(a["state_dir"], a["names"], a["q"], a["before"], a.get("cursor"), a.get("lang"), a.get("budget", SEARCH_BUDGET))
    sys.stdout.write(json.dumps(got, ensure_ascii=False))
