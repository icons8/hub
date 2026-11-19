"""Language data: the words the core recognises in a language and the earlier forms of its own texts, kept out of the
code in `i18n/data/<language>.json` (one file per language; the interface catalogs `i18n/<language>.json` are a
different thing — what the core says, not what it reads).

A file is JSON {section: {key: value}}:
- `words` — the owner's and the sessions' words the core acts on: "yes", the hub's name, a "Lessons" heading, generic
  form labels, "no data";
- `watch` — the phrases the watcher's rules look for (regex fragments);
- `translit` — letters to Latin for executor names; `letters` — what tells the language apart by its letters;
- `self_name` — the language's name in itself;
- `secrets` — `labels`: the words that name a key or a password in a text ("password: …" in the language), plain lowercase
  phrases, not regexes (core/redact.py masks the value after one);
- `comms` — what tools/comms_stats.py asks the journal for (follow-up phrases, regexes) and `runner` — the line forms the
  modules repository's test runner printed before its output was English (tools/private_check.py): both tools read the
  files with the stdlib alone, they run without the core;
- `legacy` — the forms the core wrote before its texts were English: labels, headers, reasons, identifiers. Journals,
  state.json and old feeds of every hub hold them, so the core reads them forever; only the language the core spoke
  then (OLD) has this section.

What the core recognises is the union over all languages: a hub of any language understands an owner who writes Russian
or Ukrainian, as before. A regex fragment is a whole alternative (`a|b`), stored as the regex text. The files ship with
the code: a missing or broken one is a broken install and fails loudly at import, and so does a missing `legacy` key (the
core reads and writes those forms). A missing key of another section reads as nothing — a rule built from nothing
matches nothing (NEVER), it does not match everything.
stdlib only, no imports of the hub: old processes import this file fresh from the disk during an update's self-check."""
import copy
import json
import os

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "i18n", "data")
LANGS = ("ru", "uk", "de", "es", "fr", "pt-BR", "tr", "ar", "hi", "ja", "ko", "zh-CN")   # the languages with data, in the
# order their values join
OLD = "ru"             # the language of the core's texts before the English ones: the `legacy` section
NEVER = "(?!)"         # a regex that matches nothing: the rule of an empty alternation

_cache = {}
_folds = {}   # (language or None for all, the loaded data) -> fold pairs


def load(lang):
    """The data of one language (cached); no file or a broken one — an error (OSError, ValueError)."""
    if lang not in _cache:
        with open(os.path.join(ROOT, f"{lang}.json"), encoding="utf-8") as f:
            got = json.load(f)
        if not isinstance(got, dict):
            raise ValueError(f"{lang}.json: not an object")
        _cache[lang] = got
    return _cache[lang]


def get(lang, section, key, default=None):
    """One value of one language: data[section][key] (a copy: a caller that changes it changes nothing for the others),
    or default."""
    part = load(lang).get(section)
    return copy.deepcopy(part[key]) if isinstance(part, dict) and key in part else default


def legacy(key):
    """An earlier form of the core's own text (the `legacy` section of OLD, a copy); no such key — KeyError."""
    return copy.deepcopy(load(OLD)["legacy"][key])


def words(section, key):
    """The values of every language joined into one list, in LANGS order, without repeats; a string value is one item."""
    out = []
    for lang in LANGS:
        v = get(lang, section, key)
        for x in [v] if isinstance(v, str) else v if isinstance(v, list) else []:
            if x not in out:
                out.append(x)
    return out


def alt(section, key):
    """A regex alternation of every language's fragments; none — NEVER."""
    return "|".join(words(section, key)) or NEVER


def fold(text, lang=None):
    """Text with the letters treated as one written as one (`letters.fold` pairs): of every language with data, or of
    lang alone. The pairs are read once (the feed search calls this per string)."""
    key = (lang, tuple(id(_cache.get(x)) for x in LANGS))   # other data loaded (a test's) — other pairs
    if key not in _folds:
        _folds[key] = [tuple(p) for p in (words("letters", "fold") if lang is None else get(lang, "letters", "fold", []))]
    for a, b in _folds[key]:
        text = text.replace(a, b)
    return text


def merged(section, key):
    """The dict values of every language merged into one (an earlier language wins on a repeated key)."""
    out = {}
    for lang in LANGS:
        v = get(lang, section, key)
        for k, x in (v.items() if isinstance(v, dict) else ()):
            out.setdefault(k, x)
    return out
