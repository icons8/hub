"""CHANGES.md: what is new for the owner and what changed for sessions (the "### For sessions" subsection under a date;
released files have its earlier Russian heading, forms.SESSION_SECTIONS). The core attaches the items for sessions to each session's next message — only unseen ones (owner decision 2026-10-05)."""

from core import forms

SECTIONS = forms.SESSION_SECTIONS   # and a future "### For sessions": released CHANGES files have the earlier one
KEEP = 100   # how many items the core remembers (plus every item the file still lists: a forgotten one reads as new)
SHOW = 15    # how many to attach at a time (after a long sleep — the last ones)
ITEM_MAX = 3000     # an item in a fact or a release entry (the file's longest runs to 2.5 thousand characters)
NOTICE_ITEM = 600   # an item in the update notice (the page's "Updates" has it whole); a Telegram notice of 12 whole items is pages long

# the texts a session reads at the end of its next message (the items themselves are as CHANGES.md has them)
UPDATED = "The hub was updated — what changed for sessions:"
DO_SO = "Do it this way, even if earlier in the conversation it was different."
RULES_CHANGED = "This hub's rules (from the owner, for all sessions) have changed — now they are:"
RULES_REMOVED = "This hub's rules (from the owner, for all sessions) are removed — the earlier ones no longer apply."


def parse(text):
    """(date, for_sessions, item, raw) in file order. A date heading comes as (date, None, None, None); an item is its "- "
    line and every indented line after it up to the next "- ", heading or blank line (a nested "  - " belongs to it): item
    is those lines joined with a space (a re-wrapped item reads the same), raw — the lines as the file has them."""
    date, ours, cur = None, False, None
    for line in (text or "").splitlines() + [""]:
        if cur is not None and line.strip() and line[:1] in " \t":
            cur.append(line)
            continue
        if cur is not None:
            yield date, ours, " ".join(x.strip() for x in cur)[2:].strip(), "\n".join(cur)
            cur = None
        if line.startswith("## "):
            date, ours = line[3:].strip(), False
            yield date, None, None, None
        elif line.startswith("### "):
            ours = line.strip() in SECTIONS
        elif line.startswith("- ") and date:
            cur = [line]


def _walk(text):
    """(date, for_sessions, item) for each list item."""
    for date, ours, item, _ in parse(text):
        yield date, ours, item


def bullets(notes, count=12, total=None):
    """The update notice's list: a "• " line for each of the first `count` items, a long item cut at a word; with `total`,
    items are added while the text stays within it (the first always goes: a message with a button must fit one message)."""
    out, size = [], 0
    for n in notes[:count]:
        line = f"• {shorten(str(n), NOTICE_ITEM)}"
        if total and out and size + len(line) + 1 > total:
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


def shorten(text, limit):
    """The text up to limit characters; cut at a word, with "…" (a notice must not end in the middle of a word)."""
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    if text[limit - 1:limit].strip() and " " in cut:   # the cut falls inside a word: back to the space before it
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip() + "…"


def session_heads(text):
    """For each of session_notes(text), in its order: the item's first line when it runs over several lines, else "" — what
    a hub before 2026-11-08 remembered of it (news.load)."""
    return [raw.split("\n")[0].strip()[2:].strip() if "\n" in raw else "" for _, ours, _, raw in parse(text) if ours]


def session_notes(text):
    return [item for _, ours, item in _walk(text) if ours]


def changes(text, limit=30):
    """What is new for the owner — by date, without the items for sessions."""
    out = []
    for date, ours, item in _walk(text):
        if item is None:
            if len(out) == limit:
                break
            out.append({"date": date, "items": []})
        elif not ours:
            out[-1]["items"].append(item)
    return out


def load(st, ev, data):
    """news.loaded: the core read CHANGES.md at start — remember the new items with the event number.
    The file lists the newest first: append from the oldest, so the newest end up last (the tail is what a session is
    shown). Only items the file no longer lists are forgotten, oldest first, beyond KEEP — an item forgotten while still
    in the file would read as new at every start."""
    known = st.setdefault("news", [])
    texts = {n["text"] for n in known}
    notes = [str(t) for t in data.get("notes") or []]
    listed = set(notes)
    heads = data.get("heads") if isinstance(data.get("heads"), list) and len(data["heads"]) == len(notes) else [""] * len(notes)
    head_of = dict(zip(notes, map(str, heads)))   # the first line of an item that runs over several lines
    for t in reversed(notes):
        if t not in texts:
            # a hub before 2026-11-08 remembered only the first line of an item: the same item, now whole (not new to sessions)
            was = next((n for n in known if head_of[t] and n["text"] == head_of[t] and n["text"] not in listed), None)
            if was:
                texts.discard(was["text"])
                was["text"] = t
            else:
                known.append({"text": t, "seq": ev["seq"]})
            texts.add(t)
    extra = len(known) - KEEP
    if extra > 0:
        kept = []
        for n in known:
            if extra > 0 and n["text"] not in listed:
                extra -= 1
            else:
                kept.append(n)
        known[:] = kept


def rules(st, ev, lines):
    """rules.reviewed: the hub rules (hub.json `rules`) changed. A running session read them at process start and would
    live by the old ones until restart — the unseen set is attached to its next message next to the "### Sessions" items.
    Only the last set is remembered — a session that slept needs the current one, not the history."""
    lines = [str(r) for r in lines]
    text = (RULES_CHANGED + "\n" + "\n".join(f"- {r}" for r in lines)) if lines else RULES_REMOVED
    st["rules_note"] = {"text": text, "seq": ev["seq"]}


def _rules_seq(st):
    return (st.get("rules_note") or {}).get("seq", 0)


def latest(st):
    return max([n["seq"] for n in st.get("news") or []] + [_rules_seq(st)])


def unseen(st, sess):
    """Text for the session's next message or None; marks it as seen."""
    seen = sess.get("news_seen", 0)
    fresh = [n["text"] for n in st.get("news") or [] if n["seq"] > seen][-SHOW:]
    note = st["rules_note"]["text"] if _rules_seq(st) > seen else None
    if not fresh and not note:
        return None
    sess["news_seen"] = latest(st)
    out = [(UPDATED + "\n" + "\n".join(f"• {t}" for t in fresh))] if fresh else []
    out += [note] if note else []
    return "\n\n(" + "\n\n".join(out) + "\n" + DO_SO + ")"
