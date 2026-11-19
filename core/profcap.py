"""The size cap of an agent's own CLAUDE.md (owner decisions 2026-11-08: a cap of 250 KB by default, lower in the hub's
settings; over it the core itself moves out the items that may go, never the pinned ones, and the agent can look at
what went and put an item back).

The file is split into items as the executors' digest splits it (core/execprofile._items: a list entry with its indented
lines, a paragraph, a heading, a code block). Pinned (never moved out): the start of the file up to the first heading of
level 2 or deeper, every heading, an item whose first line carries PIN, and every item of a section whose heading
carries it (until a heading of the same or a higher level). Every other item may be moved out.

After an agent's turn the core checks its file (hubd, action profile_check). Over the cap, and only for an agent whose
pinning was confirmed (`hub profile pin`, the fact core.profile_marked: one stray pin must not open the rest of the file),
the oldest items that may go move whole into CLAUDE.evicted.md next to it, down to max(TARGET of the cap, the pinned
part); a pinned part over the cap moves nothing. Nobody of the agent busy (the agent and its executors) — or the check
waits for the next tick; the file changed between the read and the replace — it waits too (the evicted file is written
first: an item lost to nobody). The age of an item is when the core first saw its text (CLAUDE.ages.json; a rewritten
item is young again); on the first look, the last date written in it.

Files next to CLAUDE.md, written by the core only and never in a prompt: CLAUDE.evicted.md, CLAUDE.ages.json and
CLAUDE.before-evict.md (the file as it was before the last eviction)."""
import datetime
import hashlib
import json
import math
import os
import re
import shutil
import tempfile

from core import execprofile

AGENT, EVICTED, AGES, BEFORE = "CLAUDE.md", "CLAUDE.evicted.md", "CLAUDE.ages.json", "CLAUDE.before-evict.md"
PIN = "\U0001F4CC"   # the pushpin: visible to the agent in its own file, the same in every language
CAP_KB = 250     # hub.json agents.profile_max_kb; 0 — no cap
TARGET = 0.9     # an eviction goes down to this share of the cap: not one item per new lesson
EVICTED_HEAD = ("# Evicted from CLAUDE.md\n\nItems the core moved out of the agent's CLAUDE.md over its size cap, whole, "
                "as they were (the core writes this file; `hub profile restore <id>` puts one back).\n")
ENTRY = re.compile(r"^## (e[0-9a-f]{12}(?:-\d+)?) · ([^·\n]+?) · ?(.*)$", re.M)
LIST_MARK = re.compile(r"^(\s*(?:[-*+]|\d+[.)])[ \t]+)")
HEAD_MARK = re.compile(r"^(\s{0,3}#{1,6}[ \t]+)")
# a date in an item: YYYY-MM-DD or DD.MM[.YY[YY]] with two-digit day and month; a version (3.12, 2.1.218) or a decimal
# (1.5) is not one
# (an ISO date may run on into a time: 2025-10-03T12:00); a one-digit day or month (6.11) is not read — so is "3.12"
DATES = re.compile(r"(?<![\w.])(?:(\d{4})-(\d{2})-(\d{2})(?!\d)|(\d{2})\.(\d{2})(?:\.(\d{2}|\d{4}))?(?![\w.]*\d))")


def cap_kb(cfg):
    """The cap in KB from hub.json agents.profile_max_kb: a number >= 0 (a fraction — up to a whole KB); anything else —
    the default. 0 — no cap."""
    box = (cfg or {}).get("agents") if isinstance(cfg, dict) else None
    v = box.get("profile_max_kb", CAP_KB) if isinstance(box, dict) else CAP_KB
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return CAP_KB
    if isinstance(v, float) and not math.isfinite(v):   # an int is finite; isfinite(10**400) would raise
        return CAP_KB
    return CAP_KB if v < 0 else int(math.ceil(v))


def _level(line):
    m = HEAD_MARK.match(line)
    return len(m.group(1).strip()) if m else 0


def items(text):
    """The file's items in order: {a, b (line span), text, section (the heading above it, "" before any), fixed (never
    moved out), pinned (fixed by a pin), heading}."""
    lines = text.split("\n")
    out, section, pins, started = [], "", [], False   # pins: the levels of pinned headings above
    for a, b in execprofile._items(lines):
        first = lines[a]
        body = "\n".join(lines[a:b])
        level = _level(first) if execprofile.HEADING.match(first) else 0
        if level:
            pins = [lv for lv in pins if lv < level]
            if PIN in first:
                pins.append(level)
            started = started or level >= 2
            section = first.strip()
            out.append({"a": a, "b": b, "text": body, "section": section, "fixed": True, "pinned": PIN in first,
                        "heading": True})
            continue
        pinned = bool(pins) or PIN in first
        fixed = pinned or not started or _unclosed(lines[a:b])
        out.append({"a": a, "b": b, "text": body, "section": section, "fixed": fixed, "pinned": pinned, "heading": False})
    return out


def _unclosed(block):
    """A code block whose fence is never closed: it runs to the end of the file, headings and pins included — never moved
    out whole (release review 2026-11-09)."""
    m = execprofile.OPEN.match(block[0])
    if not m:
        return False
    fence, last = m.group(1), block[-1].strip()
    return len(block) < 2 or not (last.startswith(fence[0]) and len(last) >= len(fence) and not last.strip(fence[0]))


def item_id(section, text):
    """An item's id: its exact text and its section — the heading's words, without a pin (pinning a heading changes no
    id); in the text spaces count: code is not folded together."""
    return "e" + hashlib.sha256((_norm(section) + "\n" + text).encode("utf-8")).hexdigest()[:12]


def _key(section, text):
    return _norm(section), text


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return None


def _utf8(path):
    try:
        with open(path, "rb") as f:
            f.read().decode("utf-8")
        return True
    except (OSError, UnicodeDecodeError):
        return False


def _write(path, text, like=None):
    """Atomically: a temporary file next to it, flushed to disk, then os.replace; the mode of `like` if given."""
    folder = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".profcap-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if like:
            try:
                shutil.copymode(like, tmp)
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _stamp(now):
    return datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# --- the evicted file ----------------------------------------------------------------------------------------------
def entries(text):
    """CLAUDE.evicted.md -> [{id, when, section, text}] in file order."""
    out, hits = [], list(ENTRY.finditer(text or ""))
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        out.append({"id": m.group(1), "when": m.group(2).strip(), "section": m.group(3).strip(),
                    "text": text[m.end():end].strip("\n")})
    return out


def render(found):
    return EVICTED_HEAD + "".join(f"\n## {e['id']} · {e['when']} · {e['section']}\n{e['text']}\n" for e in found)


def _put(found, section, text, when):
    """Add an item to the evicted list: the same item again — one entry, its date renewed; another text under the same
    id (a collision) — the id gets a suffix."""
    eid = item_id(section, text)
    for e in found:
        if e["id"].split("-")[0] == eid and _key(e["section"], e["text"]) == _key(section, text):
            e["when"] = when
            return e["id"]
    taken = {e["id"] for e in found}
    new, n = eid, 2
    while new in taken:
        new, n = f"{eid}-{n}", n + 1
    found.append({"id": new, "when": when, "section": section, "text": text})
    return new


# --- ages ----------------------------------------------------------------------------------------------------------
def _text_date(text, now):
    """The last date written in an item (YYYY-MM-DD, DD.MM, DD.MM.YY[YY]) as a timestamp; a date without a year takes the
    year that puts it not after now. None — no date."""
    year_now = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).year
    best = None
    for m in DATES.finditer(text):
        try:
            if m.group(1):
                d = datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=datetime.timezone.utc)
            else:
                y = m.group(6)
                y = (2000 + int(y) if len(y) == 2 else int(y)) if y else year_now
                d = datetime.datetime(y, int(m.group(5)), int(m.group(4)), tzinfo=datetime.timezone.utc)
                if not m.group(6) and d.timestamp() > now:
                    d = d.replace(year=y - 1)
        except ValueError:
            continue
        ts = d.timestamp()
        if ts <= now and (best is None or ts > best):
            best = ts
    return best


def _load_ages(folder):
    try:
        with open(os.path.join(folder, AGES), encoding="utf-8") as f:
            got = json.load(f)
        return got if isinstance(got, dict) and isinstance(got.get("first"), dict) else None
    except (OSError, ValueError):
        return None


def _aged(ages, its, now):
    """Ages of the current items: the known ones keep theirs, a new text is young now; on the first look (no ages yet)
    the last date written in it, or now. Ids gone from the file are forgotten."""
    first = dict((ages or {}).get("first") or {})
    out = {}
    for it in its:
        if it["heading"]:
            continue
        iid = item_id(it["section"], it["text"])
        if iid in first:
            out[iid] = first[iid]
        elif ages is None:
            out[iid] = _text_date(it["text"], now) or now
        else:
            out[iid] = now
    return out


# --- the check after a turn ----------------------------------------------------------------------------------------
def _strip(lines, spans):
    """The lines without the item spans; a blank line left doubled where an item went is dropped."""
    gone = set()
    for a, b in spans:
        gone.update(range(a, b))
        if b < len(lines) and not lines[b].strip() and (a == 0 or not lines[a - 1].strip()):
            gone.add(b)
    return [ln for i, ln in enumerate(lines) if i not in gone]


def check(folder, cap, now, marked=False, busy=False):
    """The check after an agent's turn (hubd). Returns None (no file) or {bytes, fixed (bytes never moved out), cap,
    state, pending, evicted: [ids], before}. state: unchanged | under | unmarked | fixed_over | busy | not_utf8 (not
    rewritten: its bytes would not survive) | changed (the file moved under us) | evicted. pending — the eviction is still due: the next turn or tick checks again even
    with the file untouched. busy — the agent or its executors work now: an eviction waits."""
    path = os.path.join(folder, AGENT)
    try:
        st = os.stat(path)
    except OSError:
        return None
    ages = _load_ages(folder)
    seen = (st.st_mtime_ns, st.st_size, cap, bool(marked))   # a new cap or a confirmed pinning — look again
    last = (ages or {}).get("last") or {}
    if ages and tuple(ages.get("seen") or ()) == seen and not ages.get("pending"):
        return {**last, "state": "unchanged", "evicted": []}
    text = _read(path)
    if text is None:
        return None
    its = items(text)
    size = len(text.encode("utf-8"))
    free = [it for it in its if not it["fixed"]]
    # what stays if every free item goes: the pinned items, the head, headings and the blank lines between them
    fixed = len("\n".join(_strip(text.split("\n"), [(it["a"], it["b"]) for it in free])).encode("utf-8"))
    first = _aged(ages, its, now)
    limit = cap * 1024
    res = {"bytes": size, "fixed": fixed, "cap": cap, "evicted": [], "before": size, "pending": False}
    ev_path = os.path.join(folder, EVICTED)
    ev_text = _read(ev_path)
    found = entries(ev_text) if ev_text else []
    # an evicted item the agent wrote back (or restored by hand) lives in the file again: one place, not two
    here = {_key(it["section"], it["text"]) for it in its}
    back = [e for e in found if _key(e["section"], e["text"]) in here]
    if not cap or size <= limit:
        res["state"] = "under"
    elif not marked:
        res["state"] = "unmarked"
    elif fixed > limit:
        res["state"] = "fixed_over"
    elif busy:
        res.update(state="busy", pending=True)
    elif not _utf8(path):   # rewritten from the decoded text, its own bytes would be U+FFFD in both copies (review)
        res["state"] = "not_utf8"
    else:
        res["state"] = "evicted"
    if res["state"] != "evicted":
        if back:
            _write(ev_path, render([e for e in found if e not in back]))
        _save_ages(folder, first, seen, res, pending=res["pending"])
        return res
    goal = max(int(limit * TARGET), fixed)
    lines = text.split("\n")
    order = sorted(free,
                   key=lambda it: (first.get(item_id(it["section"], it["text"]), now), it["a"]))
    picked, left = [], size
    for it in order:
        if left <= goal:
            break
        picked.append(it)
        left -= len(it["text"].encode("utf-8")) + 1
    new_text = "\n".join(_strip(lines, [(it["a"], it["b"]) for it in picked]))
    when = _stamp(now)
    found = [e for e in found if e not in back]
    ids = [_put(found, it["section"], it["text"], when) for it in picked]
    # the evicted file first: if the replace below does not happen, the items are in both — nothing lost
    _write(ev_path, render(found))
    _write(os.path.join(folder, BEFORE), text)
    if not _same(path, st):
        res.update(state="changed", pending=True)
        _save_ages(folder, first, seen, res, pending=True)
        return res
    _write(path, new_text, like=path)
    after = os.stat(path)
    res.update(bytes=len(new_text.encode("utf-8")), evicted=ids)
    gone = {item_id(it["section"], it["text"]) for it in picked}
    _save_ages(folder, {k: v for k, v in first.items() if k not in gone},
               (after.st_mtime_ns, after.st_size, cap, bool(marked)), res, pending=False)
    return res


def _same(path, st):
    """The file is what we read: the same inode, size and modification time (checked right before the replace)."""
    try:
        now = os.stat(path)
    except OSError:
        return False
    return (now.st_ino, now.st_size, now.st_mtime_ns) == (st.st_ino, st.st_size, st.st_mtime_ns)


def _save_ages(folder, first, seen, res, pending):
    last = {k: res[k] for k in ("bytes", "fixed", "cap") if k in res}
    _write(os.path.join(folder, AGES), json.dumps({"first": first, "seen": list(seen), "pending": bool(pending),
                                                   "last": {**last, "pending": bool(pending)}}, ensure_ascii=False))


# --- restore -------------------------------------------------------------------------------------------------------
def restore(folder, eid, now):
    """Put an evicted item back: after the last item of its own section, before a nested heading (no such section — at
    the end of the file); its age is now. The entry stays in the evicted file until a check sees the item in the file
    (check: `back`): a rewrite of the file from a copy read before the restore does not lose it. -> (ok, why)."""
    path, ev_path = os.path.join(folder, AGENT), os.path.join(folder, EVICTED)
    found = entries(_read(ev_path) or "")
    hit = next((e for e in found if e["id"] == eid), None)
    if not hit:
        return False, "no such evicted item"
    try:
        st = os.stat(path)
    except OSError:
        return False, "no CLAUDE.md"
    text = _read(path) or ""
    lines = text.split("\n")
    its = items(text)
    if _key(hit["section"], hit["text"]) in {_key(it["section"], it["text"]) for it in its}:
        _recount(folder, item_id(hit["section"], hit["text"]), now)
        return True, ""   # back in the file already (a restore again): its entry goes at the next check
    at = None
    for i, it in enumerate(its):
        if it["heading"] and _norm(it["section"]) == _norm(hit["section"]):
            end = it["b"]
            for nxt in its[i + 1:]:
                if nxt["heading"]:
                    break
                end = nxt["b"]
            at = end
            break
    body = hit["text"].split("\n")
    if at is None:
        while lines and not lines[-1].strip():
            lines.pop()
        new = lines + [""] + body + [""]
    else:
        prev = lines[at - 1] if at else ""
        glue = [] if LIST_MARK.match(prev) and LIST_MARK.match(body[0]) else [""]
        new = lines[:at] + glue + body + lines[at:]
    if not _same(path, st):
        return False, "the file changed meanwhile — try again"
    _write(path, "\n".join(new), like=path)
    _recount(folder, item_id(hit["section"], hit["text"]), now)
    return True, ""


def _recount(folder, iid, now):
    """A restored item is young now, and the next check looks at the file anew (its size; the entry leaves the evicted)."""
    ages = _load_ages(folder) or {"first": {}}
    ages["first"][iid] = now
    ages["pending"] = True
    _write(os.path.join(folder, AGES), json.dumps(ages, ensure_ascii=False))


# --- reading -------------------------------------------------------------------------------------------------------
def search(folder, grep=None, n=20):
    """(entries to show, how many matched in all): with grep — every entry where it occurs, case-insensitive; without —
    the last n."""
    found = entries(_read(os.path.join(folder, EVICTED)) or "")
    if grep:
        g = grep.casefold()
        hits = [e for e in found if g in e["text"].casefold() or g in e["section"].casefold()]
        return hits, len(hits)
    return found[-n:] if n else found, len(found)


def has_evicted(folder):
    try:
        return os.path.getsize(os.path.join(folder, EVICTED)) > len(EVICTED_HEAD.encode("utf-8"))
    except OSError:
        return False


# --- the first pinning ---------------------------------------------------------------------------------------------
def _norm(s):
    return " ".join(s.replace(PIN, " ").split())


def _bare(line):
    """A first line without its list or heading mark and pin, spaces folded: what a pinning list names."""
    line = LIST_MARK.sub("", line, count=1)
    return _norm(line)


def pin_plan(text, spec):
    """Where the pinning list `spec` puts pins in the file `text`. The list is Markdown: a heading copied from the file
    (as it stands there) opens its section; under it every line names an item of that section by the start of its
    first line (list mark optional); a line `*` pins the whole section (its heading). -> (lines to mark, problems):
    a line that matches no item or two of them is a problem, and nothing is marked then (all or nothing)."""
    lines = text.split("\n")
    its = items(text)
    marks, problems, section = set(), [], None
    for raw in spec.split("\n"):
        if not raw.strip():
            continue
        if execprofile.HEADING.match(raw):
            section = _norm(raw)
            heads = [it for it in its if it["heading"] and _norm(it["section"]) == section]
            if len(heads) != 1:
                problems.append(f"{raw.strip()[:80]}: {'no such section' if not heads else 'two sections with this heading'}")
            continue
        want = _bare(raw)
        if section is None:
            problems.append(f"{raw.strip()[:60]}: no section above it")
            continue
        if want == "*":
            heads = [it for it in its if it["heading"] and _norm(it["section"]) == section]
            if len(heads) == 1:
                marks.add(heads[0]["a"])
            continue
        hits = [it for it in its if not it["heading"] and _norm(it["section"]) == section
                and want and _bare(lines[it["a"]]).startswith(want)]
        if len(hits) == 1 and execprofile.OPEN.match(lines[hits[0]["a"]]):
            # a pin on a fence would break the fence: the block is pinned by its section's heading
            problems.append(f"{section} / {raw.strip()[:60]}: a code block — pin its section with *")
            continue
        if len(hits) != 1:
            problems.append(f"{section} / {raw.strip()[:60]}: {'no such item' if not hits else f'{len(hits)} items match'}")
            continue
        marks.add(hits[0]["a"])
    return sorted(marks), problems


def _mark(line):
    if PIN in line:
        return line
    for rx in (HEAD_MARK, LIST_MARK):
        m = rx.match(line)
        if m:
            return m.group(1) + PIN + " " + line[m.end():]
    return PIN + " " + line


def pin(folder, spec, cut=None, now=None):
    """The first pinning (`hub profile pin`): marks the items `spec` names (pin_plan; any problem — nothing is written)
    and seeds the evicted file with the items of `cut` (a list of items someone cut out of the file by hand, under their
    section headings) that the file does not hold. -> {ok, pinned, seeded, problems}."""
    path = os.path.join(folder, AGENT)
    try:
        st = os.stat(path)
    except OSError:
        return {"ok": False, "pinned": 0, "seeded": 0, "problems": ["no CLAUDE.md"]}
    text = _read(path) or ""
    marks, problems = pin_plan(text, spec)
    if problems:
        return {"ok": False, "pinned": 0, "seeded": 0, "problems": problems}
    lines = text.split("\n")
    pinned = sum(PIN not in lines[i] for i in marks)
    for i in marks:
        lines[i] = _mark(lines[i])
    seeded = 0
    if cut:
        ev_path = os.path.join(folder, EVICTED)
        found = entries(_read(ev_path) or "")
        here = {_key(it["section"], it["text"]) for it in items("\n".join(lines))}
        when = _stamp(now) if now else "seed"
        for it in items(cut):
            if it["heading"] or _key(it["section"], it["text"]) in here:
                continue
            _put(found, it["section"], it["text"], when)
            seeded += 1
        _write(ev_path, render(found))
    if not _same(path, st):
        return {"ok": False, "pinned": 0, "seeded": seeded, "problems": ["the file changed meanwhile — try again"]}
    if pinned:
        _write(path, "\n".join(lines), like=path)
    ages = _load_ages(folder)
    if ages:   # now marked: the next check counts anew even with the file untouched
        ages["pending"] = True
        _write(os.path.join(folder, AGES), json.dumps(ages, ensure_ascii=False))
    return {"ok": True, "pinned": pinned, "seeded": seeded, "problems": []}
