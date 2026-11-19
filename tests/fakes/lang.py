"""Russian and Ukrainian text for the tests of what the core recognises, taken from the language data (core/langdata.py,
i18n/data/<lang>.json): a test keeps checking a Russian-speaking owner, a session's Russian result or a label of the
earlier Russian texts, while its own source holds no Cyrillic. The data is the same the code reads, so a test proves the
rule works on the words it is built from; that the words themselves do not change is tests/test_langdata.py.

    word("watch", "promise_verbs")       the first plain alternative of a regex fragment ("take"-like verb)
    words("watch", "doubt")              every plain alternative (letters and spaces only), in order
    legacy("origin")["owner"]            an earlier form of the core's text
    LETTERS["ru"]                        the alphabet of a language (from its transliteration), own letters added
    YO, YE                               the letter pair the search folds into one (letters.fold)
"""
import re

from core import langdata

legacy = langdata.legacy


def data(section, key, lang="ru"):
    return langdata.get(lang, section, key)


def _top(fragment):
    """The top-level alternatives of a regex fragment: split on "|" outside groups and classes."""
    out, cur, depth, cls, esc = [], "", 0, False, False
    for c in fragment:
        if esc:
            cur, esc = cur + c, False
            continue
        if c == "\\":
            cur, esc = cur + c, True
            continue
        if cls:
            cls = c != "]"
        elif c == "[":
            cls = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "|" and depth == 0:
            out.append(cur)
            cur = ""
            continue
        cur += c
    return out + [cur]


def words(section, key, lang="ru"):
    """The plain alternatives of a fragment (or the items of a list): letters and single spaces only."""
    v = data(section, key, lang)
    items = v if isinstance(v, list) else _top(v or "")
    return [x for x in items if isinstance(x, str) and re.fullmatch(r"[^\W\d_]+(?: [^\W\d_]+)*", x)]


def word(section, key, i=0, lang="ru"):
    return words(section, key, lang)[i]


def _alphabet(lang):
    letters = "".join(sorted(data("translit", "latin", lang) or {}))
    return letters + "".join(c for c in (data("letters", "own", lang) or "") if c not in letters)


LETTERS = {"ru": _alphabet("ru")}
# Ukrainian: the letters both languages share and its own, without the Russian-only ones
LETTERS["uk"] = "".join(c for c in LETTERS["ru"] if c not in data("letters", "own")) + data("letters", "own", "uk")
(YO, YE), = langdata.words("letters", "fold")


def origin(kind):
    """The label of the earlier texts after the message id: " <from> <who>" (core/forms.ORIGIN_*)."""
    return legacy("origin")[kind]


def head(mid, who=None, kind=None):
    """The label at the start of a turn as the earlier core wrote it: [hub:mN <from> <who>] (a kind's word or a name)."""
    tail = origin(kind) if kind else f" {legacy('from')} {who}" if who else ""
    return f"[hub:{mid}{tail}]"
