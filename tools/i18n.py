#!/usr/bin/env python3
"""Checks and housekeeping of the string catalogs (i18n/README.md).

    tools/i18n.py check              catalogs are intact and fresh; Russian literals in texts for people have not grown
    tools/i18n.py check --release    and every ready language is whole: a release carries every phrase in every language
                                     the hub offers (publish.sh, code 10; tools/translate.py fills what is missing)
    tools/i18n.py status <language>  what to translate in a language (missing or stale) — with the English text (JSON)
    tools/i18n.py stamp <language> [key …]  after translating: which English text each key was translated from
    tools/i18n.py drop               remove stale translations of optional languages (they will show English)
    tools/i18n.py scan [--write]     Russian literals in texts for people, by file; --write — save them as the
                                     new i18n/pending.json

i18n/sync.json — for every language and key, a short hash of the English text it was translated from. The English was
changed — the hash does not match: for required languages (stage.json translated: ru, en) it is an error until they are
translated and marked again; for the others `drop` removes the stale entry — the language shows English until there is a
translation.

i18n/pending.json — the files with texts for people (page, "Files", Telegram, hub output) that still hold Russian string
literals, with their line counts: the count only goes down, a clean file leaves the list. Comments and docstrings do not
count. A line marked `i18n-ok` is Russian on purpose (language names, parsing of earlier Russian forms).
"""
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tokenize
import unicodedata

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from core import embed, i18n  # noqa: E402

CATALOGS = os.path.join(REPO, "i18n")
SYNC = "sync.json"
PENDING = "pending.json"
STAGE = "stage.json"
NOTES = "glossary.json"   # what the glossary.* terms mean, for translators (tools/translate.py) - not a language
MARK = "i18n-ok"
CYRILLIC = re.compile(r"[\u0410-\u044f\u0401\u0451\u0406\u0456\u0407\u0457\u0404\u0454\u0490\u0491]")   # Russian and Ukrainian letters
# human-facing texts: a Russian literal belongs only in the catalog here (slice S1 — the page, S2 — Telegram and hub)
SCOPE = re.compile(r"^(modules/web/(static/|bundled/|[^/]+\.py$)|modules/files/|modules/telegram/|bin/hub$)")
# keys the code assembles from parts (t(`web.x.${k}`)): a text search does not see them
DYNAMIC = {f"web.{p}.manifest_{f}" for p in ("flow", "usage") for f in ("description", "why")}   # server.manifest_text
# complete in all ready languages: the agent's CLAUDE.md template — the agent maintains the file in the language it is written in
EVERYWHERE = ("agent_file.",)

# scripts a language catalog may have besides Latin
SCRIPTS = {"ru": {"CYRILLIC"}, "uk": {"CYRILLIC"}, "hi": {"DEVANAGARI"}, "ar": {"ARABIC"}, "zh-CN": {"CJK"},
           "ja": {"CJK", "HIRAGANA", "KATAKANA"}, "ko": {"HANGUL", "CJK"}}


def source_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]


def foreign_script(text, lang):
    """Scripts of the text's letters that the language does not write in (a phrase of another language that leaked into a translation)."""
    own = SCRIPTS.get(lang, set()) | {"LATIN"}
    return sorted({unicodedata.name(c).split()[0] for c in text
                   if unicodedata.category(c) in ("Lu", "Ll", "Lt", "Lo")} - own)


def _read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _write(path, data):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")


def _catalog(root, lang):
    return _read(os.path.join(root, f"{lang}.json"), None)


def _langs(root):
    return sorted(n[:-5] for n in os.listdir(root) if n.endswith(".json") and n not in (SYNC, PENDING, STAGE, NOTES))


def problems(root=None, langs=None, release=False):
    """What keeps the catalogs from being whole and fresh, one string each; [] — all good. release — every ready language
    must be whole too, not only en and ru: between releases a new phrase may show English in the others, a release may not."""
    root = root or CATALOGS
    en = _catalog(root, i18n.SOURCE) or {}
    sync = _read(os.path.join(root, SYNC), {})
    stage = _read(os.path.join(root, STAGE), None) or _read(os.path.join(CATALOGS, STAGE), {})   # an embed's: the core's
    translated = stage.get("translated") or [i18n.SOURCE]
    out = []
    for lang in langs or sorted(set(i18n.LANGS) | set(_langs(root))):
        cat = _catalog(root, lang)
        if cat is None:
            out.append(f"{lang}: no catalog i18n/{lang}.json")
            continue
        need, may = i18n.forms(lang)
        stamps = sync.get(lang, {})
        for key, value in cat.items():
            if key not in en:
                out.append(f"{lang}: {key}: not in English")
                continue
            if isinstance(value, dict):
                if not isinstance(en[key], dict) or not need <= set(value) <= may or \
                        not all(isinstance(v, str) for v in value.values()):
                    out.append(f"{lang}: {key}: plural forms {sorted(value)}, {lang} needs {sorted(need)}")
            elif not isinstance(value, str) or isinstance(en[key], dict):
                out.append(f"{lang}: {key}: plural forms — a text where English has forms, or the other way")
            texts = value.values() if isinstance(value, dict) else [value]
            for script in sorted({sc for t in texts if isinstance(t, str) for sc in foreign_script(t, lang)}):
                out.append(f"{lang}: {key}: letters of another script ({script})")
            if i18n.placeholders(value) != i18n.placeholders(en[key]):
                out.append(f"{lang}: {key}: placeholders {sorted(i18n.placeholders(value))}, English has "
                           f"{sorted(i18n.placeholders(en[key]))}")
            if lang != i18n.SOURCE and stamps.get(key) != source_hash(en[key]):
                out.append(f"{lang}: {key}: stale — English changed since it was translated "
                           f"(tools/i18n.py status {lang}; translate, then stamp — or drop)")
        if lang in translated and lang != i18n.SOURCE:
            out += [f"{lang}: {key}: missing" for key in en if key not in cat]
        elif release and lang in (stage.get("ready") or ()) and lang != i18n.SOURCE:
            out += [f"{lang}: {key}: missing — a release translates it (tools/translate.py {lang})" for key in en
                    if key not in cat]
        elif lang in (stage.get("ready") or ()) and lang != i18n.SOURCE:
            out += [f"{lang}: {key}: missing (needed in every ready language)" for key in en
                    if key.startswith(EVERYWHERE) and key not in cat]
    return out


def embedded(repo=None, release=False):
    """problems() of every embed's catalogs (core/embed.py: embed/<name>/i18n — its English is its own): the languages it
    brings and those every catalog must have whole (the tree's stage.json "translated"; for a release "ready" too)."""
    out = []
    stage = _read(os.path.join(repo or REPO, "i18n", STAGE), {})
    need = set(stage.get("translated") or [i18n.SOURCE]) | (set(stage.get("ready") or ()) if release else set())
    for name, top in embed.roots(repo or REPO):
        folder = os.path.join(top, "i18n")
        if os.path.isdir(folder):
            out += [f"{embed.FOLDER}/{name}: {x}"
                    for x in problems(folder, sorted(set(_langs(folder)) | set(need)), release=release)]
    return out


def status(lang, root=None):
    """[(key, English text)] that the language lacks: missing or translated from the earlier English."""
    root = root or CATALOGS
    en = _catalog(root, i18n.SOURCE) or {}
    cat = _catalog(root, lang) or {}
    stamps = _read(os.path.join(root, SYNC), {}).get(lang, {})
    return [(k, v) for k, v in en.items() if k not in cat or stamps.get(k) != source_hash(v)]


def stamp(lang, keys=None, root=None):
    """Record that these keys of the language (all by default) are translated from the current English text."""
    root = root or CATALOGS
    en = _catalog(root, i18n.SOURCE) or {}
    cat = _catalog(root, lang) or {}
    path = os.path.join(root, SYNC)
    sync = _read(path, {})
    mine = {k: v for k, v in sync.get(lang, {}).items() if k in en and k in cat}
    for key in keys or list(cat):
        if key not in cat or key not in en:
            raise SystemExit(f"{lang}: {key}: not in both English and {lang} — translate it first")
        mine[key] = source_hash(en[key])
    sync[lang] = mine
    _write(path, sync)


def drop_stale(root=None, langs=None):
    """Remove from optional languages the translations that are stale or have no English key: English will show instead."""
    root = root or CATALOGS
    en = _catalog(root, i18n.SOURCE) or {}
    path = os.path.join(root, SYNC)
    sync = _read(path, {})
    translated = _read(os.path.join(root, STAGE), {}).get("translated") or [i18n.SOURCE]
    for lang in langs or _langs(root):
        if lang in translated:
            continue
        cat = _catalog(root, lang) or {}
        stamps = sync.get(lang, {})
        keep = {k: v for k, v in cat.items() if k in en and stamps.get(k) == source_hash(en[k])}
        if keep != cat:
            _write(os.path.join(root, f"{lang}.json"), keep)
        sync[lang] = {k: v for k, v in stamps.items() if k in keep}
    _write(path, sync)


# --- Russian string literals (not comments) ------------------------------------------------------------------

_REGEX_BEFORE = set("(,=:[!&|?{};+-*%<>~^")


def js_literal_lines(text):
    """Line numbers of JS where Russian letters sit in a string, a template or a regex rather than in a comment."""
    lines, i, n, line = set(), 0, len(text), 1
    stack = []   # open templates: the depth of curly braces inside ${…}
    last = ""    # the last significant character outside strings — a regex or a division

    def mark(a, b, start_line):
        seg = text[a:b]
        for k, part in enumerate(seg.split("\n")):
            if CYRILLIC.search(part):
                lines.add(start_line + k)

    def template(i, line):
        """A template from the position after `; return the position after the closing ` and the line number."""
        a = i
        while i < n:
            c = text[i]
            if c == "\\":
                i += 2
                continue
            if c == "`":
                mark(a, i, line)
                return i + 1, line + text[a:i].count("\n")
            if text.startswith("${", i):
                mark(a, i, line)
                line += text[a:i].count("\n")
                i, line = code(i + 2, line, "}")
                a = i
                continue
            i += 1
        mark(a, n, line)
        return n, line + text[a:n].count("\n")

    def code(i, line, until=None):
        nonlocal last
        depth = 0
        while i < n:
            c = text[i]
            if c == "\n":
                line += 1
                i += 1
                continue
            if c in " \t\r":
                i += 1
                continue
            if text.startswith("//", i):
                j = text.find("\n", i)
                i = n if j < 0 else j
                continue
            if text.startswith("/*", i):
                j = text.find("*/", i)
                j = n if j < 0 else j + 2
                line += text[i:j].count("\n")
                i = j
                continue
            if c in "\"'":
                j = i + 1
                while j < n and text[j] != c and text[j] != "\n":
                    j += 2 if text[j] == "\\" else 1
                mark(i, j, line)
                i, last = j + 1, "a"
                continue
            if c == "`":
                i, line = template(i + 1, line)
                last = "a"
                continue
            if c == "/" and (last in _REGEX_BEFORE or last == "" or re.search(r"\b(return|typeof|case|of|in)$",
                                                                               text[max(0, i - 8):i].rstrip())):
                j, cls = i + 1, False
                while j < n and text[j] != "\n":
                    if text[j] == "\\":
                        j += 2
                        continue
                    if text[j] == "[":
                        cls = True
                    elif text[j] == "]":
                        cls = False
                    elif text[j] == "/" and not cls:
                        break
                    j += 1
                mark(i, j, line)
                i, last = j + 1, "a"
                continue
            if until and c == "{":
                depth += 1
            elif until and c == "}":
                if not depth:
                    return i + 1, line
                depth -= 1
            last = c if c in _REGEX_BEFORE or c in ")]" else "a"
            if c in ")]":
                last = "a"
            i += 1
        return n, line

    code(0, 1)
    return lines


def py_literal_lines(text):
    """Line numbers of Python where Russian letters are in a string literal (not a comment, not a docstring)."""
    lines = set()
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return {k for k, x in enumerate(text.splitlines(), 1) if CYRILLIC.search(x.split("#", 1)[0])}
    kinds = {tokenize.STRING} | {getattr(tokenize, x) for x in ("FSTRING_MIDDLE",) if hasattr(tokenize, x)}
    skip = (tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE, tokenize.NL, tokenize.COMMENT)
    for k, tok in enumerate(toks):
        if tok.type not in kinds or not CYRILLIC.search(tok.string):
            continue
        prev = next((x for x in reversed(toks[:k]) if x.type not in (tokenize.NL, tokenize.COMMENT)), None)
        nxt = toks[k + 1] if k + 1 < len(toks) else None
        if tok.type == tokenize.STRING and (prev is None or prev.type in skip or
                                            (prev.type == tokenize.OP and prev.string == ":")) and \
                nxt is not None and nxt.type in (tokenize.NEWLINE, tokenize.ENDMARKER):
            continue   # a docstring and an expression string are not text for a human
        for no in range(tok.start[0], tok.end[0] + 1):
            if CYRILLIC.search(text.splitlines()[no - 1]):
                lines.add(no)
    return lines


def literal_lines(path, text):
    if path.endswith((".js", ".mjs")):
        found = js_literal_lines(text)
    elif path.endswith(".py") or path == "bin/hub":
        found = py_literal_lines(text)
    elif path.endswith(".html"):
        # what stays Russian when the server serves the page in English (it substitutes data-i18n itself)
        shown = re.sub(r"<!--.*?-->", lambda m: re.sub(r"[^\n]", " ", m.group(0)), i18n.render_html(text, i18n.SOURCE),
                       flags=re.S)
        found = {k for k, x in enumerate(shown.splitlines(), 1) if CYRILLIC.search(x)}
    else:
        return set()
    rows = text.splitlines()
    return {k for k in found if k <= len(rows) and MARK not in rows[k - 1]}


def public_files(root=None):
    root = root or REPO
    out = subprocess.run(["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                         capture_output=True, text=True, check=True).stdout
    for path in sorted(set(p for p in out.split("\0") if p)):
        if os.path.isfile(os.path.join(root, path)) and "/vendor/" not in path:
            yield path


def scan(root=None):
    """{path: lines with Russian literals} over the human-facing texts — only the files that have them."""
    root = root or REPO
    found = {}
    for path in public_files(root):
        if not SCOPE.search(path):
            continue
        try:
            with open(os.path.join(root, path), encoding="utf-8") as f:
                text = f.read()
        except (UnicodeDecodeError, OSError):
            continue
        got = len(literal_lines(path, text))
        if got:
            found[path] = got
    return found


def ratchet(root=None):
    """Files with more Russian literals than i18n/pending.json records, and files recorded there that have become clean."""
    root = root or REPO
    pending = _read(os.path.join(root, "i18n", PENDING), {})
    now = scan(root)
    out = [f"{path}: {got} lines with Russian literals, {pending.get(path, 0)} expected — the text goes through t()"
           for path, got in sorted(now.items()) if got > pending.get(path, 0)]
    out += [f"{path}: {now.get(path, 0)} lines now, {want} listed — lower it in i18n/{PENDING} (tools/i18n.py scan --write)"
            for path, want in sorted(pending.items()) if now.get(path, 0) < want]
    return out


_KEY = r"[a-z0-9_]+(?:\.[a-z0-9_]+)+"
# areas whose two-part keys are certainly catalog strings (for others "a.b" may also be an event type: core.started)
OWN = ("web.", "files.", "agent_file.")
NOT_KEYS = {"files.roots", "web.upload_max_mb"}   # settings paths in hub.json, not catalog strings
# a key is a whole string literal of the form "area.thing" (t("…"), tr(lang, "…"), self.tx("…"), data-i18n="…"):
# the area comes from the English catalog, so "a.b" in another sense (a file name, a field) is not counted as a key
USES = re.compile(rf"""["'`]({_KEY})["'`]""")


def used_keys(root=None):
    """Keys the code asks for: string-literal keys of the areas the catalog knows (t(), tr(), tx(),
    data-i18n…)."""
    root = root or REPO
    areas = {k.split(".")[0] for k in (_catalog(CATALOGS, i18n.SOURCE) or {})}
    found = set()
    for path in public_files(root):
        if embed.core_path(path).startswith(("tests/", "tools/")) or not path.endswith((".py", ".js", ".mjs", ".html")):
            continue
        try:
            with open(os.path.join(root, path), encoding="utf-8") as f:
                text = f.read()
        except (UnicodeDecodeError, OSError):
            continue
        found |= {k for k in (m.group(1) for m in USES.finditer(text)) if k.split(".")[0] in areas and
                  (k.startswith(OWN) or k.count(".") >= 2) and k not in NOT_KEYS}
    return found


def main(argv):
    cmd = argv[0] if argv else "check"
    if cmd == "check":
        release = "--release" in argv[1:]
        bad = problems(release=release) + ratchet() + embedded(release=release)
        print("\n".join(bad) if bad else "translations: OK")
        return 1 if bad else 0
    if cmd == "status" and len(argv) == 2:
        print(json.dumps(dict(status(argv[1])), ensure_ascii=False, indent=1))
        return 0
    if cmd == "stamp" and len(argv) >= 2:
        stamp(argv[1], argv[2:] or None)
        return 0
    if cmd == "drop":
        drop_stale()
        return 0
    if cmd == "scan":
        found = scan()
        if "--write" in argv:
            _write(os.path.join(CATALOGS, PENDING), found)
        for path, got in sorted(found.items(), key=lambda x: -x[1]):
            print(f"{got:6} {path}")
        print(f"{sum(found.values()):6} total")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
