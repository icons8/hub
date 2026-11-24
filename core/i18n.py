"""Texts for a human in their language: string catalogs `i18n/<language>.json`, the source is English (language spec, slice S1).

A catalog is flat JSON: key (`area.thing`) -> text or plural forms {"one": ..., "few": ..., "other": ...}; the form is
chosen by the `count` parameter with the language's CLDR rules. The `{name}` substitutions are the same in all languages;
`{{` and `}}` are literal braces. No key in the language - the English text, none in English either - the key itself (tests see it).

Languages: the required complete ones are ru and en (`stage.json` translated); the others are ready (`ready`) and show English
where there is no translation; a not-ready language (Arabic before right-to-left writing) from any path is treated as
having no catalog: English.
The pseudo-language `xx` (from hub.json only) is English wrapped in ⟦...⟧: every string that bypasses the catalog is visible in it.

A human's language is the `human()` chain: hub.json `language` -> the owner's language from his messages (not Latin) -> for a
hub whose history began before languages, Russian (`ui_lang_default`, set by the fold on core.started) -> the browser language
(page only) -> English. How to add a string or a language and the catalog checks - i18n/README.md, tools/i18n.py.
Model prompts (the footnote, rules, core messages to sessions) are not translated by catalogs - that is a different layer.
"""
import json
import os
import re
import time
import zlib

from core import embed, langdata

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "i18n")
SOURCE = "en"
PSEUDO = "xx"   # test language: English in ⟦...⟧
LANGS = ("en", "ru", "zh-CN", "es", "hi", "pt-BR", "fr", "de", "ja", "ko", "ar", "tr", "uk")
# the English name of a language - for model prompts ("write in {name}")
NAMES = {"en": "English", "ru": "Russian", "zh-CN": "Simplified Chinese", "es": "Spanish", "hi": "Hindi",
         "pt-BR": "Brazilian Portuguese", "fr": "French", "de": "German", "ja": "Japanese", "ko": "Korean",
         "ar": "Arabic", "tr": "Turkish", "uk": "Ukrainian"}
# a language's name in itself - for the language choice in settings; the Cyrillic ones are language data
# (core/langdata.py, `self_name`)
SELF = {"en": "English", "ru": langdata.get("ru", "self_name", "name"), "zh-CN": "简体中文", "es": "Español",  # i18n-ok: language names
        "hi": "हिन्दी", "pt-BR": "Português (Brasil)", "fr": "Français", "de": "Deutsch", "ja": "日本語", "ko": "한국어",
        "ar": "العربية", "tr": "Türkçe", "uk": langdata.get("uk", "self_name", "name")}  # i18n-ok: language names
RTL = {"ar", "fa", "he", "ur"}
FORMS = ("zero", "one", "two", "few", "many", "other")
PAGE = ("web.", "files.")   # keys needed by the page and by "Files" (/i18n.json)
STAGE = "stage.json"
CATALOG = re.compile(r"[a-z]{2,3}(-[A-Za-z0-9]+)?\.json")   # a language catalog file (pending, sync, stage are service files)
_PLACE = re.compile(r"\{\{|\}\}|\{(\w+)\}")
_cache = {}
_current = None
_retry_at = None   # state.json could not be read: the language is a fallback until this time (time.monotonic), then read again
RETRY = 1.0        # s: no more often - t() calls current() for every string, and state.json can be tens of MB


def _base(lang):
    return lang.split("-")[0].lower()


FRESH = 1.0   # catalog files are checked at most once per period this long: t() is called thousands of times per snapshot and fold


def _fresh(key, load):
    """The value from the cache within the current FRESH period; then load(previous) again (the previous one - so as not to
    reread an unchanged file). Periods are shared by all keys: a value built from other cached ones (the strings of tr(),
    _strings) is of the same generation as they are, and never older than what stamp() tags the page with."""
    now = time.monotonic() // FRESH
    hit = _cache.get(key)
    if hit and hit[0] == now:
        return hit[1]
    value = load(hit[1] if hit else None)
    _cache[key] = (now, value)
    return value


def _read(name, root=None):
    """JSON from the catalog folder (root, the core's by default), reread only when the file changes; broken or not an
    object - {}. A key read once stays in the process with its last text when it leaves the file (12.11: an update
    moved phrases into an embed's catalog, and a process still on the previous code, until its restart, showed their
    keys), and so does the whole file while a write leaves it broken."""
    root = root or ROOT
    path = os.path.join(root, name)

    def load(was):
        known = was[1] if was else {}
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            return (None, known)
        if was and was[0] == mtime:
            return was
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        return (mtime, {**known, **data} if isinstance(data, dict) else known)
    return _fresh((root, name), load)[1]


def available():
    """Languages that have a catalog here."""
    def load(_):
        try:
            names = os.listdir(ROOT)
        except OSError:
            return [SOURCE]
        return sorted(n[:-5] for n in names if CATALOG.fullmatch(n))
    return _fresh((ROOT, "/"), load)


def ready():
    """Languages the hub speaks: with a catalog and marked "ready" in stage.json (English is always)."""
    marked = _read(STAGE).get("ready")
    have = available()
    return [x for x in have if x == SOURCE or (marked is None or x in marked)]


def catalog(lang):
    """The strings of one language ({} - there are none); reread when the file changes."""
    if lang == PSEUDO:
        en = catalog(SOURCE)
        hit = _cache.get((ROOT, PSEUDO))
        if not hit or hit[0] is not en:   # the wrapping is redone only when English was reread
            hit = _cache[(ROOT, PSEUDO)] = (en, {k: _wrap(v) for k, v in en.items()})
        return hit[1]
    own = _read(f"{lang}.json")
    more = [_read(f"{lang}.json", root) for root in _embedded()]
    if not any(more):
        return own
    hit = _cache.get((ROOT, "+", lang))   # the merge is redone only when one of the catalogs was reread
    if not hit or len(hit[0]) != 1 + len(more) or any(a is not b for a, b in zip(hit[0], [own, *more])):
        merged = {}
        for got in more:
            merged.update(got)
        merged.update(own)   # the core's own key wins (core/embed.problems refuses an embed that repeats one)
        hit = _cache[(ROOT, "+", lang)] = ((own, *more), merged)
    return hit[1]


def _embedded():
    """The catalog folders of the embeds of this tree (core/embed.py: embed/<name>/i18n), listed once per FRESH."""
    def load(_):
        return [os.path.join(top, "i18n") for _, top in embed.roots(os.path.dirname(ROOT))]
    return _fresh((ROOT, "embed/"), load)


def _wrap(value):
    if isinstance(value, dict):
        return {k: f"⟦{v}⟧" for k, v in value.items()}
    return f"⟦{value}⟧"


def known(lang):
    """A language name as it was sent (any case) -> the spelling of a ready catalog; the pseudo-language - itself; else None."""
    if not isinstance(lang, str) or not lang:
        return None
    if lang.lower() == PSEUDO:
        return PSEUDO
    return next((x for x in ready() if x.lower() == lang.lower()), None)


def plural(lang, n):
    """The CLDR category of an integer in a language (rules for our own languages only)."""
    if isinstance(n, float) and not n.is_integer():
        return "other"
    try:
        n = abs(int(n))
    except (TypeError, ValueError):
        return "other"
    base = _base(lang)
    if base in ("zh", "ja", "ko"):
        return "other"
    if base in ("ru", "uk"):
        if n % 10 == 1 and n % 100 != 11:
            return "one"
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return "few"
        return "many"
    if base == "ar":
        if n in (0, 1, 2):
            return ("zero", "one", "two")[n]
        if 3 <= n % 100 <= 10:
            return "few"
        if 11 <= n % 100 <= 99:
            return "many"
        return "other"
    if base in ("fr", "pt", "hi"):
        if n in (0, 1):
            return "one"
    elif n == 1:
        return "one"
    if base in ("es", "fr", "pt") and n and n % 1000000 == 0:
        return "many"
    return "other"


def forms(lang):
    """The plural forms a language catalog must have, and those it may."""
    base = _base(lang)
    need = {"zh": (), "ja": (), "ko": (), "ru": ("one", "few", "many"), "uk": ("one", "few", "many"),
            "ar": ("zero", "one", "two", "few", "many")}.get(base, ("one",))
    may = ("many",) if base in ("es", "fr", "pt") else ()
    return {*need, "other"}, {*need, *may, "other"}


def placeholders(value):
    """The names of the {substitutions} of a text or of all its forms."""
    texts = value.values() if isinstance(value, dict) else [value]
    return {m.group(1) for t in texts if isinstance(t, str) for m in _PLACE.finditer(t) if m.group(1)}


def format(text, params):
    """Substitute {name} from params; a name without a value stays as is."""
    def one(m):
        if not m.group(1):
            return m.group(0)[0]
        return str(params[m.group(1)]) if m.group(1) in params else m.group(0)
    return _PLACE.sub(one, text)


def _strings(lang):
    """(the language a text is given in, its strings) - resolved once per FRESH period: a snapshot asks for thousands of
    texts in a row, and resolving the language and looking up the catalogs (cached, but a dozen calls each) for every
    one of them took half of its build."""
    def load(_):
        got = known(lang) or SOURCE
        return got, catalog(got)
    return _fresh((ROOT, "tr", lang), load) if isinstance(lang, str) or lang is None else load(None)


def tr(lang, key, **params):
    """The key's text in the language; no translation - English; none of that either - the key itself."""
    lang, strings = _strings(lang)
    value = strings.get(key)
    if not isinstance(value, (str, dict)):
        lang, value = SOURCE, catalog(SOURCE).get(key)
        if not isinstance(value, (str, dict)):
            return key
    if isinstance(value, dict):
        value = value.get(plural(SOURCE if lang == PSEUDO else lang, params.get("count", 0))) or value.get("other", key)
    return format(value if isinstance(value, str) else key, params)


def use(lang):
    """The language of this process - t() speaks it."""
    global _current, _retry_at
    _current = known(lang) or SOURCE
    _retry_at = None


def current():
    """The process language: set by use(); a session's process (HUB_SESSION) - English: the hub's output in its steps is
    read by the model; otherwise HUB_LANG (tests are pinned to en), otherwise the human() chain over hub.json
    and the state, once (the page server checks it itself, core/i18n.py follow)."""
    global _current, _retry_at
    if _retry_at is not None and time.monotonic() >= _retry_at:
        _current = None
    if _current is None and os.environ.get("HUB_SESSION"):
        use(SOURCE)
    if _current is None:
        use(SOURCE)   # reading the settings may itself report trouble via t() - in English
        lang, unread = _chain()
        use(lang)
        if unread:
            _retry_at = time.monotonic() + RETRY
    return _current


_owner = None
_owner_retry = None


def owner():
    """The owner's language whoever runs the process - for what is kept for him or shown to him (a fact in the
    journal, a caption on a picture) when a session runs the command; without HUB_SESSION - current()."""
    global _owner, _owner_retry
    if not os.environ.get("HUB_SESSION"):
        return current()
    if _owner_retry is not None and time.monotonic() >= _owner_retry:
        _owner = None
    if _owner is None:
        _owner, unread = _chain()
        _owner_retry = time.monotonic() + RETRY if unread else None
    return _owner


def _chain():
    """(language, unread): HUB_LANG, otherwise the human() chain over hub.json and the state; unread - the settings or
    the state could not be read now (being replaced): read again later."""
    if os.environ.get("HUB_LANG"):
        return known(os.environ["HUB_LANG"]) or SOURCE, False
    st, unread = None, False
    try:
        from core import catalog as settings, daemon
        state_dir = daemon.default_state_dir()
        cfg = settings.load_config(state_dir)
    except OSError:   # being replaced (Windows) - read again
        cfg, state_dir, unread = {}, None, True
    except ValueError:   # not JSON - not temporary
        cfg, state_dir = {}, None
    if state_dir:
        try:
            st = saved_state(state_dir, strict=True)
        except (OSError, ValueError, TypeError, AttributeError):
            unread = True   # could not be read (state.json being replaced, Windows) - not "language unset": read again
    return human(cfg, st), unread


def lang_facts(st):
    """From the state - only what language selection needs (the owner, the pre-languages hub mark): a small copy that is
    read without the journal lock."""
    if not st:
        return None
    o = st.get("owner") or {}
    # came — the language the owner's page said when they first came (reducer owner.came), for what the hub does for
    # them before they write (the greeting, the setup steps); met — the core no longer waits for them to come
    came = o.get("came") if isinstance(o.get("came"), dict) else {}
    return {"owner": {"lang": o.get("lang"), "came": came.get("lang"),
                      "met": bool(came or o.get("invited") or o.get("greeted") or o.get("seen"))},
            "ui_lang_default": st.get("ui_lang_default")}


def saved_state(state_dir, strict=False):
    """The language from the core state file (state.json, without the journal tail): the owner and the pre-languages hub
    mark; none - None. strict - a read failure (other than a missing file) is raised, not turned into None."""
    from core import journal
    try:
        return lang_facts((journal.read_state_file(state_dir) or {}).get("state") or {})
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, AttributeError):
        if strict:
            raise
        return None


def t(key, **params):
    """The key's text in this process's language."""
    return tr(current(), key, **params)


def name(lang):
    """The English name of a language for prompts: "pt-BR" -> "Brazilian Portuguese"; an unknown one - its code."""
    code = str(lang or SOURCE)
    hit = next((c for c in NAMES if c.lower() == code.lower()), None) or \
        next((c for c in NAMES if _base(c) == _base(code)), None)
    return NAMES[hit] if hit else code


def negotiate(header):
    """The best ready language for an Accept-Language header; none fits - None."""
    have = ready()
    wanted = []
    for i, part in enumerate((header or "").split(",")):
        tag, _, rest = part.strip().partition(";")
        q = 1.0
        m = re.search(r"q\s*=\s*([0-9.]+)", rest)
        if m:
            try:
                q = float(m.group(1))
            except ValueError:
                q = 0.0
        if tag and tag != "*" and q > 0 and tag.lower() != PSEUDO:
            wanted.append((-q, i, tag))
    for _, _, tag in sorted(wanted):
        exact = next((x for x in have if x.lower() == tag.lower()), None)
        if exact:
            return exact
        same = [x for x in LANGS if x in have and _base(x) == _base(tag)]
        if same:
            return same[0]
    return None


def owner_code(st):
    """The owner's language from the state, if known: one of the 13 codes (old words as codes); Latin and the old
    "English" (the pre-languages detector called any Latin text that) are not a language, None."""
    from core import forms
    lang = forms.lang_code(((st or {}).get("owner") or {}).get("lang"))
    if not isinstance(lang, str) or lang in ("latin", "en") or lang not in LANGS:
        return None
    return lang


def state_lang(st):
    """A human's language from the core state: the owner (not Latin) -> a pre-languages hub (ru); unknown - None."""
    return owner_code(st) or ((st or {}).get("ui_lang_default") or None)


def human(cfg, st, header=None, page=False, default=None):
    """A human's language (spec section 4.2): hub.json -> the owner -> a pre-languages hub (ru) -> the browser (page) -> English.
    The first one named decides: a not-ready language means English, not the next step. default - state_lang() computed
    in advance (the session host gets it in the spawn action, it has no state). HUB_LANG - the default language before the
    browser (tests are pinned to en)."""
    picked = ((cfg or {}).get("language") or None) or state_lang(st) or default or os.environ.get("HUB_LANG") or None
    if not picked and page:
        picked = negotiate(header)
    return known(picked) or SOURCE


def stamp(lang):
    """A version mark of a language's strings (a browser cache tag): the modification time of its catalog and of English, and the language itself."""
    def mtime(x, root):   # the same time the strings were read with (the _read cache): the tag does not outrun the body
        _read(f"{x}.json", root)
        return (_cache.get((root, f"{x}.json")) or (0, (None, {})))[1][0] or 0
    # an embed's catalogs too (core/embed.py): they are part of the language's strings; which embeds there are counts
    # as well — one that leaves, or comes with older files, changes no time
    roots = _embedded()
    which = f"-{zlib.crc32(chr(0).join(roots).encode()):x}" if roots else ""
    return f"{max(mtime(x, r) for x in {lang, SOURCE} if x != PSEUDO for r in [ROOT, *roots]):x}-{lang}{which}"


def direction(lang):
    return "rtl" if _base(lang) in RTL else "ltr"


def page_bundle(lang):
    """What the page scripts take as /i18n.json: the language, the direction, the page strings, and English ones - only
    those the language lacks (empty for Russian and English)."""
    mine = {k: v for k, v in catalog(lang).items() if k.startswith(PAGE)}
    fallback = {} if lang == SOURCE else \
        {k: v for k, v in catalog(SOURCE).items() if k.startswith(PAGE) and k not in mine}
    return {"lang": lang, "dir": direction(lang), "messages": mine, "fallback": fallback}


_STATIC_TEXT = re.compile(r'(<(\w+)\b[^>]*?\sdata-i18n="([\w.]+)"[^>]*>)(.*?)(</\2>)', re.S)
_TAG = re.compile(r"<\w+\b[^>]*\sdata-i18n-[\w-]+=\"[\w.]+\"[^>]*>")
_STATIC_ATTR = re.compile(r'\sdata-i18n-([\w-]+)="([\w.]+)"')


def render_html(html, lang):
    """The page's static text in its language, before the scripts: `data-i18n="key"` - the element's text (without nested
    tags), `data-i18n-<attribute>="key"` - an attribute; `<html lang dir>`. Without a flash of another language while modules load."""
    from html import escape

    def text(m):
        return m.group(1) + escape(tr(lang, m.group(3)), quote=False) + m.group(5)

    def tag(m):
        out = m.group(0)
        for attr, key in _STATIC_ATTR.findall(out):
            value = escape(tr(lang, key))
            have = re.compile(rf'(\s{re.escape(attr)}=")[^"]*(")')
            out = have.sub(lambda x: x.group(1) + value + x.group(2), out, count=1) if have.search(out) else \
                out[:-1].rstrip("/") + f' {attr}="{value}"' + (" />" if out.endswith("/>") else ">")
        return out

    out = _TAG.sub(tag, _STATIC_TEXT.sub(text, html))
    return re.sub(r"<html\b[^>]*>", f'<html lang="{lang}" dir="{direction(lang)}">', out, count=1)
