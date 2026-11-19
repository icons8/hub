"""The core's cards of the hub bus for the owner (spec hubbus §5.6, §5.5, §5.7): ready forms in the shape of a session's
AskUserQuestion — the page and Telegram show them as any card, the answer comes back to the core
(core/busstate.card_answered), not to a session. Built by the daemon in the owner's language; the fold stores what it
gets. Each card says what it is about without a follow-up question: whose words, to whom, what the relay will see.

- no card for an outgoing message or a doubtful incoming one any more (owner 02.11): the liaison decides and asks by a
  form of its own when the owner's word is needed.
- offer: a session brought the invitation of another hub it was given — the label, the code of that hub, the term and
  who brought it, the relay it would give a hub without one; «Link» is the join of a pasted invitation, «No» does nothing.
- introduction: a hub that asks to be a contact — its label, fingerprint, check code, the sessions it shows; «The code
  is checked — accept as <name>», «Not now», «Block».
"""
import time
import unicodedata

from core import busstate, i18n


def _short(fp):
    return f"{fp[:10]}…" if isinstance(fp, str) else "—"


def _visible(text, limit=3000):
    """Hidden and control characters as <U+XXXX> — the owner sees what the check found."""
    out = []
    for c in str(text)[:limit]:
        if c not in "\n\t" and unicodedata.category(c) in ("Cc", "Cf", "Co", "Cs", "Cn"):
            out.append(f"<U+{ord(c):04X}>")
        else:
            out.append(c)
    return "".join(out) + ("…" if len(str(text)) > limit else "")


def _form(lang, header, question, options):
    labels = {key: i18n.tr(lang, label_key, **params) for key, label_key, _, params in options}
    return {"input": {"questions": [{"question": question, "header": header, "multiSelect": False,
                                     "options": [{"label": labels[key], "description": i18n.tr(lang, desc_key, **params) if desc_key else ""}
                                                 for key, _, desc_key, params in options]}]},
            "labels": labels}


def intro(lang, info, name):
    t = lambda key, **p: i18n.tr(lang, key, **p)  # noqa: E731
    agents = ", ".join(sorted((info.get("agents") or {}).keys())) or "—"
    lines = [t("core.bus.card.intro_title", label=info.get("label") or "—"),
             t("core.bus.card.intro_fp", fp=info.get("fp")),
             t("core.bus.card.intro_code", code=info.get("code")),
             t("core.bus.card.intro_agents", agents=agents),
             t("core.bus.card.intro_suggest", names=", ".join(info["suggest"])) if info.get("suggest") else "",
             "", t("core.bus.card.intro_check")]
    params = {"name": name}
    return _form(lang, t("core.bus.card.header"), "\n".join(x for x in lines if x is not None),
                 [("accept", "core.bus.card.accept", "core.bus.card.accept_why", params),
                  ("later", "core.bus.card.later", "core.bus.card.later_why", params),
                  ("block", "core.bus.card.block", "core.bus.card.block_why", params)])


def offer(lang, info):
    """The card for an invitation a session brought: whose hub, who brought it, what «Link» does (the check of the pair
    code is a separate press afterwards), the term, and the relay the hub gets from it when it has none."""
    t = lambda key, **p: i18n.tr(lang, key, **p)  # noqa: E731
    until = time.strftime("%Y-%m-%d %H:%M", time.localtime(info["exp"])) if isinstance(info.get("exp"), (int, float)) else "-"
    label = _visible(info.get("label") or "—", 100)
    lines = [t("core.bus.card.offer_title", by=_visible(info.get("by") or "—", 100), label=label),
             t("core.bus.card.offer_code", code=info.get("code") or "—", until=until),
             t("core.bus.card.offer_relay", relay=_visible(info["relay"], 200)) if info.get("relay") else None,
             "", t("core.bus.card.offer_what", label=label)]
    return _form(lang, t("core.bus.card.header"), "\n".join(x for x in lines if x is not None),
                 [("link", "core.bus.card.offer_link", "core.bus.card.offer_link_why", {"label": label}),
                  ("no", "core.bus.card.offer_no", "core.bus.card.offer_no_why", {})])
