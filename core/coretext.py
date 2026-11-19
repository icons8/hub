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


def T(key, english, **fixed):
    """The core's phrase key in English (a text, or plural forms {"one", "other"} chosen by the count argument). fixed —
    substitutions every use has: a word the core recognizes in a session's answer whatever the language (the "nothing
    to continue" one, delegations.NOTHING) stays out of the catalog text. Substitutions are plain {name}, as the
    catalogs have them: a format spec ({hours:.1f}) is refused at once — the catalog could not render it."""
    for text in (english.values() if isinstance(english, dict) else [english]):
        for _, name, spec, conv in string.Formatter().parse(text):
            if name is not None and (spec or conv or not name.isidentifier()):
                raise ValueError(f"{key}: {{{name}}} — a catalog substitution is a plain {{name}}")
    TEMPLATES[key] = english
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


def _render(text, spans, lang):
    from core import i18n
    out, at = [], 0
    for start, n, key, args in sorted(spans, key=lambda s: s[0]):
        if start < at or text[start:start + n] != i18n.tr(i18n.SOURCE, key, **_args(args, i18n.SOURCE)):
            continue   # overlapping, or the text was changed since: this span stays as it is
        out += [text[at:start], i18n.tr(lang, key, **_args(args, lang))]
        at = start + n
    return "".join(out) + text[at:]


def _args(args, lang):
    from core import i18n
    return {k: (v["text"] if lang == i18n.SOURCE else _render(v["text"], v["l10n"], lang)) if isinstance(v, dict) else v
            for k, v in args.items()}


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
    """The message text in the language lang if it differs from what the session got; otherwise (no marks, English,
    marks of another shape) None."""
    spans, text = (msg or {}).get("l10n"), (msg or {}).get("text")
    if not spans or not isinstance(text, str):   # almost every message: no lookup of the language
        return None
    from core import i18n
    lang = i18n.known(lang)
    if lang in (None, i18n.SOURCE):
        return None
    try:
        out = _render(text, spans, lang)
    except (TypeError, ValueError, KeyError, AttributeError, IndexError):   # marks not of this shape: the text as is
        return None
    return out if out != text else None
