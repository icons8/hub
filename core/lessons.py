"""The "Lessons" block in an executor's result: what the delegator should write into its own CLAUDE.md.

The executor ends its result with a "Lessons" block (core/executors.REPORT_RULE). When the task is handed in, the core
sends the delegator the lessons themselves; no block or an empty one (a bare "none", "no new ones") does not wake it at
all (a line in the task record). The heading is matched the way sessions write it, in Russian: "## Uroki",
"**Uroki:**", "Uroki (dlya CLAUDE.md …):", "- **Uroki** te zhe: …" (Uroki = Lessons); "Urokov net", "Uroki: net" — no
lessons (a bare "net" = "none": followed by "krome" = "except", "no" = "but" or more text, it is no longer "none");
an empty block or the word without a block — unclear, wake the old way. The block ends at a "#" heading no deeper
than its own or at the end of the text.
The host extracts the block from the full result (the journal gets the result truncated, and the lessons are at the
end) — with the same function as the core.
"""
import re

from core import langdata

MAX = 3000   # characters of lessons in the message to the delegator

# the words of the heading, of "none" and of "but" in the languages with data (core/langdata.py, `words`), then English
_W = {k: langdata.alt("words", "lessons_" + k) for k in ("head", "none", "none_claude", "word", "but", "no")}
_GENITIVE = langdata.words("words", "lessons_genitive")   # "no lessons" form of the word: the heading says "none" itself
_FOR = tuple(langdata.words("words", "lessons_for"))      # "Lessons for CLAUDE.md:" — the heading's tail
_HEAD = re.compile(rf"^([\s>#*_-]*)({_W['head']}|lessons?)\b(.*)$", re.I)
_NONE = re.compile(rf"^({_W['none']}|none|no new|nothing)(\W|$)"
                   rf"|^[—–-]\s*$|{_W['none_claude']}", re.I)
_WORD = re.compile(rf"{_W['word']}|lesson", re.I)
_BUT = re.compile(rf"(?i)\b({_W['but']}|except|but)\b")
_NO = re.compile(rf"(?i)\b({_W['no']})\b")
_ITEM = re.compile(r"^\s*([-*•]|\d+[.)])\s+\S")


def _heading(m):
    """A "Lessons…" line is a block heading (and not "Lessons are also recorded in …"): bold, "#", colon, bracket, dash."""
    rest = m.group(3)
    bold = any(x in m.group(1) for x in ("**", "__", "#"))
    plain = rest.lstrip("*_ ")
    return bold or not plain or plain[0] in ":.(—–-" or plain.lower().startswith(_FOR)


def _is_head(line):
    """The next "Lessons…" heading (a second block), and not an item like "- lessons of the previous task were confirmed"."""
    m = _HEAD.match(line)
    return bool(m) and _heading(m)


def _tail(m):
    """What follows the heading on its line: "**Lessons (for CLAUDE.md):** text" → "text"."""
    rest = m.group(3).lstrip("*_ ")
    if rest.startswith("("):
        rest = rest[rest.find(")") + 1:] if ")" in rest else ""
    elif rest.lower().startswith(_FOR):
        rest = rest[rest.find(":") + 1:] if ":" in rest else ""
    return rest.lstrip("*_:.—–- ").strip()


def _bare_none(line):
    """The line is a bare "none": "none", "no new ones", "nothing" without "except", "but", "however" (otherwise a lesson follows them)."""
    line = line.strip().lstrip("*_>").strip()
    return bool(_NONE.match(line)) and not _BUT.search(line)


def _none(lines):
    """The block says outright that there are no lessons: the first paragraph is a single "none" line, no list items below.
    An empty block is not "none": lessons may have gone past it — wake the old way. In doubt it is not "none" (extra text beats a miss)."""
    rest = list(lines)
    while rest and not rest[0].strip():
        rest.pop(0)
    if not rest or not _bare_none(rest[0]) or len(rest) > 1 and rest[1].strip():
        return False
    return not any(_ITEM.match(x) for x in rest[1:])


def _level(m):
    """Level of the "Lessons" heading: "## Lessons" — 2; bold or plain — 6 (any "#" heading ends it)."""
    return m.group(1).count("#") or 6


def _ends(line, level):
    """The block ends at a "#" heading no deeper than itself ("### Tests" inside "## Lessons" is part of the lessons); bold
    lines are lesson items (in journal results, 393 of 395 blocks run to the end of the text)."""
    h = re.match(r"^\s{0,3}(#{1,6})\s", line)
    return bool(h) and len(h.group(1)) <= level


def scan(text):
    """[outcome, lessons]: "yes" — a block with lessons (as text; several blocks joined by a blank line); "none" — the
    block says outright "none" ("Lessons: none", "No lessons …"); "unclear" — the word is there but no lessons (the block
    is empty, "lessons recorded in hub task note") — wake the old way, let it look itself; "" — not a word about lessons."""
    out, said, unclear, lines = [], False, False, str(text or "").splitlines()
    i = 0
    while i < len(lines):
        m = _HEAD.match(lines[i])
        if m and m.group(2).lower() in _GENITIVE:   # genitive form: "No lessons", "No lessons for the profession's CLAUDE.md"
            alone = i + 1 >= len(lines) or not lines[i + 1].strip()   # text after it in the same paragraph — no longer "none"
            if _NO.search(m.group(3)) and not _BUT.search(m.group(3)) and alone:
                said = True
            else:
                unclear = True
        if not m or not _heading(m) or m.group(2).lower() in _GENITIVE:
            i += 1
            continue
        body, level = [_tail(m)], _level(m)
        i += 1
        while i < len(lines) and not _ends(lines[i], level) and not _is_head(lines[i]):
            body.append(lines[i])
            i += 1
        if _none(body):
            said = True
        elif any(x.strip() for x in body):
            out.append("\n".join(body).strip())
        else:
            unclear = True
    found = "\n\n".join(out)
    if found:
        return ["yes", found if len(found) <= MAX else found[:MAX] + "…"]
    return ["none" if said and not unclear else "unclear" if _WORD.search(str(text or "")) else "", ""]
