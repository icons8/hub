"""The digest of the owner's open tasks for the Telegram bot (telegram.digest): one message, grouped by the owner's
tasks, built from the core state only.

A task is the chain of work that grew from one message of the owner (the delegations with the same root). The task is
a bold heading, under it its sub-tasks, one row each: "- <status emoji> **Name**: gist -> stage", up to ~70
characters, no session names, ids and paths, at most four sub-tasks under a task. A task without sub-tasks is one row.
The status and the blocker come from the core state (the same facts the page shows); what the core does not know is
not written. The texts are in the catalogs (telegram.digest.*) and go in the hub language at the moment of sending.
Nothing here writes: the bot only sends the text and erases the previous one."""
import re
import time

from core import delegations, forms, i18n, minihub, owner, tools, views

OPEN = ("working", "waiting-owner")
NAME_WORDS = 4           # the task's name in a row: up to four words, bold
ROW = 70                 # a row as a whole: up to ~70 characters, on a phone no longer than two lines
SUBS = 4                 # sub-tasks under a task; the rest - "and N more"
TASKS = 8                # tasks in the digest; the rest - "and N more tasks"
ICON = {"owner": "⏭", "busy": "⏳", "subtasks": "🔄", "working": "🔄", "idle": "⏸", "stopped": "⚠️"}
ORDER = ("owner", "stopped", "busy", "working", "subtasks", "idle")   # blockers first
CHARS = 3000             # the whole digest: one Telegram message (fmt.LIMIT 3800 with the tags); the rest - "and N more tasks"


def tr(lang, key, **params):
    """A text of the digest in the language (a language without a catalog - English); {name} placeholders."""
    return i18n.tr(lang, "telegram.digest." + key, **params)


_MARKS = re.compile(r"\[(?:Image|Video|Audio|File)\s*#\d+\]|\[hub:[^\]]*\]|\b[dmq]\d{3,}\b")   # hub labels and ids
_MARKUP = re.compile(r"[*_`#>]+|\[([^\]]*)\]\([^)]*\)")
_CODE = re.compile(r"\s*\([^()`\n]*`[^`\n]*`[^()\n]*\)|\s*`[^`\n]*`")                  # `code` and brackets around it
_PATH = re.compile(r"(?<![\w/.:-])(?:~|[A-Za-z]:)?[\\/](?:[\w.\-]+[\\/])+[\w.\-]*")   # file paths, POSIX and Windows
_NAME_SPLIT = re.compile(r"\s*(?::|\s·|\s—|\s–)\s+")    # "Name: ...", "Name · ...", "Name — ..."
_SHORT = re.compile(r"[^\W\d_]{1,2}")                    # a preposition or conjunction: does not end a name
_VOICE = re.compile(r"^\s*🎤\s*(?:" + forms.VOICE_HEAD + r")?")   # a transcription's head in any language (forms.VOICE)


def clean(text):
    """One line of plain words: the owner's voice mark, hub labels, ids, code, paths and markdown are gone."""
    text = _VOICE.sub(" ", _PATH.sub(" ", _CODE.sub(" ", _MARKS.sub(" ", str(text or "")))))
    text = _MARKUP.sub(lambda m: m.group(1) or "", text)
    return re.sub(r"\s+([,.;:?)]|!(?!\d))", r"\1", " ".join(text.split())).strip()   # "merge !483" keeps its space


def cut(text, limit):
    """By a word; the ellipsis is inside the limit."""
    text = text.strip()
    if len(text) <= limit:
        return text
    head = text[:limit - 1]
    word = head.rsplit(" ", 1)[0] if " " in head and text[limit - 1] != " " else head
    while " " in word and _SHORT.fullmatch(word.rsplit(" ", 1)[1]):   # no dangling preposition before the ellipsis
        word = word.rsplit(" ", 1)[0]
    return word.rstrip(" ,;:—–-") + "…"


def split_name(text):
    """(name, rest, separator): the name is what stands before a ":" within the first NAME_WORDS words, the rest is
    the gist (": "); no mark - the first words, the rest goes on as a phrase (" ")."""
    text = clean(text)
    mark = _NAME_SPLIT.search(text)
    if mark and mark.start() and len(text[:mark.start()].split()) <= NAME_WORDS:
        name, rest, sep = text[:mark.start()], text[mark.end():], ": "
    else:
        words = text.split()
        n = NAME_WORDS
        while n > 1 and len(words) > n - 1 and _SHORT.fullmatch(words[n - 1]):
            n -= 1                      # a preposition does not hold the name: it goes into the rest
        name, rest, sep = " ".join(words[:n]), " ".join(words[n:]), " "
    name = name.strip(" .,;:—–-")
    return name[:1].upper() + name[1:], rest.strip(), sep


def lower(text):
    """The gist after the colon starts lower-case if its first word is an ordinary one, not a name."""
    words = text.split(" ", 2)
    if len(words[0]) > 1 and words[0][0].isupper() and words[0][1:].islower() and \
            (len(words) < 2 or not words[1][:1].isupper()):
        return text[0].lower() + text[1:]
    return text


def row(text, extra="", room=ROW):
    """A row "- **Name**: gist -> extra", the whole up to room characters: a long gist is cut at the tail, extra (the
    stage, the reason and the date) stays."""
    name, rest, sep = split_name(text)
    name = cut(name, max(room - 24, 16))   # a long word or a link in the name does not stretch the row
    extra = cut(clean(extra), max(room - len(name) - 2, 20))
    left = room - len(name) - (len(extra) + 2 if extra else 0) - len(sep)
    rest = cut(rest, max(left, 12)) if rest and left >= 12 else ""
    rest = lower(rest) if rest and sep != " " else rest
    rest = "" if not rest.strip(" .,;:…") else rest          # only a stump "…" is left of the gist
    if rest and sep == " ":              # the phrase goes on past the bold: "**Made two modes** and …: reason"
        return f"- **{name}** {rest}" + (f": {extra}" if extra else "")
    return f"- **{name}**" + (f": {rest}" if rest else "") + (f"{' → ' if rest else ': '}{extra}" if extra else "")


def when(ts, st):
    """Date "DD.MM" of a moment in the owner's time zone, as the bot's other lines (UTC when it is not known)."""
    return owner._local(ts, (st.get("owner") or {}).get("tz") or "UTC").strftime("%d.%m")


def words(text):
    """The words of a task without the heads the core puts before them (a card answer, "Task from ...", a voice
    transcription; forms.HEADS) and the dash a form answer starts its question with."""
    return forms.strip_heads(str(text or "")).lstrip(" \n—–") if text else text


def question(sess, d, now):
    """The text of the card the task waits on (a card of this task, or a form of its session while it works on it), or
    "": what the core shows the owner, not a phrase from the session's result."""
    asks = [a for a in owner.open_asks(sess, now) if a.get("delegation") == d["id"]]
    if not asks and sess.get("delegation") == d["id"]:
        asks = sess.get("pending") or []
    for a in asks:
        if a.get("tool") == "AskUserQuestion":
            text = clean(owner.question_text(a)).rstrip(" .")
            if text:
                return text
    return ""


def page_status(sess, now):
    """The session's status as the page shows it (views.snapshot): its state in the snapshot's words (a parked one is
    idle, a handed-in one asleep), the cards of the hub's questions among its pending ones, no background for a sleeping
    one."""
    state = sess.get("state")
    row = {**sess, "state": views.shown(state),
           "pending": (sess.get("pending") or []) + owner.open_asks(sess, now),
           "background": sess.get("background") if state not in (*views.SLEEP, "closed", "failed") else None}
    return views.session_status(row)


def chains(st):
    """{root: [delegations, oldest first]} of the owner's tasks with open work: a task is the chain that grew from one
    message of the owner (its delegations carry the message id as root; before that field - the head's own "for").
    Whatever a session started on its own is not the owner's task."""
    out = {}
    for d in sorted(st.get("delegations", {}).values(), key=lambda d: d.get("created") or d.get("since") or 0):
        root = d.get("root") or (d.get("for") if not d.get("for_session") else None)
        if root:
            out.setdefault(root, []).append(d)
    return {root: ds for root, ds in out.items() if any(d.get("state") in OPEN for d in ds) and (
        (st["messages"].get(root) or {}).get("from_owner")
        or any(d.get("for") == root and not d.get("for_session") for d in ds))}


def heading(st, d, root, head, now):
    """(text, own): the line that names the work. The head - its title or the owner's words (the message the chain grew
    from when the head has none). A sub-task - its title, else what its session says it does (own is True: that text
    is already the stage), else the words of the brief. Never an id or a session name."""
    sess = st["sessions"].get(d.get("session")) or {}
    gist = recap = None
    if sess.get("delegation") == d.get("id"):   # what the session says is about this task only while it works on it
        gist, recap = (tools.gist_now(sess, now) or {}).get("text"), (sess.get("recap") or {}).get("text")
    said = [d.get("words"), (st["messages"].get(root) or {}).get("text")] if head else [d.get("words")]
    for text, own in [(d.get("title"), False), *([] if head else [(gist, True), (recap, True)]),
                      *[(words(w), False) for w in said], (gist, True), (recap, True)]:
        if clean(text):
            return clean(text), own
    return "", False


def status(st, d, kids, lang, now):
    """(kind, stage) of one task from the core state only: waits for the owner, busy with another task, in progress
    (what its session says), waits for its sub-tasks, stands (since when), no session or crashed."""
    sess = st["sessions"].get(d.get("session"))
    if d["state"] == "waiting-owner":
        return "owner", question(sess or {}, d, now) or tr(lang, "owner")
    if sess is None or sess.get("state") == "removed":
        return "stopped", tr(lang, "no_session")
    s = page_status(sess, now)
    asks = owner.open_asks(sess, now)
    ours = any(a.get("delegation") == d["id"] for a in asks)
    other = any(a.get("delegation") not in (None, d["id"]) for a in asks)
    if s == "waiting" and (ours or sess.get("delegation") == d["id"] or not sess.get("delegation") and not other):
        return "owner", question(sess, d, now) or tr(lang, "owner")   # a card of the session's other task is not this one's
    if s == "failed":
        return "stopped", tr(lang, "failed")
    if s == "working" and sess.get("delegation") == d["id"]:
        say = (sess.get("recap") or {}).get("text") or (tools.gist_now(sess, now) or {}).get("text")
        return "working", clean(say) or tr(lang, "working")
    if kids or minihub.waiting_on(st, d):
        return "subtasks", tr(lang, "subtasks")
    stuck = delegations.stuck(st, d)
    if stuck is not None:
        hub_decides = d.get("timed_out") or (d.get("nudges") or 0) >= len(delegations.NUDGES)
        why = tr(lang, "stuck_hub" if hub_decides else "stuck_core")
        return "idle", f"{why} · {tr(lang, 'since', date=when(stuck, st))}"
    if s in ("working", "waiting"):
        other = (st["delegations"].get(sess.get("delegation") or "")) or {}
        what = split_name(heading(st, other, other.get("root"), True, now)[0])[0] if other else ""
        return "busy", tr(lang, "busy", what=what) if what else tr(lang, "busy_bare")
    return "idle", tr(lang, "asleep" if s == "asleep" else "idle")


def line(kind, text, stage):
    """A task or sub-task as a row: "- <emoji> **Name**: gist -> stage", the whole up to ROW characters."""
    return f"- {ICON[kind]} " + row(text, stage, ROW - len(ICON[kind]) - 1)[2:]


def group(st, root, ds, lang, now):
    """One task: (rank, ("lone", row)) for a task without open sub-tasks or (rank, ("chain", lines)); the rank is the
    worst status inside (blockers first), then the age."""
    ids = {d["id"] for d in ds}
    head = next((d for d in ds if d.get("for") == root and not d.get("for_session")), ds[0])
    under = {}
    for d in ds:
        if d is not head:
            up = d.get("parent") if d.get("parent") in ids and d.get("parent") != head["id"] else head["id"]
            under.setdefault(up, []).append(d)

    def below(d, seen=()):
        """The open work under the task, flat: a sub-task below a sub-task is listed under the task, and so is an open
        one below a closed one."""
        out = []
        for k in under.get(d["id"], []):
            if k["id"] not in seen:
                out += ([k] if k["state"] in OPEN else []) + below(k, (*seen, d["id"], k["id"]))
        return out

    subs = []
    for k in below(head):
        kind, stage = status(st, k, below(k), lang, now)
        text, own = heading(st, k, root, False, now)
        if kind == "working" and own:    # the name already says what the session does: no second phrase after it
            stage = ""
        subs.append((ORDER.index(kind), k.get("since") or 0, line(kind, text or tr(lang, "subtask"), stage)))
    subs.sort(key=lambda s: s[:2])
    title = heading(st, head, root, True, now)[0] or tr(lang, "task")
    own = status(st, head, below(head), lang, now) if head["state"] in OPEN else None
    ranks = ([ORDER.index(own[0])] if own else []) + [s[0] for s in subs]
    rank = (min(ranks) if ranks else len(ORDER), head.get("created") or 0)
    if not subs:
        return rank, ("lone", line(own[0], title, own[1]) if own else f"- **{split_name(title)[0]}**")
    lines = [f"**{cut(title, ROW)}**"]
    if own and own[0] in ("owner", "stopped"):   # the head's own blocker must not drown under its sub-tasks
        lines.append(f"- {ICON[own[0]]} {cut(own[1], ROW - 2)}")
    lines += [s[2] for s in subs[:SUBS]]
    if len(subs) > SUBS:
        lines.append(f"- {tr(lang, 'more', n=len(subs) - SUBS)}")
    return rank, ("chain", lines)


def build(st, lang=None, now=None):
    """The digest text or "" (nothing open): the owner's tasks, blockers first; a task is a bold heading with its open
    sub-tasks under it, a task without sub-tasks is one row. No session names, ids and paths. lang - hub language by
    default, resolved now."""
    lang, now = lang or i18n.current(), time.time() if now is None else now
    found = sorted((group(st, root, ds, lang, now) for root, ds in chains(st).items()), key=lambda g: g[0])
    if not found:
        return ""
    take, size = 0, 0   # one message: a part of a digest that did not go would stay in the chat, never erased
    for _, (kind, body) in found[:TASKS]:
        size += len(body if kind == "lone" else "\n".join(body)) + 2
        if take and size > CHARS:
            break
        take += 1
    blocks = []   # in the order of the rank; one-row tasks next to each other make one block
    for _, (kind, body) in found[:take]:
        if kind == "lone" and blocks and blocks[-1][0] == "lone":
            blocks[-1][1].append(body)
        else:
            blocks.append((kind, [body] if kind == "lone" else list(body)))
    parts = ["\n".join(lines) for _, lines in blocks]
    if len(found) > take:
        parts.append(f"- {tr(lang, 'more_tasks', n=len(found) - take)}")
    return "\n\n".join([tr(lang, "head", n=len(found)), *parts])
