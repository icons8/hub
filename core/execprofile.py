"""Compressed agent profile for its executors (owner decision 2026-10-23). Next to the agent's CLAUDE.md there may be a pair:
CLAUDE.executor.md — a digest, first line `source-sha256: <sha256 of the source>`, and CLAUDE.executor.src.md — that
source itself (the full CLAUDE.md it was compressed from). The pair is placed and recompressed not by the core; the core
only reads it when raising an executor (hosts/streamjson.command):
- CLAUDE.md unchanged since the source — the digest;
- items appended or changed in CLAUDE.md — the digest and those items verbatim and whole at the end, under a heading that
  says they win where they differ from the digest, each run with its section. Whole items, compared item by item (an
  item: a list entry with its indented lines, a paragraph, a heading, a code block): an agent appends to an old item and
  rewraps the paragraph, so a changed line alone would lose its subject, or a "never" on the line above;
- the full CLAUDE.md and a stale mark, when the delta could lose a rule: an item gone with nothing in its place (or two
  merged into one), changed items that lost a gate phrase (prod, merge, a secret, "never", "not …" — GATE: a revoked
  permission inside a rewrapped paragraph), a code block changed or added (verbatim out of its place it reads as a rule),
  the delta text over DELTA_MAX, or an inconsistent pair (half missing, the source hash differs from the one in the digest
  header: an interrupted write, a manual edit). The stale mark is carried by the start's host.done, and recompression is
  driven by it.
The agent itself always gets the full one; no pair — as before."""
import difflib
import hashlib
import os
import re

from core import langdata

EXEC, SRC = "CLAUDE.executor.md", "CLAUDE.executor.src.md"
HEAD = re.compile(r"source-sha256:\s*([0-9a-f]{64})")
HEADING = re.compile(r" {0,3}#{1,6}[ \t]")
LIST = re.compile(r"(?:[-*+]|\d+[.)])[ \t]")   # a list item's first line, not indented
OPEN = re.compile(r" {0,3}(`{3,}|~{3,})")   # a code block's opening fence: the closing one is the same character, no shorter
CODE = re.compile(r"(?m)^[ \t]*(```|~~~)")   # a fence at any indent: in a list item a block is indented further
# bytes of the delta text as the executor gets it (heading, labels, whole items): beyond that the digest's gain melts away,
# and there is no point carrying it without recompressing. On the owner's week (413 spawns of an agent with long items,
# 2026-11-08) whole changed items were 16 KB median, 43 KB at p90: 60 KB keeps 312 of the 314 a digest can serve
DELTA_MAX = 60 * 1024
DELTA_HEAD = ("# New and changed in the agent's profile since the compression (verbatim; where it differs from the digest "
              "above, this is right)")
# a phrase that holds a rule back: a revoked permission or a gate. A replacement that drops one gives the full profile:
# the old wording stays in the digest, and a rewrapped paragraph without the "not" next to it would read as a permission.
# Wide on purpose ("not …" is any negation): a false refusal costs one full profile, a miss — a rule lost. The words of
# the owner's languages come from the language data (core/langdata.py, `words.gate`)
GATE = re.compile(r"(?<!\w)(?:prod\w*|merge\w*|secret\w*|token\w*|password\w*|credential\w*|gate\w*|never|"
                  r"forbid\w*|irreversib\w*|--force|force\w*|owner's\s+(?:yes|word|approval)|only\s+the\s+owner|"
                  r"not\s+\w+|no\s+\w+|\w+n't\s+\w+|cannot\s+\w+|" + langdata.alt("words", "gate") + ")", re.I)


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _items(lines):
    """The text split into items: [(first line, end)] over the non-empty lines. An item starts after an empty line (an
    indented line after it still continues the item above: a loose list item's next paragraph), at a heading (an item of
    one line), at a list marker without indent, and a code block is an item from fence to fence; anything else continues
    the item above — an indented line, a sub-item, a wrapped line of a paragraph. Empty lines belong to none."""
    out, kind, blank, fence = [], None, True, ""   # kind of the open item: text | heading | code | done (a closed block)
    for j, ln in enumerate(lines):
        if kind == "code":
            out[-1][1] = j + 1
            if ln.strip().startswith(fence[0]) and len(ln.strip()) >= len(fence) and not ln.strip().strip(fence[0]):
                kind = "done"
            continue
        if not ln.strip():
            blank = True
            continue
        m = OPEN.match(ln)
        indented = ln[:1] in (" ", "\t")
        if (not out or kind in ("heading", "done") or (blank and not (indented and kind == "text"))
                or HEADING.match(ln) or m or LIST.match(ln)):
            out.append([j, j + 1])
            kind = "heading" if HEADING.match(ln) else "code" if m else "text"
            fence = m.group(1) if m else ""
        else:
            out[-1][1] = j + 1
        blank = False
    return [tuple(x) for x in out]


def _gates(text):
    """The gate phrases of the text with their counts, the spacing and the apostrophe folded (a rewrap moves a line break
    inside "not X", an edit may swap ' and ’)."""
    counts = {}
    for m in GATE.finditer(text.replace("\u2019", "'")):
        k = re.sub(r"\s+", " ", m.group(0).lower())
        counts[k] = counts.get(k, 0) + 1
    return counts


def _inserted(src, cur):
    """The spans of cur that are pure insertions, if the source is entirely a subsequence of the current sequence:
    [(i, i, j1, j2)]; None — something was deleted or changed. Greedy in-order matching finds the subsequence if it
    exists, where difflib takes the longest common chunk and, on repeated items, sees a replacement where there is only
    an insertion."""
    out, i = [], 0
    for j, x in enumerate(cur):
        if i < len(src) and x == src[i]:
            i += 1
        elif out and out[-1][3] == j:
            out[-1] = (out[-1][0], out[-1][1], out[-1][2], j + 1)
        else:
            out.append((i, i, j, j + 1))
    return out if i == len(src) else None


def _kept(old, new):
    """An old item lives on in one of the new ones: at least half of its words are there, in order (an edit, an addition,
    a rewrap); else it is gone — replaced by an unrelated item in its place, a permission without a gate word."""
    words = old.split()
    for x in new:
        same = sum(b.size for b in difflib.SequenceMatcher(None, words, x.split(), autojunk=False).get_matching_blocks())
        if 2 * same >= len(words):
            return True
    return False


def _delta(src, cur):
    """The changed items of cur against the source, compared item by item (a changed line reads with its item: its
    start, a "never" on the line above): (cur's items, [(first line, end) of each run of changed items in cur]); or the
    reason the delta cannot stand for the change: removed (an item gone — with nothing in its place, two merged into one,
    an unrelated one in its place), gate (the changed items lost a gate phrase), code (a code block changed or added)."""
    si, ci = _items(src), _items(cur)   # cur's items go back with the runs: the labels need them
    st = ["\n".join(src[a:b]) for a, b in si]
    ct = ["\n".join(cur[a:b]) for a, b in ci]
    spans = _inserted(st, ct)
    if spans is None:
        spans = [(i1, i2, j1, j2) for op, i1, i2, j1, j2 in
                 difflib.SequenceMatcher(None, st, ct, autojunk=False).get_opcodes() if op != "equal"]
    out = []
    for i1, i2, j1, j2 in spans:
        old, new = st[i1:i2], ct[j1:j2]
        if any(CODE.search(x) for x in old + new):
            return "code"
        if len(new) < len(old) or any(not _kept(x, new) for x in old):
            return "removed"
        dropped = _gates("\n".join(old))
        for k, n in _gates("\n".join(new)).items():
            dropped[k] = dropped.get(k, 0) - n
        if any(n > 0 for n in dropped.values()):
            return "gate"
        out.append((ci[j1][0], ci[j2 - 1][1]))
    return ci, out


def _labelled(cur, items, runs):
    """The delta text: each run of changed items verbatim, with its section — the last Markdown heading above it,
    outside code blocks; a run that starts with its own heading names its section itself."""
    blocks, head, k = [], None, 0
    for a, b in runs:
        while k < len(items) and items[k][0] < a:   # the headings above the run, one pass over the items
            if HEADING.match(cur[items[k][0]]):
                head = cur[items[k][0]]
            k += 1
        body = "\n".join(cur[a:b])
        if HEADING.match(cur[a]):
            blocks.append(body)
        else:
            blocks.append((f"(in the section \"{head.strip()}\")" if head else "(at the start of the file)") + "\n" + body)
    return DELTA_HEAD + "\n\n" + "\n\n".join(blocks)


def pick(agent_file):
    """What to give the agent's executor: (profile text, info) or None — no pair, the full one as before. Info —
    {kind: executor|executor+delta|full, stale?: changed|delta|pair, why?: removed|gate|code (of changed), bytes,
    full_bytes, source, pair}."""
    folder = os.path.dirname(agent_file)
    short, src = _read(os.path.join(folder, EXEC)), _read(os.path.join(folder, SRC))
    if short is None and src is None:
        return None
    full = _read(agent_file)
    if full is None:
        return None
    text_full = full.decode("utf-8", "replace").rstrip("\n")
    info = {"full_bytes": len(full), "source": _sha(full)[:12]}

    def whole(stale, **more):
        return text_full, {**info, "kind": "full", "stale": stale, **more, "bytes": len(full)}

    if short is None or src is None:
        return whole("pair")
    first, _, rest = short.decode("utf-8", "replace").partition("\n")
    m = HEAD.search(first)
    if not m or m.group(1) != _sha(src):
        return whole("pair")
    info["pair"] = m.group(1)[:12]
    text = rest.strip("\n")
    if not text.strip():   # just the header: compression was cut off — do not leave the executor without a profile
        return whole("pair")
    kind = "executor"
    if full != src:
        cur = text_full.splitlines()
        got = _delta(src.decode("utf-8", "replace").rstrip("\n").splitlines(), cur)
        if isinstance(got, str):
            return whole("changed", why=got)
        items, runs = got
        if runs:
            delta = _labelled(cur, items, runs)
            size = len(delta.encode())
            if size > DELTA_MAX or len(text.encode()) + size >= len(full):   # no gain left over the full one
                return whole("delta")
            text += "\n\n" + delta
            kind = "executor+delta"
    return text, {**info, "kind": kind, "bytes": len(text.encode())}
