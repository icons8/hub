"""Layout of the session list (groups, who is where, order) and a session's own icon — docs/spec-session-groups.md.

The page sends the whole layout (layout.set) with the revision it computed it from (base): another tab's edit cannot
be silently overwritten. No st["layout"] — it was never touched, the view is the default; {"rev"} without groups —
"Reset" after edits. Here only validation and normalisation: what to show where is decided by the page."""
import re
import unicodedata

from core.coretext import T
from core.guards import HUB_OR_OWNER, from_owner_or_hub

# default groups: the same keys as hub.addOrder on the page; if the layout was never touched, these are visible. The
# title is the page's catalog key (modules/web/static/js/groups.js): default_groups(lang) gives the texts
DEFAULT_GROUPS = [{"id": "agents", "title": "web.sidebar.group_agents"},
                  {"id": "directions", "title": "web.sidebar.group_directions"},
                  {"id": "workers", "title": "web.sidebar.group_workers"}]
GROUP_ID = re.compile(r"[A-Za-z0-9_-]{1,40}")
# keys the page already uses itself (hub on top, "Asleep or crashed", "No group"), and JS object property names:
# a group with such an id would break the list
LOOSE = "none"   # the "outside groups" place is not a group: the id is taken, but allowed in place and order
RESERVED = {"top", "rest", "none", "__proto__", "constructor", "hasOwnProperty", "isPrototypeOf",
            "propertyIsEnumerable", "toLocaleString", "toString", "valueOf", "__defineGetter__", "__defineSetter__",
            "__lookupGetter__", "__lookupSetter__"}
TITLE_MAX, GROUPS_MAX, ORDER_MAX, ICON_MAX = 60, 50, 500, 16


class Bad(ValueError):
    """A wrong layout: str() gives the reason itself, with the marks of the core's phrases (core/coretext.py)."""
    def __str__(self):
        return self.args[0] if len(self.args) == 1 and isinstance(self.args[0], str) else super().__str__()


def default_groups(lang):
    """DEFAULT_GROUPS with their titles in the language: i18n.current() to print them, i18n.owner() when they go into the
    journal (the first `hub layout move` writes the whole layout: the page shows the stored titles)."""
    from core import i18n
    return [{"id": g["id"], "title": i18n.tr(lang, g["title"])} for g in DEFAULT_GROUPS]


def who_may(st, data):
    """The layout and icons are changed by the owner (page, terminal) and the hub — the refusal reason or None."""
    return HUB_OR_OWNER if from_owner_or_hub(st, data) else None


def icon_ok(icon):
    """A custom icon: an emoji or a sign, not text — up to 16 code points (a family emoji — up to 11), no spaces, control
    characters, Latin letters or digits."""
    if not isinstance(icon, str) or not icon or len(icon) > ICON_MAX:
        return False
    for ch in icon:
        if ch.isspace() or (ch.isascii() and ch.isalnum()) or (unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cn")
                                                                and ch not in "\u200d"):
            return False
    return True


def normalize(st, layout):
    """Layout from the event → state; a wrong shape — Bad. Foreign group ids and names the core does not know at all are
    dropped; deleted sessions stay (revive returns them to the same group)."""
    if not isinstance(layout, dict) or not isinstance(layout.get("groups"), list):
        raise Bad(T("core.layout.shape", "a layout is an object with groups: [{{id, title}}]").format())
    groups, seen = [], set()
    if len(layout["groups"]) > GROUPS_MAX:
        raise Bad(T("core.layout.too_many", "more than {max} groups").format(max=GROUPS_MAX))
    for g in layout["groups"]:
        if not isinstance(g, dict) or not isinstance(g.get("id"), str) or not GROUP_ID.fullmatch(g["id"]):
            raise Bad(T("core.layout.bad_id", "a group id is Latin letters, digits, hyphen, up to 40 characters"))
        if g["id"] in RESERVED:
            raise Bad(T("core.layout.reserved", "group id {id} is taken by the page — pick another").format(id=g["id"]))
        title = g.get("title").strip() if isinstance(g.get("title"), str) else ""
        if not title or len(title) > TITLE_MAX:
            raise Bad(T("core.layout.bad_title", "a group title is 1–{max} characters").format(max=TITLE_MAX))
        if g["id"] in seen:
            raise Bad(T("core.layout.twice", "group {id} twice").format(id=g["id"]))
        seen.add(g["id"])
        groups.append({"id": g["id"], "title": title})
    place, order = layout.get("place", {}), layout.get("order", {})
    if not isinstance(place, dict) or not isinstance(order, dict):
        raise Bad(T("core.layout.objects", "place and order are objects"))
    known = st["sessions"]
    seen = seen | {LOOSE}   # "outside groups": the session sits in the list on top, without a group (owner decision 2026-10-15)
    out_place = {n: g for n, g in place.items() if n in known and isinstance(g, str) and g in seen}
    out_order = {}
    for g, names in order.items():
        if not isinstance(names, list):
            raise Bad(T("core.layout.order", "order is {{group: [sessions]}}").format())
        if g not in seen:
            continue
        keep = list(dict.fromkeys(n for n in names if isinstance(n, str) and n in known))[:ORDER_MAX]
        if keep:
            out_order[g] = keep
    return {"groups": groups, "place": out_place, "order": out_order}


def rev(st):
    return (st.get("layout") or {}).get("rev", 0)


def set_layout(st, ev, data):
    """layout.set: the whole layout or None ("Reset") from base; the layout revision is the event seq, only grows."""
    why = who_may(st, data)
    if why:
        return why
    if data.get("base") != rev(st):
        return T("core.layout.stale", "the layout was changed already (another tab or the hub) — reload the page and try again")
    if data.get("layout") is None:
        st["layout"] = {"rev": ev["seq"]}
        return None
    try:
        st["layout"] = {"rev": ev["seq"], **normalize(st, data["layout"])}
    except Bad as e:
        return str(e)
    return None


def rename(st, old, new, seq):
    """A session was renamed: its place and order go under the new name; the revision grows (a tab with the old name is refused).
    The new name is already in the layout (left from a deleted session) — forget it first: place and order belong only to the renamed one."""
    lay = st.get("layout")
    if not lay or "groups" not in lay:
        return
    forget(st, new, seq)
    hit = old in lay["place"]
    if hit:
        lay["place"][new] = lay["place"].pop(old)
    for names in lay["order"].values():
        if old in names:
            hit = True
            names[names.index(old)] = new
    if hit:
        lay["rev"] = seq


def forget(st, name, seq):
    """The name starts clean: no place or order of a former session under it; if it was in the layout — the revision grows."""
    lay = st.get("layout")
    if not lay or "groups" not in lay:
        return
    hit = lay["place"].pop(name, None) is not None
    for g in list(lay["order"]):
        if name in lay["order"][g]:
            hit = True
            lay["order"][g] = [n for n in lay["order"][g] if n != name]
            if not lay["order"][g]:
                del lay["order"][g]
    if hit:
        lay["rev"] = seq


def keeps_place(st, sess):
    """Starting under the old name with a new conversation keeps the icon and place only for the hub and system agents
    (core/system.py): for the owner it is the same session. Any other is new, starting clean."""
    return sess.get("kind") == "hub" or sess["name"] in (st.get("system") or [])
