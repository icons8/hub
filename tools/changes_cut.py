#!/usr/bin/env python3
"""Cut the history of CHANGES.md: the published file becomes English, the earlier (Russian) history leaves the tree.

    tools/changes_cut.py status <tag>
    tools/changes_cut.py apply <tag> [--date YYYY-MM-DD] [--history docs/CHANGES-history-ru.md] [--force]

Who reads the file (and why the cut is made right after a release tag, not at an arbitrary time):
  - the updater of a colleague's hub on the stable channel builds its message from `update.new_notes(its file, the new
    file)`: the lines the old file did not have. Its code is that of the tag and cannot be changed, so the cut file must
    add no old line to that message;
  - the core reads the items under the sessions heading (`news.session_notes`; both headings are known to every live
    tag) and attaches the ones it has not seen to the sessions;
  - the "Updates" page lists the dates of the file (`news.changes`), the Sync commit takes the first section, a tag takes
    the entries since the previous one (tools/release.py).

What stays in the file: the entries the tag's CHANGES.md did not have (the stable hubs have not seen them yet) and one
entry of the cut day. Everything the tag already had goes to the history file, which is not published (docs/). The kept
entries must be English already: translate them in CHANGES.md first (the command lists them), then apply.
"""
import argparse
import datetime
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core import forms  # noqa: E402
from tools import public_language  # noqa: E402

SESSIONS = forms.SESSION_SECTIONS   # both headings: a file of any live tag has the earlier one
CYRILLIC = public_language.CYRILLIC   # the ratchet's own definition: what it would count, the cut must not leave
DATE = re.compile(r"^## (\d{4}-\d{2}-\d{2})\s*$")
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HISTORY = "docs/CHANGES-history-ru.md"

HEADER = """# What's new in the hub

Newest first. Every hub shows this list: the "Updates" page and the message after an update take their items from here.
An item from someone else's PR ends with thanks: "— thanks: @login (PR #N)".
The "### For sessions" subsection under a date is what changed for the sessions themselves (commands, rules): the core
attaches to each session's next message only what it has not seen yet; the page does not show it.
"""
CUT_ITEM = "The release notes are written in English now; the \"Updates\" page lists the entries from this date on."


class Refused(Exception):
    pass


def parse(text, strict=True):
    """[{"date", "owner": [item], "sessions": [item]}] newest first; an item is its lines (the "- " line and the indented
    lines that continue it). The lines before the first date (the header) and blank lines are dropped; any other line the
    parser would drop is refused (strict), the tag's old file is read leniently: it is only compared with."""
    blocks, block, items, last = [], None, None, None
    for line in (text or "").splitlines():
        m = DATE.match(line)
        if m:
            block = {"date": m.group(1), "owner": [], "sessions": []}
            blocks.append(block)
            items, last = block["owner"], None
        elif line.startswith("## "):
            if strict:
                raise Refused(f"a heading that is not a date: {line!r}")
            block, items, last = None, None, None
        elif line.startswith("### "):
            if block is None:
                continue
            if line.strip() not in SESSIONS:
                if strict:
                    raise Refused(f"a subheading the cut does not know: {line!r}")
                items, last = [], None
                continue
            items, last = block["sessions"], None
        elif line.startswith("- ") and items is not None:
            last = [line]
            items.append(last)
        elif line.strip() and last is not None and line[:1] in " \t":
            last.append(line)
        elif strict and line.strip() and (blocks or line.startswith("- ")):   # the header before the first date is free text
            raise Refused(f"a line the cut does not know how to keep: {line[:80]!r}")
    return blocks


def kept(current, tag_text):
    """The entries of `current` the tag's file did not have. An item is old when the tag's file has all of its lines (the old
    updater compares the first line only; an item whose later lines changed is kept here, so no edit is lost). Dates without
    new items are dropped."""
    old = {tuple(i) for b in parse(tag_text, strict=False) for kind in ("owner", "sessions") for i in b[kind]}
    out = []
    for b in parse(current):
        n = {"date": b["date"], "owner": [i for i in b["owner"] if tuple(i) not in old],
             "sessions": [i for i in b["sessions"] if tuple(i) not in old]}
        if n["owner"] or n["sessions"]:
            out.append(n)
    return out


def cyrillic(blocks):
    """The kept lines the ratchet would count: with a Cyrillic letter, or a Russian word spelled with escapes."""
    return [line for b in blocks for kind in ("owner", "sessions") for item in b[kind] for line in item
            if CYRILLIC.search(line) or public_language.hides_a_word(line)]


def render(blocks, date):
    """The new file: the English header, the cut day with its entry first, then the kept entries."""
    if blocks and blocks[0]["date"] > date:
        raise Refused(f"the kept entries are dated {blocks[0]['date']}, later than the cut day {date}")
    if not blocks or blocks[0]["date"] != date:
        blocks = [{"date": date, "owner": [], "sessions": []}] + blocks
    out = [HEADER]
    for n, b in enumerate(blocks):
        owner = ([["- " + CUT_ITEM]] if n == 0 else []) + b["owner"]
        part = [f"## {b['date']}"] + [line for item in owner for line in item]
        if b["sessions"]:
            part += ["", SESSIONS[1]] + [line for item in b["sessions"] for line in item]
        out.append("\n".join(part) + "\n")
    return "\n".join(out)


def cut(current, tag_text, date):
    """(new text, kept blocks); Refused if a kept entry still has Cyrillic or the file is cut already."""
    if not CYRILLIC.search(current or ""):
        raise Refused("CHANGES.md has no Cyrillic: it is cut already")
    blocks = kept(current, tag_text)
    left = cyrillic(blocks)
    if left:
        raise Refused(f"{len(left)} kept lines still in Russian, translate them in CHANGES.md first:\n"
                      + "\n".join(x[:160] for x in left))
    return render(blocks, date), blocks


def verify(new, tag_text):
    """What the readers get from the new file against the tag's one: nothing old and nothing in Russian. Needs the core."""
    from core import news
    from modules.update.update import new_notes
    notes = new_notes(tag_text, new)
    sess = news.session_notes(new)
    bad = [x for x in notes + sess if CYRILLIC.search(x)]
    if bad:
        raise Refused("the readers would still see Russian: " + "; ".join(x[:80] for x in bad[:5]))
    return notes, sess


def tag_file(repo, tag):
    r = subprocess.run(["git", "show", f"{tag}:CHANGES.md"], cwd=repo, capture_output=True)
    if r.returncode:
        raise Refused(f"git show {tag}:CHANGES.md: {r.stderr.decode('utf-8', 'replace').strip()[:200]}")
    try:
        return r.stdout.decode("utf-8")   # strictly: a replaced byte would make an old item look new
    except UnicodeDecodeError as e:
        raise Refused(f"{tag}:CHANGES.md is not UTF-8: {e}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("action", choices=("status", "apply"))
    p.add_argument("tag", help="the release tag whose CHANGES.md the stable hubs have")
    p.add_argument("--date", default=datetime.date.today().isoformat(), help="the day of the cut (the first heading)")
    p.add_argument("--history", default=HISTORY, help="where the old file goes (not a published path)")
    p.add_argument("--repo", default=ROOT)
    p.add_argument("--force", action="store_true", help="overwrite an existing history file")
    a = p.parse_args(argv)
    path = os.path.join(a.repo, "CHANGES.md")
    try:
        try:
            if not DAY.fullmatch(a.date):
                raise ValueError(a.date)
            datetime.date.fromisoformat(a.date)
        except ValueError:
            raise Refused(f"--date {a.date!r} is not a day as YYYY-MM-DD")
        with open(path, encoding="utf-8") as f:
            current = f.read()
        tag_text = tag_file(a.repo, a.tag)
        blocks = kept(current, tag_text)
        left = cyrillic(blocks)
        items = sum(len(b["owner"]) + len(b["sessions"]) for b in blocks)
        print(f"kept: {items} items in {len(blocks)} days; still in Russian: {len(left)} lines")
        if a.action == "status":
            for x in left:
                print("  " + x[:160])
            return 1 if left else 0
        text, _ = cut(current, tag_text, a.date)
        hist = os.path.join(a.repo, a.history)
        if os.path.exists(hist) and not a.force:
            with open(hist, encoding="utf-8") as f:
                if f.read() != current:   # a rerun after an interrupted cut finds the same file
                    raise Refused(f"{a.history} exists and differs from CHANGES.md (--force to overwrite)")
        notes, sess = verify(text, tag_text)
        print(f"the old updater shows {len(notes)} lines, sessions get {len(sess)} items")
        os.makedirs(os.path.dirname(hist), exist_ok=True)
        for target, body in ((hist, current), (path, text)):   # each file whole or not at all
            with open(target + ".new", "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            os.replace(target + ".new", target)
        print(f"CHANGES.md is cut to {len(text.splitlines())} lines; the history ({len(current.splitlines())} lines) is in {a.history}")
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
