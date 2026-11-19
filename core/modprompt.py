"""Module API points into session prompts (Module API spec, §2.4, S7). Called by the stream-json host (hosts/streamjson.py):

- prompt.parts — on every launch of a session process: the core's system addendum as a list of parts [{name, text}]
  (names — hosts/streamjson.command; for an agent the first part is agent_file — its CLAUDE.md); a module returns its
  own list: append, replace, remove. Modules go in turn in load order, each gets the result of the previous; a module
  failure — its step is skipped (the list stays as it was before it);
- prompt.input — the text the host writes to the session (a message with core markers); a module returns its own.
  The core tracks delivery by its own accounting (message number), not by markers in the text.
What modules changed, the host writes as a mod.prompt fact (entity — the session): {parts: {module: {added, replaced,
removed, moved}}, input: []} on every successful process launch and {input: [modules]} — from the first changed
message of the process; the fold keeps it in sessions[name].mod_prompt, `hub show <session>` shows it. A hub that has
never had modules (no modrun folder) never writes the fact.
"""
from core.tools import Rejected

PARTS, INPUT = "prompt.parts", "prompt.input"


def diff(before, after):
    """What changed between two lists of parts: {added, replaced, removed, moved} (non-empty ones) or {}; moved — parts
    that kept the same text but stand in a different order."""
    a, b = {p["name"]: p["text"] for p in before}, {p["name"]: p["text"] for p in after}
    out = {"added": [n for n in b if n not in a], "replaced": [n for n in b if n in a and a[n] != b[n]],
           "removed": [n for n in a if n not in b], "moved": _moved(list(a), [n for n in b if n in a])}
    return {k: v for k, v in out.items() if v}


def _moved(old, kept):
    """Parts that changed place: everything outside the longest chain that kept the former order (there are dozens of parts)."""
    pos = [old.index(n) for n in kept]
    best = [[i] for i in range(len(pos))]
    for i in range(len(pos)):
        for j in range(i):
            if pos[j] < pos[i] and len(best[j]) + 1 > len(best[i]):
                best[i] = best[j] + [i]
    stay = set(max(best, key=len, default=[]))
    return [n for i, n in enumerate(kept) if i not in stay]


def parts(mods, names, session, parts, **ctx):
    """The prompt.parts chain over modules `names`. -> (new list, or None — nothing changed overall, {module: diff})."""
    cur, changed = [dict(p) for p in parts], {}
    for name in names:
        got = mods.call(name, PARTS, {"session": session, **ctx, "parts": cur})
        if got is None:
            continue   # failure (module.error recorded) — the module's step is skipped
        d = diff(cur, got)
        if d:
            changed[name] = d
        cur = got
    if [(p["name"], p["text"]) for p in cur] == [(p["name"], p["text"]) for p in parts]:
        return None, {}   # nothing changed overall (one appended, another removed) — the core's addendum, no record
    return cur, changed


def text(mods, session, text, message=None):
    """The prompt.input chain. -> (text, [modules that changed it])."""
    names = mods.answering(INPUT) if mods is not None else []
    changed = []
    for name in names:
        got = mods.call(name, INPUT, {"session": session, "text": text, "message": message})
        if got is not None and got != text:
            changed.append(name)
            text = got
    return text, changed


def fact(mods, session, data):
    mods.emit("mod.prompt", session, {**data, "channel": "core", "owner": False})


# --- fold and display -----------------------------------------------------------------------------------------------

def apply(st, ev, data):
    """mod.prompt: what modules changed in the session's addendum (parts) and input; empty — the record is removed."""
    if data.get("channel") != "core":
        raise Rejected("the core writes what modules changed in the prompt")
    sess = st["sessions"].get(ev.get("entity"))
    if sess is None:
        raise Rejected("mod.prompt: no such session")
    rec = dict(sess.get("mod_prompt") or {})
    if isinstance(data.get("parts"), dict):
        got = {str(m)[:64]: {k: [str(n)[:64] for n in d[k]][:64] for k in WORDS if isinstance(d.get(k), list) and d[k]}
               for m, d in list(data["parts"].items())[:16] if isinstance(d, dict)}
        rec["parts"] = {m: d for m, d in got.items() if d}
    if isinstance(data.get("input"), list):
        rec["input"] = [str(m)[:64] for m in data["input"]][:16]
    rec = {k: v for k, v in rec.items() if v}
    if rec:
        sess["mod_prompt"] = rec
    else:
        sess.pop("mod_prompt", None)


WORDS = {"added": "core.modprompt.added", "replaced": "core.modprompt.replaced", "removed": "core.modprompt.removed",
         "moved": "core.modprompt.moved"}   # what a module did to a part -> catalog key


def lines(sess):
    """Lines for `hub show` (the reader's language: a session's process reads English, the owner's terminal his
    language): which module changed what in the session prompt."""
    from core import i18n
    rec = (sess or {}).get("mod_prompt") or {}
    out = ["  " + i18n.t("core.modprompt.parts", module=m, what="; ".join(
        f"{i18n.t(WORDS[k])} {', '.join(d[k])}" for k in WORDS if d.get(k))) for m, d in (rec.get("parts") or {}).items()]
    out += ["  " + i18n.t("core.modprompt.input", module=m) for m in rec.get("input") or []]
    return out
