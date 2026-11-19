"""The owner's addressees: whom sessions write to in his name (the table exists so that addressees are not mixed up: a
session once found an addressee by searching the contacts and wrote to a namesake; the first send in Telegram is only
after a question to the owner).

One table for all sessions — `contacts.json` in the settings folder (mode 600, not in the repo; HUB_CONTACTS — for tests):
a person → Telegram (username + id), Teams (chat or UPN), mail; nicknames; the source and the date of confirmation. In
Telegram they write only by an id from the table. An address seen at work is written by the session itself (`hub contacts
add`, since 06.11): it adds what is missing and does not change a recorded address; a record is an address, not a
permission to write in the owner's name.

  hub contacts                         the whole table
  hub contacts find <name|@username|id> who this is (none — exit code 1 and a hint how to record)
  hub contacts add "<name>" [--telegram @username:id] [--teams-chat ID] [--teams UPN] [--email X]
                   [--alias A]… [--note "…"] --source "<where from: the owner, mN, a chat>"
  hub contacts rename "<name|alias|id>" "<new name>"   the owner only: the record stays (its id, the hubs linked to it)
  hub contacts remove "<name|alias|id>"                the owner only: a record a hub is linked to is not removed
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from core import catalog  # noqa: E402


def default_path():
    return os.environ.get("HUB_CONTACTS") or os.path.join(catalog.config_dir(), "contacts.json")


def load(path):
    """The book as it lies in the file (a list, or the {"people": […]} form); no file - an empty list."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def _rows(raw):
    """The list of entries inside the book of either form (the same list object: a change goes into the book)."""
    if isinstance(raw, dict) and isinstance(raw.get("people"), list):
        return raw["people"]
    return raw if isinstance(raw, list) else []


def people(path_or_book):
    """The one reader of the book (spec hubbus §5.5.1, p. 8) for find, the list and the page: both forms, entries that
    are not a dict or have no name are skipped, aliases that are not a list read as none; duplicate names are all kept.
    The entries are the book's own dicts with `aliases` read as a list."""
    raw = load(path_or_book) if isinstance(path_or_book, str) else path_or_book
    out = []
    for r in _rows(raw):
        if isinstance(r, dict) and isinstance(r.get("name"), str) and r["name"]:
            aliases = r.get("aliases")
            out.append(r if isinstance(aliases, list) and all(isinstance(a, str) for a in aliases) else
                       {**r, "aliases": [a for a in aliases if isinstance(a, str)] if isinstance(aliases, list) else []})
    return out


def _save(path, raw):
    """The whole book, 0600, through a temp file of its own and a replace, synced (catalog.write_settings)."""
    catalog.write_settings(path, raw, private=True)


def update(path, change):
    """Read the book, `change(rows)` the list of its entries in place, write it back — the whole cycle under one lock
    (core.lock.exclusive on `<book>.lock`): `add` and the owner's link to a bus contact (later slices) at the same time do
    not lose each other's change. The form of the file is kept; a change that changed nothing is not written. Returns
    what change returned."""
    from core import lock
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    with lock.exclusive(path + ".lock"):
        raw = load(path)
        if not isinstance(raw, (list, dict)):
            raw = []
        if isinstance(raw, dict) and not isinstance(raw.get("people"), list):
            raw["people"] = []
        was = json.dumps(raw, sort_keys=True, ensure_ascii=False)
        out = change(_rows(raw))
        if json.dumps(raw, sort_keys=True, ensure_ascii=False) != was or not os.path.exists(path):
            _save(path, raw)
    return out


def parse_telegram(spec):
    """«@username:id» or «username:id» -> {"username", "id"}. The id is required: Telegram confuses people by name."""
    name, _, tid = str(spec).lstrip("@").partition(":")
    if not name or not tid.strip().isdigit():
        from core import i18n
        raise ValueError(i18n.t("cli.contacts.telegram_format"))
    return {"username": name, "id": int(tid)}


def _norm(key, value):
    """An address in the form two addresses compare in: a Telegram id as text (a hand-edited book may keep it as a
    string), an e-mail or a Teams UPN without case, a Teams chat id as is; None — none."""
    if value in (None, ""):
        return None
    return str(value) if key in ("telegram", "teams_chat") else str(value).casefold()


def _address(row, key):
    if key == "telegram":
        tg = row.get("telegram") if isinstance(row.get("telegram"), dict) else {}
        return _norm(key, tg.get("id"))
    return _norm(key, row.get(key)) if isinstance(row.get(key), str) else None


def _shown(row, key):
    """An address as the book holds it, for a message."""
    if key == "telegram":
        tg = row.get("telegram") if isinstance(row.get("telegram"), dict) else {}
        return f"@{tg.get('username')} id {tg.get('id')}"
    return str(row.get(key))


def _names(row):
    al = row.get("aliases") if isinstance(row.get("aliases"), list) else []
    return {k.casefold() for k in [row.get("name"), *al] if isinstance(k, str) and k}


def _for_session(rows, name, aliases, given):
    """The record a session's entry goes to — the one with this name or alias (None: a new one) — or ValueError: the name
    fits two records, or an address it gives is already another record's. The core does not merge two people by a
    guess (a shared mailbox, a group chat): the session names the person the book already has."""
    from core import i18n
    names = {n.casefold() for n in [name, *aliases] if n}
    hits = [r for r in rows if _names(r) & names]
    if len(hits) > 1:
        raise ValueError(i18n.t("contacts.ambiguous", who=name, names=", ".join(r["name"] for r in hits)))
    row = hits[0] if hits else None
    for r in rows:
        if r is not row:
            same = [k for k, v in given.items() if _address(r, k) == v]
            if same:
                raise ValueError(i18n.t("contacts.taken", who=name, other=r["name"], what=", ".join(same)))
    if row is not None:
        clash = [k for k, v in given.items() if _address(row, k) not in (None, v)]
        if clash:
            raise ValueError(i18n.t("contacts.no_overwrite", who=row["name"],
                                    what=", ".join(f"{k} {_shown(row, k)}" for k in clash)))
    return row


def add(path, name, **kw):
    return add_new(path, name, **kw)[0]


def add_new(path, name, telegram=None, teams_chat=None, teams=None, email=None, aliases=(), note=None, source=None,
            by=None):
    """(record, whether anything changed). Add a person or what is new about them. by — the session that records it
    (None: the owner): it fills in only what the record lacks (each such address remembers it: `seen`), never replaces an
    address and never puts one on a second person (ValueError); its source joins the record's; a record it makes is
    marked with its name (`by`). The owner's add goes by the exact name and writes over, as before, and makes the whole
    record his (no `by`, no `seen`)."""
    if not source:
        from core import i18n
        raise ValueError(i18n.t("cli.contacts.source_required"))
    tg = parse_telegram(telegram) if telegram else None
    values = {"telegram": tg, "teams_chat": teams_chat, "teams": teams, "email": email}
    values = {k: v for k, v in values.items() if v}
    aliases = [a for a in aliases if a]

    def change(rows):
        rows_ = [r for r in rows if isinstance(r, dict) and isinstance(r.get("name"), str)]
        if by:
            given = {k: _norm(k, v["id"] if k == "telegram" else v) for k, v in values.items()}
            row = _for_session(rows_, name, aliases, given)
        else:
            row = next((r for r in rows_ if r["name"].casefold() == name.casefold()), None)
        fresh = row is None
        if fresh:
            row = {"name": name, "aliases": []}
        before = json.dumps(row, sort_keys=True, ensure_ascii=False)
        seen = row.get("seen") if isinstance(row.get("seen"), dict) else {}
        for key, value in values.items():
            if by and not fresh and _address(row, key) is not None:
                # the same address (checked above) is kept as written; a Telegram id under a new @username takes it
                if key == "telegram" and row["telegram"].get("username") != value["username"]:
                    row["telegram"] = {**row["telegram"], "username": value["username"]}
                    seen[key] = by
                continue
            row[key] = value
            if by and not fresh:
                seen[key] = by
            elif not by:
                seen.pop(key, None)   # the owner's word on this address
        if seen:
            row["seen"] = seen
        else:
            row.pop("seen", None)
        if note:
            had = row.get("note") if isinstance(row.get("note"), str) else ""
            if not by or not had:
                row["note"] = note
            elif note not in had.split("; "):
                row["note"] = f"{had}; {note}"
        old = [a for a in row.get("aliases") if isinstance(a, str)] if isinstance(row.get("aliases"), list) else []
        known = _names(row)
        row["aliases"] = old + list(dict.fromkeys(a for a in [name, *aliases] if a.casefold() not in known))
        if not by:
            row.pop("by", None)
            row.update(source=source, confirmed=datetime.date.today().isoformat())
        elif fresh:
            row.update(source=source, confirmed=datetime.date.today().isoformat(), by=by)
        elif json.dumps(row, sort_keys=True, ensure_ascii=False) == before:
            return row, False   # nothing new
        elif source not in str(row.get("source") or "").split("; "):
            row["source"] = f"{row['source']}; {source}" if row.get("source") else source
        if fresh:
            rows.append(row)
        return row, True
    return update(path, change)


def _one(rows, who):
    """The only record that has this name, alias or id; ValueError (the owner's words) for none and for several."""
    from core import i18n
    q = str(who).strip().casefold()
    hits = [r for r in rows if isinstance(r, dict) and isinstance(r.get("name"), str)
            and (q in _names(r) or str(r.get("id") or "").casefold() == q)]
    if not hits:
        raise ValueError(i18n.t("cli.bus.person_none", who=who))
    if len(hits) > 1:
        raise ValueError(i18n.t("contacts.ambiguous", who=who, names=", ".join(
            f"{r['name']} [{r['id']}]" if r.get("id") else r["name"] for r in hits)))
    return hits[0]


def rename(path, who, new):
    """The owner's rename of a record: (the record, its old name). The record, its id and so every hub linked to it stay;
    the name is tidied (spaces) and refused when empty, longer than 100 characters, with control characters, taken by
    another record (as a name or an alias) or the text of a link, an invitation or a card (a paste in the wrong field is
    not a name). ValueError — the reason, nothing is written."""
    from core import busflow, busstate, i18n
    name = " ".join(str(new).split())
    if not name or not busstate.alias_ok(name) or busflow.invite_like(name):   # alias_ok: the bus's rule of a name
        raise ValueError(i18n.t("contacts.name_bad", name=name[:40]))

    def change(rows):
        row = _one(rows, who)
        for r in rows:
            if r is not row and isinstance(r, dict) and name.casefold() in _names(r):
                raise ValueError(i18n.t("contacts.name_taken", name=name, other=r["name"]))
        old = row["name"]
        row["name"] = name
        if isinstance(row.get("aliases"), list):   # a name is not also an alias of the same record
            row["aliases"] = [a for a in row["aliases"] if not (isinstance(a, str) and a.casefold() == name.casefold())]
        return row, old
    return update(path, change)


def hubs_of(hubs, row):
    """The local names of the bus contacts (the core's state) linked to this record."""
    pid = row.get("id")
    return sorted(n for n, c in (hubs or {}).items()
                  if pid and isinstance(c, dict) and isinstance(c.get("person"), dict) and c["person"].get("id") == pid)


def remove(path, who, hubs=None):
    """The owner's removal of a record: the removed record. A record a hub is linked to stays (ValueError: unlink the hub
    first) — the hub would hang on a person the book no longer has."""
    from core import i18n

    def change(rows):
        row = _one(rows, who)
        linked = hubs_of(hubs, row)
        if linked:
            raise ValueError(i18n.t("contacts.still_linked", who=row["name"], hubs=", ".join(linked), hub=linked[0]))
        rows[:] = [r for r in rows if r is not row]
        return row
    return update(path, change)


def _hit(q, keys):
    return q and any(q == k.casefold() or q in k.casefold().split() or (len(q) > 3 and q in k.casefold())
                     for k in keys if isinstance(k, str) and k)


def find(path, query, view=None, owner=True):
    """Records of the book matching the query; with the bus on — also by the local names and labels of the hubs linked
    to a record (a linked hub office42 is found by `find office42`)."""
    q = str(query).strip().lstrip("@").casefold()
    rows = people(path) if isinstance(path, str) else path
    linked = _linked(rows, _hubs(view, owner))
    out = []
    for r in rows:
        tg = r.get("telegram") if isinstance(r.get("telegram"), dict) else {}
        keys = [r["name"], *r["aliases"], tg.get("username") or "", str(tg.get("id") or ""),
                r.get("email") or "", r.get("teams") or ""]
        for n, c in linked.get(r.get("id"), []) if r.get("id") else []:
            keys += [n, c.get("label") or "", c.get("stated") or ""]
        if _hit(q, keys):
            out.append(r)
    return out


# --- the bus path of a person (spec hubbus §5.5.1): the trusted part is in the core state; here — only showing and
# finding. `view` is what the core writes for commands outside it (core/busd.write_view): contacts by local name with
# status, label and `person` = {id, name}; None — the bus was never on, and nothing below changes the output.

def new_id(rows):
    """`c` + 8 random hex, not taken in the book."""
    import secrets
    taken = {r.get("id") for r in rows if isinstance(r, dict)}
    while True:
        cid = "c" + secrets.token_hex(4)
        if cid not in taken:
            return cid


def person_of(rows, person):
    """(state, row) of a bus contact's person by its id: ok — exactly one record; missing — none (deleted, or made anew
    under the same name: the link hangs and is not restored by itself); ambiguous — two or more (copied by hand):
    none is chosen, whatever the order in the file."""
    if not isinstance(person, dict) or not person.get("id"):
        return "none", None
    hits = [r for r in rows if r.get("id") == person["id"]]
    if len(hits) == 1:
        return "ok", hits[0]
    return ("missing" if not hits else "ambiguous"), None


def _hubs(view, owner):
    """Bus contacts a reader may see: the owner — all; a session — only accepted ones (the status of a hub is not given
    to sessions)."""
    if not (isinstance(view, dict) and view.get("enabled")):
        return {}
    cs = view.get("contacts") if isinstance(view.get("contacts"), dict) else {}
    return {n: c for n, c in cs.items() if isinstance(c, dict) and (owner or c.get("status") == "known")}


def _whole(q, key):
    return isinstance(key, str) and key and q == key.casefold()


def resolve(view, rows, query):
    """The one resolver of a name for the bus (`hub send liaison --to`, §5.5.1 p. 5): (local name, via, None) or
    (None, None, (code, detail)). (1) the local name of an accepted bus contact — via local; (2) otherwise a person's
    name or alias → that person's only accepted hub — via person. None — no_hub_path; two or more, or a local name of
    one hub equal to a person's name of another — ambiguous_path with the list; the core does not choose."""
    q = str(query or "").strip().casefold()
    hubs = _hubs(view, owner=False)
    local = [n for n in hubs if n.casefold() == q]
    by_person = []
    for r in rows:
        if any(_whole(q, k) for k in [r["name"], *r["aliases"]]):
            mine = [n for n, c in hubs.items() if person_of(rows, c.get("person"))[0] == "ok"
                    and (c.get("person") or {}).get("id") == r.get("id")]
            ambiguous = [n for n, c in hubs.items() if person_of(rows, c.get("person"))[0] == "ambiguous"
                         and (c.get("person") or {}).get("id") == r.get("id")]
            by_person += mine + ambiguous + (["?"] if ambiguous else [])
    by_person = list(dict.fromkeys(by_person))
    if local and by_person and set(by_person) != set(local):
        return None, None, ("ambiguous_path", sorted(set(local) | {n for n in by_person if n != "?"}))
    if local:
        return local[0], "local", None
    if len(by_person) == 1:
        return by_person[0], "person", None
    if by_person:
        return None, None, ("ambiguous_path", sorted(n for n in by_person if n != "?"))
    return None, None, ("no_hub_path", [])


def bus_part(name, c, owner):
    from core import i18n
    if owner:
        return i18n.t("cli.contacts.bus_owner", name=name, status=c.get("status"))
    return i18n.t("cli.contacts.bus_liaison", name=name)


def hub_line(name, c, owner, why=""):
    """A bus contact without a person in the book: a record of its own."""
    from core import i18n
    return (i18n.t("cli.contacts.hub_line", name=name, label=c.get("label") or "—", why=why)
            + (f" — {c.get('status')}" if owner else i18n.t("cli.contacts.hub_liaison", name=name)))


def _linked(rows, hubs):
    """{record id: [(local name, contact)]} of hubs linked to exactly one record."""
    out = {}
    for n, c in hubs.items():
        state, row = person_of(rows, c.get("person"))
        if state == "ok":
            out.setdefault(row["id"], []).append((n, c))
    return out


def line(r, linked=None, owner=True):
    from core import i18n
    seen = r.get("seen") if isinstance(r.get("seen"), dict) else {}

    def mark(key):   # an address a session added to the owner's record: not his confirmation
        return " " + i18n.t("contacts.seen_by", by=seen[key]) if isinstance(seen.get(key), str) else ""
    parts = [r["name"]]
    if r.get("aliases"):
        parts.append(i18n.t("contacts.also", names=", ".join(r["aliases"])))
    tg = r.get("telegram")
    if isinstance(tg, dict) and tg:
        parts.append(f"Telegram @{tg.get('username')} id {tg.get('id')}" + mark("telegram"))
    if r.get("teams_chat") or r.get("teams"):
        parts.append(f"Teams {(r.get('teams') or '') + mark('teams')} {(r.get('teams_chat') or '') + mark('teams_chat')}"
                     .rstrip())
    if r.get("email"):
        parts.append(i18n.t("contacts.email", email=r["email"]) + mark("email"))
    if r.get("note"):
        parts.append(r["note"])
    for name, c in (linked or {}).get(r.get("id"), []) if r.get("id") else []:
        parts.append(bus_part(name, c, owner))
    if r.get("by"):   # recorded by a session from its work: an address, not the owner's confirmation
        parts.append(i18n.t("contacts.by_session", by=r["by"], source=r.get("source"), date=r.get("confirmed")))
    else:
        parts.append(i18n.t("contacts.confirmed_by", source=r.get("source"), date=r.get("confirmed")))
    return " · ".join(parts[:-1]) + " " + parts[-1]


def _orphans(rows, hubs):
    """Bus contacts that no single record of the book carries: [(name, contact, why)]."""
    from core import i18n
    out = []
    for n, c in hubs.items():
        state, _ = person_of(rows, c.get("person"))
        if state != "ok":
            why = i18n.t("cli.contacts.link_missing") if state == "missing" else \
                i18n.t("cli.contacts.link_ambiguous") if state == "ambiguous" else ""
            out.append((n, c, why))
    return out


def main(argv, view=None, owner=True, by=None):
    """view — the bus as the core wrote it (core/busd.read_view) or None; owner — the command is the owner's (not a
    session's): he sees every bus contact with its status, a session only accepted ones and how to write to them; by —
    who runs it when not the owner (a session, a plugin): its add fills in, it does not write over (add_new)."""
    from core import i18n
    path = default_path()
    try:
        if not argv:
            rows = people(path)
            hubs = _hubs(view, owner)
            linked = _linked(rows, hubs)
            lines = [line(r, linked, owner) for r in rows] + [hub_line(n, c, owner, why)
                                                              for n, c, why in _orphans(rows, hubs)]
            print("\n".join(lines) if lines else i18n.t("contacts.none"))
            return 0
        if argv[0] == "find" and len(argv) >= 2:
            rows = people(path)
            hubs = _hubs(view, owner)
            linked = _linked(rows, hubs)
            q = " ".join(argv[1:])
            hits = [line(r, linked, owner) for r in find(rows, q, view, owner)]
            qq = q.strip().casefold()
            hits += [hub_line(n, c, owner, why) for n, c, why in _orphans(rows, hubs)
                     if _hit(qq, [n, c.get("label") or "", c.get("stated") or ""])]
            if not hits:
                print(i18n.t("contacts.not_found", who=" ".join(argv[1:])))
                return 1
            print("\n".join(hits))
            return 0
        if argv[0] == "add" and len(argv) >= 2:
            name, rest, opts, aliases = argv[1], argv[2:], {}, []
            it = iter(rest)
            for a in it:
                if a == "--alias":
                    aliases.append(next(it))
                elif a in ("--telegram", "--teams-chat", "--teams", "--email", "--note", "--source"):
                    opts[a[2:].replace("-", "_")] = next(it)
                else:
                    raise ValueError(i18n.t("cli.contacts.unknown_flag", flag=a))
            row, changed = add_new(path, name, aliases=aliases, by=by, **opts)
            if changed:
                print(i18n.t("contacts.added", row=line(row)))
            else:
                print(i18n.t("contacts.already", line=line(row)))
            return 0
        print(i18n.t("cli.contacts.usage"))
        return 2
    except (ValueError, StopIteration) as e:
        print(e or i18n.t("cli.contacts.missing_value"), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
