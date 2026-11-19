"""The language of the published tree, with a ratchet (open-source preparation): how many lines with Cyrillic letters the
published files hold, per file. The tree is being translated into English (code, comments, tests, docs); this count is the
plan of that work and its guard — the number of a file can only go down. A new Cyrillic line, or a new file with one, is
red with the file's name (tests/test_public_language.py); a file that got shorter is red too until the baseline is lowered,
so that the gain is kept.

What is counted is what tools/publish.sh puts into the snapshot: the files git knows (a new file after `git add`; their text
in the working tree, so a run sees an edit before the commit), without docs/ and without the paths the publish filter skips
(tools/publish_allow.py); binary files and symbolic links are not text. A line counts once, however many Cyrillic letters
it has — the count is a plan of the work, not a measure of the text.

A Russian word written with escapes is Russian: a line with no Cyrillic letter in it, where escapes (the four-digit and the
eight-digit and the braced forms, \\N{name}) and chr() calls (joined with "+" or by neighbouring literals) spell two or more
Cyrillic letters in a row, counts like a line with the letters. A single letter and a range in a class ([A-Z] with escaped ends)
are not words and do not count; what a regex or a join builds from pieces further away is not seen.

The baseline is tools/public_language_baseline.json: {file: lines}. Files where Russian is legitimate are in EXEMPT below,
each with its reason; they are not counted at all.

    python3 tools/public_language.py            — the files that broke the ratchet (exit 1), or the total
    python3 tools/public_language.py --report   — every file with Cyrillic, the biggest first, and the totals by folder
    python3 tools/public_language.py --prune    — lower the baseline to what is there now (it never takes growth)
    python3 tools/public_language.py --move OLD NEW — a file with Russian was renamed: its entry follows it (never more)

The baseline is derived: two branches that both lowered the same line conflict there — take either side and run --prune.

stdlib only, no imports of the hub."""
import collections
import json
import os
import re
import subprocess
import sys
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = os.path.join(ROOT, "tools", "public_language_baseline.json")
CYRILLIC = re.compile(r"[\u0400-\u052f\u1c80-\u1c8f\u1d2b\u1d78\u2de0-\u2dff\ua640-\ua69f]")   # the Cyrillic blocks, as escapes: this file stays clean

# A word hidden from the plain search. The marks of an escape are quick tests before the work of decoding.
_MARKS = ("\\u", "\\U", "\\N{", "chr(")
_ESCAPE = re.compile(r"\\+(?:u\{([0-9a-fA-F]{1,6})\}|u([0-9a-fA-F]{4})|U([0-9a-fA-F]{8})|N\{([^}\n]+)\})|"
                     r"\bchr\(\s*(0[xX][0-9a-fA-F]+|[0-9]{1,7})\s*\)")
_LETTERS = "\u0400-\u052f"   # what is read as a word: the Cyrillic and its supplement (no modifiers, no extended blocks)
_JOIN = re.compile(r"(?<=[%s])(?:[\"']\s*\+\s*[\"']|\s*\+\s*|[\"']\s*[\"'])(?=[%s])" % (_LETTERS, _LETTERS))
_RUN = re.compile("[%s]{2,}" % _LETTERS)
_CLASS = re.compile(r"\[[^\]\n]*\]")
_RANGE = re.compile("[%s]-[%s]" % (_LETTERS, _LETTERS))


def _char(m):
    """The letter an escape or a chr() call stands for; the text itself if it is not one."""
    hexa, short, long_, name, number = m.groups()
    try:
        if name:
            return unicodedata.lookup(name)
        if number:
            return chr(int(number, 0))
        return chr(int(hexa or short or long_, 16))
    except (KeyError, ValueError, OverflowError):
        return m.group(0)


def hides_a_word(line):
    """True if a line without a Cyrillic letter spells a word in it with escapes or chr() (a run of two or more
    letters, outside a class that holds a range)."""
    if not any(mark in line for mark in _MARKS):
        return False
    text = _JOIN.sub("", _ESCAPE.sub(_char, line))   # zero-width at the letters: a chain of any length is joined in one pass
    ranges = [m.span() for m in _CLASS.finditer(text) if _RANGE.search(m.group(0))]
    return any(not any(a <= m.start() and m.end() <= b for a, b in ranges) for m in _RUN.finditer(text))


# Not counted: a path from the root (a folder ends with "/") and why Cyrillic is right there. A new entry needs a reason a
# reader of the open repository would accept; a path that is gone is red (a stale entry hides nothing, but is noise).
EXEMPT = {
    "i18n/ru.json": "the Russian catalog of the interface: Russian is what the file is for",
    "i18n/uk.json": "the Ukrainian catalog of the interface: Cyrillic is what the file is for",
    "i18n/data/ru.json": "the Russian language data: the words and earlier forms the core recognises (core/langdata.py)",
    "i18n/data/uk.json": "the Ukrainian language data: what the core recognises in Ukrainian (core/langdata.py)",
    "modules/web/static/emoji/ru.json": "the Russian names of the emoji (Unicode CLDR) for the icon search: the language's own words",
    "modules/web/static/emoji/uk.json": "the Ukrainian names of the emoji (Unicode CLDR) for the icon search: the language's own words",
}


def published(root=ROOT):
    """The tracked files of the snapshot, as paths from the root: what tools/publish.sh archives (docs/ is removed) and
    checks (the paths its filter skips are not)."""
    from tools import publish_allow   # tools/ is a namespace package: the root must be on sys.path
    out = subprocess.run(["git", "ls-files", "-z", "--cached"], cwd=root, capture_output=True, text=True,
                         check=True).stdout
    return [p for p in out.split("\0") if p and p.split("/")[0] != "docs" and not publish_allow.skipped(p)
            and os.path.isfile(os.path.join(root, p))]


def exempt(path):
    # an embed's catalogs (core/embed.py: embed/<name>/i18n/<lang>.json) are catalogs like the core's
    path = re.sub(r"^embed/[a-z0-9_-]{1,32}/(?=i18n/)", "", path)
    return any(path == e or (e.endswith("/") and path.startswith(e)) for e in EXEMPT)


def lines_with_cyrillic(path):
    """How many lines of a text file have a Cyrillic letter; None for a binary file or a symbolic link."""
    if os.path.islink(path):
        return None
    with open(path, "rb") as f:
        data = f.read()
    if b"\0" in data:
        return None
    return sum(1 for line in data.decode("utf-8", "replace").split("\n") if CYRILLIC.search(line) or hides_a_word(line))


def count(root=ROOT, paths=None):
    """{file: lines with Cyrillic} for the files that have any and are not exempt."""
    out = {}
    for p in published(root) if paths is None else paths:
        if exempt(p):
            continue
        n = lines_with_cyrillic(os.path.join(root, p))
        if n:
            out[p] = n
    return out


def load(path=BASELINE):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def dump(counts, path=BASELINE):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(counts, f, indent=1, sort_keys=True)
        f.write("\n")


def grown(now, baseline):
    """[(file, was, now)] where the number rose or the file is new (was 0)."""
    return sorted((p, baseline.get(p, 0), n) for p, n in now.items() if n > baseline.get(p, 0))


def lowered(now, baseline):
    """[(file, was, now)] where the baseline is above what is there (now 0: the file is clean or gone)."""
    return sorted((p, n, now.get(p, 0)) for p, n in baseline.items() if now.get(p, 0) < n)


def growth_lines(found):
    return [f"{p}: {n} lines with Cyrillic, the baseline allows {was} — write English (or, for a text that is Russian by "
            f"design, add the file to EXEMPT in tools/public_language.py with the reason)" for p, was, n in found]


def problems(now, baseline):
    """The lines that explain why the ratchet is red; nothing — it holds."""
    out = growth_lines(grown(now, baseline))
    out += [f"{p}: {n} lines with Cyrillic, the baseline still holds {was} — keep the gain: "
            f"python3 tools/public_language.py --prune" for p, was, n in lowered(now, baseline)]
    return out


def pruned(now, baseline):
    """The baseline lowered to what is there; never above what it was, no zeros."""
    return {p: min(n, now.get(p, 0)) for p, n in baseline.items() if min(n, now.get(p, 0)) > 0}


def moved(now, baseline, old, new, files):
    """The baseline with the entry of `old` under the name `new` (a rename of a file; `files` are the published ones);
    None and why not, if it cannot be."""
    if old not in baseline:
        return None, f"{old}: not in the baseline"
    if new in baseline:
        return None, f"{new}: already in the baseline"
    if new not in files:
        return None, f"{new}: not a published file (a typo, or not yet added to git)"
    if old in files:
        return None, f"{old}: still a published file — this is not a rename"
    if now.get(new, 0) > baseline[old]:
        return None, f"{new}: {now[new]} lines with Cyrillic, more than {old} had ({baseline[old]})"
    out = {p: n for p, n in baseline.items() if p != old}
    if now.get(new, 0):
        out[new] = now[new]
    return out, ""


def report(now):
    lines = [f"{n:6} {p}" for p, n in sorted(now.items(), key=lambda x: (-x[1], x[0]))]
    folders = collections.Counter()
    for p, n in now.items():
        folders[p.split("/")[0] if "/" in p else "(root)"] += n
    lines += ["", *(f"{n:6} {d}" for d, n in folders.most_common()), "", f"{sum(now.values()):6} lines in {len(now)} files"]
    return "\n".join(lines)


def main(argv):
    now, baseline = count(ROOT), load(BASELINE)   # the globals are read now: tests point them elsewhere
    if argv == ["--report"]:
        print(report(now))
        return 0
    if argv == ["--prune"]:
        left = pruned(now, baseline)
        dump(left, BASELINE)
        print(f"baseline: {sum(baseline.values())} lines before, {sum(left.values())} now")
        found = grown(now, left)
        print("\n".join(growth_lines(found)))
        return 1 if found else 0
    if len(argv) == 3 and argv[0] == "--move":
        moved_to, why = moved(now, baseline, argv[1], argv[2], set(published(ROOT)))
        if moved_to is None:
            print(why)
            return 1
        dump(moved_to, BASELINE)
        print(f"baseline: {argv[1]} -> {argv[2]}")
        return 0
    if argv:
        print(__doc__)
        return 2
    found = problems(now, baseline)
    print("\n".join(found) or f"holds: {sum(now.values())} lines with Cyrillic in {len(now)} files")
    return 1 if found else 0


if __name__ == "__main__":
    sys.path.insert(0, ROOT)
    sys.exit(main(sys.argv[1:]))
