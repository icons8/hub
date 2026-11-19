#!/usr/bin/env python3
"""The emoji names for the session icon search (modules/web/static/emoji/<lang>.json, one file per hub language):

    tools/emoji_names.py [--cache DIR]

The names and keywords are Unicode CLDR annotations (annotations and annotationsDerived, the cldr-json release CLDR_TAG;
Unicode License v3, the text is next to the files and in THIRD-PARTY-NOTICES.md); which emoji and in what order — the
fully-qualified lines of emoji-test.txt of EMOJI_VERSION, without the skin tone variants and the Component group
(a variant is the same picture as its base for a search, 5 times over). An emoji the core would not take as a session icon
(core/layout.py icon_ok: a keycap has a digit in it, a subdivision flag has tag characters) is not written either. A file
is {"<emoji>": "name|keyword|keyword"} in the order of emoji-test.txt; the first word is the name (CLDR "tts"), the case
is the language's own. An emoji that CLDR has no text for in a language is left out of its file: English has them all.
A word of the data that the publish check holds back (tools/publish.sh deny patterns of the main copy: a real word of a
language can coincide with a name or a place) is written with one letter as a \\uXXXX escape: the JSON means the same and the
file has no such piece of text. --cache — a folder where the downloaded sources are kept and read from (offline repeat)."""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core.layout import icon_ok  # noqa: E402

OUT = os.path.join(ROOT, "modules", "web", "static", "emoji")
CLDR_TAG = "49.0.0"
EMOJI_VERSION = "16.0"   # the newest the page's browsers draw by now; the newer ones are a square for many
CLDR = f"https://raw.githubusercontent.com/unicode-org/cldr-json/{CLDR_TAG}/cldr-json"
EMOJI_TEST = f"https://unicode.org/Public/emoji/{EMOJI_VERSION}/emoji-test.txt"
LANGS = {"en": "en", "ru": "ru", "zh-CN": "zh", "es": "es", "hi": "hi", "pt-BR": "pt", "fr": "fr", "de": "de", "ja": "ja",
         "ko": "ko", "ar": "ar", "tr": "tr", "uk": "uk"}   # the hub's 13: the page language → the CLDR locale
TONES = re.compile("[\U0001F3FB-\U0001F3FF]")
LINE = re.compile(r"^[0-9A-F ]+?\s*;\s*fully-qualified\s*#\s*(\S+)\s+E[\d.]+\s")


def fetch(url, cache):
    path = os.path.join(cache, re.sub(r"[^A-Za-z0-9.]+", "_", url)) if cache else None
    if path and os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    req = urllib.request.Request(url, headers={"User-Agent": "agents-hub-core/emoji_names"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    if path:
        os.makedirs(cache, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    return data


def emoji_list(cache):
    """The emoji of the page's list, in emoji-test.txt order."""
    out, group = [], None
    for line in fetch(EMOJI_TEST, cache).decode("utf-8").splitlines():
        if line.startswith("# group:"):
            group = line.split(":", 1)[1].strip()
            continue
        m = LINE.match(line)
        if m and group != "Component" and not TONES.search(m.group(1)) and icon_ok(m.group(1)):
            out.append(m.group(1))
    return out


def names(locale, cache):
    """{emoji without U+FE0F: "name|keyword|…"} from the annotations and the derived ones of the locale."""
    out = {}
    for pkg, key in (("cldr-annotations-derived-full", "annotationsDerived"), ("cldr-annotations-full", "annotations")):
        raw = fetch(f"{CLDR}/{pkg}/annotations{'Derived' if pkg.endswith('derived-full') else ''}/{locale}/"
                    f"annotations.json", cache)
        for emoji, a in json.loads(raw)[key]["annotations"].items():   # the base ones go last and win
            name = (a.get("tts") or [""])[0]
            words = [name] + [w for w in a.get("default", []) if w != name]
            out[emoji.replace("️", "")] = "|".join(w for w in words if w)
    return out


def denied():
    """The compiled deny patterns of the main copy (they are not in the repo: .gitignore), none when it has no such files."""
    try:
        common = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=ROOT, capture_output=True, text=True, check=True)
        main = os.path.dirname(os.path.realpath(os.path.join(ROOT, common.stdout.strip())))
    except (OSError, subprocess.CalledProcessError):
        return []
    out = []
    for name, flags in ((".publish-deny", re.I), (".publish-deny-case", 0)):
        try:
            with open(os.path.join(main, name), encoding="utf-8") as f:
                out += [re.compile(x, flags) for x in (y.rstrip("\n") for y in f) if x]
        except OSError:
            pass
    return out


def hidden(text, data):
    """The JSON text with one letter of every piece the deny patterns match written as an escape (from the end, the offsets
    stay); the result must read back as `data` and match nothing."""
    patterns = denied()
    done = set()
    for m in sorted((m for p in patterns for m in p.finditer(text)), key=lambda m: -m.start()):
        at = next((i for i in range(m.start(), m.end()) if text[i].isalpha() and ord(text[i]) < 0x10000), None)
        if at is None:
            raise SystemExit(f"a piece the publish check holds back, with no letter to write as an escape: {m.group()!r}")
        if at not in done:   # two patterns on one piece: one escape is enough
            done.add(at)
            text = text[:at] + "\\u%04x" % ord(text[at]) + text[at + 1:]
    if json.loads(text) != data or any(p.search(text) for p in patterns):
        raise SystemExit("the escapes did not hide the pieces the publish check holds back, or changed the data")
    return text


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache", help="a folder for the downloaded sources")
    args = ap.parse_args(argv)
    order = emoji_list(args.cache)
    os.makedirs(OUT, exist_ok=True)
    for lang, locale in LANGS.items():
        got = names(locale, args.cache)
        data = {e: got[e.replace("️", "")] for e in order if got.get(e.replace("️", ""))}
        if lang == "en" and len(data) != len(order):
            raise SystemExit(f"English lacks names for {len(order) - len(data)} emoji")
        path = os.path.join(OUT, lang + ".json")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(hidden(json.dumps(data, ensure_ascii=False, separators=(",", ":")), data) + "\n")
        print(f"{lang}: {len(data)} emoji, {os.path.getsize(path) // 1024} KB")


if __name__ == "__main__":
    main(sys.argv[1:])
