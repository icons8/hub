"""The core's words to sessions that the owner also reads (language spec, decision R4): the session gets English, the
owner — the same text in the hub's language.

A phrase is written in the code in English with its catalog key: `CUT_TURN = T("core.msg.cut_turn", "A hub restart …")`.
It is a str — the session gets exactly that English; `T(...).format(name=…)`, `+` and `join(...)` keep the marks of
which spans of the result are which phrases with which substitutions. `new_message` puts the marks next to the text,
`l10n` [[start, length, key, args], …]; an argument is a string or a number, or {text, l10n} of a built text.
`shown(msg, lang)` gives the text for a human: every marked span that still reads as its English is replaced by the
catalog text of the language; a text edited since (a prefix, a rewrite) keeps that span in English instead of breaking.

The English stays in the code (the session's prompt is read in the code), i18n/en.json keeps the same text — a test
compares them (TEMPLATES); ru.json and the other catalogs keep the translations. The journal does not depend on the
language: the marks are made by the fold, the language is applied only where the text is shown.
"""

import string

TEMPLATES = {}   # key -> the English of the code (a text or plural forms): tests compare it with i18n/en.json
SHOWN = ".shown"   # the suffix of a phrase's variant the owner reads (T(shown=…)): no tag, no numbers for the session


class Text(str):
    """English text with marked spans (start, length, key, args) — the core's phrases in it."""
    spans = ()

    def __add__(self, other):
        if not isinstance(other, str):
            return NotImplemented
        return _text(str.__add__(str(self), str(other)), self.spans + _moved(getattr(other, "spans", ()), len(self)))

    def __radd__(self, other):
        if not isinstance(other, str):
            return NotImplemented
        return _text(str.__add__(str(other), str(self)), _moved(self.spans, len(other)))


class Phrase(Text):
    """A whole catalog phrase; with {substitutions} — a template: .format(**args) gives the text with the arguments kept."""

    def format(self, *args, **kw):
        if args:   # positional substitutions are not marks of a catalog text: the plain English
            return str.format(self._template, *args, **kw)
        kw = {**self._fixed, **kw}
        english = _english_of(self._template, kw).format(**kw)
        return _text(english, ((0, len(english), self.key, {k: _arg(v) for k, v in kw.items()}),))


def T(key, english, shown=None, **fixed):
    """The core's phrase key in English (a text, or plural forms {"one", "other"} chosen by the count argument). fixed —
    substitutions every use has: a word the core recognizes in a session's answer whatever the language (the "nothing
    to continue" one, delegations.NOTHING) stays out of the catalog text. Substitutions are plain {name}, as the
    catalogs have them: a format spec ({hours:.1f}) is refused at once — the catalog could not render it. shown — the
    same phrase as the owner reads it (the catalog key + SHOWN): a session needs the tag and the task and message
    numbers ("REPLIED d8: …"), the owner reads only the words; it may use fewer of the substitutions."""
    for text in (english.values() if isinstance(english, dict) else [english]):
        for _, name, spec, conv in string.Formatter().parse(text):
            if name is not None and (spec or conv or not name.isidentifier()):
                raise ValueError(f"{key}: {{{name}}} — a catalog substitution is a plain {{name}}")
    TEMPLATES[key] = english
    if shown is not None:
        TEMPLATES[key + SHOWN] = shown
    text = english if isinstance(english, str) else english["other"]
    p = Phrase(text.format_map(_Kept(fixed)) if fixed else text)   # the other {names} stay for .format()
    p.key, p._template, p._fixed = key, english, fixed
    p.spans = ((0, len(p), key, dict(fixed)),)
    return p


class _Kept(dict):
    """The fixed substitutions of a phrase; a name it lacks stays a {name} of the template."""
    def __missing__(self, name):
        return "{" + name + "}"


def catalog(key, **args):
    """A catalog text as a phrase of the core: the English text of the key, marked — the owner reads it in his language
    (for texts assembled from catalog keys, not written in the code)."""
    from core import i18n
    english = i18n.tr(i18n.SOURCE, key, **args)
    return _text(english, ((0, len(english), key, {k: _arg(v) for k, v in args.items()}),))


def join(*pieces):
    """The pieces one after another, the marks of each kept (an f-string loses them)."""
    spans, at = [], 0
    for piece in pieces:
        spans += _moved(getattr(piece, "spans", ()), at)
        at += len(str(piece))
    text = "".join(str(piece) for piece in pieces)
    return _text(text, spans) if spans else text


def _text(text, spans):
    t = Text(text)
    t.spans = tuple(spans)
    return t


def _moved(spans, by):
    return tuple((at + by, n, key, args) for at, n, key, args in spans)


def _english_of(template, kw):
    if isinstance(template, str):
        return template
    from core import i18n
    return template.get(i18n.plural("en", kw.get("count", 0))) or template["other"]


def _arg(v):
    if isinstance(v, str) and getattr(v, "spans", ()):   # a Text, or a reason (forms.Why) that carries the marks
        return {"text": str(v), "l10n": marks(v)}
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)


def marks(text):
    """The marks of a text for the state: plain lists and dicts (no object of the text is shared with the state)."""
    return [[at, n, key, _plain(args)] for at, n, key, args in getattr(text, "spans", ())]


def _plain(value):
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def append(msg, tail):
    """Add tail to a message's text, with its marks (the message may have none of its own)."""
    at = len(msg["text"])
    msg["text"] = msg["text"] + str(tail)
    more = marks(tail)
    if more:
        msg["l10n"] = (msg.get("l10n") or []) + [[a + at, n, k, args] for a, n, k, args in more]


def said(msg):
    """The author's own words of a message: its text without what the core put around them — the head of the owner's
    answer to a card (answer_body, what the page shows him as his answer), the head and the tail of words the hub passed
    on for him (said, set where the core adds them), the frame of a task around the setter's words (brief: where they
    lie in the text, moved with a head). A message with none of them (nothing put around, or one of an earlier
    version): its text."""
    for key in ("said", "answer_body"):
        if isinstance(msg.get(key), str):
            return msg[key]
    span = brief_span(msg)
    return msg["text"][span[0]:span[1]] if span else msg.get("text")


def brief_span(msg):
    """Where the setter's words lie in a task message's text, [start, end) in characters (`brief`, moved by prepend);
    None when the core did not mark them or the text no longer holds them."""
    span, text = msg.get("brief"), msg.get("text")
    if not (isinstance(span, list) and len(span) == 2 and isinstance(text, str)) \
            or not all(isinstance(x, int) and not isinstance(x, bool) for x in span) or not 0 <= span[0] < span[1] <= len(text):
        return None
    return span


def prepend(msg, head):
    """Put head before a message's text: its marks move with the text (a mark that no longer fits shows English)."""
    msg["text"] = str(head) + str(msg["text"])
    if isinstance(msg.get("brief"), list):   # the task's words (core/delegations.brief_of) move with the text
        msg["brief"] = [x + len(str(head)) for x in msg["brief"] if isinstance(x, int)]
    if msg.get("l10n") or getattr(head, "spans", ()):
        moved = []
        for mark in msg.get("l10n") or []:
            try:
                a, n, k, args = mark
                moved.append([a + len(str(head)), n, k, args])
            except (TypeError, ValueError):   # a mark not of this shape: it would not show anyway
                continue
        msg["l10n"] = marks(head) + moved


def restore(text, saved):
    """A text with its marks kept as data (l10n of the state) — as a text the core builds on again; no marks — as is."""
    try:
        spans = tuple((a, n, k, args) for a, n, k, args in saved or ())
    except (TypeError, ValueError):
        spans = ()
    return _text(text, spans) if spans and isinstance(text, str) else text


def put(obj, field, text, limit=None):
    """Keep a text the core made in a record (an event's data, a state entry) that a human reads later: obj[field] — the
    plain English (as before; whoever compares or decides reads it), obj[field + "_l10n"] — the marks of its phrases
    (none — no such key). A list of texts — a list of plain texts and a list of their marks. limit cuts the text (a mark
    past the cut shows English). None — obj[field] = None. Returns obj. An earlier core ignores the extra key."""
    many = isinstance(text, (list, tuple))
    items = list(text) if many else [text]
    plain = [None if t is None else str(t)[:limit] for t in items]
    obj[field] = plain if many else plain[0]
    kept = [marks(t) for t in items]
    if any(kept):
        obj[field + "_l10n"] = kept if many else kept[0]
    else:
        obj.pop(field + "_l10n", None)
    return obj


def got(obj, field):
    """The text put() kept, with its marks again (human() shows it in the reader's language); a record without marks
    (an earlier core's) — the text as it is."""
    obj = obj if isinstance(obj, dict) else {}
    text, saved = obj.get(field), obj.get(field + "_l10n")
    if isinstance(text, list):
        saved = saved if isinstance(saved, list) and len(saved) == len(text) else [None] * len(text)
        return [restore(t, s) for t, s in zip(text, saved)]
    return restore(text, saved)


def drop(obj, field):
    """Remove a text put() kept, with its marks."""
    obj.pop(field, None)
    obj.pop(field + "_l10n", None)


def _render(text, spans, lang, owner=False):
    """The marked phrases of text in lang; owner — as the owner reads them: a phrase with a SHOWN variant in the catalog
    (T(shown=…)) is rendered by that variant, the others as they are."""
    from core import i18n
    out, at = [], 0
    for start, n, key, args in sorted(spans, key=lambda s: s[0]):
        if start < at or text[start:start + n] != i18n.tr(i18n.SOURCE, key, **_args(args, i18n.SOURCE)):
            continue   # overlapping, or the text was changed since: this span stays as it is
        shown = key + SHOWN if owner and key + SHOWN in i18n.catalog(i18n.SOURCE) else key
        # a phrase inside the phrase (the target of REPLIED) takes its own variant only inside a variant: the words of
        # the plain phrase expect the plain target ("de la session b", not "de à la session b")
        out += [text[at:start], i18n.tr(lang, shown, **_args(args, lang, owner and shown != key))]
        at = start + n
    return "".join(out) + text[at:]


def _args(args, lang, owner=False):
    from core import i18n
    return {k: (v["text"] if lang == i18n.SOURCE and not owner else _render(v["text"], v["l10n"], lang, owner))
            if isinstance(v, dict) else v for k, v in args.items()}


def why_of(data, default=""):
    """The reason of a core.rejected as the core gave it: the text (default when there is none), its code and the marks
    of the core's phrases in it (forms.Why; a human reads it through human())."""
    from core import forms
    return forms.Why(restore(data.get("reason") or default, data.get("l10n")), data.get("code"))


def human(text, lang=None):
    """A text the core made (a refusal reason, a message) as a human reads it: its phrases in lang — the process
    language by default (core/i18n.py current: a session's process reads English, the owner's terminal his language);
    a text without marks as it is. A reason keeps its code (forms.Why)."""
    if not getattr(text, "spans", ()):
        return text
    from core import i18n
    lang = i18n.known(i18n.current() if lang is None else lang)
    if lang in (None, i18n.SOURCE):
        return text
    try:
        out = _render(str(text), text.spans, lang)
    except (TypeError, ValueError, KeyError, AttributeError, IndexError):   # marks not of this shape: the text as is
        return text
    if getattr(text, "code", None) is not None:
        from core import forms
        return forms.Why(out, text.code)
    return out


def shown(msg, lang):
    """The message text as the owner reads it in the language lang (English by default) if it differs from what the
    session got: the phrases in his language, and those with a SHOWN variant — without the tag and the numbers of
    the session's form; otherwise (no marks, the same text, marks of another shape) None. The session's form is the
    text itself: a session's own `hub show` prints it (core/views.py)."""
    spans, text = (msg or {}).get("l10n"), (msg or {}).get("text")
    if not spans or not isinstance(text, str):   # almost every message: no lookup of the language
        return None
    from core import i18n
    lang = i18n.known(lang) or i18n.SOURCE
    try:
        out = _render(text, spans, lang, owner=True)
    except (TypeError, ValueError, KeyError, AttributeError, IndexError):   # marks not of this shape: the text as is
        return None
    return out if out != text else None
