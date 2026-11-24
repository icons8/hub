#!/usr/bin/env python3
"""The contributors of the hub, built from the thanks of CHANGES.md at every release, never kept by hand (owner decision
2026-11-17: show the colleagues who contributed, and keep the author first, not lost among them; 2026-11-18: the author
only in the section, not at the top of the README).

  python3 tools/contributors.py write <tree> — the "## Contributors" section at the end of <tree>/README.md and
      <tree>/CONTRIBUTORS.md, from <tree>/CHANGES.md (all of it, every date); code 3 — README.md has no author line
  python3 tools/contributors.py coauthors <tree> [--old <CHANGES.md>] [--history <clone>]
      — the "Co-authored-by:" lines of a release commit of <tree>: those its CHANGES.md thanks more often than --old does
      (no --old — the first commit of a repository: all of them); with --history (a clone of the repository, HEAD is its
      main) also everyone the thanks name whom no commit of that history credits yet, so a login the thanks-counting
      missed (a snapshot made before the credits) is caught up by the next commit, and a credited one is never repeated;
      a login without an id is a warning, not a refusal

The author is the "Created by <Name> ([@login](https://github.com/login))" line of README.md: the first line of its
built section (written there once by hand, kept by every rebuild; a line above the section, the older layout, wins); it
heads the section and CONTRIBUTORS.md, and is never a co-author (they author the release commits). A thanks is
"thanks: @a (PR #1)", "thanks: @a, @b (PR #1, #2)", "thanks: @a (issue #3)", "thanks: @a (PR #4 of the modules)",
"thanks: @a" (a report or an idea, over the bus or a chat), wrapped over lines or not; the header's "@login (PR #N)" says
how one is written and is not one. A pull request counts once however many items thank it. The GitHub id of a co-author
(its noreply address) comes from `gh api users/<login>`, cached in github-ids.json of the publishing state folder (next
to published.json, not in the repository)."""
import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import publish_allow  # noqa: E402

LOGIN = publish_allow.LOGIN
# the author line, the whole line: the very form the publish checks spare in README.md and CONTRIBUTORS.md
# (tools/publish_allow.py credit_free)
AUTHOR = re.compile(rf"^{publish_allow.AUTHOR_LINE.pattern}(?=\r?$)", re.M)   # a CRLF checkout too
THANKS = re.compile(rf"thanks:\s+(@{LOGIN}(?:\s*,\s*@{LOGIN})*)(?:\s+\(([^()]*)\))?")
REF = re.compile(r"\b(PR|issue)\s+#(\d+)((?:\s*(?:,|\band\b)\s*#\d+)*)(\s+of\s+the\s+modules)?", re.I)
HEADING = "## Contributors"
FILE = "CONTRIBUTORS.md"
# a GitHub login renamed since its thanks were written: old (lowered) -> new. The old thanks of CHANGES.md stay as they
# are (the updater shows hubs the items new by the difference of CHANGES.md); every list and co-author line built from
# them says the new login, and the two spellings are one contributor
RENAMED = {"dnnaumov99": "DeValar"}


def current(login):
    """login as GitHub calls it now."""
    return RENAMED.get(login.lower(), login)


def clauses(text):
    """[(logins, refs)] of every thanks in a CHANGES.md text: refs — {("pr"|"issue"|"modules-pr", n)}, empty for a
    thanks without a number. The header's example (a "#" with no number) is not one."""
    flat = re.sub(r"\n[ \t]+", " ", text)   # an item's wrapped lines are indented: one line again
    out = []
    for m in THANKS.finditer(flat):
        inside = m.group(2) or ""
        if re.search(r"#(?!\d)", inside):
            continue
        refs = set()
        for r in REF.finditer(inside):
            kind = "modules-pr" if r.group(4) else r.group(1).lower()
            for n in [r.group(2)] + re.findall(r"#(\d+)", r.group(3)):
                refs.add(("pr" if kind == "pr" else kind, int(n)))
        out.append(([current(x) for x in re.findall(rf"@({LOGIN})", m.group(1))], refs))
    return out


def tally(text, author=None):
    """[(login, pull requests, reports)] most first: a pull request (of the core or of the modules) counts once, an issue
    or a thanks without a number is a report; the author is not on it. A login keeps its first spelling."""
    seen, refs, notes = {}, {}, {}
    for logins, got in clauses(text):
        for login in logins:
            key = login.lower()
            if author and key == author.lower():
                continue
            seen.setdefault(key, login)
            refs.setdefault(key, set()).update(got)
            notes[key] = notes.get(key, 0) + (0 if got else 1)
    rows = []
    for key, login in seen.items():
        prs = sum(1 for k, _ in refs[key] if k in ("pr", "modules-pr"))
        reports = sum(1 for k, _ in refs[key] if k == "issue") + notes[key]
        rows.append((login, prs, reports))
    return sorted(rows, key=lambda r: (-(r[1] + r[2]), -r[1], r[0].lower()))


def mentions(text, author=None):
    """{login lowered: how many thanks name it} — what a co-author is told by (the open tree's CHANGES.md has no numbers)."""
    got = {}
    for logins, _ in clauses(text):
        for login in logins:
            if not (author and login.lower() == author.lower()):
                got[login.lower()] = got.get(login.lower(), 0) + 1
    return got


def own_part(readme):
    """The README above its built section (the last "## Contributors" heading: the section is the file's end)."""
    found = list(re.finditer(rf"(?m)^{re.escape(HEADING)}\r?$", readme))
    return readme if not found else readme[:found[-1].start()]


def author_of(readme):
    """(name, login) of the README's author line, or None: one above the built section (the older layout) first, else
    the section's own (its first line: the section keeps it, a rebuild writes it back)."""
    m = AUTHOR.search(own_part(readme)) or AUTHOR.search(readme)
    return (m.group(1), m.group(2)) if m else None


def plural(n, one, many):
    return f"{n} {one if n == 1 else many}"


def listing(rows):
    out = []
    for login, prs, reports in rows:
        parts = ([plural(prs, "pull request", "pull requests")] if prs else []) + \
                ([plural(reports, "report or idea", "reports and ideas")] if reports else [])
        out.append(f"- [@{login}](https://github.com/{login}): {', '.join(parts)}")
    return "\n".join(out)


def section(author_line, rows):
    body = [HEADING, "", author_line, ""]
    if rows:
        body += ["With the pull requests, bug reports and ideas of (most first; the whole list with what it counts is in",
                 f"[{FILE}]({FILE})):", "", listing(rows), ""]
    return "\n".join(body)


def contributors_md(author_line, rows):
    body = ["# Contributors", "", author_line, ""]
    if rows:
        body += ["With the pull requests, bug reports and ideas of, most first (a pull request counts once, however many",
                 "changes thank it):", "", listing(rows), ""]
    body += ["Built from the thanks of the changelog at every release by `tools/contributors.py`; not edited by hand.", ""]
    return "\n".join(body)


def write(tree):
    """The section at the end of README.md and CONTRIBUTORS.md of tree, from its CHANGES.md. -> "changed", "same", or
    None: no README.md or no CHANGES.md (nothing to do), "no author": README.md has no author line (nothing written)."""
    readme_path, changes_path = os.path.join(tree, "README.md"), os.path.join(tree, "CHANGES.md")
    if not (os.path.isfile(readme_path) and os.path.isfile(changes_path)):
        return None
    with open(readme_path, encoding="utf-8", newline="") as f:
        readme = f.read()
    with open(changes_path, encoding="utf-8") as f:
        changes = f.read()
    who = author_of(readme)
    if not who:
        return "no author"
    line = (AUTHOR.search(own_part(readme)) or AUTHOR.search(readme)).group(0)
    rows = tally(changes, who[1])
    # the section is the README's last: from its heading to the end, or added after the rest
    head = own_part(readme)
    head = head if head != readme else readme.rstrip("\n") + "\n\n"
    new = head + section(line, rows)
    changed = False
    if new != readme:
        with open(readme_path, "w", encoding="utf-8", newline="") as f:
            f.write(new)
        changed = True
    path = os.path.join(tree, FILE)
    text = contributors_md(line, rows)
    try:
        with open(path, encoding="utf-8", newline="") as f:
            old = f.read()
    except FileNotFoundError:
        old = None
    if old != text:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        changed = True
    return "changed" if changed else "same"


def state_dir():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import release   # the publishing state folder, as publish.sh and tools/release.py read it
    return release.state_dir()


def gh_id(login):
    """The GitHub id of login, or None: no such account, no gh, no network."""
    try:
        r = subprocess.run(["gh", "api", f"users/{login}", "--jq", ".id"], capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    got = r.stdout.strip()
    return int(got) if r.returncode == 0 and got.isdigit() else None


def trailers(logins, ids=None, fetch=None):
    """The "Co-authored-by:" lines of logins, with GitHub's noreply address by id (the only one it counts); ids —
    {login lowered: GitHub id}, a cache filled from fetch (gh by default). -> (lines, logins without an id)."""
    fetch = fetch or gh_id
    ids = {} if ids is None else ids
    lines, missing = [], []
    for login in logins:
        key = login.lower()
        if key not in ids:
            got = fetch(login)
            if got is None:
                missing.append(login)
                continue
            ids[key] = got
        lines.append(f"Co-authored-by: {login} <{ids[key]}+{login}@users.noreply.github.com>")
    return lines, missing


TRAILER = re.compile(r"^Co-authored-by:\s*(.*?)\s*(?:<([^<>]*)>)?\s*$", re.I | re.M)
NOREPLY = re.compile(rf"^(?:\d+\+)?({LOGIN})@users\.noreply\.github\.com$", re.I)


def credited(history):
    """{login lowered} of the "Co-authored-by:" trailers of a commit history's text (git log --format=%B): by the noreply
    address (GitHub's own, the form this tool writes), or the name of a trailer with another address."""
    got = set()
    for name, mail in TRAILER.findall(history):
        m = NOREPLY.match(mail.strip())
        got.add(current(m.group(1) if m else name).lower())
    return got


def coauthors(new, old=None, author=None, ids=None, fetch=None, history=None, extra=()):
    """trailers() of the logins new thanks more often than old (old None — all of new's), in the order of tally; history —
    the text of the repository's commits so far (None: not known): whom it already credits is left out, and whom it
    does not is added, however old's counts stand — with extra, logins beyond the thanks (the open tree's list)."""
    before = mentions(old, author) if old is not None else {}
    now = mentions(new, author)
    got = credited(history) if history is not None else None
    logins = []
    for login, _, _ in tally(new, author):
        key = login.lower()
        if now.get(key, 0) > before.get(key, 0) or got is not None:
            logins.append(login)
    if got is not None:
        logins += [x for x in extra if x.lower() not in {y.lower() for y in logins}
                   and not (author and x.lower() == author.lower())]
        logins = [x for x in logins if x.lower() not in got]
    return trailers(logins, ids, fetch)


def listed(text):
    """The logins of a CONTRIBUTORS.md list, in its order (the author line is not one of the list)."""
    return [current(x) for x in re.findall(rf"(?m)^- \[@({LOGIN})\]\(https://github\.com/\1\)", text)]


def ids_path():
    return os.path.join(state_dir(), "github-ids.json")


def load_ids():
    try:
        got = json.loads(read(ids_path()))
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def save_ids(ids):
    path = ids_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".part", "w", encoding="utf-8") as f:
        json.dump(ids, f, indent=1, sort_keys=True)
    os.replace(path + ".part", path)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def credit(tree, old, whole_list=False, history=None):
    """The co-author lines of a release commit of tree (its README.md, CHANGES.md and CONTRIBUTORS.md): whom its
    CHANGES.md thanks more often than old (main's CHANGES.md before it; None — the repository's first commit: all of
    them, or with whole_list everyone of its CONTRIBUTORS.md — the open tree, whose CHANGES.md keeps only its own history).
    history — the text of the commits main already has: whoever the thanks (with whole_list, the list) name and it does
    not credit yet is credited now, whoever it does is not repeated. The ids are cached beside the publishing state.
    -> (lines, logins without an id)."""
    def text(name):
        try:
            return read(os.path.join(tree, name))
        except OSError:
            return ""
    who = author_of(text("README.md"))
    ids = load_ids()
    before = dict(ids)
    author = who[1] if who else None
    if old is None and whole_list and history is None:
        lines, missing = trailers(listed(text(FILE)), ids)
    else:
        # the open tree's CHANGES.md keeps only its own history: with whole_list its list is whom to catch up as well
        lines, missing = coauthors(text("CHANGES.md"), old, author, ids, history=history,
                                   extra=listed(text(FILE)) if whole_list else ())
    if ids != before:
        save_ids(ids)
    return lines, missing


def main(argv=None):
    ap = argparse.ArgumentParser(prog="contributors.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write")
    w.add_argument("tree")
    c = sub.add_parser("coauthors")
    c.add_argument("tree", help="the snapshot: its README.md, CHANGES.md")
    c.add_argument("--old", help="main's CHANGES.md before this commit (none — the first commit)")
    c.add_argument("--history", help="a clone of the repository: whom its commits already credit is not repeated, whom "
                                     "the thanks name and they do not is added")
    a = ap.parse_args(argv)
    if a.cmd == "write":
        got = write(a.tree)
        if got == "no author":
            print("README.md has no author line \"Created by <Name> ([@login](https://github.com/login))\"",
                  file=sys.stderr)
            return 3
        if got == "changed":
            print("contributors: README.md and CONTRIBUTORS.md rebuilt from CHANGES.md", file=sys.stderr)
        return 0
    history = None
    if a.history:
        r = subprocess.run(["git", "-C", a.history, "log", "--format=%B"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
        if r.returncode:
            print(f"the commits of {a.history} are not readable: {r.stderr.strip()}", file=sys.stderr)
            return 3
        history = r.stdout
    lines, missing = credit(a.tree, read(a.old) if a.old else None, history=history)
    for login in missing:   # said, not refused: a typo in a thanks must not stop every release after it
        print(f"WARNING: contributors: no GitHub id for @{login} (gh api users/{login}): not a co-author of this commit "
              "(CONTRIBUTORS.md still lists it)", file=sys.stderr)
    for x in lines:
        print(x)
    return 0


if __name__ == "__main__":
    sys.exit(main())
