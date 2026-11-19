"""Event journal: JSONL, append-only, written by a single process (hubd).

Event: {"seq", "ts", "src", "type", "entity", "data"}. An unfinished last line (power cut) is skipped on read
and cut off on the next write.
"""
import hashlib
import json
import os
import re
import shutil
import tempfile
import time

import platforms
from core import coretext, forms, i18n, modrun, redact

REJECTED = coretext.T("core.journal.rejected", "rejected")   # a refusal without a reason (core.rejected without one)

# what the session's tools and its background commands received and returned (title — the command itself, output — the
# last line): a mirror for the page, known key forms are masked in it (core/redact.py). Not masked is what goes on
# as written: messages (delivered to sessions), session.said (received by the owner). permission.requested is masked
# by hubd: it keeps the input as is for the answer (Hub.hold_input)
MASKED = ("turn.tool", "turn.tool_result", "session.background", "session.background_output",
          "core.run_ended",   # the last lines of a `hub run` (core/hubrun.py) may carry a token, as tool output does
          # the start of a turn: Claude Code's notification about a background command quotes the command; the hook
          # masks it before the cut (hooks/hub_hook.translate: a key across the cut keeps no tail), here the cut text
          # is masked again for any other writer and for `hub journal scrub`
          "turn.started")

# a session's words (session.said) and a turn's result (turn.result) go into the journal whole up to TEXT_MAX characters (a
# model reply is a few tens of KB at most; measured: ~1% of replies are longer than 4000, together +15 KB a day on a 76 MB journal); only
# a runaway output is cut, and the event says so with "cut" (the original length). Hosts before this limit cut at OLD_CUT without a mark.
TEXT_MAX = 100_000
OLD_CUT = 4000


def is_cut(data):
    """The text of session.said / turn.result lost its tail: marked by the host ("cut") or, in old journals, exactly OLD_CUT long."""
    return bool(data.get("cut")) or len(data.get("text") or "") == OLD_CUT


def read(path):
    """All complete events in order."""
    try:
        with open(path, "rb") as f:
            lines = f.read().split(b"\n")
    except FileNotFoundError:
        return []
    events = []
    for line in lines:
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            break  # torn tail; everything after it is ignored
    return events


class Journal:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._repair()
        events = read(path)
        self.seq = events[-1]["seq"] if events else 0

    def _repair(self):
        if not os.path.exists(self.path):
            return
        with open(self.path, "rb") as f:
            raw = f.read()
        good = 0
        *complete, _tail = raw.split(b"\n")  # _tail — bytes after the last \n: a torn line or empty
        for line in complete:
            try:
                if line.strip():
                    json.loads(line)
            except ValueError:
                break
            good += len(line) + 1
        if good == len(raw):
            return
        if raw[good:].strip(b"\n").count(b"\n"):   # more than a torn last line: whole events are being dropped
            folder = os.path.join(os.path.dirname(self.path) or ".", ARCHIVE)
            os.makedirs(folder, exist_ok=True)
            kept = os.path.join(folder, f"cut-{time.time_ns()}.jsonl")   # not a journal archive (archives): nobody reads it
            with open(kept, "wb") as f:
                f.write(raw[good:])
                f.flush()
                platforms.current.sync(f.fileno())
            platforms.current.sync_dir(folder)   # the cut-off part is on disk before the journal loses it
            print(f"journal: a broken line at byte {good}; {len(raw) - good} bytes after it — in {kept}", flush=True)
        with open(self.path, "r+b") as f:
            f.truncate(good)

    def append(self, type, entity=None, data=None, src="core", ts=None):
        event = self.event(type, entity, data, src, ts)
        self.write(event)
        return event

    def event(self, type, entity=None, data=None, src="core", ts=None, dedup=None):
        """The next numbered event, not yet written: write it with write (a fact and its rejection go in one record).
        dedup — the repeat key of a module fact (an event field, core/modstate._scope)."""
        self.seq += 1
        if type in MASKED and data:   # before folding: hubd's state and a reader's fold of the journal match
            data = redact.mask_data(data)
        event = {"seq": self.seq, "ts": round(time.time() if ts is None else ts, 3), "src": src,
                 "type": type, "entity": entity, "data": data or {}, **({} if dedup is None else {"dedup": dedup})}
        if type == "core.snapshot":   # the state is folded from already checked events; a snapshot is megabytes
            return event
        try:
            line = _line(event)
            line.encode("utf-8")
        except UnicodeEncodeError:   # a lone surrogate (tool output) becomes "?": the fold sees the same as the journal
            line = _line(event).encode("utf-8", "replace").decode("utf-8")
            event = json.loads(line)
        if redact.LINK_MARK in line or redact.INVITE_MARK in line:   # a bus link (a one-time key), a relay permit's
            # signature — in any event: masked before folding and writing
            event = {**event, "data": redact.mask_links(event["data"])}
        return event

    def write(self, *events):
        """Events in one record, all or nothing: a fact without its rejection would be taken by the CLI as accepted. If it
        fails (full disk) the error goes up: hubd crashes and re-reads the journal on start, the fact file waits in the inbox."""
        _write(self.path, "".join(_line(e) for e in events).encode("utf-8"))


def _write(path, data):
    """Append the lines or nothing: a failed write is cut back, and a fragment the previous one could not cut is cut
    before this one (otherwise the event sticks to it and readers stop at that line)."""
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o666)
    try:
        end = os.lseek(fd, 0, os.SEEK_END)
        if end and _last_byte(fd, end) != b"\n":
            end = _line_start(fd, end)
            os.ftruncate(fd, end)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)   # not platforms.sync: a full flush on macOS is milliseconds per event
        except OSError:
            try:
                os.ftruncate(fd, end)
            except OSError:
                pass   # the next write or the repair at start will cut it
            raise
    finally:
        os.close(fd)


def _last_byte(fd, end):
    os.lseek(fd, end - 1, os.SEEK_SET)
    return os.read(fd, 1)


def _line_start(fd, end):
    """Where the last line of the file starts (after the last newline before end)."""
    pos = end
    while pos > 0:
        step = min(65536, pos)
        os.lseek(fd, pos - step, os.SEEK_SET)
        i = os.read(fd, step).rfind(b"\n")
        if i >= 0:
            return pos - step + i + 1
        pos -= step
    return 0


# --- rotation and the snapshot for commands (hub 01.10: 211 MB, 190 of them snapshots inside the journal; hub status 5.3 s) ---
ARCHIVE = "journal-archive"   # old journals — whole, as they were (owner decision 2026-10-01: needed for evals); with
                              # journal.compact_on_rotate on (off by default) or after `hub journal compact` the earlier
                              # ones lose what rotation repeated, the intermediate state snapshots among it (compact)
STATE_FILE = "state.json"     # the last state and the journal position to continue reading from
KEEP = 3000                   # recent events in the new journal: the page feed does not go empty
ROTATE_BYTES = 50 << 20       # above this hubd starts a new journal, on the fly and at start (10.10: at 20 MiB rotation
                              # came hourly; hub commands read state.json and the tail, the whole journal — page, Telegram, schedule)


_ARCHIVE_NAME = re.compile(r"journal-([0-9]+)-([0-9]+)\.jsonl")   # archive name as given by rotate


def archives(state_dir):
    """Paths of the journal archives in order (by the number of the last event). An archive is only a file named the
    way rotate names it: journal-<first>-<last>.jsonl; anything else in the archive folder (a copy, a renamed file) is
    not an archive and is not read by the feed, the "Stream", the spending or the statistics (15.10: a copy of an
    archive doubled the signals and the spending)."""
    try:
        found = _archive_paths(os.path.join(state_dir, ARCHIVE))
    except FileNotFoundError:
        return []
    # One journal is one archive. A rotation refused at the replace (Windows: a reader holds the journal) drops its link,
    # but the same reader may refuse that too: the name is then the live journal itself, and the next try links a second
    # name of it. Of the names of one file the newest stays (by the file, not the first number: two journals may share
    # it — a rotation that carried a whole small journal); a name of the live journal is not an archive yet.
    live = _file(os.path.join(state_dir, "journal.jsonl"))
    newest = {names[-1] for key, names in _by_file(found).items() if key != live}
    return [p for p in found if p in newest]


def _archive_paths(folder):
    """The archive names in folder, as paths, in order of the last event (FileNotFoundError without the folder)."""
    return [os.path.join(folder, n) for n in sorted(filter(_seqs, os.listdir(folder)), key=archive_last)]


def _file(path):
    """What names the file itself: (device, inode) — one stat; the path when that says nothing (gone, no inode)."""
    try:
        st = os.stat(path)
    except OSError:
        return path
    return (st.st_dev, st.st_ino) if st.st_ino else path


def _by_file(paths):
    """paths grouped by the file they name, each group in the order given."""
    groups = {}
    for p in paths:
        groups.setdefault(_file(p), []).append(p)
    return groups


def _seqs(name):
    m = _ARCHIVE_NAME.fullmatch(os.path.basename(name))
    return m and (int(m[1]), int(m[2]))


def archive_first(name):
    """Number of the archive's first event, from the name journal-<first>-<last>.jsonl; any other name gives 0."""
    return (_seqs(name) or (0, 0))[0]


def archive_last(name):
    """Number of the archive's last event, from the name journal-<first>-<last>.jsonl; any other name gives 0."""
    return (_seqs(name) or (0, 0))[1]


def _written(state_dir, seq):
    """The archive event seq was written to — the first one whose last number is not below seq (archives follow one
    another); None — it is newer than every archive. Not every archive whose name window covers the number: the first
    number in a name is that of the carried part, which can stay far behind (31.10: hundreds of archives named from the
    same old number — an old seq read them all, 85 MB each)."""
    return next((path for path in archives(state_dir) if archive_last(path) >= seq), None)


def _files(state_dir, seq):
    """Journal files that may hold event seq: the archive it was written to, then the live journal (events_at reads
    the same files, for several numbers at once)."""
    written = _written(state_dir, seq)
    if written:
        yield written
    yield os.path.join(state_dir, "journal.jsonl")


def _rejected(line, seq):
    """Is this line the core's rejection of event seq: hubd writes core.rejected on the line right after the event it
    rejected (reducer.verdicts)."""
    if b'"core.rejected"' not in line:
        return False
    try:
        ev = json.loads(line)
    except ValueError:
        return False
    return isinstance(ev, dict) and ev.get("type") == "core.rejected" and (ev.get("data") or {}).get("of") == seq


def event_at(state_dir, seq, accepted=False):
    """The event with number seq, from the live journal or an archive (the number of `mN`, `qN`, `dN` is an event
    number); none — None. accepted: an event the core rejected is None too (the journal keeps rejected facts)."""
    return events_at(state_dir, [seq], accepted).get(seq)


def _line_seq(line):
    """The seq a journal line starts with ({"seq":N, …}); not such a line — None. Only the start is looked at: a number
    quoted inside another event's text is not taken."""
    if not line.startswith(b'{"seq":'):
        return None
    end = line.find(b",", 7)
    digits = line[7:end].strip(b" ") if end > 0 else b""
    return int(digits) if digits.isdigit() else None


def events_at(state_dir, seqs, accepted=False):
    """{seq: event} for the numbers found in the live journal or the archives (event_at for several): each file is read
    once, line by line, for all the numbers it may hold — the archive each was written to, then the live journal for
    the rest; only the matching lines are parsed, no folding of a large journal. Numbers that are not positive ints are
    skipped."""
    want = {n for n in seqs if isinstance(n, int) and not isinstance(n, bool) and n >= 1}
    by_file, arcs = {}, archives(state_dir)
    for n in sorted(want):
        by_file.setdefault(next((path for path in arcs if archive_last(path) >= n), None), set()).add(n)
    live = by_file.pop(None, set())
    found, seen = {}, set()   # seen: matched in some file, also the rejected ones — not looked for again
    plan = [(path, ns) for path, ns in by_file.items()] + [(os.path.join(state_dir, "journal.jsonl"), None)]
    for path, ns in plan:
        if ns is None:   # the live journal last: the numbers newer than every archive and those an archive did not have
            ns = live | {n for group in by_file.values() for n in group if n not in seen}
        if not ns:
            continue
        try:
            with open(path, "rb") as f:
                line = f.readline()
                while line and ns:
                    n, after = _line_seq(line), None
                    if n in ns:
                        try:
                            ev = json.loads(line)
                        except ValueError:
                            ev = None
                        if ev is not None:
                            # hubd writes core.rejected on the line right after the event; that line may be a wanted one
                            after = f.readline()
                            if not (accepted and _rejected(after, n)):
                                found[n] = ev
                            seen.add(n)
                            ns = ns - {n}
                    line = after if after is not None else f.readline()
        except OSError:
            continue
    return found


def answer_of(state_dir, seq, request_id):
    """The owner's answer (permission.answered) to question seq with this request_id; none — None. No request_id: no
    answer can match it, nothing is read (an empty needle matched every line)."""
    if not request_id:
        return None
    needle = request_id.encode()
    for path in [*(p for p in archives(state_dir) if archive_last(p) >= seq), os.path.join(state_dir, "journal.jsonl")]:
        try:
            with open(path, "rb") as f:
                for line in f:
                    if needle in line and b'"permission.answered"' in line:
                        try:
                            ev = json.loads(line)
                        except ValueError:
                            continue
                        if ev.get("type") == "permission.answered" and (ev.get("data") or {}).get("request_id") == request_id:
                            return ev
        except OSError:
            continue
    return None


def _line(event):
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"


def _first_seq(path):
    try:
        with open(path, "rb") as f:
            return json.loads(f.readline()).get("seq")
    except (OSError, ValueError):
        return None


HEAD_BYTES = 160   # the start of the journal's first line: the number and the time of its first event


def head_of(path):
    """A mark of the journal file: the hash of the start of its first line (the number and the time of the first
    event), no further than that line: the journal grows, and a short first line must not take in the second. The state
    file keeps it: a journal started anew from number 1 has the same first number as the one it replaced, and a line
    boundary at the saved offset (the same line length) made the old file look like its own (11-18)."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read(HEAD_BYTES).split(b"\n", 1)[0]).hexdigest()[:16]
    except OSError:
        return None


def of_this_journal(snap, path):
    """Whether the state file snap was saved over the start of this journal: its number, and the mark of the start (a
    file saved before the mark was kept has none — only the number is checked, until hubd saves the file anew)."""
    head = snap.get("head")
    return snap["first"] == _first_seq(path) and (head is None or head == head_of(path))


PER_SESSION = 200            # and each session keeps its last events, even if it has been silent for long (02.10)


def carried_bytes(path):
    """How much of the journal is taken by what rotation carried over (up to and including its "rotate" snapshot); 0 if
    the journal did not start with a rotation. The journal cannot get smaller than this by rotation — hubd rotates
    again only when it has grown beyond it (10.10)."""
    try:
        with open(path, "rb") as f:
            got = _carry(f)
    except OSError:
        return 0
    return got[0] if got else 0


def _carry(f):
    """What rotation carried over, in a journal opened from the start: (end — after the "rotate" snapshot, start of that
    snapshot's line); None if the journal did not start with a rotation (the first line is not a snapshot) or there is no
    "rotate" snapshot. An empty carry means "rotate" is the first line. Snapshot lines run to tens of MB: only their
    beginnings are looked at."""
    size = 0
    for line in f:
        size += len(line)
        if b'"type":"core.snapshot"' in line[:200]:
            if b'"why":"rotate"' in line[:300]:
                return size, size - len(line)
        elif size == len(line):   # the first event is not a snapshot: the journal did not start with a rotation
            return None
    return None


_SEQ = re.compile(rb'^\{"seq":([0-9]+),')
_START = re.compile(rb'"why":"rotate-start","archive":"([^"/\\]+)"')
_SCRUBBED = re.compile(rb'"why":"rotate-start","archive":"[^"/\\]+","scrub":true')


def go_on(path, ino, offset, now):
    """The reader has read file ino up to offset, and path is now file now (identity): (archive events after offset,
    where to read path from) — or None: read path from the start (it did not start from a rotation of that file, or
    another rotation happened meanwhile). The page (core/tail), the Telegram bot and the watchdog use one rule."""
    got = rotated_from(path, ino, offset) if offset else None
    return None if got is None or got[2] != now else got[:2]


def scrubbed(path):
    """The journal began with the rotation of `hub journal scrub` (rotate scrub=True): what a reader carried over from the old
    one is not masked in its memory - a reader that writes what it folded to disk (the page's checkpoint) reads it anew."""
    try:
        with open(path, "rb") as f:
            return bool(_SCRUBBED.search(f.readline(300)))
    except OSError:
        return False


def rotated_from(path, ino, offset):
    """Journal path started with a rotation of file ino, which the reader has read up to offset: (archive events after
    offset, where the "rotate" snapshot starts in path — the end of what was carried over). The reader finishes the
    archive, folds this snapshot (its state is the core's) and goes on without folding the carried part again: its
    tail would land on the pre-tail snapshot, which can be a week old — sessions born later are not in it (22.10: a
    session file leaked into the hub feed) — and the feed was rebuilt entirely. None if not so (the archive is another
    file: two rotations between reads, or a copy instead of a link; the journal did not start with a rotation; the
    carry was taken from an older archive too; the end of the archive does not join the "rotate" snapshot) — read path
    from the start. The third item is the inode of the path just read: the reader compares it with its own."""
    if not ino:
        return None
    try:
        with open(path, "rb") as f:   # one open file: the carry, its end and the number of the "rotate" snapshot all come from it
            now = os.fstat(f.fileno()).st_ino
            first = f.readline(300)
            name, start = _START.search(first), _SEQ.match(first)
            if not name or not start:   # not from a rotation — do not read further (snapshots are tens of MB)
                return None
            archived = os.path.join(os.path.dirname(path), ARCHIVE, os.path.basename(name[1].decode()))
            # the carry was taken from an older archive too (history backfill, needs_backfill) — the reader has not read it
            if not _seqs(archived) or int(start[1]) < archive_first(archived) or identity(archived) != ino:
                return None
            f.seek(0)
            got = _carry(f)
            if not got:
                return None
            rotate_at = got[1]
            f.seek(rotate_at)
            rotated = _SEQ.match(f.read(64))
        with open(archived, "rb") as f:
            if os.fstat(f.fileno()).st_ino != ino:
                return None
            size = f.seek(0, 2)
            if offset >= size:   # everything is read — only the number of the last line is needed
                f.seek(_line_start(f.fileno(), size - 1))
                tail = _SEQ.match(f.read(64))
                events, last = [], int(tail[1]) if tail else None
            else:
                f.seek(offset)
                events = [json.loads(x) for x in f.read().split(b"\n") if x.strip()]
                last = events[-1].get("seq") if events else None
    except (OSError, ValueError):   # a broken line in the archive — as before, from the start
        return None
    if last is None or last != archive_last(archived) or not rotated or int(rotated[1]) != last + 1:
        return None
    return events, rotate_at, now


def session_of(e):
    d = e.get("data") or {}
    if e["type"] == "message.received":
        return d.get("to")
    if e["type"] == "delegation.created":
        return d.get("session")
    return e.get("entity")


def agent_steps(events):
    """Numbers of the helper agent's step events (text, call, call result): they do not go into the session feed."""
    steps, ids = set(), set()
    for e in events:
        d = e.get("data") or {}
        if e["type"] in ("session.said", "turn.tool") and d.get("parent"):
            steps.add(e["seq"])
            if e["type"] == "turn.tool":
                ids.add(d.get("id"))
    for e in events:
        if e["type"] == "turn.tool_result" and (e.get("data") or {}).get("id") in ids:
            steps.add(e["seq"])
    return steps


def needs_backfill(state_dir):
    """The journal started with a rotation lacking session history (before 02.10) and its archive is in place — the history is taken from there."""
    path = os.path.join(state_dir, "journal.jsonl")
    try:
        with open(path, "rb") as f:
            first = json.loads(f.readline())
    except (OSError, ValueError):
        return False
    d = first.get("data") or {}
    return (first.get("type") == "core.snapshot" and d.get("why") == "rotate-start" and "per_session" not in d
            and os.path.exists(os.path.join(state_dir, ARCHIVE, d.get("archive") or "-")))


def rotate(state_dir, state, keep=None, per_session=None, scrub=False, compact_previous=False):
    """The old journal goes to the archive unchanged (readers go on in it by its file). The new one: a pre-tail snapshot,
    the tail (the KEEP last events and the PER_SESSION last ones of each session, without old snapshots), a snapshot of
    now. compact_previous (journal.compact_on_rotate): the new journal's first line is marked "compact", and when a
    marked journal is archived, the archive it began from is compacted (compact) — it is no longer the newest; journals
    rotated without the setting are not marked, and their archives are compacted only by command. The
    fold starts from the last snapshot, the feed (views.Reader) from the first and sees the tail. scrub — `hub journal scrub`: the tail and both
    snapshots are written as the current hub writes them (core/scrub.py), no keys of the previous one are in the new
    journal; the command cleans the archive."""
    from core import reducer
    if scrub:
        from core import scrub as cleaner
        ev_of, st_of = (lambda e: cleaner.clean(e) or e), (lambda s: cleaner.clean_state(s) or s)
    else:
        ev_of = st_of = lambda x: x  # noqa: E731
    path = os.path.join(state_dir, "journal.jsonl")
    events = read(path)
    if not events:
        return None
    if needs_backfill(state_dir):  # the previous rotation lacks session history — take it from its archive
        old = read(os.path.join(state_dir, ARCHIVE, events[0]["data"]["archive"]))
        last = old[-1]["seq"] if old else 0
        source = old + [e for e in events if e["seq"] > last]
    else:
        source = events
    clean = lambda st: {k: v for k, v in st.items() if not k.startswith("_")}  # noqa: E731
    keep = KEEP if keep is None else keep
    per_session = PER_SESSION if per_session is None else per_session
    body = [e for e in source if e["type"] != "core.snapshot"]
    chosen = {e["seq"] for e in body[-keep:]} if keep else set()
    if per_session:
        by = {}
        sessions = state.get("sessions", {})
        steps = agent_steps(body)   # 13.10: a session waited for an agent — its 200 last events were agent steps, the feed went empty
        for e in body:
            name = session_of(e)
            if e["seq"] in steps:
                continue
            # not for a removed one: they are the majority in the state, and the tail grew with each (10.10: 112 of 140, 16 MB of 21)
            if name in sessions and sessions[name].get("state") != "removed":
                by.setdefault(name, []).append(e["seq"])
        for seqs in by.values():
            chosen.update(seqs[-per_session:])
    carried = [e for e in body if e["seq"] in chosen]
    archive_dir = os.path.join(state_dir, ARCHIVE)
    os.makedirs(archive_dir, exist_ok=True)
    archived = os.path.join(archive_dir, f"journal-{events[0]['seq']}-{events[-1]['seq']}.jsonl")
    name = os.path.basename(archived)
    tmp = path + ".rotate"
    with open(tmp, "w", encoding="utf-8") as f:
        if carried:
            idx = next(i for i, e in enumerate(source) if e is carried[0])
            before = reducer.replay(source[:idx])
            f.write(_line({"seq": carried[0]["seq"] - 1, "ts": carried[0]["ts"], "src": "core", "type": "core.snapshot",
                           "entity": None, "data": {"why": "rotate-start", "archive": name,   # "scrub" right after "archive": _SCRUBBED
                                                    **({"scrub": True} if scrub else {}),
                                                    **({"compact": True} if compact_previous else {}),
                                                    "per_session": per_session,
                                                    "from": body[-keep:][0]["seq"] if keep and body else None,
                                                    "state": clean(st_of(before))}}))
            for e in carried:
                f.write(_line(ev_of(e)))
        f.write(_line({"seq": events[-1]["seq"] + 1, "ts": round(time.time(), 3), "src": "core", "type": "core.snapshot",
                       "entity": None, "data": {"why": "rotate", "archive": name,
                                                **({"compact": True} if compact_previous else {}),
                                                "state": clean(st_of(state))}}))
        f.flush()
        platforms.current.sync(f.fileno())
    linked = not os.path.exists(archived)   # already there — hubd died between the link and the replace of this same journal
    if linked:
        os.link(path, archived)     # the archive is the same file, byte for byte; then the journal is replaced atomically
    platforms.current.sync_dir(archive_dir)   # the archive name is on disk before the journal's old name goes
    try:
        platforms.current.replace(tmp, path)
    except OSError:   # Windows: a reader holds the journal past the retries — it stays as it was, and the link goes: it
        modrun._drop(tmp)   # would be a second name of the live journal, growing with it (if the same reader refuses the
        if linked:          # unlink too, archives() skips the name, and the next rotation drops it)
            modrun._drop(archived)
        raise
    platforms.current.sync_dir(state_dir)
    # names left by refused tries whose unlink was refused as well: of each file only its newest name stays (this journal's
    # is archived) — before compact (below) or a scrub gives the file a new inode: an old name would then be an archive
    # of its own again, uncompacted
    for names in _by_file(_archive_paths(archive_dir)).values():
        keep = archived if archived in names else names[-1]
        for other in names:
            if other != keep:
                print(f"journal: dropped a redundant archive name {os.path.basename(other)} — the same file as {os.path.basename(keep)}", flush=True)
                modrun._drop(other)
    try:
        os.unlink(os.path.join(state_dir, STATE_FILE))
    except FileNotFoundError:
        pass
    except PermissionError:   # Windows: a reader (`hub`) holds the file — rotation must not fail; the old file is harmless:
        pass                  # load_state sees by the first seq that it belongs to the previous journal, and save_state replaces it
    first = events[0].get("data") or {}
    if compact_previous and events[0]["type"] == "core.snapshot" and first.get("compact") and first.get("archive"):
        # the journal just archived began under this rule: the archive it began from is no longer the newest — readers
        # do not go on in it, and what it repeats now lies in the archive before it
        previous = os.path.join(archive_dir, os.path.basename(first["archive"]))
        try:
            if shutil.disk_usage(archive_dir).free < os.path.getsize(previous) + SPARE:
                raise OSError(i18n.t("cli.compact.no_space"))
            compact(previous)
        except OSError as e:   # a full disk, a reader holding the file (Windows): the archive stays whole
            print(f"journal: archive {first['archive']} not compressed — {e}", flush=True)
    return archived


_ROTATED = re.compile(rb'"why":"rotate","archive":"([^"/\\]+)"')   # the snapshot rotate writes after the carry
_SNAPSHOT = b'"type":"core.snapshot"'
_TEMP = re.compile(r"\.journal-[0-9]+-[0-9]+\.jsonl\..+")   # compact's unfinished file next to the archive
SPARE = 64 << 20   # free space beyond a compacted archive's size: other writers keep theirs


def _previous(path, head):
    """The archive a rotation snapshot line (its start, head) names, next to archive path, if it is in place."""
    got = _ROTATED.search(head) if _SNAPSHOT in head[:200] else None
    previous = got and os.path.join(os.path.dirname(path), os.path.basename(got[1].decode()))
    return previous if previous and os.path.exists(previous) else None


def before(path):
    """The archive that archive path continues, if path starts right at its own events (compact, or a rotation with
    nothing to carry) and that archive is in place; else None. A reader that folds one archive at a time (the feed
    history, the "Flow") reads it first: a turn or a tool call started there ends in path."""
    try:
        with open(path, "rb") as f:
            return _previous(path, f.readline(300))
    except OSError:
        return None


def _plan(path, f):
    """What compact keeps of archive path (open as f, from the start): byte ranges [(start, end)] in order. Kept: the
    first line — the rotation snapshot (the state the archive's own events start from), or the first line of a journal
    that did not start with a carry — and the events after it; dropped: the carry from the previous archive and the
    later snapshots (the next archive begins with the same state). If the archive the carry came from is gone, the
    carry stays: it may lie nowhere else. Snapshot lines run to tens of MB: only their beginnings are looked at."""
    keep, carry, own, pos = [], [], False, 0
    for i, line in enumerate(f):
        at, pos = (pos, pos + len(line)), pos + len(line)
        snapshot = _SNAPSHOT in line[:200]
        if not i and not (snapshot and b'"why":"rotate-start"' in line[:300]):
            own, keep = True, [at]   # not a carry: everything from here is the journal's own
        elif not own and snapshot and _ROTATED.search(line[:300]):
            own, keep = True, ([] if _previous(path, line[:300]) else carry) + [at]
        elif not own:
            carry.append(at)
        elif not snapshot:
            keep.append(at)
    if not own:   # the carry never ended with a rotation snapshot: not as rotate writes it — kept whole
        return [(0, pos)]
    merged = []
    for a, b in keep:
        if merged and merged[-1][1] == a:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged


def replacing(path):
    """The lock under which compact and `hub journal scrub` replace an archive: compact's check that the file is still
    the one it read and its replacement are one step — scrub cannot put its cleaned file in between and lose it."""
    return platforms.current.exclusive(os.path.join(os.path.dirname(os.path.dirname(path)), "archive.lock"))


def compact(path, backup_dir=None, apply=True):
    """The archive path without what it repeats (_plan): a new file next to it with the same access and time, and a
    replacement; backup_dir — the old file is hard-linked there first. -> (bytes before, bytes after); nothing to drop —
    None, the file is not touched. apply=False — only counts. The file changed meanwhile (`hub journal scrub` rewrote
    it) — OSError, it is left as the other writer made it."""
    tmp = None
    try:
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
            plan = _plan(path, f)
            after = sum(b - a for a, b in plan)
            if after == st.st_size:
                return None
            if not apply:
                return st.st_size, after
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix="." + os.path.basename(path) + ".")
            with os.fdopen(fd, "wb") as out:
                for a, b in plan:
                    f.seek(a)
                    left = b - a
                    while left:
                        chunk = f.read(min(left, 1 << 20))
                        if not chunk:
                            raise OSError(i18n.t("cli.compact.shorter"))
                        out.write(chunk)
                        left -= len(chunk)
                out.flush()
                platforms.current.sync(out.fileno())
        # the replacement comes after the archive is closed: Windows refuses to replace a file this process holds open
        os.chmod(tmp, st.st_mode & 0o7777)
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))   # readers take an archive's time as the time of its events
        with replacing(path):
            now = os.stat(path)
            if (now.st_ino, now.st_size, now.st_mtime_ns) != (st.st_ino, st.st_size, st.st_mtime_ns):
                raise OSError(i18n.t("cli.compact.changed"))
            if backup_dir:
                platforms.current.makedirs_private(backup_dir)
                os.link(path, os.path.join(backup_dir, os.path.basename(path)))
            platforms.current.replace(tmp, path)
    except BaseException:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise
    platforms.current.sync_dir(os.path.dirname(path))
    return st.st_size, after


def compact_archives(state_dir, apply=False, backup_dir=None, no_space=lambda path, need: None,
                     failed=lambda path, error: None):
    """`hub journal compact`: every archive but the newest (readers go on in it by its file) without what it repeats.
    -> [(path, bytes before, bytes after)]; apply — rewritten, backup_dir — the old files are hard-linked there (the
    space comes free once that folder is removed). Not enough free space for a new file — the archive is skipped
    (no_space(path, bytes needed)); it could not be rewritten — failed(path, error); a repeat takes both again.
    Unfinished files of a compaction that was cut off (a crash) older than an hour are removed."""
    folder = os.path.join(state_dir, ARCHIVE)
    if apply:
        try:
            names = os.listdir(folder)
        except FileNotFoundError:
            names = []
        for name in filter(_TEMP.fullmatch, names):
            try:
                if os.path.getmtime(os.path.join(folder, name)) < time.time() - 3600:
                    os.remove(os.path.join(folder, name))
            except OSError:
                pass
    found = []
    for path in archives(state_dir)[:-1]:
        try:
            got = compact(path, apply=False)
            if got and apply:
                if shutil.disk_usage(folder).free < got[1] + SPARE:
                    no_space(path, got[1] + SPARE)
                    continue
                got = compact(path, backup_dir)
        except FileNotFoundError:   # removed while we were looking
            continue
        except OSError as e:   # a file held by a reader (Windows), rewritten meanwhile: left as it is
            failed(path, e)
            continue
        if got:
            found.append((path, *got))
    return found


def save_state(state_dir, state):
    """The state and the journal end it is folded up to — commands read only what comes after."""
    path = os.path.join(state_dir, "journal.jsonl")
    snap = {"seq": state["seq"], "offset": os.path.getsize(path), "first": _first_seq(path), "head": head_of(path),
            "state": {k: v for k, v in state.items() if not k.startswith("_")}}
    target = os.path.join(state_dir, STATE_FILE)
    with open(target + ".tmp", "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))
    try:
        platforms.current.replace(target + ".tmp", target)
    except OSError:   # the whole state (and what `hub journal scrub` has just cleaned) is not left behind in .tmp
        modrun._drop(target + ".tmp")
        raise


def read_state_file(state_dir):
    """The state file as saved (seq, offset, first, head — the mark of the journal's start, journal.head_of; absent in a file saved
    before it was kept — and state). Read whole and closed before parsing: while a reader holds it,
    Windows refuses hubd the replace, and parsing tens of MB takes longer than reading them. OSError, ValueError."""
    with open(os.path.join(state_dir, STATE_FILE), encoding="utf-8") as f:
        text = f.read()
    return json.loads(text)


DECISIONS_ARCHIVES = 4   # how many of the newest archives hubd reads, after the live journal, for `hub decided` records


def decided_events(state_dir, kept, archived=True):
    """The owner.decided / owner.decided_revoked events and the core's rejections of them, oldest first: the live journal
    and (archived) the newest archives, until kept records are found - read line by line: the journal's last line is a
    snapshot of the whole state, tens of MB, and a window over the end of the file holds nothing else (10.11)."""
    files = [os.path.join(state_dir, "journal.jsonl")] + (archives(state_dir)[::-1][:DECISIONS_ARCHIVES] if archived else [])
    found = {}
    for path in files:
        try:
            with open(path, "rb") as f:
                for line in f:
                    if b'"owner.decided' not in line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("type") in ("owner.decided", "owner.decided_revoked", "core.rejected"):
                        found.setdefault(ev["seq"], ev)
        except OSError:
            continue
        refused = {(e.get("data") or {}).get("of") for e in found.values() if e["type"] == "core.rejected"}
        if sum(e["type"] == "owner.decided" and n not in refused for n, e in found.items()) >= kept:
            break
    return [found[n] for n in sorted(found)]


def _rotated(path):
    """The journal starts with a snapshot (a rotation): what came before it is in the archives."""
    try:
        with open(path, "rb") as f:
            return b'"core.snapshot"' in f.readline(400)
    except OSError:
        return False


def load_state(state_dir, decisions=False):
    """The state now: the state file + the journal tail after it; a file not from this journal — the whole journal.
    decisions (hubd at start): a state not yet searched for `hub decided list` records (reducer.decisions_pending) gets
    them from the live journal and the newest archives; commands read the state often and skip that."""
    from core import reducer
    path = os.path.join(state_dir, "journal.jsonl")
    try:
        snap = read_state_file(state_dir)
        with open(path, "rb") as f:
            size = f.seek(0, 2)
            if snap["offset"] > size or not of_this_journal(snap, path):
                raise ValueError("the state file is not of this journal")
            f.seek(snap["offset"])
            tail = [json.loads(x) for x in f.read().split(b"\n") if x.strip()]
        if tail and tail[0]["seq"] != snap["seq"] + 1:
            raise ValueError("the state file does not join the journal")
    except (OSError, ValueError, KeyError):
        state = reducer.replay(read(path))
        if decisions and _rotated(path) and reducer.decisions_pending(state, True):   # a whole journal has them all
            reducer.backfill_decisions(state, decided_events(state_dir, reducer.DECISIONS_KEPT), complete=True)
        return state
    state = reducer.loaded({**reducer.empty(), **snap["state"]})
    state["seq"] = snap["seq"]
    rotated = _rotated(path)
    if decisions and reducer.decisions_pending(state, rotated):
        reducer.backfill_decisions(state, decided_events(state_dir, reducer.DECISIONS_KEPT, archived=rotated), complete=True)
    return reducer.fold(state, tail)


AHEAD = object()   # state_at: the state file is further along the journal than the reader - the reader will get there


def state_at(state_dir, seq, snap=None):
    """The core's state exactly at event seq: the state file + the journal events after it up to seq; None - no file, a
    file of another journal, one that does not join the journal; AHEAD - the file is ahead of seq. For a reader that has
    read the journal only to seq and wants the core's decisions up to the same place - load_state gives the state at the
    journal's end now, and on a journal hubd writes into that is never the same place. The same checks as load_state (no
    replay of the whole journal instead: a reader has its own). snap - the state file already read (read_state_file),
    taken outside the reader's lock; it is consumed (folded in place)."""
    path = os.path.join(state_dir, "journal.jsonl")
    try:
        snap = snap or read_state_file(state_dir)
        if snap["seq"] > seq:
            return AHEAD
        tail = []
        with open(path, "rb") as f:
            if snap["offset"] > f.seek(0, 2) or not of_this_journal(snap, path):
                return None
            f.seek(snap["offset"])
            for line in f:
                if not line.endswith(b"\n"):
                    break   # an unfinished line: hubd is writing it
                if line.strip():
                    event = json.loads(line)
                    if event["seq"] > seq:
                        break
                    tail.append(event)
        if tail and tail[0]["seq"] != snap["seq"] + 1:
            return None
        from core import reducer
        state = reducer.loaded({**reducer.empty(), **snap["state"]})
        state["seq"] = snap["seq"]
        state = reducer.fold(state, tail)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return state if state.get("seq") == seq else None


def identity(path):
    """Which journal file this is: after a rotation it is another one (readers continue from the archive — go_on, rotated_from)."""
    try:
        return os.stat(path).st_ino
    except FileNotFoundError:
        return None


def read_tail(path, max_bytes=4 << 20):
    """The last events without reading the whole journal (a command waits for its event in the tail)."""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, 2)
            f.seek(max(0, size - max_bytes))
            chunk = f.read()
    except FileNotFoundError:
        return []
    lines = chunk.split(b"\n")
    if size > max_bytes:
        lines = lines[1:]  # the first line is a fragment
    out = []
    for line in lines:
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


# answer to (None, None) from decided: the fact is in the inbox but hubd has not recorded it (page, hubmod.owner(wait=));
# the page itself says it in the owner's language (web.srv.queued), this module API constant is that string's English
NOT_DECIDED = "The command is in the hub's spool, but the core has not recorded it yet. Is hubd running? (hub status)"


def mark(path):
    """Where the journal ends now — the end of the last complete line: decided(start=mark(…)) reads only what is
    appended after it. Take it before putting a fact in the inbox. A fragment at the end (a failed write) will be cut
    by hubd back to its start and the next record goes there — the mark stands at that start."""
    try:
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
            base = max(0, st.st_size - 65536)
            f.seek(base)
            chunk = f.read(st.st_size - base)
    except FileNotFoundError:
        return None, 0
    return st.st_ino, base + chunk.rfind(b"\n") + 1


def _appended(path, at):
    """Events appended after the mark at=(file, offset), and the new mark; None if the file is already another one
    (rotation) or shorter than the mark (hubd repaired it at start): then the tail, as without a mark. An unfinished
    last line waits for the next time."""
    ino, pos = at
    try:
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
            if st.st_ino != ino or st.st_size < pos:
                return None
            f.seek(pos)
            chunk = f.read(st.st_size - pos)
    except FileNotFoundError:
        return [], at
    done = chunk.rfind(b"\n") + 1
    out = []
    for line in chunk[:done].split(b"\n"):
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out, (ino, pos + done)


def waited(path, start, match, wait, max_bytes=4 << 20):
    """The first event appended after the mark `start` (mark()) for which match(event) is true, or None after `wait`
    seconds; after a rotation — looked for in the tail. Reads only what is appended, not the state (a command that waits
    for the core's decision on its request, core/buscli)."""
    end, at = time.time() + wait, start
    while True:
        got = _appended(path, at) if at is not None and at[0] is not None else None
        if got is None:   # rotated (or no file at the mark): the tail once, then follow from a new mark
            evs, at = read_tail(path, max_bytes)[-400:], mark(path)
        else:
            evs, at = got
        hit = next((e for e in evs if match(e)), None)
        if hit is not None or time.time() >= end:
            return hit
        time.sleep(0.1)


def decided(path, req, wait, max_bytes=4 << 20, start=None):
    """How the core decided a fact put in the inbox with the label req (hub in the terminal, page commands): (seq, None)
    — accepted, (seq, reason) — rejected (the reason is a string with the code beside it, forms.Why), (None, None) —
    not recorded within wait seconds (hubd is busy or down). The core writes the rejection in the same record as the
    fact (daemon.record); one more pass after the fact is for journals of earlier versions.
    start — a mark() taken before the inbox: read only what is appended after it (on a long wait, do not re-read the
    tail and do not lose the fact behind a batch of events); without it, the last 200 events of the tail."""
    end, seq, seen, at = time.time() + wait, None, False, start
    while time.time() < end:
        got = _appended(path, at) if at is not None else None
        if got is None:
            at, evs = None, read_tail(path, max_bytes)[-200:]
        else:
            evs, at = got
        for e in evs:
            if seq is None and (e.get("data") or {}).get("req") == req:
                seq = e["seq"]
            elif seq is not None and e["type"] == "core.rejected" and e["data"].get("of") == seq:
                # a string, as before, with the code beside it (forms.Why: .code is duplicate for a dedup_key repeat, else None)
                return seq, coretext.why_of(e["data"], REJECTED)
        if seq is not None and seen:
            return seq, None
        seen = seq is not None
        time.sleep(0.1)
    return None, None


def decided_all(path, reqs, wait, max_bytes=4 << 20, start=None):
    """decided() for several facts at once (the readings of one hub accounts probe): {req: (seq, None) | (seq, reason) |
    (None, None)} within one wait for all of them, one read of the journal per pass. A fact seen on the last pass gets
    one more pass for a refusal behind it (decided would call it not recorded)."""
    end, at, want = time.time() + wait, start, set(reqs)
    seqs, why, passes = {}, {}, 0
    while True:
        got = _appended(path, at) if at is not None else None
        if got is None:
            at, evs = None, read_tail(path, max_bytes)[-200:]
        else:
            evs, at = got
        for e in evs:
            req = (e.get("data") or {}).get("req")
            if req in want and req not in seqs:
                seqs[req] = (e["seq"], passes)
            elif e["type"] == "core.rejected":
                of = (e.get("data") or {}).get("of")
                for r, (seq, _) in seqs.items():
                    if seq == of and r not in why:
                        why[r] = coretext.why_of(e["data"], REJECTED)
        fresh = [r for r, (_, p) in seqs.items() if p == passes and r not in why]
        if len(seqs) == len(want) and not fresh or time.time() >= end and not fresh:
            break
        passes += 1
        time.sleep(0.1)
    return {r: (seqs[r][0], why.get(r)) if r in seqs else (None, None) for r in reqs}
