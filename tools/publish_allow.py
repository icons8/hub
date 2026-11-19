"""The publish check filter: snapshot lines that contain a deny pattern, except those where it does not remain without the allowed
substrings (tools/publish-allow.txt — for example, the author's login in a public project address). Arguments: the patterns file,
the flags (-i or empty), the allow file and the snapshot folder. Prints "path:line:text" of every line that counts.
It reads the folder itself, the same on all OSes (no grep: GNU and BSD flags differ), binary files too; a symbolic
link is checked by its target — that is what the snapshot publishes. Patterns are matched against the text of one line: ^ and $ are its start and
end. The same functions keep the tree clean between releases (tests/test_public_tree.py)."""
import os
import re
import sys

# not checked, a path from the snapshot root: the eval cases of a Russian-speaking owner (publish.sh removes them, like docs/), a foreign library in vendor, the shared forms themselves and the signature of the "Agents Hub.app" bundle — the signature and the signed executable
# (they contain the certificate holder's name, owner decision 2026-10-02)
SKIP = ("modules/web/vendor/", "tools/publish-deny.txt", "evals/locale/ru/",
        "deploy/mac/Agents Hub.app/Contents/_CodeSignature/", "deploy/mac/Agents Hub.app/Contents/MacOS/Agents Hub")

# the credits (owner decision 2026-11-17: the author and the contributors are named in the public release): in these two
# files of the root only, the author line as a whole line and a profile link "[@login](https://github.com/login)" do not
# count; the rest of their lines does. tools/contributors.py writes both forms; a login has no "/" or ".", so no server or
# repository address passes this way
CREDIT_FILES = ("README.md", "CONTRIBUTORS.md")
LOGIN = r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}"
CREDIT_LINK = re.compile(rf"\[@({LOGIN})\]\(https://github\.com/\1\)")
AUTHOR_LINE = re.compile(rf"Created by ([^\W\d_]+(?:[ '-][^\W\d_]+){{0,4}}) \(\[@({LOGIN})\]\(https://github\.com/\2\)\)")


def patterns(path, ignore_case):
    case = re.I if ignore_case else 0
    with open(path, encoding="utf-8") as f:
        return [re.compile(p, case) for p in (x.rstrip("\n") for x in f) if p]


def allowed(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [x.strip() for x in f if x.strip() and not x.startswith("#")]
    except FileNotFoundError:
        return []


def credit_free(rel, text):
    """The text of one line of rel without its credits (above); any other file — as it is."""
    if rel not in CREDIT_FILES:
        return text
    if AUTHOR_LINE.fullmatch(text.rstrip("\r")):
        return ""
    return CREDIT_LINK.sub("", text)


def credit_file(rel, data):
    """The bytes of a whole file without its credits, line by line (core/appcode.open_guard's spare); None — not a file
    of credits, read it as it is."""
    if rel not in CREDIT_FILES:
        return None
    return "\n".join(credit_free(rel, x) for x in data.decode("utf-8", "replace").split("\n")).encode("utf-8")


def skipped(rel):
    return any(rel == s or (s.endswith("/") and rel.startswith(s)) for s in SKIP)


def lines(path):
    """The lines of one file as they are published: the bytes of a binary file can be anything — an unreadable byte becomes
    a replacement character, not a crash; a symbolic link is one line, its target."""
    if os.path.islink(path):
        return [os.readlink(path)]
    with open(path, "rb") as f:
        return f.read().decode("utf-8", "replace").split("\n")


def tree(root):
    """(path from the root, line number, text) of every line of every file under root, without .git."""
    for folder, dirs, files in os.walk(root):
        links = [d for d in dirs if os.path.islink(os.path.join(folder, d))]
        dirs[:] = sorted(d for d in dirs if d != ".git" and d not in links)
        for name in sorted(files + links):
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            if skipped(rel):
                continue
            for number, text in enumerate(lines(path), 1):
                yield rel, number, text


def denied(entries, deny, allow):
    """The entries (path, line number, text) that count as a violation — as "path:line:text" strings."""
    for rel, number, text in entries:
        if skipped(rel) or not any(p.search(text) for p in deny):
            continue
        rest = credit_free(rel, text)
        for a in allow:
            rest = rest.replace(a, "")
        if any(p.search(rest) for p in deny):
            yield f"{rel}:{number}:{text}\n"


if __name__ == "__main__":
    deny_path, flags, allow_path, root = sys.argv[1:5]
    for hit in denied(tree(root), patterns(deny_path, "-i" in flags), allowed(allow_path)):
        sys.stdout.buffer.write(hit.encode("utf-8", "replace"))
