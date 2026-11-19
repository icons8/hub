"""Keys that an earlier hub already recorded: `hub journal scrub` rewrites them the way the current hub writes them
(core/redact.py) - MCP servers without env and headers, tool input and output, background and permission requests are
masked. By command only: no step after an update, no rotation at startup.

- Journal archives (`journal-archive/journal-*.jsonl`, and the `cut-*.jsonl` cut off by repair): line for line of the
  old one, the same event with the same seq, unchanged lines byte for byte; a new file next to it and an atomic
  replacement, an archive without keys is not rewritten. The archive's parse for the page next to it (core/history.py,
  core/shown.py) is deleted - the page will parse it anew.
- Snapshots (`core.snapshot`, state.json): all state fields that the fold takes from the masked types (clean_state;
  the test tests/test_scrub.py folds a journal with a key in all types and looks for the key in the state). Message
  texts in the queue are not touched: they will be delivered verbatim.
- The live hubd journal is not rewritten (hubd writes to it, readers hold offsets in it), but rotated with cleaning
  (journal.rotate scrub=True): the old one goes to the archive, and the archive is cleaned right here.
- Which archives are already cleaned - `<state>/scrub.done` (name, size, time after the rewrite): a repeat does not
  read them; an archive that is not there or has changed (a rollback to the earlier code) goes through again.
"""
import glob
import json
import os
import re
import shutil
import tempfile
import time

import platforms
from core import history, journal, redact, shown, usage, uses

# where there may be something to clean: the type is at the start of every journal line (`{"seq":...,"ts":...,"src":...,"type":...`);
# the masked types are taken from the journal's list, so a type added there is cleaned here too
HINTS = tuple(f'"type":"{t}"'.encode() for t in (*journal.MASKED, "catalog.loaded", "core.snapshot",
                                                   "permission.requested", "watch.alert"))
DONE = "scrub.done"
# the page's parses and the "Spending" facts are taken from the archive before cleaning; .sessions.json (who from which event) stays valid
PARSES = (history.CACHE, history.INDEX, shown.SIDE, usage.SIDECAR, uses.SIDE)


def _request(req):
    """A permission request as the current hubd holds it (Hub.hold_input): the input is masked, a form question stays as
    written (answers are matched by the question text)."""
    if not isinstance(req, dict) or req.get("tool") == "AskUserQuestion" or not isinstance(req.get("input"), dict):
        return req
    masked = redact.mask_data(req["input"])
    return req if masked == req["input"] else {**req, "input": masked, "masked": True}


def _session(sess):
    """A snapshot session: the requests it awaits and its background (the title is a command, out is its output) - through the mask."""
    if not isinstance(sess, dict):
        return sess
    out = dict(sess)
    for k in ("pending", "asks"):
        if isinstance(sess.get(k), list):
            out[k] = [_request(r) for r in sess[k]]
    if isinstance(sess.get("background"), dict):
        out["background"] = {tid: redact.mask_data(b) for tid, b in sess["background"].items()}
    cut = sess.get("cut_turn")
    if isinstance(cut, dict) and isinstance(cut.get("background"), list):   # titles of the cut-off background
        out["cut_turn"] = {**cut, "background": redact.mask_data(cut["background"])}
    return out


def clean_state(state):
    """The state (a snapshot, state.json) as the current hub would fold it, or None - it already is."""
    if not isinstance(state, dict):
        return None
    new = dict(state)
    if isinstance(state.get("catalog"), dict):
        new["catalog"] = redact.public_catalog(state["catalog"])
    if isinstance(state.get("sessions"), dict):
        new["sessions"] = {name: _session(s) for name, s in state["sessions"].items()}
    return new if new != state else None


def clean(event):
    """The event as the current hub would have written it, or None - it already is."""
    kind, data = event.get("type"), event.get("data")
    if not isinstance(data, dict):
        return None
    if kind == "catalog.loaded" and isinstance(data.get("tools"), dict):
        new = {**data, "tools": redact.public_catalog(data["tools"])}
    elif kind == "core.snapshot":
        state = clean_state(data.get("state"))
        new = {**data, "state": state} if state is not None else data
    elif kind == "permission.requested":
        new = _request(data)
    elif kind in journal.MASKED or kind == "watch.alert":   # a watchdog alert quotes the background output
        new = redact.mask_data(data)
    else:
        return None
    return {**event, "data": new} if new != data else None


def _lines(path):
    """(the line as is, the cleaned event or None) for each line of a journal file."""
    with open(path, "rb") as f:
        for raw in f:
            changed = None
            if any(h in raw[:200] for h in HINTS):
                try:
                    changed = clean(json.loads(raw))
                except ValueError:
                    pass   # a truncated line - as is
            yield raw, changed


def count(path):
    """How many events of a journal file the cleaning will change."""
    try:
        return sum(1 for _, changed in _lines(path) if changed)
    except FileNotFoundError:
        return 0


class _Replaced(Exception):
    """The file was replaced (journal.compact) while rewrite read it."""


def rewrite(path, backup_dir=None):
    """One pass: a cleaned file next to the old one (0600) - line for line of the old one, the same event with the same
    seq - and a replacement; the new file starts at the first changed line (the part before it is copied as is), with no
    changes nothing is written. backup_dir - the old file is hard-linked there (the space is not doubled until it is
    replaced). -> how many events changed. The file replaced meanwhile (compact) - read again."""
    st = os.stat(path)   # the file both reads below come from - compared under the lock before the replacement
    out, tmp, pos, changed_n = None, None, 0, 0
    try:
        for raw, changed in _lines(path):
            if changed is not None and out is None:
                fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix="." + os.path.basename(path) + ".")
                out = os.fdopen(fd, "wb")
                with open(path, "rb") as f:   # lines before the first changed one - as they were
                    if os.fstat(f.fileno()).st_ino != st.st_ino:
                        raise _Replaced()
                    _copy(f, out, pos)
            if changed is None:
                if out is not None:
                    out.write(raw)
            else:
                line = journal._line(changed)
                out.write((line if raw.endswith(b"\n") else line.rstrip("\n")).encode("utf-8"))
                changed_n += 1
            pos += len(raw)
        if out is None:
            return 0
        out.flush()
        platforms.current.sync(out.fileno())
        out.close()
        with journal.replacing(path):   # not in the middle of compact's check and replacement (it would lose this file)
            now = os.stat(path)
            if (now.st_ino, now.st_size, now.st_mtime_ns) != (st.st_ino, st.st_size, st.st_mtime_ns):
                raise _Replaced()
            if backup_dir:
                platforms.current.makedirs_private(backup_dir)
                os.link(path, os.path.join(backup_dir, os.path.basename(path)))   # the old file as it was
            platforms.current.replace(tmp, path)
    except BaseException as e:
        if out is not None:
            try:
                out.close()   # on a full disk closing itself raises (it flushes the buffer) - the temporary file must be removed anyway
            except OSError:
                pass
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if isinstance(e, _Replaced):
            return rewrite(path, backup_dir)
        raise
    platforms.current.sync_dir(os.path.dirname(path))
    return changed_n


def _copy(src, dst, n):
    """The first n bytes of src into dst in chunks."""
    while n > 0:
        chunk = src.read(min(n, 1 << 20))
        if not chunk:
            raise ValueError("the archive is shorter than what was read")
        dst.write(chunk)
        n -= len(chunk)


def archives(state_dir):
    """Journal archives (journal.archives) and what repair cut off (cut-*.jsonl)."""
    root = os.path.join(state_dir, journal.ARCHIVE)
    try:
        cuts = sorted(n for n in os.listdir(root) if n.startswith("cut-") and n.endswith(".jsonl"))
    except FileNotFoundError:
        return []
    return journal.archives(state_dir) + [os.path.join(root, n) for n in cuts]


def drop_parses(path):
    """Delete the archive's parses for the page: they contain the same as the archive did before cleaning."""
    for side in PARSES:
        try:
            os.unlink(path + side)
        except FileNotFoundError:
            pass


def _done(state_dir):
    try:
        with open(os.path.join(state_dir, DONE), encoding="utf-8") as f:
            got = json.load(f)
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_done(state_dir, done):
    path = os.path.join(state_dir, DONE)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(done, f, ensure_ascii=False, indent=1, sort_keys=True)
    platforms.current.replace(path + ".tmp", path)


def _mark(path):
    st = os.stat(path)
    return [st.st_size, st.st_mtime_ns]


def live(state_dir):
    """How many events of the live journal the cleaning will change and whether there is anything to clean in state.json (bool)."""
    n = count(os.path.join(state_dir, "journal.jsonl"))
    try:
        saved = journal.read_state_file(state_dir)
        dirty = clean_state(saved.get("state")) is not None
    except (OSError, ValueError, AttributeError):
        dirty = False
    return n, dirty


_ARCHIVED = re.compile(rb'"why":"rotate(?:-start)?","archive":"([^"/\\]+)"')
# the archive that rotation has just carried the live journal into is rewritten no sooner than this many seconds later:
# readers (the page - every 0.3 s, the Telegram bot and the watchdog - every 2 s) finish reading it by the old inode
# (journal.go_on), while a rewrite gives a new one - and they would start the journal over (the bot silently skipping what
# it had not managed to send)
SETTLE = 30.0


def _archived(path):
    """The name of the archive with which the journal path was started (the rotation snapshot in the first line), or None."""
    try:
        with open(path, "rb") as f:
            m = _ARCHIVED.search(f.readline(400))
    except OSError:
        return None
    return m[1].decode() if m else None


def rotate_live(state_dir, ask, timeout=120.0, clock=time.monotonic, sleep=time.sleep):
    """The live journal goes to the archive, the new one starts clean (journal.rotate scrub=True). If hubd is running
    (holds hubd.lock) it is asked by ask() (the fact journal.scrub_requested; hubd rotates on a tick, rotate_if_big;
    False - it refused), and the command waits for a new journal file with nothing to clean (an ordinary size rotation
    between the request and the cleaning carries the tail as is - keep waiting); if it is not running - it rotates itself
    under its lock. -> (started clean, the archive names of all rotations seen in the meantime - also on failure: their
    rewrite waits for SETTLE)."""
    from core import lock, reader_cache, tail
    path = os.path.join(state_dir, "journal.jsonl")
    held = lock.acquire(os.path.join(state_dir, "hubd.lock"))
    names = []
    if held is not None:
        try:
            archived = journal.rotate(state_dir, journal.load_state(state_dir), scrub=True)
        finally:
            held.close()
        names = [os.path.basename(archived)] if archived else []
    else:
        seen = journal.identity(path)
        if ask() is False:
            return False, names
        end = clock() + timeout
        while True:
            now = journal.identity(path)
            if now != seen:
                seen = now
                names += [n for n in [_archived(path)] if n]
                if not count(path):
                    break
            if clock() >= end:
                return False, names
            sleep(0.2)
    # hub show and the page parsed the old journal - together with what was in it (the page's reader checkpoint, and the temp
    # file of a write that was cut off)
    for parsed in [os.path.join(state_dir, tail.CACHE), os.path.join(state_dir, tail.SESSION_CACHE),
                   os.path.join(state_dir, reader_cache.STORE),
                   *glob.glob(os.path.join(state_dir, "cache", ".web-reader.*"))]:
        try:
            os.unlink(parsed)
        except FileNotFoundError:
            pass
    return True, names


SPARE = 64 << 20   # free space beyond the archive's size: the new file is written next to it, the old one goes after the replacement


def run(state_dir, apply=False, backup=False, say=lambda *_: None, no_space=lambda *_: None, settle=None,
        sleep=time.sleep, now=time.time):
    """Archives: [(path, how many events to clean)] - with apply they are cleaned (backup - the old ones go to
    backup/scrub-<time>/), cleaned and clean ones are remembered in scrub.done. No space for the new file - the archive is
    skipped (no_space(path, bytes needed)), the next run will take it again. settle - {archive name: the earliest time to
    rewrite} (SETTLE). The live journal is not here (rotate_live)."""
    done, found, settle = _done(state_dir), [], settle or {}
    backup_dir = os.path.join(state_dir, "backup", time.strftime("scrub-%Y%m%d-%H%M%S")) if backup and apply else None
    for path in archives(state_dir):
        name = os.path.basename(path)
        if done.get(name) == _mark(path):
            continue   # cleaned (or was clean) and unchanged since
        if not apply or shutil.disk_usage(os.path.dirname(path)).free < os.path.getsize(path) + SPARE:
            n = count(path)
            if n:
                found.append((path, n))
                say(path, n)
                if apply:
                    no_space(path, os.path.getsize(path) + SPARE)
                    continue
            if apply:
                done[name] = _mark(path)
                _save_done(state_dir, done)
            continue
        if name in settle:
            sleep(max(0.0, settle[name] - now()))
        n = rewrite(path, backup_dir)   # one pass: counting and cleaning together
        if n:
            found.append((path, n))
            say(path, n)
            drop_parses(path)   # the parse of a clean archive holds no keys from it - let it live
        done[name] = _mark(path)
        _save_done(state_dir, done)
    return found
