"""Live reading of the core journal - for processes next to hubd (the page, the schedule clock): they only read, and
write facts to the mailbox."""
import json
import os
import threading
import time

import platforms
from core import i18n, journal, views


ADOPT_EVERY = 300.0   # seconds between the comparisons of the page's state with the core's (adopt_core_state)
ADOPT_RETRY = 5.0     # ... when the core's state file is ahead of the page: the page will catch up
ADOPT_TRIES = 3       # ... when it could not be used (none, another journal): a few tries, then the usual interval again


class Tail:
    """Reads the journal from where it stopped; whole lines only. Wakes waiters on changes."""

    # the first pass over the journal (from the start: startup, rotation of the wrong file, a language change) is done (set by poll) - until then page
    # requests that need a reader do not wait on the lock for minutes (25.10: 96 MB under swap - over 10 min), but answer "later" at once
    ready = False

    def __init__(self, path, lang=None, page=True, *, checkpoint=False, on_rotate=None):
        self.path = path
        # page - build the page snapshot (views.Reader.snapshot) on every new event. The schedule clock does not need it:
        # it only needs the core state (reader.state), and a snapshot costs 0.1 s of CPU on a big journal (26.10: more than
        # half of the clock's idle time)
        self.page = page
        # lang(state) -> the hub language: the core's captions (views) are in it; when it changes (a setting, the owner's
        # language) the process language is switched and the feeds are reread from the journal start: their captions were computed in the old one
        self.lang = lang
        self.reader = views.Reader()
        # i18n.lang_facts of the state: replaced whole, read without a lock; before the first journal read (tens of seconds
        # on a big one) - from the core state file, so the page is in the hub language from the first request
        self.lang_state = i18n.saved_state(os.path.dirname(path) or ".") if lang else None
        self.on_rotate = on_rotate   # called (no arguments) when the journal is started anew: the archive is already there
        self.offset = 0
        self.changed = threading.Condition()
        self.snapshot = self.reader.snapshot() if page else None
        self.feed_posts = {}   # detached plugin posts from the latest completed publication; read-only
        self.feed_marks = {}   # what each channel of feed_posts was copied from (_feeds)
        self.records = {}   # state["modules"] of the same snapshot: whether modules are ready (core/modstate.view) - without the read lock
        self.dedup = {}     # state["web_dedup"] of the same snapshot: what the core accepted from the page (receipts) - without the read lock
        self.early, self.early_lock = (-1e9, None), threading.Lock()   # core_started: what the journal's end said, and when
        self.failed = None   # run died with this exception (the first pass did not finish): core_started is None then
        self.started = None   # state["started"] of the same snapshot: when the core came up ("About the hub") - without the read lock
        # state["updates"] of the same snapshot (the last 20, with their notes): the "Updates" screen reads them through
        # /api/about - without the read lock, and during a re-read from the start it is the previous list, not an empty one
        self.updates = []
        self.reading = threading.Lock()
        # fresh - the reader has no journal yet: until the first whole line (startup) or after a reset (rotation of the wrong
        # file, a language change), until not a single line of the new file is read; had - the journal was read at least once (an
        # empty or missing file after that means the journal is being replaced mid-way, not an empty hub); published - a
        # snapshot from the journal is published
        self.fresh, self.had, self.published = True, False, False
        self.checkpoint = None
        self.checkpoint_tried = False
        self.adopt_next = 0.0   # monotonic: when the state is compared with the core's next
        self.adopt_misses = 0   # the comparisons in a row that found no usable state file
        self.adopted = 0        # how many times the state was replaced by the core's (adopt_core_state)
        self.rev = 0            # `adopted` of the published snapshot: a replacement changes it with no new event (the stream's key)
        if checkpoint:
            from core.reader_cache import Checkpoint
            self.checkpoint = Checkpoint(path)

    def poll(self):
        # the state file is read and parsed (tens of MB) outside the lock: page requests do not wait on it
        snap = None
        if self.ready and time.monotonic() >= self.adopt_next:
            try:
                snap = journal.read_state_file(os.path.dirname(self.path) or ".")
            except (OSError, ValueError):
                pass
        with self.reading:
            fed = self._poll()
            # the pass from the start, if there was one, is over - if the reader already holds a journal: not "another rotation
            # happened while we were deciding" after a reset and not a new file without a single whole line (otherwise requests would take the empty reset reader)
            self.ready = not self.fresh
            if self.ready and time.monotonic() >= self.adopt_next and self.adopt_core_state(snap):
                fed = self._publish(True) or fed
            job = self.checkpoint.capture(self.reader, self.offset, self.ino) if fed and self.ready and self.checkpoint else None
        if job:
            self.checkpoint.write(job)   # hashing the journal and writing the file - without holding page requests
        return fed

    EARLY_FOR = 10.0   # seconds an answer of the journal's end is kept (one read for every poll of that time)

    def core_started(self):
        """When the core came up: from the published snapshot; before the first pass over the journal is done — from the
        journal's end, where the start's own event is, while that pass is still going: the update's watchdog asks it
        (/api/about) and does not wait for a fold that takes minutes on a big journal of a machine that has just booted.
        A pass that failed (the thread of run is gone with the exception) gives None: the page that cannot read its
        journal is not a hub that came up. A pass that hangs cannot be told from a slow one: no answer of its own, the
        hub's own checks (hub status) show it."""
        with self.changed:
            started, known = self.started, self.published
        if known:
            return started
        if self.failed:
            return None
        with self.early_lock:   # one read for everyone who asks at once
            at, got = self.early
            if time.monotonic() - at > (self.EARLY_FOR if got is not None else 1.0):   # no start yet: look again soon
                got = journal.last_started(self.path)
                self.early = (time.monotonic(), got)
            return got

    BUSY = object()

    def peek(self, fn, wait, poll=False):
        """fn(reader) under the read lock (poll - first read the journal tail, without rereading from the start). If the lock
        was not obtained within wait seconds - it is held by a reread of the journal from the start (startup, rotation of the wrong
        file, a language change: tens of seconds on a big one) - Tail.BUSY. The first pass over the journal (not ready) holds
        the lock to the end - BUSY at once, without waiting: the request does not hold a thread and a connection."""
        if not self.ready or not self.reading.acquire(timeout=wait):
            return self.BUSY
        try:
            if poll:
                self._poll(light=True)
            return fn(self.reader)
        finally:
            self.reading.release()

    def _poll(self, light=False):
        """light - only read the tail (from a page request): rereading the journal from the start (the first read, rotation
        of the wrong file, a language change) is the job of its own thread (run), not of a request."""
        restored = False
        if not light and self.checkpoint and not self.checkpoint_tried:
            self.checkpoint_tried = True
            saved = self.checkpoint.restore()
            if saved is not None:
                saved[0].epoch = self.reader.epoch   # a restarted web invalidates clients' old timeline revisions
                self.reader, self.offset, self.ino = saved
                self.fresh, self.had, restored = False, True, True
        ino = journal.identity(self.path)
        if light and (not self.offset or ino != getattr(self, "ino", ino)):
            return False
        went_on = restored
        if ino != getattr(self, "ino", ino):  # the journal was started anew (journal.rotate)
            if self.on_rotate:
                self.on_rotate()
            went_on = self._go_on(self.ino, ino)
            if not went_on:   # cannot continue - read the new one from the start
                self.reader, self.offset, self.ready, self.fresh = views.Reader(), 0, False, True
        want = None
        if not light and self.lang and self.offset and time.monotonic() - getattr(self, "lang_at", 0) >= 2:   # hub.json - not on every tick
            self.lang_at = time.monotonic()
            want = self.lang(self.reader.state)
        if want and want != i18n.current():
            i18n.use(want)
            self.reader, self.offset, self.ready, self.fresh = views.Reader(), 0, False, True
            went_on = False
        self.ino = ino
        try:
            with open(self.path, "rb") as f:
                if os.fstat(f.fileno()).st_ino != ino:   # another rotation happened while we were deciding: this file - next time
                    return self._publish(went_on)
                f.seek(self.offset)
                chunk = f.read()
        except FileNotFoundError:
            self.fresh = self.fresh and self.had   # there is no journal and there was none - nothing to read
            return self._publish(went_on)
        end = chunk.rfind(b"\n")
        if end >= 0 or not (chunk or self.offset or self.had):   # a whole line, or the empty journal of a new hub
            self.fresh, self.had = False, True
        if end < 0:
            if restored:
                self.adopt_core_state()
            return self._publish(went_on)
        events = []
        for line in chunk[:end].split(b"\n"):
            if line.strip():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
        first = self.offset == 0
        self.reader.feed_all(events)
        fed = bool(events)
        self.offset += end + 1
        if (first and fed) or restored:
            self.adopt_core_state()
        return self._publish(fed or went_on)

    def _go_on(self, ino, now):
        """Rotation of the very file we were reading: read the archive on from our position and stand after what was carried
        over (journal.rotated_from) - the feeds stay the same, the carried-over part is not folded again. False - read the new one from the start."""
        if self.checkpoint and journal.scrubbed(self.path):
            return False   # hub journal scrub: the feeds in memory hold what was cleaned - the checkpoint must not write them back
        got = journal.go_on(self.path, ino, self.offset, now)
        if got is None:
            return False
        events, self.offset = got   # next comes the "rotate" snapshot: state as the core's, the feeds stay
        self.reader.feed_all(events)
        self.reader.drop_removed()   # the reader outlives the journal: do not accumulate the feeds of removed sessions
        return True

    def _publish(self, fed):
        if fed:
            self.lang_state = i18n.lang_facts(self.reader.state)   # the hub language - without a lock (the page, on every request)
            # build outside the snapshot lock: /api/snapshot and the page's threads take it under the same lock, and on a big
            # state under load the build would hold them
            st = self.reader.state
            # Published snapshots are read-only. Copy only histories whose revision changed:
            # a busy session must not allocate every sleeping session's nested rows again.
            snap = self.reader.snapshot(previous=self.snapshot) if self.page else None
            records = {k: dict(v) for k, v in (st.get("modules") or {}).items()}
            dedup = {k: dict(v) for k, v in (st.get("web_dedup") or {}).items()}
            updates = [dict(u) for u in (st.get("updates") or [])[-20:]]
            feeds = self._feeds(st) if self.page else {}   # only the page serves /api/feed
            with self.changed:
                self.feed_posts = feeds
                if self.page:
                    self.snapshot = snap
                self.records, self.dedup, self.started, self.updates = records, dedup, st.get("started"), updates
                self.rev = self.adopted
                self.published = True
                self.changed.notify_all()
        return fed

    def _feeds(self, st):
        """Plugin channel posts detached from the reader: /api/feed serializes them while the reader folds the next batch.
        A channel only gets appended to and trimmed from the front, and a post never changes, so the same length, first
        and last post of the same reader (its object and epoch: a restored checkpoint keeps the epoch) mean the same posts - such a channel keeps its previous copy."""
        out, marks = {}, {}
        for k, posts in (st.get("feeds") or {}).items():
            marks[k] = (id(self.reader), self.reader.epoch, len(posts), posts and posts[0]["id"], posts and posts[-1]["id"])
            kept = self.feed_posts.get(k)
            out[k] = kept if kept is not None and self.feed_marks.get(k) == marks[k] else tuple(dict(p) for p in posts)
        self.feed_marks = marks
        return out

    def adopt_core_state(self, snap=None):
        """The state is the core's (the state file + the journal tail up to where this reader has read), not a recomputation
        of old history by today's rules (04.10: a fork was in an unclear state - after queue changes the page kept a message
        as "sending" for a fork that no longer existed in the core). The feeds stay from the journal. A file about a
        different position - as it was. It is repeated (ADOPT_EVERY): hubd decides by the code it started with, this process
        recomputes new events by its own, and after a core update that has not restarted hubd yet the two part (10.11: an
        accepted spec stayed a card "Spec is ready", the owner's words - "Sending", the core refusing the card's buttons).
        The comparison is at the place the reader stands at, not at the journal's end now: hubd writes all the time.
        True - the state was replaced."""
        seq = self.reader.state.get("seq")
        try:
            core = journal.state_at(os.path.dirname(self.path) or ".", seq, snap) if isinstance(seq, int) else None
        except Exception:  # noqa: BLE001 - a broken state file: the recomputation stays, as it always did
            core = None
        # a file that is ahead of what the page has read: soon again, the page catches up; one that does not fit (none,
        # another journal): a few tries, then the interval
        self.adopt_misses = self.adopt_misses + 1 if core is None else 0
        self.adopt_next = time.monotonic() + (ADOPT_RETRY if core is journal.AHEAD else
                                              ADOPT_EVERY if core is not None or self.adopt_misses >= ADOPT_TRIES
                                              else ADOPT_RETRY)
        if core is journal.AHEAD:
            core = None
        if core is None or core == self.reader.state:
            return False
        self.reader.state = core
        self.adopted += 1
        return True

    def alerts_now(self):
        """Open notification reasons right now (core/owner.py alerts) - for the push module, only while it is on: they are not
        part of the snapshot on every event (push text for 50 notifications is milliseconds on every redraw)."""
        from core import owner
        with self.reading:
            now = time.time()
            key = (id(self.reader), self.reader.state.get("seq"), int(now // 60))   # the same state, the same minute
            if getattr(self, "_alerts", (None,))[0] != key:
                self._alerts = (key, owner.alerts(self.reader.state, now))
            return [dict(a) for a in self._alerts[1]]

    def run(self, stop, every=0.3):
        try:
            while not stop.is_set():
                self.poll()
                stop.wait(every)
        except BaseException as e:   # the thread ends here as it did: the page serves what it had; core_started knows it
            self.failed = repr(e)
            raise


# --- hub show: the parsed journal sits next to it, the next call reads only what is new (07.10: 10.7 s on 14.7 MB) ---
CACHE = os.path.join("cache", "show.pickle")
SESSION_CACHE = os.path.join("cache", "show-session.pickle")   # a session reads English captions: its own parse
# the search reads the whole journal, not the last 400 rows of a feed: its own parse (one more file of the same size as the first)
FULL_CACHE = os.path.join("cache", "show-full.pickle")
FULL_SESSION_CACHE = os.path.join("cache", "show-full-session.pickle")
FULL_LIMIT = 10 ** 9   # rows of a feed kept: all of the journal (as history.parse keeps an archive's)
CACHE_KEY = os.path.join("cache", "show.key")   # pickle executes code on load: only a cache signed by this key is loaded


def _cache_key(state_dir):
    """The hub's own key for the cache (32 random bytes, 0600): created once."""
    path = os.path.join(state_dir, CACHE_KEY)
    try:
        with open(path, "rb") as f:
            key = f.read()
        if len(key) == 32:
            return key
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    key = os.urandom(32)
    tmp = f"{path}.{os.getpid()}.{key[:4].hex()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    platforms.current.replace(tmp, path)
    return key


def _code():
    """The feed rules changed (a hub update) - what was parsed by the old ones is no good."""
    from core import reducer
    stamp = [i18n.current()]   # the feed captions are in the process language
    for mod in (views, reducer):
        try:
            stamp.append(os.stat(mod.__file__).st_mtime_ns)
        except (OSError, TypeError):
            stamp.append(None)
    return tuple(stamp)


def cached(state_dir, full=False):
    """The journal reader (views.Reader) with the feeds of all sessions as of now. full — every row the journal holds
    (`hub show --grep`): the feeds are not cut to the last TIMELINE_LIMIT rows and the fold starts at the journal's first
    line, not at the last snapshot (views.recent); without it the feeds are the recent ones (`hub show`)."""
    import hashlib
    import hmac
    import pickle
    import tempfile
    path = os.path.join(state_dir, "journal.jsonl")
    store = os.path.join(state_dir, (FULL_SESSION_CACHE if full else SESSION_CACHE) if os.environ.get("HUB_SESSION")
                         else FULL_CACHE if full else CACHE)
    key = (journal.identity(path), _code())
    try:
        secret = _cache_key(state_dir)
    except OSError:
        secret = None   # no key - no cache: the journal is read whole
    try:
        if secret is None:
            raise ValueError("no key")
        with open(store, "rb") as f:
            sig, blob = f.read(32), f.read()
        if not hmac.compare_digest(sig, hmac.new(secret, blob, hashlib.sha256).digest()):
            raise ValueError("not signed by this hub")   # a foreign or an old cache - parse anew
        saved = pickle.loads(blob)
        if saved["key"] != key:
            raise ValueError("another journal or other rules")
        reader, offset = saved["reader"], saved["offset"]
    except Exception:  # noqa: BLE001 - no file, broken, or stale: parse anew
        reader, offset = views.Reader(limit=FULL_LIMIT if full else None), 0
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
    except FileNotFoundError:
        return reader
    end = chunk.rfind(b"\n") + 1   # whole lines only: a half-written one will be read next time
    events = []
    for line in chunk[:end].split(b"\n"):
        if line.strip():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    if not events:
        return reader
    reader.feed_all(events if offset or full else views.recent(events))
    if secret is None:
        return reader
    try:
        os.makedirs(os.path.dirname(store), exist_ok=True)
        blob = pickle.dumps({"key": key, "offset": offset + end, "reader": reader}, protocol=pickle.HIGHEST_PROTOCOL)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(store), prefix=".show.")
        with os.fdopen(fd, "wb") as f:
            f.write(hmac.new(secret, blob, hashlib.sha256).digest() + blob)
        os.replace(tmp, store)
    except OSError:
        pass   # not written - the next call will parse anew, the answer exists anyway
    return reader


def known(state_dir, name):
    """Whether such a session exists - by the core state file, without the journal; no file or it cannot be read - "yes", let the
    feed decide."""
    try:
        return name in journal.read_state_file(state_dir)["state"]["sessions"]
    except (OSError, ValueError, KeyError, TypeError):
        return True
